"""Shared HTTP helper: proxy support + retry on transient network errors.

Both the Foxycrown client and the Firebase calls use this so they share the
same proxy/retry policy. Retries only fire on network-level failures
(connection refused, proxy errors, timeouts, SSL handshake) — never on a
successful HTTP response, even one with a 4xx/5xx status. Callers inspect
status themselves and decide whether to retry at a higher level.
"""

import time
import requests


NETWORK_ERRORS = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
)


def proxies_for(proxy):
    """Convert a proxy URL into the dict shape requests expects.
    `None` (or empty) returns None so the request goes direct."""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def request_with_retry(
    method,
    url,
    *,
    params=None,
    headers=None,
    json=None,
    data=None,
    proxy=None,
    timeout=30,
    retries=3,
    backoff=1.0,
):
    """Run requests.<method> with proxy + retry on transient network errors.

    Returns the Response object on success (any HTTP status). Raises the last
    exception after `retries` retries have all failed.

    Backoff is exponential with the given base: attempt 1 sleeps `backoff`,
    attempt 2 sleeps `backoff*2`, etc.
    """
    proxies = proxies_for(proxy)
    last_err = None
    for attempt in range(retries + 1):
        try:
            return requests.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json,
                data=data,
                proxies=proxies,
                timeout=timeout,
            )
        except NETWORK_ERRORS as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(backoff * (2 ** attempt))
    raise last_err
