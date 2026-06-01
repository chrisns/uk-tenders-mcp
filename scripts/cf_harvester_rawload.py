"""Append-only raw load of CF harvester releases for a date range (NO compile).

Concurrency-safe (WRITE_APPEND) so many shards can run in parallel without colliding.
Compile/dedup happens once afterwards in cf_finalize.py.

  python scripts/cf_harvester_rawload.py 2018-01-01 2018-03-01
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime

from uk_tenders_ingest.adapters.contracts_finder_harvester import ContractsFinderHarvesterAdapter
from uk_tenders_ingest.bq import BigQueryLoader
from uk_tenders_ingest.config import SOURCE_CONTRACTS_FINDER, Settings
from uk_tenders_ingest.pipeline import event_row

frm, to = sys.argv[1], sys.argv[2]
s = Settings.from_env()
loader = BigQueryLoader(s.project, s.raw_dataset, s.public_dataset, s.bq_location)
ad = ContractsFinderHarvesterAdapter()
run_id = f"cf-harv-{uuid.uuid4().hex[:8]}"
load_date = datetime.utcnow().strftime("%Y-%m-%d")

batch: list = []
seen = appended = 0
for rel in ad.iter_releases(frm, to):
    batch.append(event_row(rel, SOURCE_CONTRACTS_FINDER, ad, run_id, load_date))
    seen += 1
    if len(batch) >= 2000:
        appended += loader.append_event_log(batch)
        batch = []
if batch:
    appended += loader.append_event_log(batch)

print(f"RAWLOAD {frm}..{to} seen={seen} appended={appended}", flush=True)
