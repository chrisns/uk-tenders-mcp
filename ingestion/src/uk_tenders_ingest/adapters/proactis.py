"""Public Contracts Scotland + Sell2Wales adapter (Proactis "OCDS Web API").

Both run the same Proactis platform exposing `/v1/Notices?outputType=0` as an OCDS release
package. IMPORTANT (verified live): the dateFrom/dateTo params are NOT honoured by
outputType=0 — it returns a fixed recent snapshot of notices and does not paginate. So this
adapter ingests the *current* notice snapshot (kept fresh nightly, idempotent on content_hash)
rather than a historical window. Deep historical backfill needs the correct Proactis params
(PRD §12 open question) and is not yet implemented.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import urllib3

from ..config import SOURCE_PCS, SOURCE_SELL2WALES, SOURCES, SourceConfig
from ..http_client import get_json
from .base import Adapter

# PCS/Sell2Wales serve an incomplete TLS chain (verified: browsers/curl fetch the
# intermediate via AIA, requests does not). Data is public OGL, read-only.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ProactisAdapter(Adapter):
    def __init__(self, key: str, *, lang: str | None = None, timeout_s=30, max_retries=8, session=None):
        self.key = key
        self.cfg: SourceConfig = SOURCES[key]
        self.lang = lang
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session

    def iter_releases(self, window_from: str, window_to: str) -> Iterator[dict[str, Any]]:
        # The Proactis OCDS API filters by MONTH via dateFrom=MM-YYYY (one month per call,
        # no pagination). Iterate every month in [window_from, window_to] for full history.
        y, m = int(window_from[:4]), int(window_from[5:7])
        y1, m1 = int(window_to[:4]), int(window_to[5:7])
        while (y, m) <= (y1, m1):
            url = f"{self.cfg.base_url}/Notices?dateFrom={m:02d}-{y}&outputType=0"
            if self.lang:
                url += f"&lang={self.lang}"
            pkg = get_json(
                url, timeout_s=self.timeout_s, max_retries=self.max_retries,
                session=self.session, verify=False,
            )
            for release in pkg.get("releases", []) or []:
                yield release
            m += 1
            if m > 12:
                m, y = 1, y + 1

    def notice_url(self, release: dict[str, Any]) -> str:
        # Best-effort: derive the public notice id from the numeric OCID suffix.
        ocid = release.get("ocid", "")
        num = ocid.split("-")[-1].lstrip("0") or ocid
        return self.cfg.notice_url_template.format(id=num)

    def notice_type(self, release: dict[str, Any]) -> str | None:
        return None


def pcs_adapter(**kw) -> ProactisAdapter:
    return ProactisAdapter(SOURCE_PCS, **kw)


def sell2wales_adapter(**kw) -> ProactisAdapter:
    return ProactisAdapter(SOURCE_SELL2WALES, lang="en", **kw)
