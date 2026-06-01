"""Ingest path for the non-OCDS scrape sources (Sell2Wales, eTendersNI).

These portals expose no usable OCDS feed (Sell2Wales' API is broken upstream; eTendersNI is
CAPTCHA-gated with no API), so they can't go through the OCDS `pipeline.run`. Instead we scrape
canonical rows directly and load them idempotently (delete-by-ocid + content-hash event merge),
so the nightly job can re-scrape a recent window safely. eTendersNI's CAPTCHA is solved by
Gemini (see adapters.etendersni_scrape) — no human in the loop.

Wired into the CLI: `python -m uk_tenders_ingest --source sell2wales|etendersni --mode nightly`.
"""

from __future__ import annotations

import calendar
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .canonical import content_hash

CP_COLS = [
    "ocid", "source", "process_group_id", "title", "description", "buyer_name", "buyer_id",
    "status", "stage", "regime", "main_category", "value_amount", "value_currency",
    "awarded_amount", "awarded_currency", "cpv_codes", "cpv_division", "region",
    "published_date", "tender_end_date", "last_updated", "official_url", "awards", "parties",
]


def _iso_to_ddmmyyyy(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "")).strftime("%d/%m/%Y")


def _compiled(p: dict) -> dict:
    row = {k: p.get(k) for k in CP_COLS}
    row["compiled_json"] = json.dumps(p, ensure_ascii=False, default=str)
    return row


def _event(p: dict, source: str, run_id: str, load_date: str) -> dict:
    return {
        "content_hash": content_hash(p), "source": source, "ocid": p["ocid"],
        "release_id": p.get("_web_id") or p["ocid"].split("-")[-1],
        "release_date": p.get("published_date"),
        "tags": [p["stage"]] if p.get("stage") else [],
        "updated": p.get("last_updated") or p.get("published_date"),
        "title": p.get("title"), "buyer_name": p.get("buyer_name"),
        "value_amount": p.get("value_amount"), "value_currency": p.get("value_currency"),
        "load_run_id": run_id, "load_date": load_date,
        "raw_json": json.dumps(p, ensure_ascii=False, default=str),
    }


def _persist(loader, source: str, rows: list[dict], run_id: str, load_date: str, mode: str) -> int:
    if not rows:
        return 0
    events = [_event(p, source, run_id, load_date) for p in rows]
    compiled = [_compiled(p) for p in rows]
    loader.merge_event_log(events)                 # content_hash idempotent
    loader.replace_compiled(compiled)              # delete-by-ocid + insert (idempotent per ocid)
    return len(rows)


def _sell2wales_api_healthy() -> bool:
    """Has Sell2Wales' OCDS API recovered? It's broken upstream (`nvarchar→float` SQL error on
    every query mode — confirmed by the Welsh Government, no ETA), serving an HTML error page
    instead of JSON. Healthy = a real OCDS JSON response. Cheap one-shot probe."""
    import requests
    import urllib3
    urllib3.disable_warnings()
    from .config import SOURCE_SELL2WALES, SOURCES
    try:
        url = f"{SOURCES[SOURCE_SELL2WALES].base_url}/Notices?dateFrom=01-2025&outputType=0&lang=en"
        r = requests.get(url, timeout=20, verify=False,
                         headers={"Accept": "application/json", "User-Agent": "uk-tenders-mcp/0.1"})
        ct = r.headers.get("content-type", "").lower()
        return r.status_code == 200 and "json" in ct and "nvarchar" not in r.text[:500].lower()
    except Exception:
        return False


def run_sell2wales(settings, loader, window_from: str, window_to: str, mode: str = "nightly") -> dict:
    from .adapters.sell2wales_scrape import UA, Sell2WalesScrapeAdapter
    import requests

    # Self-heal: the upstream OCDS API is broken (no ETA). The moment it returns valid OCDS,
    # switch to the clean API path and backfill the FULL history (idempotent, per-ocid replace),
    # auto-completing Sell2Wales to ~100% with zero manual intervention. Until then, fall back
    # to the HTML scrape below (the only working endpoint, ~2k of the ~165k notices).
    if _sell2wales_api_healthy():
        from .config import SOURCE_SELL2WALES, SOURCES
        from .pipeline import run as ocds_run
        print("[sell2wales] OCDS API healthy — full backfill via API", flush=True)
        return ocds_run(source=SOURCE_SELL2WALES, mode="backfill",
                        window_from=SOURCES[SOURCE_SELL2WALES].backfill_floor,
                        window_to=window_to, settings=settings, loader=loader)

    ad = Sell2WalesScrapeAdapter()
    run_id = f"s2w-{mode}-{uuid.uuid4().hex[:8]}"
    load_date = datetime.utcnow().strftime("%Y-%m-%d")
    df, dt = datetime.fromisoformat(window_from[:19]), datetime.fromisoformat(window_to[:19])
    # iterate whole months spanning the window
    rows: list[dict] = []
    y, m = df.year, df.month
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    sess.verify = False
    while (y, m) <= (dt.year, dt.month):
        last = calendar.monthrange(y, m)[1]
        for wid, title in ad.enumerate(f"01/{m:02d}/{y}", f"{last:02d}/{m:02d}/{y}"):
            p = ad.fetch_notice(sess, wid, title)
            if p and p.get("ocid"):
                rows.append(p)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    loaded = _persist(loader, "sell2wales", rows, run_id, load_date, mode)
    loader.insert_ingest_run({"run_id": run_id, "source": "sell2wales", "status": "running",
                              "mode": mode, "window_from": window_from, "window_to": window_to})
    loader.finish_ingest_run(run_id, "success", len(rows), loaded, None)
    loader.upsert_source_status("sell2wales")
    return {"source": "sell2wales", "seen": len(rows), "loaded": loaded}


def run_etendersni(settings, loader, window_from: str, window_to: str, mode: str = "nightly") -> dict:
    from .adapters.etendersni_scrape import ETendersNIAdapter, build_row

    ad = ETendersNIAdapter()
    run_id = f"ni-{mode}-{uuid.uuid4().hex[:8]}"
    load_date = datetime.utcnow().strftime("%Y-%m-%d")
    listings = ad.search_window(_iso_to_ddmmyyyy(window_from), _iso_to_ddmmyyyy(window_to))

    rows: list[dict] = []

    def enrich(listing):
        f = ad.fetch_detail(listing["resourceId"])
        return build_row(listing["resourceId"], listing, f) if f else None

    with ThreadPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(enrich, listing) for listing in listings]):
            r = fut.result()
            if r:
                rows.append(r)
    loaded = _persist(loader, "etendersni", rows, run_id, load_date, mode)
    loader.insert_ingest_run({"run_id": run_id, "source": "etendersni", "status": "running",
                              "mode": mode, "window_from": window_from, "window_to": window_to})
    loader.finish_ingest_run(run_id, "success", len(listings), loaded, None)
    loader.upsert_source_status("etendersni")
    return {"source": "etendersni", "seen": len(listings), "loaded": loaded}


SCRAPE_RUNNERS = {"sell2wales": run_sell2wales, "etendersni": run_etendersni}
