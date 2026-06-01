"""Reproject the previously-redacted OCDS sources (FTS, Contracts Finder, PCS) from the raw
event log, rebuilding compiled_process + process_change VERBATIM (ADR-0006 — redaction removed).

No re-scrape: the full release JSON is already in release_event_log. The scrape sources
(sell2wales, etendersni) never went through redact(), so they're already verbatim and are
carried over unchanged. Cross-source dedup re-runs at the end (stamps every ocid's group id).

Quota-safe (same approach as cf_finalize.py): compile into NON-partitioned staging tables, then
assemble the live tables with a single CTAS each + atomic RENAME swap. Staging is built fully
before the live tables are touched, so a mid-compile failure leaves the live tables intact.

  GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src python scripts/reproject_redacted.py
"""
from __future__ import annotations

import json

from google.cloud import bigquery

from uk_tenders_ingest import compile as comp
from uk_tenders_ingest import match
from uk_tenders_ingest.adapters.contracts_finder_harvester import ContractsFinderHarvesterAdapter
from uk_tenders_ingest.bq import _jsonify
from uk_tenders_ingest.config import Settings
from uk_tenders_ingest.pipeline import make_adapter

REBUILD = ("fts", "contracts_finder", "pcs")  # the sources that went through redact()

s = Settings.from_env()
client = bigquery.Client(project=s.project, location=s.bq_location)
DS = f"{s.project}.{s.public_dataset}"
CP = f"`{DS}.compiled_process`"
PC = f"`{DS}.process_change`"
CPS = f"`{DS}.compiled_process_reproj_stage`"
PCS = f"`{DS}.process_change_reproj_stage`"
EL = f"`{s.project}.{s.raw_dataset}.release_event_log`"

# CF was bulk-loaded via the Harvester CSV channel, so its notice_url/notice_type come from the
# harvester adapter (matching cf_finalize.py); FTS/PCS use their pipeline adapters.
adapters = {
    "fts": make_adapter("fts", s),
    "pcs": make_adapter("pcs", s),
    "contracts_finder": ContractsFinderHarvesterAdapter(),
}


def q(sql):
    return client.query(sql).result()


print("creating non-partitioned staging tables ...", flush=True)
q(f"CREATE OR REPLACE TABLE {CPS} AS SELECT * FROM {CP} WHERE FALSE")
q(f"CREATE OR REPLACE TABLE {PCS} AS SELECT * FROM {PC} WHERE FALSE")
cps_tbl = client.get_table(f"{DS}.compiled_process_reproj_stage")
pcs_tbl = client.get_table(f"{DS}.process_change_reproj_stage")


def flush(comp_rows, chg_rows):
    if comp_rows:
        client.load_table_from_json(
            _jsonify(comp_rows, ("compiled_json",)), cps_tbl,
            job_config=bigquery.LoadJobConfig(schema=cps_tbl.schema, write_disposition="WRITE_APPEND", max_bad_records=50),
        ).result()
    if chg_rows:
        client.load_table_from_json(
            chg_rows, pcs_tbl,
            job_config=bigquery.LoadJobConfig(schema=pcs_tbl.schema, write_disposition="WRITE_APPEND", max_bad_records=50),
        ).result()


srcs = "','".join(REBUILD)
print(f"streaming releases for {REBUILD} ordered by source, ocid ...", flush=True)
rows = client.query(
    f"SELECT source, ocid, raw_json FROM {EL} WHERE source IN ('{srcs}') ORDER BY source, ocid"
).result()

cur_key = None
rels: list = []
comp_rows: list = []
chg_rows: list = []
n_proc = n_chg = 0


def compile_one(src, ocid, releases):
    global n_proc, n_chg
    ad = adapters[src]
    comp_rows.append(comp.project_process(ocid, src, releases, ad))
    ch = comp.diff_process(ocid, src, releases, ad)
    chg_rows.extend(ch)
    n_proc += 1
    n_chg += len(ch)


for r in rows:
    key = (r["source"], r["ocid"])
    if cur_key is not None and key != cur_key:
        compile_one(cur_key[0], cur_key[1], rels)
        rels = []
        if len(comp_rows) >= 5000:
            flush(comp_rows, chg_rows)
            print(f"  ... {n_proc} processes compiled", flush=True)
            comp_rows, chg_rows = [], []
    cur_key = key
    try:
        rels.append(json.loads(r["raw_json"]))
    except Exception:
        pass
if cur_key is not None and rels:
    compile_one(cur_key[0], cur_key[1], rels)
flush(comp_rows, chg_rows)
print(f"compiled {n_proc} processes, {n_chg} changes -> staging", flush=True)

print("assembling live tables (single CTAS each, then atomic swap) ...", flush=True)
q(f"""CREATE OR REPLACE TABLE `{DS}.compiled_process_rebuilt`
      CLUSTER BY source, buyer_name, cpv_division AS
      SELECT * FROM {CP} WHERE source NOT IN ('{srcs}')
      UNION ALL SELECT * FROM {CPS}""")
q(f"DROP TABLE {CP}")
q(f"ALTER TABLE `{DS}.compiled_process_rebuilt` RENAME TO compiled_process")

q(f"""CREATE OR REPLACE TABLE `{DS}.process_change_rebuilt`
      CLUSTER BY source, change_class, ocid AS
      SELECT * FROM {PC} WHERE source NOT IN ('{srcs}')
      UNION ALL SELECT * FROM {PCS}""")
q(f"DROP TABLE {PC}")
q(f"ALTER TABLE `{DS}.process_change_rebuilt` RENAME TO process_change")

q(f"DROP TABLE {CPS}")
q(f"DROP TABLE {PCS}")

print("=== cross-source dedup ===", flush=True)
print(match.run(s.project, s.raw_dataset, s.public_dataset, s.bq_location), flush=True)
print("REPROJECT_COMPLETE", flush=True)
