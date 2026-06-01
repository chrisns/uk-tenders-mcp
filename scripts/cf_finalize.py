"""Finalize Contracts Finder: compile ALL CF OCIDs from the raw event log → rebuild
compiled_process + process_change for source=contracts_finder, then cross-source dedup.

Quota-safe: compiles into NON-partitioned staging tables (no partition-modification quota),
then assembles the live tables with a single CTAS each (clustered, non-partitioned — fine for
a ~640k-row corpus and avoids the daily partition-mod limit that date-partitioning triggers).

  GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src python scripts/cf_finalize.py
"""
from __future__ import annotations

import json

from google.cloud import bigquery

from uk_tenders_ingest import compile as comp
from uk_tenders_ingest import match
from uk_tenders_ingest.adapters.contracts_finder_harvester import ContractsFinderHarvesterAdapter
from uk_tenders_ingest.bq import _jsonify
from uk_tenders_ingest.config import Settings

s = Settings.from_env()
client = bigquery.Client(project=s.project, location=s.bq_location)
ad = ContractsFinderHarvesterAdapter()
SRC = "contracts_finder"
DS = f"{s.project}.{s.public_dataset}"
CP = f"`{DS}.compiled_process`"
PC = f"`{DS}.process_change`"
CPS = f"`{DS}.compiled_process_cf_stage`"
PCS = f"`{DS}.process_change_cf_stage`"
EL = f"`{s.project}.{s.raw_dataset}.release_event_log`"


def q(sql):
    return client.query(sql).result()


print("creating non-partitioned CF staging tables ...", flush=True)
q(f"CREATE OR REPLACE TABLE {CPS} AS SELECT * FROM {CP} WHERE FALSE")
q(f"CREATE OR REPLACE TABLE {PCS} AS SELECT * FROM {PC} WHERE FALSE")
cps_tbl = client.get_table(f"{DS}.compiled_process_cf_stage")
pcs_tbl = client.get_table(f"{DS}.process_change_cf_stage")


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


print("streaming CF releases ordered by ocid + compiling -> staging ...", flush=True)
rows = client.query(f"SELECT ocid, raw_json FROM {EL} WHERE source='{SRC}' ORDER BY ocid").result()
cur = None
rels: list = []
comp_rows: list = []
chg_rows: list = []
n_proc = n_chg = 0


def compile_one(ocid, releases):
    global n_proc, n_chg
    comp_rows.append(comp.project_process(ocid, SRC, releases, ad))
    ch = comp.diff_process(ocid, SRC, releases, ad)
    chg_rows.extend(ch)
    n_proc += 1
    n_chg += len(ch)


for r in rows:
    if cur is not None and r["ocid"] != cur:
        compile_one(cur, rels)
        rels = []
        if len(comp_rows) >= 5000:
            flush(comp_rows, chg_rows)
            print(f"  ... {n_proc} processes compiled", flush=True)
            comp_rows, chg_rows = [], []
    cur = r["ocid"]
    try:
        rels.append(json.loads(r["raw_json"]))
    except Exception:
        pass
if cur is not None and rels:
    compile_one(cur, rels)
flush(comp_rows, chg_rows)
print(f"compiled {n_proc} CF processes, {n_chg} changes -> staging", flush=True)

print("assembling live tables (single CTAS each, clustered, non-partitioned) ...", flush=True)
q(f"""CREATE OR REPLACE TABLE `{DS}.compiled_process_rebuilt`
      CLUSTER BY source, buyer_name, cpv_division AS
      SELECT * FROM {CP} WHERE source != '{SRC}'
      UNION ALL SELECT * FROM {CPS}""")
q(f"DROP TABLE {CP}")
q(f"ALTER TABLE `{DS}.compiled_process_rebuilt` RENAME TO compiled_process")

q(f"""CREATE OR REPLACE TABLE `{DS}.process_change_rebuilt`
      CLUSTER BY source, change_class, ocid AS
      SELECT * FROM {PC} WHERE source != '{SRC}'
      UNION ALL SELECT * FROM {PCS}""")
q(f"DROP TABLE {PC}")
q(f"ALTER TABLE `{DS}.process_change_rebuilt` RENAME TO process_change")

q(f"DROP TABLE {CPS}")
q(f"DROP TABLE {PCS}")

print("=== cross-source dedup ===", flush=True)
print(match.run(s.project, s.raw_dataset, s.public_dataset, s.bq_location), flush=True)
print("CF_BACKFILL_COMPLETE", flush=True)
