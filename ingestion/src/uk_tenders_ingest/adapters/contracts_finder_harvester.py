"""Contracts Finder *bulk harvester* adapter.

CF's Harvester endpoint serves one day's OCDS releases as a single flattened CSV:
  GET /Harvester/Notices/Data/CSV/{year}/{month}/{day}
This is the sanctioned BULK channel (one GET per day, no per-request pagination), far cheaper
than walking the rate-limited OCDS search API. Each CSV row is one release with path-style
columns (`releases/0/tender/title`, `releases/0/awards/0/suppliers/0/name`, …). We un-flatten
each row back into a nested OCDS release and yield it, so the rest of the pipeline (compile,
dedup) is unchanged. Source key stays `contracts_finder` — same publisher, faster path.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

from ..config import SOURCE_CONTRACTS_FINDER, SOURCES
from .base import Adapter
from .contracts_finder import _TRAILING_NUM

_PREFIX = "releases/0/"


def _set_path(root: dict, path: list[str], value: str) -> None:
    node: Any = root
    for i, seg in enumerate(path):
        is_last = i == len(path) - 1
        nxt_is_idx = (not is_last) and path[i + 1].isdigit()
        if seg.isdigit():
            idx = int(seg)
            while len(node) <= idx:
                node.append(None)
            if is_last:
                node[idx] = value
            else:
                if not isinstance(node[idx], (dict, list)):
                    node[idx] = [] if nxt_is_idx else {}
                node = node[idx]
        else:
            if is_last:
                node[seg] = value
            else:
                if not isinstance(node.get(seg), (dict, list)):
                    node[seg] = [] if nxt_is_idx else {}
                node = node[seg]


def _clean(node: Any) -> Any:
    """Drop None entries left by sparse list indices."""
    if isinstance(node, list):
        return [_clean(x) for x in node if x is not None]
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items()}
    return node


def unflatten_release(row: dict[str, str]) -> dict[str, Any]:
    """Reconstruct the nested OCDS release from one flattened CSV row."""
    rel: dict[str, Any] = {}
    for col, val in row.items():
        if not col or val is None or val == "" or not col.startswith(_PREFIX):
            continue
        _set_path(rel, col[len(_PREFIX):].split("/"), val)
    return _clean(rel)


def parse_csv(text: str) -> list[dict[str, Any]]:
    return [unflatten_release(r) for r in csv.DictReader(io.StringIO(text))]


def _days(window_from: str, window_to: str) -> Iterator[date]:
    d = date.fromisoformat(window_from[:10])
    end = date.fromisoformat(window_to[:10])
    while d < end:
        yield d
        d += timedelta(days=1)


class ContractsFinderHarvesterAdapter(Adapter):
    key = SOURCE_CONTRACTS_FINDER

    def __init__(self, *, timeout_s=60, max_retries=8, session=None):
        self.cfg = SOURCES[SOURCE_CONTRACTS_FINDER]
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session

    def iter_releases(self, window_from: str, window_to: str) -> Iterator[dict[str, Any]]:
        import requests

        sess = self.session or requests
        for d in _days(window_from, window_to):
            url = f"{self.cfg.base_url}/Harvester/Notices/Data/CSV/{d.year}/{d.month:02d}/{d.day:02d}"
            resp = sess.get(url, headers={"Accept": "text/csv"}, timeout=self.timeout_s)
            if resp.status_code != 200 or "text/csv" not in resp.headers.get("content-type", ""):
                continue  # empty/missing day
            for rel in parse_csv(resp.text):
                # guard against the rare malformed row whose ocid cell isn't a real OCID
                if str(rel.get("ocid", "")).startswith("ocds-"):
                    yield rel

    def notice_url(self, release: dict[str, Any]) -> str:
        nid = release.get("id") or release.get("ocid", "")
        guid = _TRAILING_NUM.sub("", nid)
        return self.cfg.notice_url_template.format(id=guid or nid)

    def notice_type(self, release: dict[str, Any]) -> str | None:
        return None
