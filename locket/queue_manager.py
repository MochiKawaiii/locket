"""SQLite-backed restore queue.

`QueueManager` owns one daemon worker thread per Locket account in the rotator,
plus the helper that wraps a Locket API call with rotation-aware 401 retry.

All persistent state (waiting/processing/terminal jobs, processing-time
history, recent-log ring buffer) lives in the SQLite tables defined in
`locket.db`. Workers poll the DB every `POLL_INTERVAL` for the oldest waiting
row and atomically claim it via `UPDATE...RETURNING`. Concurrency is bounded
by the worker pool size — N workers ⇒ at most N restores running.

Subscription IDs that count as a successful Gold grant are kept here because
they're a piece of business logic the queue uses to decide success/failure.
"""

import json
import threading
import time
import uuid

from . import db
from .notifications import send_telegram_notification

MAX_QUEUE_SIZE = 500

SUBSCRIPTION_IDS = [
    "locket_1600_1y",
    "locket_199_1m",
    "locket_199_1m_only",
    "locket_3600_1y",
    "locket_399_1m_only",
]


class QueueManager:
    """SQLite-backed queue. The DB is the source of truth; the only in-memory
    state is `self.workers` (so we can hot-add/hot-remove threads)."""

    POLL_INTERVAL = 0.5
    CLEANUP_INTERVAL = 30
    TERMINAL_TTL = 600
    PROCESSING_TIMES_MAX = 20
    RECENT_LOG_MAX = 100

    def __init__(self, rotator):
        db.init()
        self.rotator = rotator
        self._lock = threading.Lock()
        self.workers = {}           # slot_id -> (Thread, threading.Event)
        self._last_cleanup = 0.0    # monotonic, kept on one worker only

        if rotator is not None:
            for slot_id in rotator.list_ids():
                self._spawn_worker(slot_id)
        print(f"Queue manager initialized with {len(self.workers)} worker(s)")

    @property
    def worker_count(self):
        with self._lock:
            return max(len(self.workers), 1)

    # ---- worker pool management ----

    def _spawn_worker(self, slot_id):
        with self._lock:
            if slot_id in self.workers:
                return
            stop_event = threading.Event()
            t = threading.Thread(
                target=self._process_queue,
                args=(slot_id, stop_event),
                daemon=True,
                name=f"queue-worker-{slot_id[:8]}",
            )
            self.workers[slot_id] = (t, stop_event)
        t.start()

    def add_worker(self, slot_id):
        self._spawn_worker(slot_id)
        print(f"QueueManager: spawned worker for slot {slot_id}")

    def remove_worker(self, slot_id):
        with self._lock:
            entry = self.workers.pop(slot_id, None)
        if entry is None:
            return False
        _, stop_event = entry
        stop_event.set()
        print(f"QueueManager: signalled stop for slot {slot_id}")
        return True

    # ---- API-call helper with 401 retry on a specific slot ----

    def call_on_slot(self, slot_id, api_fn_name, *args, **kwargs):
        """Invoke a LocketAPI method on one rotator slot, refreshing that
        slot's token on 401 and retrying once. Used by both the worker loop
        and synchronous public endpoints."""
        api = self.rotator.get(slot_id)
        try:
            return getattr(api, api_fn_name)(*args, **kwargs)
        except Exception as e:
            if "401" in str(e) or "Unauthenticated" in str(e):
                print(f"401 on slot {slot_id}, refreshing and retrying")
                new_api = self.rotator.refresh(slot_id)
                if new_api is None:
                    raise
                return getattr(new_api, api_fn_name)(*args, **kwargs)
            raise

    def call_round_robin(self, api_fn_name, *args, **kwargs):
        """Pick any active slot + 401 retry — for sync endpoints not tied to
        the worker pool (e.g. /api/get-user-info)."""
        if self.rotator is None:
            raise Exception("AccountRotator not initialized")
        ids = self.rotator.list_ids()
        if not ids:
            raise Exception("No accounts configured")
        return self.call_on_slot(ids[0], api_fn_name, *args, **kwargs)

    # ---- public API used by Flask routes ----

    def add_to_queue(self, username):
        """Insert a new waiting row. Returns client_id or None if queue is full."""
        conn = db.get_conn()
        in_flight = conn.execute(
            "SELECT COUNT(*) AS c FROM queue_requests WHERE status IN ('waiting','processing')"
        ).fetchone()["c"]
        if in_flight >= MAX_QUEUE_SIZE:
            return None

        client_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO queue_requests (client_id, username, status, added_at) "
            "VALUES (?, ?, 'waiting', ?)",
            (client_id, username, time.time()),
        )
        print(f"Added {username} to queue with client_id: {client_id}")
        return client_id

    def get_status(self, client_id):
        conn = db.get_conn()
        row = conn.execute(
            "SELECT * FROM queue_requests WHERE client_id = ?", (client_id,)
        ).fetchone()

        total_queue = conn.execute(
            "SELECT COUNT(*) AS c FROM queue_requests WHERE status IN ('waiting','processing')"
        ).fetchone()["c"]

        if row is None:
            return {
                "client_id": client_id,
                "status": "not_found",
                "position": 0,
                "total_queue": total_queue,
                "estimated_time": 0,
                "result": None,
                "error": "Request not found. It may have been completed or expired.",
            }

        position = self._position_for(conn, row)
        return {
            "client_id": client_id,
            "status": row["status"],
            "position": position,
            "total_queue": total_queue,
            "estimated_time": self._estimate_wait_time(position),
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
        }

    def get_global_status(self):
        conn = db.get_conn()
        total_queue = conn.execute(
            "SELECT COUNT(*) AS c FROM queue_requests WHERE status IN ('waiting','processing')"
        ).fetchone()["c"]
        avg_time = self._avg_processing_time(conn)
        wc = self.worker_count
        estimated_time = int((total_queue * avg_time + wc - 1) / wc) if total_queue else 0
        return {
            "status": "idle" if total_queue == 0 else "active",
            "total_queue": total_queue,
            "estimated_time": estimated_time,
            "avg_processing_time": avg_time,
        }

    def admin_snapshot(self):
        """Snapshot for /admin/api/queue. Timestamps are converted to ISO so the
        frontend's `new Date(...)` parsing keeps working unchanged."""
        from datetime import datetime, timezone

        def iso(epoch):
            if epoch is None:
                return None
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

        conn = db.get_conn()
        processing = [
            {
                "slot_id": r["slot_id"],
                "client_id": r["client_id"],
                "username": r["username"],
                "started_at": iso(r["started_at"]),
            }
            for r in conn.execute(
                "SELECT slot_id, client_id, username, started_at "
                "FROM queue_requests WHERE status='processing' ORDER BY started_at ASC"
            )
        ]
        waiting_rows = list(conn.execute(
            "SELECT client_id, username, added_at FROM queue_requests "
            "WHERE status='waiting' ORDER BY added_at ASC"
        ))
        waiting = [
            {
                "position": i + 1,
                "client_id": r["client_id"],
                "username": r["username"],
                "added_at": iso(r["added_at"]),
            }
            for i, r in enumerate(waiting_rows)
        ]
        recent = [
            {
                "client_id": r["client_id"],
                "username": r["username"],
                "slot_id": r["slot_id"],
                "status": r["status"],
                "error": r["error"],
                "duration": r["duration"],
                "completed_at": iso(r["completed_at"]),
            }
            for r in conn.execute(
                "SELECT client_id, username, slot_id, status, error, duration, completed_at "
                "FROM recent_log ORDER BY id DESC LIMIT 30"
            )
        ]
        return {"processing": processing, "waiting": waiting, "recent": recent}

    # ---- worker loop ----

    def _process_queue(self, slot_id, stop_event):
        try:
            email = self.rotator.email(slot_id)
        except KeyError:
            email = "<removed>"
        print(f"Worker {slot_id[:8]} started (slot {slot_id}, {email})")

        while not stop_event.is_set():
            claimed = self._claim_next_waiting(slot_id)
            if claimed is None:
                self._maybe_cleanup()
                stop_event.wait(self.POLL_INTERVAL)
                continue

            client_id, username = claimed
            print(f"Worker {slot_id[:8]} processing {client_id}")
            try:
                self._process_request(client_id, username, slot_id)
            except Exception as e:
                print(f"Worker {slot_id[:8]} unexpected error: {e}")
                self._finalize(client_id, slot_id, "error", error=f"Internal error: {e}")

        print(f"Worker {slot_id[:8]} exited")

    def _claim_next_waiting(self, slot_id):
        """Atomically pick the oldest waiting row and mark it processing."""
        conn = db.get_conn()
        row = conn.execute(
            """
            UPDATE queue_requests
               SET status='processing', started_at=?, slot_id=?
             WHERE client_id = (
                   SELECT client_id FROM queue_requests
                    WHERE status='waiting'
                    ORDER BY added_at ASC, rowid ASC
                    LIMIT 1)
               AND status='waiting'
            RETURNING client_id, username
            """,
            (time.time(), slot_id),
        ).fetchone()
        if row is None:
            return None
        return row["client_id"], row["username"]

    def _finalize(self, client_id, slot_id, status, result=None, error=None):
        now = time.time()
        conn = db.get_conn()
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT username, started_at FROM queue_requests WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return

            duration = (now - row["started_at"]) if row["started_at"] else None
            conn.execute(
                "UPDATE queue_requests SET status=?, result=?, error=?, completed_at=? "
                "WHERE client_id=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    now,
                    client_id,
                ),
            )

            if duration is not None:
                conn.execute(
                    "INSERT INTO processing_times (duration, completed_at) VALUES (?,?)",
                    (duration, now),
                )
                conn.execute(
                    "DELETE FROM processing_times WHERE id NOT IN "
                    "(SELECT id FROM processing_times ORDER BY id DESC LIMIT ?)",
                    (self.PROCESSING_TIMES_MAX,),
                )

            conn.execute(
                "INSERT INTO recent_log "
                "(client_id, username, slot_id, status, error, duration, completed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (client_id, row["username"], slot_id, status, error, duration, now),
            )
            conn.execute(
                "DELETE FROM recent_log WHERE id NOT IN "
                "(SELECT id FROM recent_log ORDER BY id DESC LIMIT ?)",
                (self.RECENT_LOG_MAX,),
            )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _maybe_cleanup(self):
        now = time.monotonic()
        if now - self._last_cleanup < self.CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        cutoff = time.time() - self.TERMINAL_TTL
        try:
            cur = db.get_conn().execute(
                "DELETE FROM queue_requests "
                "WHERE status IN ('completed','error') AND completed_at < ?",
                (cutoff,),
            )
            if cur.rowcount:
                print(f"GC: removed {cur.rowcount} old terminal rows")
        except Exception as e:
            print(f"GC error: {e}")

    # ---- helpers ----

    def _avg_processing_time(self, conn):
        rows = conn.execute(
            "SELECT duration FROM processing_times ORDER BY id DESC LIMIT 10"
        ).fetchall()
        if not rows:
            return 5.0
        return sum(r["duration"] for r in rows) / len(rows)

    def _position_for(self, conn, row):
        if row["status"] != "waiting":
            return 0
        rank = conn.execute(
            "SELECT COUNT(*) AS c FROM queue_requests "
            "WHERE status='waiting' AND "
            "  (added_at < ? OR (added_at = ? AND rowid <= ("
            "      SELECT rowid FROM queue_requests WHERE client_id = ?)))",
            (row["added_at"], row["added_at"], row["client_id"]),
        ).fetchone()["c"]
        return max(rank, 1)

    def _estimate_wait_time(self, position):
        if position == 0:
            return 0
        avg_time = self._avg_processing_time(db.get_conn())
        return max(1, int(position * avg_time / self.worker_count))

    # ---- single-job processing ----

    def _process_request(self, client_id, username, slot_id):
        print(f"Processing restore for: {username} on slot {slot_id[:8]}")
        try:
            account_info = self.call_on_slot(slot_id, "getUserByUsername", username)
            if not account_info or "result" not in account_info:
                raise Exception("User not found or API error")
            user_data = account_info.get("result", {}).get("data")
            if not user_data:
                raise Exception("User data not found")
            uid_target = user_data.get("uid")
            if not uid_target:
                raise Exception("UID not found for user")

            restore_result = self.call_on_slot(slot_id, "restorePurchase", uid_target)
            entitlements = restore_result.get("subscriber", {}).get("entitlements", {})
            gold_entitlement = entitlements.get("Gold", {})

            if gold_entitlement.get("product_identifier") in SUBSCRIPTION_IDS:
                send_telegram_notification(
                    username,
                    uid_target,
                    gold_entitlement.get("product_identifier"),
                    restore_result,
                )
                self._finalize(
                    client_id, slot_id, "completed",
                    result={
                        "success": True,
                        "msg": f"Purchase {gold_entitlement.get('product_identifier')} for {username} successfully!",
                    },
                )
            else:
                raise Exception(
                    f"Restore purchase failed. Gold entitlement not found for {username}."
                )

        except Exception as e:
            print(f"Error processing request for {client_id}: {e}")
            self._finalize(client_id, slot_id, "error", error=str(e))
