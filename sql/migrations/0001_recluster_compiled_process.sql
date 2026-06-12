-- Migration 0001: re-cluster uk_tenders_public.compiled_process
--   from  CLUSTER BY source, buyer_name, cpv_division
--   to    CLUSTER BY source, ocid
--
-- Why: get_tender reads the wide row (incl. compiled_json) for one OCID. With no
-- cluster key on ocid, that point lookup scans the whole compiled_json column and
-- busts the 2 GiB byte cap (QUERY_TOO_LARGE). buyer_name/cpv_division never helped:
-- the API filters them with %LIKE%/UNNEST predicates, which cannot block-prune.
--
-- Runbook (one-off, run as the ingestion identity, NOT the API service account):
--   bq query --use_legacy_sql=false < sql/migrations/0001_recluster_compiled_process.sql
-- Run outside the nightly ingest window (02:30 Europe/London) so the rename swap
-- cannot race a replace_compiled DELETE/INSERT. Total table is a few GiB; the CTAS
-- is a single full-table copy billed once. The nightly job needs no change:
-- replace_compiled writes DELETE + INSERT into the existing table, which preserves
-- whatever clustering spec the table carries.

-- 1. Build the re-clustered copy.
CREATE TABLE `uk_tenders_public.compiled_process_reclustered`
CLUSTER BY source, ocid
AS SELECT * FROM `uk_tenders_public.compiled_process`;

-- 2. Verify before swapping (counts must match):
--   SELECT
--     (SELECT COUNT(*) FROM `uk_tenders_public.compiled_process`) AS old_count,
--     (SELECT COUNT(*) FROM `uk_tenders_public.compiled_process_reclustered`) AS new_count;

-- 3. Swap. Two renames, sub-second apiece; callers in that instant see NOT_FOUND
--    on the table and the API surfaces INTERNAL — acceptable for a one-off.
ALTER TABLE `uk_tenders_public.compiled_process` RENAME TO `compiled_process_preclustered`;
ALTER TABLE `uk_tenders_public.compiled_process_reclustered` RENAME TO `compiled_process`;

-- 4. Keep `compiled_process_preclustered` for a day as the rollback (swap the renames
--    back), then drop it:
--   DROP TABLE `uk_tenders_public.compiled_process_preclustered`;
