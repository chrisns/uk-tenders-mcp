"""Find a Tender Service (FTS) adapter — the reference adapter (PRD §7.2).

Harvest via release packages only (the record endpoint is single-OCID/unpaginated).
Pagination: follow `links.next` verbatim; stop when the `links` key is ABSENT
(verified live for FTS). Cursor is opaque — never constructed here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..config import SOURCE_FTS, SOURCES, SourceConfig
from ..http_client import get_json
from .base import Adapter


class FTSAdapter(Adapter):
    key = SOURCE_FTS

    def __init__(
        self,
        cfg: SourceConfig | None = None,
        *,
        timeout_s: int = 30,
        max_retries: int = 8,
        session=None,
        page_limit: int = 100,
    ):
        self.cfg = cfg or SOURCES[SOURCE_FTS]
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session
        self.page_limit = page_limit

    def iter_releases(self, window_from: str, window_to: str) -> Iterator[dict[str, Any]]:
        url = (
            f"{self.cfg.base_url}/ocdsReleasePackages"
            f"?limit={self.page_limit}&updatedFrom={window_from}&updatedTo={window_to}"
        )
        while url:
            pkg = get_json(
                url,
                timeout_s=self.timeout_s,
                max_retries=self.max_retries,
                session=self.session,
            )
            for release in pkg.get("releases", []) or []:
                yield release
            links = pkg.get("links")
            # Termination signal (FTS, verified): the `links` key disappears. Guard against
            # a present-but-falsy/non-string `next` (would otherwise loop or error).
            nxt = links.get("next") if isinstance(links, dict) else None
            url = nxt if isinstance(nxt, str) and nxt else None

    def notice_url(self, release: dict[str, Any]) -> str:
        notice_id = release.get("id") or release.get("ocid", "")
        return self.cfg.notice_url_template.format(id=notice_id)

    def notice_type(self, release: dict[str, Any]) -> str | None:
        # FTS does not surface the form code in a stable documented place in OCDS;
        # left to regime date-fallback until verified (PRD §12 Q). Best-effort probe:
        tender = release.get("tender") or {}
        for key in ("procurementMethodDetails", "noticeType"):
            val = tender.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None
