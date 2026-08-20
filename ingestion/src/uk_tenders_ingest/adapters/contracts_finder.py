"""Contracts Finder adapter (PRD §7.1).

Same OCDS release-package + `links.next` cursor model as FTS (verified live), but the
incremental params are `publishedFrom`/`publishedTo` and the publisher prefix is
`ocds-b5fd17`. Notice ids are `{guid}-{number}`; the public notice URL uses the guid.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..config import SOURCE_CONTRACTS_FINDER, SOURCES, SourceConfig
from ..http_client import get_json
from .base import Adapter

_TRAILING_NUM = re.compile(r"-\d+$")


class ContractsFinderAdapter(Adapter):
    key = SOURCE_CONTRACTS_FINDER

    def __init__(self, cfg: SourceConfig | None = None, *, timeout_s=30, max_retries=8, session=None, page_limit=100):
        self.cfg = cfg or SOURCES[SOURCE_CONTRACTS_FINDER]
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session
        self.page_limit = page_limit

    def iter_releases(self, window_from: str, window_to: str) -> Iterator[dict[str, Any]]:
        url = (
            f"{self.cfg.base_url}/Published/Notices/OCDS/Search"
            f"?limit={self.page_limit}&publishedFrom={window_from}&publishedTo={window_to}"
        )
        while url:
            pkg = get_json(url, timeout_s=self.timeout_s, max_retries=self.max_retries, session=self.session)
            yield from pkg.get("releases", []) or []
            links = pkg.get("links")
            nxt = links.get("next") if isinstance(links, dict) else None
            url = nxt if isinstance(nxt, str) and nxt else None

    def notice_url(self, release: dict[str, Any]) -> str:
        nid = release.get("id") or release.get("ocid", "")
        guid = _TRAILING_NUM.sub("", nid)  # strip trailing -<number> → public notice guid
        return self.cfg.notice_url_template.format(id=guid or nid)

    def notice_type(self, release: dict[str, Any]) -> str | None:
        return None
