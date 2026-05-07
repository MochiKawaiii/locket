"""Bulk-create Locket accounts via Foxycrown temp mail and auto-confirm email.

Usage examples:
    # Sequential pattern, 100 accounts, 5 worker threads, single proxy
    python create_accounts.py --count 100 --workers 5 \\
        --password 'StrongPwd123!' --email-pattern 'locket_{i}@crxmail.com' \\
        --proxy http://user:pass@proxy.example.com:8080

    # Rotating proxies from a file (one URL per line)
    python create_accounts.py --count 100 --workers 10 \\
        --password 'StrongPwd123!' --email-pattern 'locket_{i}@crxmail.com' \\
        --proxy-file proxies.txt

    # Random Foxycrown email, no proxy, single thread
    python create_accounts.py --count 5 --password 'StrongPwd123!' --domain crxmail.com

Each account:
    1. Resolve the email (pattern with {i} or a random Foxycrown address).
    2. POST signupNewUser on Firebase (idempotent: EMAIL_EXISTS falls back to
       verifyPassword so re-runs resume mid-pipeline).
    3. POST sendOobCode + poll the inbox + setAccountInfo.
    4. Append the result to the JSON output (saved after every success).
    5. Unless --no-add is passed, INSERT directly into the `accounts` table.
       The running app will NOT hot-reload these — restart, or re-add through
       /admin, to spawn a worker for each new account.

Concurrency:
    --workers > 1 runs N accounts in parallel via ThreadPoolExecutor. Each
    worker picks a proxy from --proxy-file round-robin (or all share the
    single --proxy). Output JSON writes are mutex-protected.

Retries:
    --retries N applies per HTTP request to Foxycrown and Firebase. Retries
    fire only on connection / proxy / timeout / SSL errors — never on a
    real HTTP 4xx response. The whole-account flow itself is NOT auto-retried;
    re-run the script and auto-resume will skip already-processed indices
    (failed records still record the attempted email so we don't loop forever
    on a permanently-broken index).
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

# Make `locket.*` imports work when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from locket.account_creator import AccountCreationError, create_account
from locket.temp_mail import FoxycrownClient


# Stages that don't carry a long URL — keep the printed line short.
class ProxyPool:
    """Round-robin (default) or random pick from a list of proxy URLs.

    Empty list → next() returns None and accounts run direct (no proxy).
    Thread-safe for both read and rotation.
    """

    def __init__(self, proxies=None, mode="round_robin"):
        self.proxies = list(proxies or [])
        self.mode = mode
        self._idx = 0
        self._lock = threading.Lock()

    def __len__(self):
        return len(self.proxies)

    def next(self):
        if not self.proxies:
            return None
        if self.mode == "random":
            return random.choice(self.proxies)
        with self._lock:
            p = self.proxies[self._idx % len(self.proxies)]
            self._idx += 1
            return p


def _load_proxy_file(path):
    """Parse a proxy list file. Comment lines (#) and blanks are ignored."""
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def _add_to_db(email, password):
    from locket import db

    db.init()
    slot_id = str(uuid.uuid4())
    db.get_conn().execute(
        "INSERT INTO accounts (slot_id, email, password, added_at) VALUES (?,?,?,?)",
        (slot_id, email, password, time.time()),
    )
    return slot_id


def _load_existing(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(path, records):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(records, f, indent=2)
    os.replace(tmp, path)


def _process_one(args, i, email, proxy, fc, lock, records, counters, on_msg):
    """Run create_account for one index. Append the result (success or
    failure) to `records` under `lock`, then update `counters`.

    Failed records still record the attempted email so a subsequent run with
    auto-resume picks the next index instead of looping on the same one.
    """
    tag = f"i={i:>4}"
    proxy_label = f"proxy={proxy}" if proxy else "direct"
    on_msg(f"[{tag}] start  {email or '(random)'}  {proxy_label}")

    def emit(stage, detail):
        on_msg(f"[{tag}] {stage}: {detail}")

    try:
        rec = create_account(
            password=args.password,
            foxycrown=fc,
            domain=args.domain,
            email=email,
            inbox_timeout=args.inbox_timeout,
            inbox_interval=args.inbox_interval,
            do_signin_check=not args.no_signin_check,
            on_progress=emit,
            proxy=proxy,
            retries=args.retries,
            backoff=args.retry_backoff,
        )
        rec["created_at"] = time.time()
        rec["proxy"] = proxy

        if not args.no_add:
            try:
                rec["slot_id"] = _add_to_db(rec["email"], rec["password"])
                emit("db_added", rec["slot_id"])
            except Exception as e:
                rec["db_error"] = str(e)
                emit("db_error", str(e))

        with lock:
            records.append(rec)
            _save(args.output, records)
            counters["ok"] += 1
        on_msg(f"[{tag}] OK     {rec['email']}")
        return True
    except Exception as e:
        on_msg(f"[{tag}] FAIL   {email or '(random)'}: {e}")
        with lock:
            records.append({
                "email": email,
                "password": args.password,
                "error": str(e),
                "proxy": proxy,
                "failed_at": time.time(),
            })
            _save(args.output, records)
            counters["fail"] += 1
        return False


def main():
    p = argparse.ArgumentParser(
        description="Bulk-create Locket accounts via Foxycrown temp mail."
    )
    p.add_argument("--count", type=int, default=1, help="Number of accounts to create")
    p.add_argument(
        "--password", required=True, help="Password used for every account (>=6 chars)"
    )
    p.add_argument(
        "--domain",
        default=None,
        help="Pin Foxycrown domain when using random local parts (ignored if --email-pattern is set)",
    )
    p.add_argument(
        "--email-pattern",
        default=None,
        help="Email template containing '{i}', e.g. 'locket_{i}@crxmail.com'",
    )
    p.add_argument(
        "--start-index",
        type=int,
        default=None,
        help="Starting integer for {i}. Defaults to 1, or max(existing)+1 if resuming",
    )
    p.add_argument("--output", default="created_accounts.json", help="Output JSON file")
    p.add_argument(
        "--no-add",
        action="store_true",
        help="Don't insert into the accounts DB (output JSON only)",
    )
    p.add_argument("--workers", type=int, default=1, help="Parallel worker threads")
    p.add_argument("--proxy", default=None, help="Single proxy URL applied to all workers")
    p.add_argument(
        "--proxy-file",
        default=None,
        help="File with one proxy URL per line; pool is round-robin per account",
    )
    p.add_argument(
        "--proxy-mode",
        choices=("round_robin", "random"),
        default="round_robin",
        help="How to pick from --proxy-file",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Per-request retries on connection/proxy/timeout/SSL errors",
    )
    p.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="Base seconds for exponential backoff between retries",
    )
    p.add_argument(
        "--inbox-timeout", type=int, default=180,
        help="Seconds to wait for the verify mail to arrive",
    )
    p.add_argument(
        "--inbox-interval", type=int, default=4, help="Inbox poll interval (seconds)"
    )
    p.add_argument(
        "--foxycrown-base", default="https://foxycrown.net",
        help="Base URL of the Foxycrown service",
    )
    p.add_argument(
        "--no-signin-check", action="store_true",
        help="Skip the post-verify Firebase sign-in sanity check",
    )
    args = p.parse_args()

    if len(args.password) < 6:
        print("Password must be >= 6 characters (Firebase requirement).", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("--workers must be >= 1.", file=sys.stderr)
        return 2
    if args.proxy and args.proxy_file:
        print("Use either --proxy or --proxy-file, not both.", file=sys.stderr)
        return 2

    proxy_list = []
    if args.proxy:
        proxy_list = [args.proxy]
    elif args.proxy_file:
        try:
            proxy_list = _load_proxy_file(args.proxy_file)
        except OSError as e:
            print(f"Cannot read --proxy-file: {e}", file=sys.stderr)
            return 2
        if not proxy_list:
            print(f"--proxy-file {args.proxy_file!r} contained no proxies.", file=sys.stderr)
            return 2
    proxy_pool = ProxyPool(proxy_list, mode=args.proxy_mode)

    # One Foxycrown client per worker thread (so retries+proxy params are
    # picked up); the first one is also used to validate connectivity.
    bootstrap_proxy = proxy_pool.next() if len(proxy_pool) else None
    fc_bootstrap = FoxycrownClient(
        base_url=args.foxycrown_base,
        proxy=bootstrap_proxy,
        retries=args.retries,
        backoff=args.retry_backoff,
    )

    try:
        domains = fc_bootstrap.domains()
    except Exception as e:
        print(f"Cannot reach Foxycrown ({args.foxycrown_base}): {e}", file=sys.stderr)
        return 2

    if args.email_pattern:
        if "{i}" not in args.email_pattern:
            print("--email-pattern must contain the '{i}' placeholder.", file=sys.stderr)
            return 2
        if "@" not in args.email_pattern:
            print("--email-pattern must contain '@<domain>'.", file=sys.stderr)
            return 2
        pattern_domain = args.email_pattern.rsplit("@", 1)[1]
        if pattern_domain not in domains:
            print(
                f"Pattern domain {pattern_domain!r} not in Foxycrown list: {domains}",
                file=sys.stderr,
            )
            return 2
    elif args.domain and args.domain not in domains:
        print(
            f"Domain {args.domain!r} not in Foxycrown list: {domains}", file=sys.stderr
        )
        return 2

    records = _load_existing(args.output)

    # Auto-resume: skip past every index already attempted (success OR fail)
    # so a re-run after a fatal error continues forward instead of looping.
    start_index = args.start_index or 1
    if args.email_pattern and args.start_index is None:
        prefix, _, suffix = args.email_pattern.partition("{i}")
        rx = re.compile(re.escape(prefix) + r"(\d+)" + re.escape(suffix) + r"$")
        used = [int(m.group(1)) for r in records if (m := rx.match(r.get("email") or ""))]
        if used:
            start_index = max(used) + 1
            print(f"Resuming at i={start_index} (highest existing was {max(used)}).")

    counters = {"ok": 0, "fail": 0}
    state_lock = threading.Lock()
    print_lock = threading.Lock()

    def on_msg(line):
        with print_lock:
            print(line, flush=True)

    print(
        f"Plan: {args.count} accounts, workers={args.workers}, "
        f"proxies={len(proxy_pool) or 'none'}, retries={args.retries}",
        flush=True,
    )

    def task(n):
        i = start_index + n
        email = args.email_pattern.replace("{i}", str(i)) if args.email_pattern else None
        proxy = proxy_pool.next()
        # Each task gets its own Foxycrown client bound to its proxy so the
        # inbox poll uses the same egress IP as the Firebase calls.
        fc = FoxycrownClient(
            base_url=args.foxycrown_base,
            proxy=proxy,
            retries=args.retries,
            backoff=args.retry_backoff,
        )
        return _process_one(args, i, email, proxy, fc, state_lock, records, counters, on_msg)

    if args.workers == 1:
        for n in range(args.count):
            try:
                task(n)
            except Exception:
                traceback.print_exc()
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(task, n) for n in range(args.count)]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    # _process_one already logged; this catches anything
                    # exotic (e.g. KeyboardInterrupt inside a worker).
                    traceback.print_exc()

    print(
        f"Done. Success={counters['ok']}, Failed={counters['fail']}. "
        f"Output: {args.output}",
        flush=True,
    )
    if not args.no_add and counters["ok"]:
        print(
            "Note: restart the app (or re-add via /admin) to spawn workers "
            "for the newly inserted accounts."
        )
    return 0 if counters["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
