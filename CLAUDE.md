# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run / develop

```bash
pip install -r requirements.txt
python wsgi.py                     # Flask dev server (debug=True, port 5001, reloader off)
gunicorn -c gunicorn.conf.py wsgi:app   # production server (1 process, 8 threads)
lsof -ti:5001 | xargs kill -9      # Free the port if it's stuck
```

There is no test suite, linter config, or build step. `vercel.json` is **legacy** and incompatible with the current architecture (background worker threads + persistent SQLite + filesystem writes); deploy on a long-running host instead. See `deploy/DEPLOY.md` for a Ubuntu/Debian VPS recipe (gunicorn + systemd + nginx + Let's Encrypt).

Required env vars (loaded via `python-dotenv` from `.env`):

| Var | Purpose |
| --- | --- |
| `EMAIL`, `PASSWORD` | One-shot seed for the `accounts` table on first boot if the table is empty. After that, manage accounts through `/admin`. |
| `gist_token_url` | Raw Gist URL returning a JSON array of RevenueCat payloads — fallback when the `tokens` table is empty. The admin panel manages payloads directly. |
| `ADMIN_PASSWORD` | Required to access `/admin`. Single password, session cookie. Without it, login always fails. |
| `FLASK_SECRET_KEY` | Optional. Required for admin sessions to survive restarts; otherwise a random key is generated per process. |
| `BEHIND_HTTPS` | Set to `1` in production to mark the session cookie `Secure` and trust `X-Forwarded-Proto`. Leave unset for local HTTP. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional success notifications |
| `LOCKET_DB` | Optional. SQLite path. Defaults to `locket.db` in the cwd. |

The app boots with **zero accounts** if the DB is empty and `EMAIL`/`PASSWORD` are unset — public endpoints return 503 until an account is added through `/admin`. This means a fresh deploy can come up before any creds exist.

## Architecture

Source tree:

```
wsgi.py                   # entrypoint: app = create_app()
gunicorn.conf.py          # 1 process, 8 threads, preload_app=False
locket/
  __init__.py             # create_app() — config + db.init + rotator + queue_manager + blueprints
  config.py               # Flask config from env (SECRET_KEY, cookie, ProxyFix)
  db.py                   # sqlite3 schema + thread-local conn + JSON→DB migrations
  rotator.py              # AccountRotator (DB-backed)
  tokens.py               # TokensStore singleton (DB-backed, gist fallback)
  locket_auth.py          # Auth → Firebase verifyPassword
  locket_api.py           # LocketAPI → Locket + RevenueCat endpoints
  queue_manager.py        # QueueManager + call_on_slot helper + SUBSCRIPTION_IDS
  notifications.py        # send_telegram_notification
  admin/
    __init__.py           # admin Blueprint (url_prefix=/admin)
    auth.py               # @admin_required, check_password, session helpers
    routes.py             # /admin/login, /admin/api/{accounts,tokens,queue}
  public/
    __init__.py           # public Blueprint
    routes.py             # /, /api/{get-user-info,restore,queue/*}
  templates/              # admin.html, admin_login.html, index.html
  static/                 # locket.mobileconfig
deploy/                   # systemd unit, nginx template, DEPLOY.md
```

Module roles:

- `locket.locket_auth` — `Auth` posts to Google `identitytoolkit verifyPassword` with hardcoded iOS Firebase headers and returns the Firebase `idToken`.
- `locket.locket_api` — `LocketAPI` wraps `getUserByUsername`, `getLastMoment` (against `api.locketcamera.com`), and `restorePurchase` (against `api.revenuecat.com/v1/receipts` with `X-Is-Sandbox: true`). The RevenueCat bearer is hardcoded; the Locket bearer is the Firebase idToken from `Auth`.
- `locket.db` — Single source of truth for all persistent state. SQLite file `locket.db`, WAL mode, thread-local connections via `db.get_conn()`. `db.init()` creates schema and **one-shot migrates** legacy JSON files (`accounts.json`, `tokens.json`, `queue_state.json`) into the DB then renames them to `.bak`. Tables: `accounts`, `tokens`, `queue_requests`, `processing_times`, `recent_log`.
- `locket.rotator` — `AccountRotator` keys slots by stable uuid (`slot_id`). DB-backed (`accounts` table). In-memory cache holds `Auth` + `LocketAPI` instances (rebuilt after restart). Boots empty when there are no accounts. API: `get(slot_id)`, `add(email, password)`, `remove(slot_id)`, `refresh(slot_id)`, `test_login(email, password)`. Workers bind to a `slot_id` for life.
- `locket.tokens` — Module-level singleton `tokens_store` backs `LocketAPI.restorePurchase`. Reads `tokens` table first; falls back to `gist_token_url` (cached 60s) when empty.
- `locket.admin.auth` — `@admin_required` decorator. Returns 401 JSON for `/admin/api/*` paths and redirects to `/admin/login` for HTML routes.
- `locket.queue_manager` — `QueueManager` polls the DB every 0.5s via atomic `UPDATE...RETURNING` to claim the oldest waiting row. One worker per rotator slot, registered in `self.workers: dict[slot_id, (Thread, Event)]`. Hot-reload methods `add_worker(slot_id)` / `remove_worker(slot_id)` are called from admin routes. `call_on_slot(slot_id, fn, …)` is the rotation-aware helper used by both worker loops and synchronous public endpoints. Workers stop within `POLL_INTERVAL` after their current job finishes.

Singletons (`rotator`, `queue_manager`) are attached to the Flask `app` object inside `create_app()`. Routes reach them via `current_app.rotator` / `current_app.queue_manager`. Worker threads receive their reference at construction time.

### QueueManager (`locket/queue_manager.py`)

A pool of N daemon worker threads (N = `rotator.size()`) **polls** the SQLite `queue_requests` table every `POLL_INTERVAL` (0.5s). Each worker is bound for life to one rotator slot — `self.workers: dict[slot_id, (Thread, Event)]`. Key invariants:

- N concurrent requests, one per account. No artificial sleep between jobs; throughput = `N × (1 / avg_processing_time)`.
- **All state lives in SQLite** — `queue_requests`, `processing_times`, `recent_log`. There is no in-memory queue or status dict. Workers and request handlers all read the same DB.
- The atomic claim is a single `UPDATE...RETURNING` keyed off the oldest `status='waiting'` row. Because SQLite serializes writers, only one worker can win per row even with N workers polling at once.
- On `db.init()` startup, any leftover `status='processing'` rows (orphaned by a crash) are reset to `status='waiting'` so a worker can pick them up again.
- Completed/error rows GC'd after `TERMINAL_TTL` (600s). The cleanup runs every 30s on whichever worker is idle.
- Wait-time estimate = `ceil(position × moving_avg(last 10 processing_times) / N)`. Errors *are* counted — failed calls still tie up a worker for their duration.
- `_finalize()` wraps "mark done + insert recent_log + insert processing_times + GC bounded history" in a single transaction so the views stay consistent.
- 401/Unauthenticated from any Locket API call triggers `rotator.refresh(slot_id)` + single retry on the **same slot**. Other slots untouched.
- `add_to_queue` rejects when in-flight (waiting + processing) ≥ `MAX_QUEUE_SIZE` (default 500); endpoint returns HTTP 503.
- Removing an account: admin endpoint sets the worker's `Event` and removes the row from `accounts`. The worker exits its loop within `POLL_INTERVAL` after finishing its current job. No `Thread.join()` — daemon threads.

### Frontend (`templates/index.html`)

Single file, ~1200 lines, vanilla JS + SweetAlert2 + reCAPTCHA. Polls `/api/queue/status` every 1s with the `client_id` returned by `/api/restore`, and runs an independent 1s countdown for smooth UI. `/api/queue/global-status` feeds the "people waiting" badge. iOS users download `static/locket.mobileconfig` to block Locket's revocation endpoints.

### API surface

Public:
- `POST /api/get-user-info` `{username}` → preview profile (synchronous, no queue)
- `POST /api/restore` `{username}` → `{client_id, position, total_queue, estimated_time}` · returns 503 when in-flight ≥ `MAX_QUEUE_SIZE`
- `POST /api/queue/status` `{client_id}` → polling endpoint; returns `status: "not_found"` with HTTP 200 when the entry has expired (frontend treats this as recoverable, not fatal)
- `GET  /api/queue/global-status` → aggregate counts

Admin (gated by session cookie + `ADMIN_PASSWORD`):
- `GET/POST /admin/login`, `POST /admin/logout`
- `GET /admin` → dashboard
- `GET/POST/DELETE /admin/api/accounts` (+ `POST /admin/api/accounts/test`) — CRUD + test login. Add spawns a worker, delete signals stop_event. Refuses delete if it would empty the pool.
- `GET/POST/DELETE /admin/api/tokens/<index>` — CRUD `tokens.json` payloads
- `GET /admin/api/queue` — snapshot `{workers, processing, waiting, recent}`

## Gotchas

- `wsgi.py` runs the Flask dev server with `debug=True, use_reloader=False`. The reloader is off because it would spawn two `QueueManager`s (= 2N workers all polling the same DB).
- Gunicorn config requires `workers=1` + `preload_app=False`. Multiple worker processes would each spawn their own thread pool and double the DB polling load for no extra throughput; preloading would fork after threads are created (threads don't survive `fork()`).
- The hardcoded `X-Firebase-AppCheck` JWTs in `locket/locket_auth.py` and `locket/locket_api.py` are expired (iat 2024). Locket's backend currently doesn't reject them, but if `getUserByUsername` starts 401-ing for everyone, that's the first thing to refresh.
- `SUBSCRIPTION_IDS` in `locket/queue_manager.py` is the allow-list checked against the RevenueCat response's `Gold.product_identifier`. If Locket adds a new SKU, restores will report failure even though the entitlement was granted — extend this list.
- `restorePurchase` re-fetches the gist on every call. Network failure there surfaces as a generic "Error loading tokens from URL" to the queued client.
