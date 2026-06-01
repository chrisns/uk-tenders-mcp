# Index on BigQuery, with raw OCDS archived in GCS and a derived star schema

**Status:** accepted

We index UK procurement data in **BigQuery**, fed from an immutable **Cloud Storage** archive of the raw OCDS releases. Each release is loaded losslessly (full JSON retained) into an append-only **release event-log** table; from that we derive a **compiled current-state** layer (one row per OCID), an analytical **star schema** (process, party, award, contract, item, document), and a **field-level change** table.

## Considered options

- **BigQuery + GCS (chosen)** — serverless, scale-to-zero, near-zero idle cost; columnar engine ideal for the analytics-first workload; native `ARRAY<STRUCT>`/`JSON` handles nested OCDS; per-query `maximum_bytes_billed` and a read-only IAM role let us safely expose a public SQL tool. Lossless raw retention future-proofs the PA2023 dual regime and OCDS extensions.
- **Cloud SQL Postgres** — rejected: always-on instance cost, and columnar aggregation over the full corpus is weaker than BigQuery's.
- **DuckDB over Parquet in GCS** — rejected: cold-start data loading on a scale-to-zero Cloud Run service, each instance holding its own copy, and nightly refresh/swap choreography outweigh the cost saving at this corpus size.

## Boundaries this ADR commits us to

- **Two datasets, not one.** A **write/raw** dataset (`uk_tenders_raw`) holds the full PII-bearing releases and is writable only by the ingestion identity; a **public/read** dataset (`uk_tenders_public`) holds redacted, query-serving tables/views and is the *only* thing the API's read-only service account can see. ADR-0003's IAM safety guarantee and the PII redaction both depend on this split.
- **Atomic publication.** Derived layers are built into staging partitions/tables and **atomically swapped** (or exposed via an `as_of` snapshot id the read views point at), so readers never observe a half-recomputed state. A failed run leaves the last good snapshot serving.
- **Deterministic compile.** Record compilation uses Python `ocds-merge` (see [ADR-0005](0005-python-ingestion-typescript-api.md)); because its canonical tie-break is input-order, we impose a deterministic secondary sort (release id) before folding so re-crawls reproduce identical compiled output.

## Consequences

Within-source compilation and change-diff run in the ingestion job and write derived tables; all derived tables are fully rebuildable from the raw event log, so they can be recomputed safely. The corpus is an estimated ~1–2M releases (no source publishes a headline total — medium confidence), so BigQuery cost is dominated by query scans, not storage — hence the per-query byte cap on the public SQL tool.
