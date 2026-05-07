"""Flask config knobs sourced from environment variables.

`configure(app)` mutates `app.config` and `app.wsgi_app` in place. Called once
from `create_app`. Keep this small — most behaviour-driving env vars
(`EMAIL`, `gist_token_url`, `ADMIN_PASSWORD`, …) are read by the modules that
use them, not lifted into Flask config.
"""

import os
import secrets


def configure(app):
    secret = os.getenv("FLASK_SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        print(
            "WARNING: FLASK_SECRET_KEY not set; admin sessions will be invalidated on restart."
        )
    app.config["SECRET_KEY"] = secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Set BEHIND_HTTPS=1 in production so the session cookie is only sent over TLS.
    # Leave unset for local HTTP development or the cookie won't be issued at all.
    behind_https = os.getenv("BEHIND_HTTPS") == "1"
    app.config["SESSION_COOKIE_SECURE"] = behind_https
    if behind_https:
        # Trust X-Forwarded-* headers from nginx so url_for() / scheme
        # detection know we're behind TLS. Without this, redirects after
        # login can drop to http.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
