"""Foxycrown temp-mail client.

Public endpoints (no auth, all JSON):
    GET /api/generate            -> {ok, email, username, domain}
    GET /api/inbox?email=<addr>  -> {ok, count, emails:[{from,sender,subject,text,html,date}]}
    GET /api/domains             -> {ok, domains:[...]}

All HTTP calls go through `request_with_retry` so a flaky proxy / temporary
network blip doesn't kill an account-creation run mid-poll.
"""

import time

from ._http import NETWORK_ERRORS, request_with_retry


class FoxycrownError(Exception):
    pass


class FoxycrownClient:
    def __init__(
        self,
        base_url="https://foxycrown.net",
        timeout=20,
        proxy=None,
        retries=3,
        backoff=1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.proxy = proxy
        self.retries = retries
        self.backoff = backoff

    def _get(self, path, **params):
        url = f"{self.base_url}{path}"
        try:
            r = request_with_retry(
                "GET",
                url,
                params=params or None,
                proxy=self.proxy,
                timeout=self.timeout,
                retries=self.retries,
                backoff=self.backoff,
            )
        except NETWORK_ERRORS as e:
            raise FoxycrownError(f"network error on {path}: {e}")
        try:
            data = r.json()
        except ValueError:
            raise FoxycrownError(
                f"non-JSON response from {path}: HTTP {r.status_code} {r.text[:200]}"
            )
        if not data.get("ok"):
            raise FoxycrownError(f"{path} failed: {data.get('error') or data}")
        return data

    def domains(self):
        return self._get("/api/domains")["domains"]

    def generate(self, domain=None):
        """Return a fresh email address. If `domain` is given, swap the domain
        portion onto the random local part returned by the API."""
        data = self._get("/api/generate")
        email = data["email"]
        if domain:
            local = email.split("@", 1)[0]
            email = f"{local}@{domain}"
        return email

    def inbox(self, email):
        return self._get("/api/inbox", email=email)["emails"]

    def wait_for_email(self, email, predicate, timeout=180, interval=4):
        """Poll inbox until predicate(msg) is truthy. Returns the matching message.
        Raises FoxycrownError on timeout. Network blips inside the poll loop are
        absorbed (we just keep polling) so a flaky proxy doesn't abort the wait."""
        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                emails = self.inbox(email)
            except FoxycrownError as e:
                last_err = e
                emails = []
            for msg in emails:
                if predicate(msg):
                    return msg
            time.sleep(interval)
        raise FoxycrownError(
            f"timeout waiting for matching email at {email}"
            + (f" (last error: {last_err})" if last_err else "")
        )
