"""Flask app factory.

The app is composed in `create_app()`:

1. Load .env, build Flask config, ProxyFix when behind HTTPS.
2. Initialize the SQLite schema (idempotent; runs JSON→DB migrations on first
   call).
3. Build the AccountRotator. If the DB has zero accounts and EMAIL/PASSWORD
   env vars are unset, the rotator boots empty — public endpoints will return
   503 until an account is added through the admin panel, but admin login
   still works.
4. Build the QueueManager. It spawns one daemon worker thread per rotator
   slot at construction time; subsequent admin add/remove calls hot-mutate
   the pool via `add_worker(slot_id)` / `remove_worker(slot_id)`.
5. Register the public + admin blueprints.

Singletons (rotator, queue_manager) are attached to the Flask `app` object
so request handlers can reach them via `current_app.rotator` /
`current_app.queue_manager`. Worker threads receive their reference at
construction time (`QueueManager(rotator)`).
"""

import dotenv
from flask import Flask

from . import config, db
from .admin import bp as admin_bp
from .public import bp as public_bp
from .queue_manager import QueueManager
from .rotator import AccountRotator


def create_app():
    dotenv.load_dotenv()

    app = Flask(__name__, instance_relative_config=False)
    config.configure(app)
    db.init()

    try:
        app.rotator = AccountRotator()
    except Exception as e:
        # Rotator now boots with 0 accounts gracefully, so this only fires on
        # truly unexpected init errors (e.g. corrupted DB). Keep the app alive
        # so admin can still log in and see logs.
        print(f"Error initializing AccountRotator: {e}")
        app.rotator = None

    app.queue_manager = QueueManager(app.rotator)

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    return app
