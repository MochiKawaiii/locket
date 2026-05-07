"""Create Locket accounts via temp mail and auto-confirm the verification email.

We bypass Locket's `createAccountWithEmailPassword` Cloud Function (it enforces
fresh Firebase AppCheck and rejects everything we can send) and talk directly
to the Firebase Identity Toolkit REST API instead. The Locket iOS Firebase
project's API key is open to anyone sending the right `X-Ios-Bundle-Identifier`
header — same trick the rest of this codebase already uses for sign-in.

Pipeline (per account):
    1. Generate a temp email through FoxycrownClient.
    2. POST signupNewUser → creates the Firebase user, returns idToken + uid.
       If Firebase says EMAIL_EXISTS we transparently fall back to verifyPassword
       so a re-run with the same pattern can resume mid-pipeline.
    3. POST getOobConfirmationCode (VERIFY_EMAIL) → triggers the verify mail.
    4. Poll the temp inbox until the Firebase verification mail lands; extract
       oobCode + apiKey from the action URL.
    5. POST setAccountInfo with the oobCode → flips emailVerified=true.
    6. (Optional) verifyPassword to sanity-check sign-in.

Verification URL pattern observed in the wild:
    https://locketcamera.com/__/auth/action?mode=verifyEmail&oobCode=<code>&apiKey=<key>&lang=en

All HTTP calls go through `_http.request_with_retry` so an upstream proxy /
network blip is absorbed by automatic retries with exponential backoff.
"""

import html
import re

from ._http import NETWORK_ERRORS, request_with_retry
from .temp_mail import FoxycrownClient


FIREBASE_SIGNUP = (
    "https://www.googleapis.com/identitytoolkit/v3/relyingparty/signupNewUser"
)
FIREBASE_SEND_OOB = (
    "https://www.googleapis.com/identitytoolkit/v3/relyingparty/getOobConfirmationCode"
)
FIREBASE_SET_INFO = (
    "https://www.googleapis.com/identitytoolkit/v3/relyingparty/setAccountInfo"
)
FIREBASE_SIGNIN = (
    "https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword"
)

# Locket iOS app's Firebase project API key. Restricted by Google Cloud
# Console to bundle id `com.locket.Locket`, so every request must carry the
# `X-Ios-Bundle-Identifier` header (see `_firebase_headers`).
LOCKET_FIREBASE_API_KEY = "AIzaSyCQngaaXQIfJaH0aS2l7REgIjD7nL431So"

VERIFY_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]*?/__/auth/action\?[^\s\"'<>]*oobCode=[^\s\"'<>&]+[^\s\"'<>]*",
    re.IGNORECASE,
)
OOB_RE = re.compile(r"[?&]oobCode=([^&\s\"'<>]+)")
APIKEY_RE = re.compile(r"[?&]apiKey=([^&\s\"'<>]+)")


class AccountCreationError(Exception):
    pass


def _firebase_headers():
    return {
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Content-Type": "application/json",
        "User-Agent": "Locket/1.82.0 (com.locket.Locket; build:3; iOS 18.0.0) Alamofire/5.6.4",
        "X-Client-Version": "iOS/FirebaseSDK/10.23.1/FirebaseCore-iOS",
        "X-Firebase-GMPID": "1:641029076083:ios:cc8eb46290d69b234fa606",
        "X-Ios-Bundle-Identifier": "com.locket.Locket",
    }


def _firebase_post(name, url, json_body, *, proxy, retries, backoff, timeout=30):
    """Wrap one Firebase call: retry on network errors, raise on HTTP error."""
    try:
        r = request_with_retry(
            "POST",
            url,
            params={"key": LOCKET_FIREBASE_API_KEY},
            headers=_firebase_headers(),
            json=json_body,
            proxy=proxy,
            timeout=timeout,
            retries=retries,
            backoff=backoff,
        )
    except NETWORK_ERRORS as e:
        raise AccountCreationError(f"{name} network error after retries: {e}")
    if not r.ok:
        raise AccountCreationError(f"{name} failed HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _extract_verify_link(msg):
    """Return the unescaped action URL from a verify mail, or None.

    HTML bodies escape ampersands as `&amp;`, which would break the [?&]oobCode
    boundary in our regex; html.unescape collapses them back to `&` first.
    """
    for body in (msg.get("html") or "", msg.get("text") or ""):
        m = VERIFY_LINK_RE.search(html.unescape(body))
        if m:
            return m.group(0)
    return None


def _is_locket_verify_mail(msg):
    return _extract_verify_link(msg) is not None


def signup(email, password, *, proxy=None, retries=3, backoff=1.0):
    """Create a Firebase user. Returns {idToken, refreshToken, localId, email}."""
    return _firebase_post(
        "signupNewUser",
        FIREBASE_SIGNUP,
        {"email": email, "password": password, "returnSecureToken": True},
        proxy=proxy, retries=retries, backoff=backoff,
    )


def send_verify_email(id_token, *, proxy=None, retries=3, backoff=1.0):
    """Trigger Firebase to send the email-verification mail."""
    return _firebase_post(
        "sendOobCode",
        FIREBASE_SEND_OOB,
        {"requestType": "VERIFY_EMAIL", "idToken": id_token},
        proxy=proxy, retries=retries, backoff=backoff,
    )


def login_idtoken(email, password, *, proxy=None, retries=3, backoff=1.0):
    """Sign in an existing account. Returns the Firebase response."""
    return _firebase_post(
        "verifyPassword",
        FIREBASE_SIGNIN,
        {"email": email, "password": password, "returnSecureToken": True},
        proxy=proxy, retries=retries, backoff=backoff,
    )


def confirm_email(action_url, *, proxy=None, retries=3, backoff=1.0):
    """Pull oobCode + apiKey from the verify URL and call setAccountInfo."""
    oob = OOB_RE.search(action_url)
    key = APIKEY_RE.search(action_url)
    if not oob:
        raise AccountCreationError(f"oobCode missing from URL: {action_url}")
    if not key:
        raise AccountCreationError(f"apiKey missing from URL: {action_url}")
    try:
        r = request_with_retry(
            "POST",
            FIREBASE_SET_INFO,
            params={"key": key.group(1)},
            headers=_firebase_headers(),
            json={"oobCode": oob.group(1)},
            proxy=proxy,
            timeout=30,
            retries=retries,
            backoff=backoff,
        )
    except NETWORK_ERRORS as e:
        raise AccountCreationError(f"setAccountInfo network error after retries: {e}")
    if not r.ok:
        raise AccountCreationError(
            f"setAccountInfo failed HTTP {r.status_code}: {r.text[:300]}"
        )
    return r.json()


def _signup_or_resume(email, password, *, proxy, retries, backoff, progress):
    """Create the Firebase user; if it already exists, sign in instead.

    This makes the pipeline idempotent for re-runs: a previous run that
    crashed after signupNewUser succeeded but before verification can resume
    by signing in with the same password and continuing from send_verify.
    """
    try:
        return signup(email, password, proxy=proxy, retries=retries, backoff=backoff)
    except AccountCreationError as e:
        if "EMAIL_EXISTS" not in str(e):
            raise
        progress("email_exists_resuming", email)
        return login_idtoken(
            email, password, proxy=proxy, retries=retries, backoff=backoff
        )


def create_account(
    password,
    foxycrown=None,
    domain=None,
    email=None,
    inbox_timeout=180,
    inbox_interval=4,
    do_signin_check=True,
    on_progress=None,
    proxy=None,
    retries=3,
    backoff=1.0,
):
    """Create one Locket account end-to-end. Returns
    {email, password, uid, verified, idToken?}.

    If `email` is provided it's used as-is (e.g. for fixed patterns like
    locket_1@crxmail.com). Otherwise a random address is pulled from
    Foxycrown, optionally pinned to `domain`.

    `proxy` is a single proxy URL applied to every Firebase + Foxycrown call
    in this account creation. `retries`/`backoff` control per-request retry
    on network errors (proxy down, timeout, SSL handshake).

    `on_progress(stage, detail)` is invoked at each milestone for CLI feedback.
    """
    fc = foxycrown or FoxycrownClient(proxy=proxy, retries=retries, backoff=backoff)
    progress = on_progress or (lambda *_a, **_kw: None)

    if not email:
        email = fc.generate(domain=domain)
    progress("email", email)

    progress("signup", email)
    signup_res = _signup_or_resume(
        email, password,
        proxy=proxy, retries=retries, backoff=backoff, progress=progress,
    )
    uid = signup_res.get("localId")
    id_token = signup_res.get("idToken")
    if not id_token:
        raise AccountCreationError(f"signup returned no idToken: {signup_res}")

    progress("send_verify", email)
    send_verify_email(id_token, proxy=proxy, retries=retries, backoff=backoff)

    progress("waiting_mail", email)
    msg = fc.wait_for_email(
        email,
        _is_locket_verify_mail,
        timeout=inbox_timeout,
        interval=inbox_interval,
    )
    link = _extract_verify_link(msg)
    progress("verify_link", link)

    confirm_email(link, proxy=proxy, retries=retries, backoff=backoff)
    progress("confirmed", email)

    rec = {
        "email": email,
        "password": password,
        "uid": uid,
        "verified": True,
    }

    if do_signin_check:
        try:
            signin = login_idtoken(
                email, password, proxy=proxy, retries=retries, backoff=backoff
            )
            rec["uid"] = rec["uid"] or signin.get("localId")
            rec["idToken"] = signin.get("idToken")
            progress("signed_in", rec["uid"])
        except AccountCreationError as e:
            # The account is verified — the sign-in check is just a sanity
            # ping. Don't fail the whole creation if Firebase momentarily
            # rate-limits us; record the warning and move on.
            rec["signin_warning"] = str(e)
            progress("signin_warning", str(e))

    return rec
