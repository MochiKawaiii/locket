"""RevenueCat receipt payload store backing LocketAPI.restorePurchase.

Source priority for `get_payloads()`:
1. Local DB (`tokens` table) — admin-managed via the dashboard. Used
   exclusively if non-empty.
2. `gist_token_url` env var — fetched as a fallback when the local table is
   empty. Cached in-memory for `GIST_CACHE_TTL` seconds.

Admin CRUD operates on the DB only — gist payloads are never edited or
exposed via the admin endpoints.
"""

import json
import os
import threading
import time

import requests

from . import db

GIST_CACHE_TTL = 60


class TokensStore:
    def __init__(self, gist_url_env="gist_token_url"):
        self._gist_url_env = gist_url_env
        self._lock = threading.Lock()
        self._gist_cache = None
        self._gist_cache_ts = 0.0

    # --- public read API used by api.LocketAPI.restorePurchase ---

    def get_payloads(self):
        """Return the active payload list. Raises if neither source is usable."""
        local = self._read_local()
        if local:
            return local

        gist = self._fetch_gist()
        if gist:
            return gist

        raise Exception(
            "No restore payloads available: tokens table is empty and "
            "gist_token_url is unset or unreachable"
        )

    # --- admin API ---

    def list(self):
        """Admin view of stored payloads (gist payloads are not exposed)."""
        return self._read_local()

    def add(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Token payload must be a JSON object")
        db.get_conn().execute(
            "INSERT INTO tokens (payload, added_at) VALUES (?, ?)",
            (json.dumps(payload), time.time()),
        )

    def remove(self, index):
        """Remove the payload at admin-visible index (0-based, by insertion order)."""
        rows = list(db.get_conn().execute("SELECT id FROM tokens ORDER BY id ASC"))
        if not (0 <= index < len(rows)):
            raise IndexError(f"Token index out of range: {index}")
        db.get_conn().execute("DELETE FROM tokens WHERE id = ?", (rows[index]["id"],))

    # --- internals ---

    def _read_local(self):
        rows = db.get_conn().execute(
            "SELECT payload FROM tokens ORDER BY id ASC"
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except json.JSONDecodeError as e:
                print(f"TokensStore: skipping malformed payload row: {e}")
        return out

    def _fetch_gist(self):
        gist_url = os.getenv(self._gist_url_env)
        if not gist_url:
            return None
        with self._lock:
            now = time.time()
            if self._gist_cache is not None and now - self._gist_cache_ts < GIST_CACHE_TTL:
                return self._gist_cache
        try:
            resp = requests.get(gist_url, timeout=10)
            if not resp.ok:
                print(f"TokensStore: gist fetch failed: {resp.status_code}")
                return None
            data = resp.json()
        except Exception as e:
            print(f"TokensStore: gist fetch error: {e}")
            return None
        if not isinstance(data, list):
            print("TokensStore: gist response is not a JSON array")
            return None
        with self._lock:
            self._gist_cache = data
            self._gist_cache_ts = time.time()
        return data


# Module-level singleton.
tokens_store = TokensStore()
