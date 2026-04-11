-- =========================================================================
-- v0.8.3 — Movement classification + enhanced quality scoring
-- =========================================================================
--
-- Follow-on to add_v082_derived_columns.sql. Adds two new fields the
-- ufo-dedup analyze.py pipeline produces as of v0.8.3:
--
--   has_movement_mentioned SMALLINT
--       0 or 1 per row. 1 if the narrative contains at least one
--       movement-category signal (see MOVEMENT_CATEGORY_PATTERNS in
--       ufo-dedup/analyze.py). NULL on rows that had no narrative text
--       at all (no description/summary).
--
--   movement_categories    TEXT  (JSON array)
--       Deduped JSON array of every movement category that fired for
--       the row. Categories are one of: hovering, linear, erratic,
--       accelerating, rotating, ascending, descending, vanished,
--       followed, landed.
--
-- These land on `sighting` (not `sighting_analysis`) so they flow
-- through the existing /api/points-bulk wire format and can be filtered
-- at the SQL level without a JOIN.
--
-- Quality-score weighting also changed in v0.8.3 — the new formula
-- heavily weights has_description + has_media + num_witnesses +
-- has_movement_mentioned, and caps rows with NULL date_event at 15.
-- No schema change is required for that; the integer column is the
-- same, only the values shift. The dev team should:
--
--   1. Apply THIS migration (adds the 2 new columns).
--   2. Re-run ufo-dedup/rebuild_db.py (or just analyze.py --reset) on
--      the private SQLite to re-populate quality_score + the 2 new fields.
--   3. Re-migrate to PG via migrate_sqlite_to_pg.py (which also now
--      includes these 2 columns in its TABLES list).
--
-- Fully idempotent:
--   * ADD COLUMN IF NOT EXISTS — safe on schemas that already have
--     the columns from a prior run.
--   * CREATE INDEX CONCURRENTLY IF NOT EXISTS — won't block writes.
-- =========================================================================

ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS has_movement_mentioned SMALLINT,
    ADD COLUMN IF NOT EXISTS movement_categories    TEXT;

-- Fast-filter index on the boolean. movement_categories is a JSON string
-- and doesn't get a b-tree index — the public app filters on the boolean
-- and reads the JSON for display only.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_has_movement
    ON sighting(has_movement_mentioned);

-- =========================================================================
-- Verification query (harmless SELECT — confirms the columns exist)
-- =========================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'sighting'
--   AND column_name IN ('has_movement_mentioned', 'movement_categories')
-- ORDER BY column_name;
