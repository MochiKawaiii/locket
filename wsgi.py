"""WSGI entry point for gunicorn.

`gunicorn -c gunicorn.conf.py wsgi:app` imports this module, which calls
`create_app()` exactly once. Worker threads inside QueueManager are spawned
during create_app, so the gunicorn config MUST set `preload_app=False` and
`workers=1` (see gunicorn.conf.py for why).
"""

from locket import create_app

app = create_app()


if __name__ == "__main__":
    # Local dev: `python wsgi.py` runs Flask's built-in server.
    # use_reloader=False prevents Flask from spawning two QueueManagers (and
    # therefore two pools of worker threads all polling the same SQLite DB).
    app.run(debug=True, port=5001, use_reloader=False)
