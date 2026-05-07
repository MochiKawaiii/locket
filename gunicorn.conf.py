"""Gunicorn config for production VPS deployment.

The QueueManager spawns one daemon thread per Locket account at import time.
That means we MUST run a single gunicorn worker process — multiple processes
would each spawn their own thread pool, all polling the same SQLite queue,
and you'd have N × workers threads for no extra throughput. Concurrency for
HTTP requests is handled by `threads` instead.
"""

import multiprocessing  # noqa: F401  (kept so users grep'ing for it find this comment)

# Listen only on loopback — nginx terminates TLS and proxies to us.
bind = "127.0.0.1:5001"

# One process. See module docstring.
workers = 1

# Threads inside the single worker process. 8 is plenty for this app's
# request volume; if you ever see the queue HTTP poll endpoint backing up,
# raise it. (QueueManager's own background threads are separate from these.)
threads = 8
worker_class = "gthread"

# Restore work runs out-of-band, so HTTP timeouts only need to cover
# /api/get-user-info (synchronous) and admin pages.
timeout = 60
graceful_timeout = 30

# preload_app=False (default) is REQUIRED. With preload, the master imports
# wsgi.py — which calls create_app() and spawns QueueManager daemon threads —
# and then forks. Threads do not survive fork(), so the daemon would silently die.
preload_app = False

# Send access + error logs to stdout/stderr; systemd journald captures them.
accesslog = "-"
errorlog = "-"
loglevel = "info"
