-- =========================================================================
-- v0.14 — Audit metadata columns (data-quality provenance)
-- =========================================================================
--
-- The 2026-04-19 v0.14 data-quality pass (docs/V014_HANDOFF.md, imported
-- from ufo-dedup/HANDOFF_v0.14.md) added 9 "audit_*" columns to the
-- sighting table that record per-row provenance of the LLM-assisted
-- location normalization + field-extraction pipeline.
--
-- These columns are populated on ~378k of 618k sighting rows (those
-- that the audit pipeline touched). They are informational only — the
-- public website doesn't surface them in the popup, filter UI, or API
-- responses yet. Keeping them in PG so:
--   (a) The column-probe in migrate_sqlite_to_pg.py doesn't spam
--       "skipping columns not in target schema" warnings every deploy.
--   (b) Future tooling (research dashboards, per-source quality
--       audits) can query them without a schema change.
--   (c) /api/sighting/<id> can expose them to MCP clients if a
--       researcher asks for data provenance.
--
-- Content of each column:
--   audit_status          pending | audited | extracted | skipped | error
--   audit_location_check  match | mismatch | normalized | no_improvement
--   audit_location_fix    JSON blob: corrected city/state/country
--   audit_geocode_check   match | mismatch
--   audit_data_extracted  JSON blob: LLM-extracted structured fields
--                         (duration, num_witnesses, movement, etc.)
--   audit_quality_notes   free-text observation from the LLM
--   audit_batch_id        integer — ties the row to a run log
--   audit_model           model provenance (e.g. "google/gemini-2.5-flash")
--   audit_timestamp       ISO 8601 string when the audit ran
--
-- Fully idempotent. Safe to run on a live DB. Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -f scripts/add_v014_audit_columns.sql
-- =========================================================================

ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS audit_status          TEXT,
    ADD COLUMN IF NOT EXISTS audit_location_check  TEXT,
    ADD COLUMN IF NOT EXISTS audit_location_fix    TEXT,
    ADD COLUMN IF NOT EXISTS audit_geocode_check   TEXT,
    ADD COLUMN IF NOT EXISTS audit_data_extracted  TEXT,
    ADD COLUMN IF NOT EXISTS audit_quality_notes   TEXT,
    ADD COLUMN IF NOT EXISTS audit_batch_id        INTEGER,
    ADD COLUMN IF NOT EXISTS audit_model           TEXT,
    ADD COLUMN IF NOT EXISTS audit_timestamp       TEXT;

-- Partial index on audit_status — lets researchers query the scope of
-- the audit pass efficiently ("give me all rows that were successfully
-- LLM-extracted"). Partial because ~40% of rows have NULL status
-- (pipeline didn't touch them).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_audit_status
    ON sighting(audit_status)
    WHERE audit_status IS NOT NULL;

-- =========================================================================
-- Verification
-- =========================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'sighting'
--   AND column_name LIKE 'audit_%'
-- ORDER BY column_name;
--
-- Expected 9 rows: audit_batch_id, audit_data_extracted, audit_geocode_check,
-- audit_location_check, audit_location_fix, audit_model,
-- audit_quality_notes, audit_status, audit_timestamp.
