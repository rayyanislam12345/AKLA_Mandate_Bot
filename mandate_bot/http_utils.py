"""Shared retry-with-backoff for plain requests.Session GET/POST calls —
transient network errors (connection resets, timeouts) shouldn't crash an
entire run when they'd likely succeed a few seconds later. Mirrors the
retry pattern already used for file downloads (pdf_utils.py) and Playwright
navigation (bppt.py/sindh.py), applied here to the listing/detail page
fetches that previously had no retry at all.
"""
from __future__ import annotations

import logging
import time

import requests

RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]  # seconds, one per retry attempt


def request_with_retry(session: requests.Session, method: str, url: str,
                        log_: logging.Logger, **kwargs) -> requests.Response:
    """Raises the last exception if every attempt fails — callers decide
    how to handle that (skip this item, abort this source, etc.)."""
    last_exc = None
    for attempt in range(RETRIES):
        try:
            resp = session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                log_.warning("%s %s failed (attempt %d/%d): %s — retrying in %ds",
                             method, url, attempt + 1, RETRIES, exc, delay)
                time.sleep(delay)
    raise last_exc


def get_with_retry(session: requests.Session, url: str, log_: logging.Logger, **kwargs) -> requests.Response:
    return request_with_retry(session, "GET", url, log_, **kwargs)


def post_with_retry(session: requests.Session, url: str, log_: logging.Logger, **kwargs) -> requests.Response:
    return request_with_retry(session, "POST", url, log_, **kwargs)
