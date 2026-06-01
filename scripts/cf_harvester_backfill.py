"""Fast full Contracts Finder backfill via the bulk Harvester CSV.

Loads the whole CF history (2016-11 → today) using ContractsFinderHarvesterAdapter — one bulk
CSV per day, no per-request rate-limit walls. Year-by-year for isolation; streaming +
idempotent (re-runs are no-ops). Then runs cross-source dedup. Far faster + more complete than
walking the OCDS search API.

  GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src python scripts/cf_harvester_backfill.py
"""
from __future__ import annotations

from datetime import date

from uk_tenders_ingest import match, pipeline
from uk_tenders_ingest.adapters.contracts_finder_harvester import ContractsFinderHarvesterAdapter
from uk_tenders_ingest.config import SOURCE_CONTRACTS_FINDER, Settings

s = Settings.from_env()
from uk_tenders_ingest.bq import BigQueryLoader

loader = BigQueryLoader(s.project, s.raw_dataset, s.public_dataset, s.bq_location)
adapter = ContractsFinderHarvesterAdapter()

today = date.today()
total_loaded = total_proc = 0
for y in range(2016, today.year + 1):
    frm = "2016-11-01" if y == 2016 else f"{y}-01-01"          # CF OCDS history starts 2016-11
    to = today.isoformat() if y == today.year else f"{y + 1}-01-01"
    try:
        r = pipeline.run(
            source=SOURCE_CONTRACTS_FINDER, mode="backfill",
            window_from=frm, window_to=to, settings=s, loader=loader, adapter=adapter,
        )
        total_loaded += r.get("loaded", 0)
        total_proc += r.get("processes", 0)
        print(f"YEAR {y}: {r}", flush=True)
    except Exception as exc:  # noqa: BLE001 — isolate the year, keep going
        print(f"YEAR {y}: FAILED {str(exc)[:200]}", flush=True)

print(f"=== harvester pass done: loaded={total_loaded} processes={total_proc} ===", flush=True)
print("=== cross-source dedup ===", flush=True)
print(match.run(s.project, s.raw_dataset, s.public_dataset, s.bq_location), flush=True)
print("CF_BACKFILL_COMPLETE", flush=True)
