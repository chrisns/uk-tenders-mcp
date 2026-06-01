"""HTTP client with polite backoff.

The FTS API publishes no numeric rate limit — the only contract is to honour
`Retry-After` on 429/503 (PRD §7.2). We add bounded exponential backoff on top.
"""

from __future__ import annotations

import time
from typing import Any

import requests

_DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "uk-tenders-mcp/0.1 (+https://github.com/chrisns/uk-tenders-mcp)",
}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


def get_json(
    url: str,
    *,
    timeout_s: int = 30,
    max_retries: int = 8,
    sleep=time.sleep,
    session: requests.Session | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """GET a URL and parse JSON, retrying on 429/503 (honouring Retry-After) and 5xx.

    verify=False is used ONLY for the Proactis portals (PCS/Sell2Wales), which serve an
    incomplete TLS chain; the data is public, read-only OGL. TODO: bundle the intermediate
    CA and re-enable verification.
    """
    sess = session or requests
    attempt = 0
    while True:
        attempt += 1
        resp = sess.get(url, headers=_DEFAULT_HEADERS, timeout=timeout_s, verify=verify)
        if resp.status_code == 200:
            return resp.json()
        # 403 is Contracts Finder's rate-limit signal (not auth); back off and retry.
        if resp.status_code in (403, 429, 503) or 500 <= resp.status_code < 600:
            if attempt > max_retries:
                raise HttpError(resp.status_code, url, resp.text)
            retry_after = resp.headers.get("Retry-After")
            delay = _parse_retry_after(retry_after) if retry_after else min(2**attempt, 60)
            sleep(delay)
            continue
        raise HttpError(resp.status_code, url, resp.text)


def _parse_retry_after(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0
