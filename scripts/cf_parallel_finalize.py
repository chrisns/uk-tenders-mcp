"""Parallel CF finalize: compile all CF OCIDs across 16 OCID-prefix shards in parallel, each
writing to its OWN staging table (so concurrent loads never hit the per-table write-rate
limit), then one CTAS UNIONs everything into compiled_process + process_change and dedup runs.

  GCP_PROJECT=govreposcrape BQ_LOCATION=EU PYTHONPATH=ingestion/src python scripts/cf_parallel_finalize.py
"""
from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor, as_completed

from google.cloud import bigquery

from uk_tenders_ingest import compile as comp
from uk_tenders_ingest import match
from uk_tenders_ingest.adapters.contracts_finder_harvester import ContractsFinderHarvesterAdapter
from uk_tenders_ingest.bq import _jsonify
from uk_tenders_ingest.config import Settings

S = Settings.from_env()
DS = f"{S.project}.{S.public_dataset}"
CP = f"`{DS}.compiled_process`"
PC = f"`{DS}.process_change`"
EL = f"`{S.project}.{S.raw_dataset}.release_event_log`"
SRC = "contracts_finder"
PREFIXES = list("0123456789abcdef")
BATCH = 10000


def compile_shard(prefix: str):
    client = bigquery.Client(project=S.project, location=S.bq_location)
    ad = ContractsFinderHarvesterAdapter()
    cp_name = f"compiled_process_cf_stage_{prefix}"
    pc_name = f"process_change_cf_stage_{prefix}"
    client.query(f"CREATE OR REPLACE TABLE `{DS}.{cp_name}` AS SELECT * FROM {CP} WHERE FALSE").result()
    client.query(f"CREATE OR REPLACE TABLE `{DS}.{pc_name}` AS SELECT * FROM {PC} WHERE FALSE").result()
    cps = client.get_table(f"{DS}.{cp_name}")
    pcs = client.get_table(f"{DS}.{pc_name}")

    def flush(c, h):
        if c:
            client.load_table_from_json(_jsonify(c, ("compiled_json",)), cps,
                job_config=bigquery.LoadJobConfig(schema=cps.schema, write_disposition="WRITE_APPEND", max_bad_records=50)).result()
        if h:
            client.load_table_from_json(h, pcs,
                job_config=bigquery.LoadJobConfig(schema=pcs.schema, write_disposition="WRITE_APPEND", max_bad_records=50)).result()

    rows = client.query(
        f"SELECT ocid, raw_json FROM {EL} WHERE source='{SRC}' AND ocid LIKE 'ocds-b5fd17-{prefix}%' ORDER BY ocid"
    ).result()
    cur = None
    rels: list = []
    cb: list = []
    hb: list = []
    n = 0
    for r in rows:
        if cur is not None and r["ocid"] != cur:
            cb.append(comp.project_process(cur, SRC, rels, ad))
            hb.extend(comp.diff_process(cur, SRC, rels, ad))
            n += 1
            rels = []
            if len(cb) >= BATCH:
                flush(cb, hb)
                cb, hb = [], []
        cur = r["ocid"]
        try:
            rels.append(json.loads(r["raw_json"]))
        except Exception:
            pass
    if cur is not None and rels:
        cb.append(comp.project_process(cur, SRC, rels, ad))
        hb.extend(comp.diff_process(cur, SRC, rels, ad))
        n += 1
    flush(cb, hb)
    return prefix, n


def main():
    client = bigquery.Client(project=S.project, location=S.bq_location)
    print("parallel compile across 16 OCID-prefix shards (per-shard staging tables) ...", flush=True)
    total = 0
    with ProcessPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(compile_shard, p): p for p in PREFIXES}
        for f in as_completed(futs):
            p, n = f.result()
            total += n
            print(f"shard {p} done: {n} processes (cumulative {total})", flush=True)
    print(f"compiled {total} CF processes across shards", flush=True)

    cp_union = " UNION ALL ".join(f"SELECT * FROM `{DS}.compiled_process_cf_stage_{p}`" for p in PREFIXES)
    pc_union = " UNION ALL ".join(f"SELECT * FROM `{DS}.process_change_cf_stage_{p}`" for p in PREFIXES)

    print("assembling compiled_process ...", flush=True)
    client.query(f"CREATE OR REPLACE TABLE `{DS}.compiled_process_rebuilt` CLUSTER BY source, buyer_name, cpv_division AS SELECT * FROM {CP} WHERE source!='{SRC}' UNION ALL {cp_union}").result()
    client.query(f"DROP TABLE {CP}").result()
    client.query(f"ALTER TABLE `{DS}.compiled_process_rebuilt` RENAME TO compiled_process").result()

    print("assembling process_change ...", flush=True)
    client.query(f"CREATE OR REPLACE TABLE `{DS}.process_change_rebuilt` CLUSTER BY source, change_class, ocid AS SELECT * FROM {PC} WHERE source!='{SRC}' UNION ALL {pc_union}").result()
    client.query(f"DROP TABLE {PC}").result()
    client.query(f"ALTER TABLE `{DS}.process_change_rebuilt` RENAME TO process_change").result()

    print("dropping staging tables ...", flush=True)
    for p in PREFIXES:
        client.query(f"DROP TABLE IF EXISTS `{DS}.compiled_process_cf_stage_{p}`").result()
        client.query(f"DROP TABLE IF EXISTS `{DS}.process_change_cf_stage_{p}`").result()

    print("=== cross-source dedup ===", flush=True)
    print(match.run(S.project, S.raw_dataset, S.public_dataset, S.bq_location), flush=True)
    print("CF_BACKFILL_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
