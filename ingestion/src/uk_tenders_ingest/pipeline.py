"""Run orchestration: one windowed-replay code path for backfill AND nightly (PRD §7.4).

Backfill and nightly differ only by window bounds. The same entrypoint:
  1. walks the source over [from, to] in windows, collecting releases;
  2. MERGEs them into the raw event log (idempotent on content_hash);
  3. recompiles every affected OCID from its full history, projects the redacted
     curated row + change diffs, and replaces them in the public dataset;
  4. refreshes source_status and closes the ingest_run ledger row.
A dry run does steps 1+compile in-memory only (no BigQuery) for validation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

from . import compile as comp
from .adapters.contracts_finder import ContractsFinderAdapter
from .adapters.fts import FTSAdapter
from .adapters.proactis import pcs_adapter, sell2wales_adapter
from .canonical import content_hash
from .config import (
    SOURCE_CONTRACTS_FINDER,
    SOURCE_FTS,
    SOURCE_PCS,
    SOURCE_SELL2WALES,
    Settings,
)

ADAPTERS = {
    SOURCE_FTS: FTSAdapter,
    SOURCE_CONTRACTS_FINDER: ContractsFinderAdapter,
    SOURCE_PCS: pcs_adapter,
    SOURCE_SELL2WALES: sell2wales_adapter,
}


class SourceNotImplemented(RuntimeError):
    pass


def make_adapter(source: str, settings: Settings):
    cls = ADAPTERS.get(source)
    if cls is None:
        # Adapter isolation (ADR-0004): unimplemented sources fail in isolation.
        raise SourceNotImplemented(
            f"adapter for '{source}' not yet implemented (PRD §12 launch gate)"
        )
    return cls(timeout_s=settings.http_timeout_s, max_retries=settings.max_retries)


def _parse(dt: str) -> datetime:
    s = dt.strip()
    if len(s) == 10:  # date only
        return datetime.strptime(s, "%Y-%m-%d")
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def windows(frm: str, to: str, days: int) -> Iterator[tuple[str, str]]:
    start, end = _parse(frm), _parse(to)
    cur = start
    step = timedelta(days=days)
    while cur < end:
        nxt = min(cur + step, end)
        yield _fmt(cur), _fmt(nxt)
        cur = nxt


def _scalars(release: dict, adapter) -> dict:
    tender = release.get("tender") if isinstance(release.get("tender"), dict) else {}
    buyer = release.get("buyer") if isinstance(release.get("buyer"), dict) else {}
    buyer_name = buyer.get("name")
    if not buyer_name:
        for p in release.get("parties", []) or []:
            if isinstance(p, dict) and "buyer" in (p.get("roles") or []):
                buyer_name = p.get("name")
                break
    value = tender.get("value") if isinstance(tender.get("value"), dict) else {}
    return {
        "ocid": comp.as_text(release.get("ocid")),
        "release_id": comp.as_text(release.get("id")),
        "release_date": comp._iso(release.get("date")),
        "tags": release.get("tag") or [],
        "updated": None,
        "title": comp.as_text(tender.get("title")),
        "buyer_name": comp.as_text(buyer_name),
        "value_amount": _num(value.get("amount")),
        "value_currency": comp.as_text(value.get("currency")),
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def event_row(release: dict, source: str, adapter, run_id: str, load_date: str) -> dict:
    row = _scalars(release, adapter)
    row.update(
        {
            "content_hash": content_hash(release),
            "source": source,
            "load_run_id": run_id,
            "load_date": load_date,
            "raw_json": release,
        }
    )
    return row


def run(
    *,
    source: str,
    mode: str,
    window_from: str,
    window_to: str,
    settings: Settings,
    loader=None,
    dry_run: bool = False,
    max_windows: int | None = None,
    max_releases: int | None = None,
    progress=print,
    adapter=None,
) -> dict:
    adapter = adapter or make_adapter(source, settings)
    run_id = f"{source}-{uuid.uuid4().hex[:12]}"
    load_date = datetime.utcnow().strftime("%Y-%m-%d")

    # ----- dry run: collect (bounded) for validation only, no BigQuery -----
    if dry_run:
        rows: list[dict] = []
        seen = 0
        win = 0
        for wf, wt in windows(window_from, window_to, settings.window_days):
            win += 1
            if max_windows and win > max_windows:
                break
            progress(f"[{source}] window {wf} → {wt}")
            for rel in adapter.iter_releases(wf, wt):
                rows.append(event_row(rel, source, adapter, run_id, load_date))
                seen += 1
                if max_releases and seen >= max_releases:
                    break
            if max_releases and seen >= max_releases:
                break
        by_ocid: dict[str, list[dict]] = {}
        for r in rows:
            by_ocid.setdefault(r["ocid"], []).append(r["raw_json"])
        sample = list(by_ocid.items())[:3]
        return {
            "run_id": run_id, "source": source, "releases": seen,
            "distinct_ocids": len(by_ocid), "windows": win,
            "sample_compiled": [comp.project_process(o, source, rels, adapter) for o, rels in sample],
        }

    if loader is None:
        raise ValueError("loader required for a non-dry run")

    # ----- real run: STREAM per window (bounded memory; incremental, resumable) -----
    loader.insert_ingest_run(
        {"run_id": run_id, "source": source, "status": "running", "mode": mode,
         "window_from": _fmt(_parse(window_from)), "window_to": _fmt(_parse(window_to))}
    )
    seen = loaded = processes = changes = win = 0
    try:
        for wf, wt in windows(window_from, window_to, settings.window_days):
            win += 1
            if max_windows and win > max_windows:
                break
            win_rows: list[dict] = []
            for rel in adapter.iter_releases(wf, wt):
                win_rows.append(event_row(rel, source, adapter, run_id, load_date))
                seen += 1
                if max_releases and seen >= max_releases:
                    break
            if win_rows:
                loaded += loader.merge_event_log(win_rows)
                # affected OCIDs are exactly this window's releases (in memory) — no need to
                # rescan the (growing) event log by content_hash.
                affected = sorted({r["ocid"] for r in win_rows})
                rel_by_ocid = loader.releases_for_ocids(affected) if affected else {}
                compiled_rows, change_rows = [], []
                for ocid, rels in rel_by_ocid.items():
                    compiled_rows.append(comp.project_process(ocid, source, rels, adapter))
                    change_rows.extend(comp.diff_process(ocid, source, rels, adapter))
                loader.replace_compiled(compiled_rows)
                loader.replace_changes(affected, change_rows)
                processes += len(compiled_rows)
                changes += len(change_rows)
                # keep the ledger live so a long backfill shows progress / stays resumable
                loader.finish_ingest_run(run_id, "running", seen, loaded, None)
            progress(f"[{source}] {wf[:10]}→{wt[:10]} | seen={seen} loaded={loaded} processes={processes}")
            if max_releases and seen >= max_releases:
                break
        loader.finish_ingest_run(run_id, "success", seen, loaded, None)
        loader.upsert_source_status(source)
        progress(f"[{source}] DONE: releases={seen} loaded={loaded} processes={processes} changes={changes}")
        return {"run_id": run_id, "releases": seen, "loaded": loaded,
                "processes": processes, "changes": changes}
    except Exception as exc:  # noqa: BLE001 — record failure then re-raise
        loader.finish_ingest_run(run_id, "failed", seen, loaded, str(exc)[:500])
        raise
