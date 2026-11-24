"""Shared HTTP download helper for the comparison scripts."""

import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 60  # seconds per request; sdists and CRAN tarballs are small
ATTEMPTS = 4
BACKOFF = 5  # seconds, doubled after each failed attempt: 5, 10, 20


def fetch(url):
    """Return the body of url, retrying transient failures.

    PyPI's pypi.org/packages/source redirect and CRAN mirrors return 5xx errors
    intermittently, so 429, 5xx, timeouts and connection errors are retried.
    Other HTTP errors (e.g. 404 for a version that is not on a mirror) are
    raised immediately, so callers can fall through to the next URL.
    """
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            error = e
        except (urllib.error.URLError, TimeoutError) as e:
            error = e
        if attempt == ATTEMPTS - 1:
            raise error
        delay = BACKOFF * 2**attempt
        print(f"retrying in {delay}s after {error}: {url}", file=sys.stderr)
        time.sleep(delay)
