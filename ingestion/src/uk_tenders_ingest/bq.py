"""BigQuery loader.

Idempotent loads (PRD §7.4): the event log MERGEs on content_hash (re-crawls are
no-ops); derived tables are rebuilt per affected OCID via delete-by-ocid + insert from
a staging table. Datasets enforce a least-privilege read boundary (ADR-0001/0003): raw
(write) vs public (read-only, query-serving).
"""

from __future__ import annotations

import json
from typing import Any

from google.cloud import bigquery


def _jsonify(rows: list[dict[str, Any]], json_cols: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        rr = dict(r)
        for c in json_cols:
            if c in rr and rr[c] is not None and not isinstance(rr[c], str):
                rr[c] = json.dumps(rr[c], ensure_ascii=False)
        out.append(rr)
    return out


class BigQueryLoader:
    def __init__(self, project: str, raw_dataset: str, public_dataset: str, location: str):
        self.project = project
        self.raw = raw_dataset
        self.public = public_dataset
        self.location = location
        self.client = bigquery.Client(project=project, location=location)

    # ----------------------------------------------------------------- bootstrap
    def ensure_datasets(self) -> None:
        for ds in (self.raw, self.public):
            ref = bigquery.Dataset(f"{self.project}.{ds}")
            ref.location = self.location
            self.client.create_dataset(ref, exists_ok=True)

    def run_ddl(self, ddl: str) -> None:
        # BigQuery scripting runs the whole multi-statement DDL (with comments) in one job.
        self.client.query(ddl).result()

    # ------------------------------------------------------------------- helpers
    def _t(self, dataset: str, table: str) -> str:
        return f"`{self.project}.{dataset}.{table}`"

    def _load_stage(self, dataset: str, target: str, rows: list[dict], json_cols=()) -> str:
        """Create a staging table LIKE target and load rows into it. Returns stage fqn."""
        stage = f"{target}_stage"
        self.client.query(
            f"CREATE OR REPLACE TABLE {self._t(dataset, stage)} "
            f"AS SELECT * FROM {self._t(dataset, target)} WHERE FALSE"
        ).result()
        table = self.client.get_table(f"{self.project}.{dataset}.{stage}")
        job = self.client.load_table_from_json(
            _jsonify(rows, json_cols),
            table,
            job_config=bigquery.LoadJobConfig(
                schema=table.schema,
                write_disposition="WRITE_APPEND",
                # Tolerate the rare malformed source record (e.g. a stray array in a scalar
                # field) rather than failing the whole window. A systematic break still fails
                # (>50 bad rows) so it gets surfaced rather than silently dropped.
                max_bad_records=50,
            ),
        )
        job.result()
        return f"{self.project}.{dataset}.{stage}"

    # ------------------------------------------------------------- event log
    def merge_event_log(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        self._load_stage(self.raw, "release_event_log", rows, json_cols=("raw_json",))
        q = (
            f"MERGE {self._t(self.raw, 'release_event_log')} T "
            f"USING {self._t(self.raw, 'release_event_log_stage')} S "
            f"ON T.content_hash = S.content_hash "
            f"WHEN NOT MATCHED THEN INSERT ROW"
        )
        result = self.client.query(q).result()
        return result.num_dml_affected_rows or 0

    def append_event_log(self, rows: list[dict]) -> int:
        """Concurrency-safe append to the event log (no staging/MERGE), for PARALLEL bulk
        loads. WRITE_APPEND load jobs can run concurrently; dedup is deferred to the compile
        step (per-OCID), so parallel shards never collide. Tolerates the rare bad row."""
        if not rows:
            return 0
        table = self.client.get_table(f"{self.project}.{self.raw}.release_event_log")
        job = self.client.load_table_from_json(
            _jsonify(rows, ("raw_json",)),
            table,
            job_config=bigquery.LoadJobConfig(
                schema=table.schema, write_disposition="WRITE_APPEND", max_bad_records=50
            ),
        )
        job.result()
        return job.output_rows or len(rows)

    def affected_ocids(self, content_hashes: list[str]) -> list[str]:
        """OCIDs touched by the staged batch (read back from the event log)."""
        q = (
            f"SELECT DISTINCT ocid FROM {self._t(self.raw, 'release_event_log')} "
            f"WHERE content_hash IN UNNEST(@h)"
        )
        job = self.client.query(
            q,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("h", "STRING", content_hashes)]
            ),
        )
        return [r["ocid"] for r in job.result()]

    def releases_for_ocids(self, ocids: list[str]) -> dict[str, list[dict]]:
        """All raw releases for the given OCIDs (full history), for compilation."""
        q = (
            f"SELECT ocid, raw_json FROM {self._t(self.raw, 'release_event_log')} "
            f"WHERE ocid IN UNNEST(@o)"
        )
        job = self.client.query(
            q,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("o", "STRING", ocids)]
            ),
        )
        out: dict[str, list[dict]] = {}
        for row in job.result():
            out.setdefault(row["ocid"], []).append(json.loads(row["raw_json"]))
        return out

    # ----------------------------------------------------- derived (public)
    def _replace_by_ocid(self, dataset: str, table: str, rows: list[dict], json_cols=()) -> None:
        if not rows:
            return
        stage = self._load_stage(dataset, table, rows, json_cols)
        ocids = sorted({r["ocid"] for r in rows})
        self.client.query(
            f"DELETE FROM {self._t(dataset, table)} WHERE ocid IN UNNEST(@o)",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("o", "STRING", ocids)]
            ),
        ).result()
        self.client.query(
            f"INSERT INTO {self._t(dataset, table)} "
            f"SELECT * FROM `{stage}`"
        ).result()

    def replace_compiled(self, rows: list[dict]) -> None:
        self._replace_by_ocid(self.public, "compiled_process", rows, json_cols=("compiled_json",))

    def replace_changes(self, ocids: list[str], rows: list[dict]) -> None:
        if not ocids:
            return
        self.client.query(
            f"DELETE FROM {self._t(self.public, 'process_change')} WHERE ocid IN UNNEST(@o)",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("o", "STRING", ocids)]
            ),
        ).result()
        if rows:
            stage = self._load_stage(self.public, "process_change", rows)
            self.client.query(
                f"INSERT INTO {self._t(self.public, 'process_change')} SELECT * FROM `{stage}`"
            ).result()

    # -------------------------------------------------------------- ledger
    def insert_ingest_run(self, row: dict) -> None:
        self.client.query(
            f"INSERT INTO {self._t(self.raw, 'ingest_run')} "
            f"(run_id, source, started_at, status, mode, window_from, window_to) "
            f"VALUES(@run_id,@source,CURRENT_TIMESTAMP(),@status,@mode,@wf,@wt)",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", row["run_id"]),
                bigquery.ScalarQueryParameter("source", "STRING", row["source"]),
                bigquery.ScalarQueryParameter("status", "STRING", row["status"]),
                bigquery.ScalarQueryParameter("mode", "STRING", row.get("mode")),
                bigquery.ScalarQueryParameter("wf", "TIMESTAMP", row.get("window_from")),
                bigquery.ScalarQueryParameter("wt", "TIMESTAMP", row.get("window_to")),
            ]),
        ).result()

    def finish_ingest_run(self, run_id: str, status: str, seen: int, loaded: int, err: str | None) -> None:
        self.client.query(
            f"UPDATE {self._t(self.raw, 'ingest_run')} "
            f"SET status=@status, finished_at=CURRENT_TIMESTAMP(), "
            f"releases_seen=@seen, releases_loaded=@loaded, error_summary=@err "
            f"WHERE run_id=@run_id",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("status", "STRING", status),
                bigquery.ScalarQueryParameter("seen", "INT64", seen),
                bigquery.ScalarQueryParameter("loaded", "INT64", loaded),
                bigquery.ScalarQueryParameter("err", "STRING", err),
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            ]),
        ).result()

    def upsert_source_status(self, source: str) -> None:
        """Recompute the public source_status row from the ledger + data."""
        self.client.query(
            f"""
            MERGE {self._t(self.public, 'source_status')} T
            USING (
              SELECT
                @source AS source,
                (SELECT MAX(finished_at) FROM {self._t(self.raw, 'ingest_run')}
                   WHERE source=@source AND status='success') AS last_success_at,
                (SELECT MAX(started_at) FROM {self._t(self.raw, 'ingest_run')}
                   WHERE source=@source) AS last_run_at,
                (SELECT status FROM {self._t(self.raw, 'ingest_run')}
                   WHERE source=@source ORDER BY started_at DESC, run_id DESC LIMIT 1) AS last_run_status,
                (SELECT MIN(published_date) FROM {self._t(self.public, 'compiled_process')}
                   WHERE source=@source) AS coverage_from,
                (SELECT MAX(last_updated) FROM {self._t(self.public, 'compiled_process')}
                   WHERE source=@source) AS coverage_to,
                (SELECT COUNT(*) FROM {self._t(self.public, 'compiled_process')}
                   WHERE source=@source) AS releases_total
            ) S
            ON T.source = S.source
            WHEN MATCHED THEN UPDATE SET
              last_success_at=S.last_success_at, last_run_at=S.last_run_at,
              last_run_status=S.last_run_status, coverage_from=S.coverage_from,
              coverage_to=S.coverage_to, releases_total=S.releases_total,
              health = CASE
                WHEN S.last_success_at IS NULL OR
                     TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), S.last_success_at, HOUR) > 48 THEN 'red'
                WHEN S.last_run_status != 'success' THEN 'amber'
                ELSE 'green' END
            WHEN NOT MATCHED THEN INSERT
              (source,last_success_at,last_run_at,last_run_status,coverage_from,coverage_to,health,releases_total)
            VALUES(S.source,S.last_success_at,S.last_run_at,S.last_run_status,S.coverage_from,S.coverage_to,
              CASE
                WHEN S.last_success_at IS NULL OR
                     TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), S.last_success_at, HOUR) > 48 THEN 'red'
                WHEN S.last_run_status != 'success' THEN 'amber'
                ELSE 'green' END, S.releases_total)
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("source", "STRING", source)
            ]),
        ).result()
