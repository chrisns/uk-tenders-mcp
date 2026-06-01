"""Sell2Wales backfill via HTML scrape (the OCDS API + bulk download are broken upstream).

Threaded across months (I/O-bound scrape); ALL BigQuery loads happen on the main thread
sequentially (one loader, no per-table write-rate contention). Appends parsed notices to
compiled_process (source='sell2wales') + a provenance row to release_event_log, then runs
cross-source dedup so cross-published notices fold into their FTS process_group.

  GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src \
    python scripts/sell2wales_backfill.py [start_year] [end_year]
"""
from __future__ import annotations

import calendar
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
import urllib3
from google.cloud import bigquery

from uk_tenders_ingest import match
from uk_tenders_ingest.adapters.sell2wales_scrape import UA, Sell2WalesScrapeAdapter
from uk_tenders_ingest.bq import _jsonify
from uk_tenders_ingest.canonical import content_hash
from uk_tenders_ingest.config import Settings

urllib3.disable_warnings()

S = Settings.from_env()
client = bigquery.Client(project=S.project, location=S.bq_location)
DS = f"{S.project}.{S.public_dataset}"
RAW = f"{S.project}.{S.raw_dataset}"
SRC = "sell2wales"
ad = Sell2WalesScrapeAdapter(pause_s=0.34)
run_id = f"s2w-scrape-{uuid.uuid4().hex[:8]}"
load_date = datetime.utcnow().strftime("%Y-%m-%d")

CP_COLS = [
    "ocid", "source", "process_group_id", "title", "description", "buyer_name", "buyer_id",
    "status", "stage", "regime", "main_category", "value_amount", "value_currency",
    "awarded_amount", "awarded_currency", "cpv_codes", "cpv_division", "region",
    "published_date", "tender_end_date", "last_updated", "official_url", "awards", "parties",
    "compiled_json",
]


def compiled_row(p: dict) -> dict:
    row = {k: p.get(k) for k in CP_COLS if k != "compiled_json"}
    row["compiled_json"] = json.dumps(p, ensure_ascii=False, default=str)
    return row


def event_row_s2w(p: dict) -> dict:
    return {
        "content_hash": content_hash(p),
        "source": SRC,
        "ocid": p["ocid"],
        "release_id": p.get("_web_id"),
        "release_date": p.get("published_date"),
        "tags": [p["stage"]] if p.get("stage") else [],
        "updated": p.get("last_updated") or p.get("published_date"),
        "title": p.get("title"),
        "buyer_name": p.get("buyer_name"),
        "value_amount": p.get("value_amount"),
        "value_currency": p.get("value_currency"),
        "load_run_id": run_id,
        "load_date": load_date,
        "raw_json": json.dumps(p, ensure_ascii=False, default=str),
    }


def do_month(y: int, m: int):
    last = calendar.monthrange(y, m)[1]
    df, dt = f"01/{m:02d}/{y}", f"{last:02d}/{m:02d}/{y}"
    try:
        ids = ad.enumerate(df, dt)
    except Exception as e:
        return y, m, [], [], f"enumerate-error: {e}"
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    sess.verify = False
    crows, erows = [], []
    for wid, title in ids:
        try:
            p = ad.fetch_notice(sess, wid, title)
        except Exception:
            p = None
        if p and p.get("ocid"):
            crows.append(compiled_row(p))
            erows.append(event_row_s2w(p))
    return y, m, crows, erows, None


def main():
    start_y = int(sys.argv[1]) if len(sys.argv) > 1 else 2008
    end_y = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    end_m = 5 if end_y == 2026 else 12
    todo = [(y, m) for y in range(start_y, end_y + 1)
            for m in range(1, 13) if not (y == end_y and m > end_m)]
    print(f"Sell2Wales backfill {start_y}-01 .. {end_y}-{end_m:02d}  ({len(todo)} months)", flush=True)

    cp_tbl = client.get_table(f"{DS}.compiled_process")
    el_tbl = client.get_table(f"{RAW}.release_event_log")

    def flush(crows, erows):
        if crows:
            client.load_table_from_json(
                _jsonify(crows, ("compiled_json",)), cp_tbl,
                job_config=bigquery.LoadJobConfig(schema=cp_tbl.schema, write_disposition="WRITE_APPEND", max_bad_records=50),
            ).result()
        if erows:
            client.load_table_from_json(
                _jsonify(erows, ("raw_json",)), el_tbl,
                job_config=bigquery.LoadJobConfig(schema=el_tbl.schema, write_disposition="WRITE_APPEND", max_bad_records=50),
            ).result()

    total_notices = 0
    done = 0
    cbuf, ebuf = [], []
    seen_ocids: set = set()  # dedup across months: the date-windowed search overlaps heavily
    # (recent/cross-published notices recur in many windows), so without this the WRITE_APPEND
    # load duplicates them. (Prefer the idempotent nightly path — scrape_ingest.run_sell2wales,
    # which replaces by ocid — for any re-run; this appender exists for the one-off bulk seed.)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(do_month, y, m): (y, m) for y, m in todo}
        for f in as_completed(futs):
            y, m, crows, erows, err = f.result()
            done += 1
            fresh = 0
            for cr, er in zip(crows, erows):
                oc = cr.get("ocid")
                if oc and oc not in seen_ocids:
                    seen_ocids.add(oc)
                    cbuf.append(cr)
                    ebuf.append(er)
                    fresh += 1
            total_notices += fresh
            tag = f"ERR {err}" if err else f"{fresh} new ({len(crows)} seen)"
            print(f"[{done}/{len(todo)}] {y}-{m:02d}: {tag}  (cumulative {total_notices})", flush=True)
            if len(cbuf) >= 2000:
                flush(cbuf, ebuf)
                cbuf, ebuf = [], []
    flush(cbuf, ebuf)
    print(f"loaded {total_notices} distinct Sell2Wales notices", flush=True)

    # ledger + dedup + status
    from uk_tenders_ingest.bq import BigQueryLoader
    loader = BigQueryLoader(S.project, S.raw_dataset, S.public_dataset, S.bq_location)
    loader.insert_ingest_run({"run_id": run_id, "source": SRC, "status": "running",
                              "mode": "backfill", "window_from": f"{start_y}-01-01T00:00:00",
                              "window_to": f"{end_y}-{end_m:02d}-28T00:00:00"})
    loader.finish_ingest_run(run_id, "success", total_notices, total_notices, None)

    print("=== cross-source dedup ===", flush=True)
    print(match.run(S.project, S.raw_dataset, S.public_dataset, S.bq_location), flush=True)
    loader.upsert_source_status(SRC)
    print("SELL2WALES_BACKFILL_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
