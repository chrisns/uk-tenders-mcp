"""Resilient Contracts Finder full backfill driver (2016-11 → 2026-05) + dedup.

Replaces the workflow approach (which kept failing on the StructuredOutput harness bug).
Per-month isolation: a failure in one month is logged and the loop continues. Skips months
already loaded with a success ingest_run, so it's cheap to resume. Streaming + idempotent,
so re-running a partial month completes it without duplicates. Then runs cross-source dedup.

  GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src python scripts/cf_backfill.py
"""
from __future__ import annotations

from google.cloud import bigquery

from uk_tenders_ingest import match, pipeline
from uk_tenders_ingest.bq import BigQueryLoader
from uk_tenders_ingest.config import SOURCE_CONTRACTS_FINDER, Settings

s = Settings.from_env()
loader = BigQueryLoader(s.project, s.raw_dataset, s.public_dataset, s.bq_location)
client = bigquery.Client(project=s.project, location=s.bq_location)


def already_done(ym: str) -> bool:
    q = (
        f"SELECT COUNT(*) c FROM `{s.project}.{s.raw_dataset}.ingest_run` "
        f"WHERE source='contracts_finder' AND mode='backfill' "
        f"AND FORMAT_TIMESTAMP('%Y-%m', window_from)=@ym AND status='success'"
    )
    job = client.query(
        q, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("ym", "STRING", ym)]
        )
    )
    return next(iter(job.result())).c > 0


months = []
y, m = 2016, 11
while y < 2026 or (y == 2026 and m <= 5):
    months.append((y, m))
    m += 1
    if m > 12:
        m, y = 1, y + 1

done = skipped = failed = 0
for (yy, mm) in months:
    ym = f"{yy}-{mm:02d}"
    if already_done(ym):
        skipped += 1
        print(f"skip {ym} (already loaded)", flush=True)
        continue
    frm = f"{yy}-{mm:02d}-01"
    ny = yy + 1 if mm == 12 else yy
    nm = 1 if mm == 12 else mm + 1
    to = f"{ny}-{nm:02d}-01"
    try:
        r = pipeline.run(
            source=SOURCE_CONTRACTS_FINDER, mode="backfill",
            window_from=frm, window_to=to, settings=s, loader=loader,
        )
        done += 1
        print(f"{ym}: {r}", flush=True)
    except Exception as exc:  # noqa: BLE001 — isolate the month, keep going
        failed += 1
        print(f"{ym}: FAILED {str(exc)[:200]}", flush=True)

print(f"=== backfill pass done: loaded={done} skipped={skipped} failed={failed} ===", flush=True)
print("=== cross-source dedup ===", flush=True)
print(match.run(s.project, s.raw_dataset, s.public_dataset, s.bq_location), flush=True)
print("CF_BACKFILL_COMPLETE", flush=True)
