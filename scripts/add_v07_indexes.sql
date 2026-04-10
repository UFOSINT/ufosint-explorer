-- v0.7 index migration — idempotent, safe to run on every deploy
--
-- Fixes the HTTP 504 GatewayTimeout on /api/sighting/<id> by adding
-- btree indexes on duplicate_candidate(sighting_id_a) and
-- duplicate_candidate(sighting_id_b). Before v0.7 the only index on
-- duplicate_candidate was idx_duplicate_status, so the duplicate lookup
-- (WHERE sighting_id_a = %s OR sighting_id_b = %s) fell back to a
-- sequential scan of all 126,730 rows and timed out on cold buffer.
--
-- The query has also been rewritten in app.py as a UNION ALL of two
-- equality scans so the planner can use each of these indexes
-- independently.
--
-- All statements are IF NOT EXISTS — re-running this file is a no-op.
-- Fresh installs pick these up from scripts/pg_schema.sql as well.

CREATE INDEX IF NOT EXISTS idx_duplicate_a
    ON duplicate_candidate(sighting_id_a);

CREATE INDEX IF NOT EXISTS idx_duplicate_b
    ON duplicate_candidate(sighting_id_b);
