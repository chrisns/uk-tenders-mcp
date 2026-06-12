-- UK Tenders MCP — BigQuery schema (ADR-0001, ADR-0003)
-- Two datasets enforce a least-privilege read boundary:
--   uk_tenders_raw    : WRITE dataset, full OCDS event log, ingestion identity only
--   uk_tenders_public : READ dataset, verbatim query-serving copy, the only thing the API read-only SA sees
-- Run with: bq query --use_legacy_sql=false < sql/schema.sql   (project from `bq` default)
-- Dataset creation is handled by Terraform (terraform/) or scripts/bootstrap_bq.sh;
-- this file defines the tables/views. Location: EU (London-friendly multi-region).

-- ============================================================================
-- RAW (write) — uk_tenders_raw
-- ============================================================================

-- Append-only release event log: the source of truth. Full release JSON retained
-- losslessly, plus hot scalars surfaced for cheap filtering. Idempotent on content_hash.
CREATE TABLE IF NOT EXISTS `uk_tenders_raw.release_event_log`
(
  content_hash   STRING   NOT NULL,   -- SHA-256 of canonicalised release JSON (idempotency key)
  source         STRING   NOT NULL,   -- fts | contracts_finder | pcs | sell2wales | etendersni
  ocid           STRING   NOT NULL,   -- publisher-scoped
  release_id     STRING,
  release_date   TIMESTAMP,
  tags           ARRAY<STRING>,       -- OCDS release.tag (tender, award, contract, ...)
  updated        TIMESTAMP,           -- source "last updated" (incremental axis)
  -- hot scalars (denormalised for cheap predicates / anomaly checks):
  title          STRING,
  buyer_name     STRING,
  value_amount   NUMERIC,
  value_currency STRING,
  -- provenance:
  load_run_id    STRING   NOT NULL,
  load_date      DATE     NOT NULL,
  raw_json       STRING   NOT NULL     -- complete release as JSON text (PARSE_JSON on read); may contain personal data
)
PARTITION BY load_date
CLUSTER BY source, ocid, release_id;

-- One row per ingestion run, per source: the health ledger (PRD 8.6).
CREATE TABLE IF NOT EXISTS `uk_tenders_raw.ingest_run`
(
  run_id          STRING    NOT NULL,
  source          STRING    NOT NULL,
  started_at      TIMESTAMP NOT NULL,
  finished_at     TIMESTAMP,
  status          STRING    NOT NULL,   -- running | success | failed
  mode            STRING,               -- backfill | nightly
  window_from     TIMESTAMP,
  window_to       TIMESTAMP,
  releases_seen   INT64,
  releases_loaded INT64,
  error_summary   STRING
)
PARTITION BY DATE(started_at)
CLUSTER BY source, status;

-- ============================================================================
-- PUBLIC (read, query-serving) — uk_tenders_public
-- A verbatim projection of the compiled releases (no redaction; the index is a
-- faithful mirror of public-domain source data). The API's read-only service
-- account is granted dataViewer on THIS dataset only — a least-privilege read
-- boundary, so it can never reach the full raw event log or GCS archive.
-- ============================================================================

-- One row per contracting process: compiled current state (verbatim).
CREATE TABLE IF NOT EXISTS `uk_tenders_public.compiled_process`
(
  ocid              STRING NOT NULL,
  source            STRING NOT NULL,
  process_group_id  STRING,            -- cross-source synthetic id (ADR-0002); NULL until matched
  title             STRING,
  description       STRING,
  buyer_name        STRING,
  buyer_id          STRING,
  status            STRING,            -- tender.status
  stage             STRING,            -- planning | tender | award
  regime            STRING,            -- pca2023 | legacy | scotland | mixed
  main_category     STRING,            -- goods | works | services
  value_amount      NUMERIC,
  value_currency    STRING,
  awarded_amount    NUMERIC,           -- summed active award value (same currency only)
  awarded_currency  STRING,
  cpv_codes         ARRAY<STRING>,
  cpv_division      STRING,            -- 2-digit roll-up
  region            STRING,            -- NUTS/ITL as published
  published_date    TIMESTAMP,
  tender_end_date   TIMESTAMP,
  last_updated      TIMESTAMP,
  official_url      STRING NOT NULL,   -- canonical source notice URL (always present)
  awards            ARRAY<STRUCT<
                      award_id STRING, status STRING,
                      amount NUMERIC, currency STRING,
                      supplier_name STRING, supplier_id STRING,
                      date TIMESTAMP >>,
  parties           ARRAY<STRUCT<
                      id STRING, name STRING,
                      roles ARRAY<STRING> >>,   -- id/name/roles summary; full party (incl. contactPoint) is in compiled_json
  compiled_json     STRING             -- verbatim compiled release as JSON text (PARSE_JSON on read)
)
-- Non-partitioned, clustered only. The full-history backfill compiles via per-shard staging
-- tables assembled with a single CTAS; date-partitioning here tripped BigQuery's
-- partition-modification quota during bulk assembly (and the corpus is small enough that
-- clustering alone prunes well). See scripts/cf_parallel_finalize.py.
-- Cluster keys serve the two scan-heavy paths: source-filtered search/analytics (source)
-- and point lookups by id (ocid) — the wide compiled_json read in get_tender must prune
-- to blocks or it alone busts the byte cap. buyer/cpv filters arrive as %LIKE%/UNNEST
-- predicates, which never block-prune, so they earn no cluster key. Existing deployments:
-- run sql/migrations/0001_recluster_compiled_process.sql.
CLUSTER BY source, ocid;

-- Field-level change feed (PRD 9): "what changed".
CREATE TABLE IF NOT EXISTS `uk_tenders_public.process_change`
(
  ocid           STRING NOT NULL,
  source         STRING NOT NULL,
  change_class   STRING,              -- deadline_extended | value_changed | cancelled | new_award | other (provisional)
  field_path     STRING,
  old_value      STRING,
  new_value      STRING,
  release_id     STRING,
  changed_at     TIMESTAMP NOT NULL,
  official_url   STRING
)
-- Non-partitioned, clustered only (assembled by CTAS alongside compiled_process; see above).
CLUSTER BY source, change_class, ocid;

-- Cross-source identity (ADR-0002): which OCIDs belong to one real procurement.
CREATE TABLE IF NOT EXISTS `uk_tenders_public.process_group`
(
  process_group_id STRING NOT NULL,
  ocid             STRING NOT NULL,
  source           STRING NOT NULL,
  confidence       FLOAT64,
  method           STRING               -- related_process | auto_forward | fuzzy
)
CLUSTER BY process_group_id;

-- Public mirror of run health so get_status can read without touching the raw dataset.
CREATE TABLE IF NOT EXISTS `uk_tenders_public.source_status`
(
  source               STRING NOT NULL,
  last_success_at      TIMESTAMP,
  last_run_at          TIMESTAMP,
  last_run_status      STRING,
  coverage_from        TIMESTAMP,
  coverage_to          TIMESTAMP,
  health               STRING,          -- green | amber | red
  releases_total       INT64
)
CLUSTER BY source;
