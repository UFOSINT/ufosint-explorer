-- v0.9.1 — one-shot cleanup of the 692 bogus "0019-..." date_event
-- records.
--
-- Root cause: UFOCAT stores dates in separate YEAR / MO / DAY columns.
-- The YEAR field uses variable-length encoding: 2-digit "19" is a
-- sentinel meaning "sometime in the 1900s, year unknown" — NOT year
-- 19 AD. The pre-v0.8.3b ETL zero-padded all years to 4 digits, which
-- correctly handled 3-digit ancient years ("034" → "0034") but
-- mis-interpreted "19" as "0019", producing 692 sighting records
-- nominally dated to year 19 AD.
--
-- The v0.8.3b date-fix pipeline (rebuild_db.py / fix_ufocat_century_only)
-- was SUPPOSED to set these records' date_event to NULL while logging
-- the correction to the date_correction audit table. On the currently-
-- deployed Azure Postgres the fix is missing — /api/stats.date_range.min
-- still returns "0019-01-01" and /api/timeline still contains a
-- {"0019": {"UFOCAT": 692}} bucket.
--
-- This script runs the cleanup explicitly, idempotently, and logs the
-- corrections to the date_correction audit table so the fix is
-- reproducible. Safe to run multiple times: the WHERE clause on the
-- UPDATE targets only records still matching the broken pattern.
--
-- v0.9.1 also adds query-time "date_event NOT LIKE '0019-%'" guards
-- in app.py (stats, timeline, sentiment/timeline) as a belt-and-
-- suspenders defense so the app is self-healing even if the DB
-- cleanup hasn't run yet. Prefer running THIS script once so the
-- data is actually correct rather than relying on the application-
-- layer filter.
--
-- Usage:
--   psql $DATABASE_URL -f scripts/fix_year_0019.sql

BEGIN;

-- 1. Log every record we're about to NULL to the audit table. The
--    date_correction table schema (source_sighting_id, original_date,
--    corrected_date, correction_type, reason) is preserved from v0.8.3b.
INSERT INTO date_correction (
    sighting_id,
    original_date,
    corrected_date,
    correction_type,
    reason,
    created_at
)
SELECT
    id,
    date_event,
    NULL,
    'fix_year_0019_v091',
    'v0.9.1: UFOCAT 2-digit year "19" sentinel (century-only, '
    'year unknown) was zero-padded to "0019" by pre-v0.8.3b ETL. '
    'Should have been NULLed by the v0.8.3b date-fix pipeline but '
    'that fix did not land on Azure PG. Logging + nulling now.',
    NOW()
FROM sighting
WHERE date_event LIKE '0019-%'
  AND NOT EXISTS (
    -- Don't double-log if a prior run of this script already did it.
    SELECT 1 FROM date_correction dc
    WHERE dc.sighting_id = sighting.id
      AND dc.correction_type = 'fix_year_0019_v091'
  );

-- 2. NULL the date_event on the matching rows.
UPDATE sighting
   SET date_event = NULL
 WHERE date_event LIKE '0019-%';

-- 3. Refresh the materialized views that cache date aggregates.
--    Non-concurrent refresh — brief read lock, but the MV is small.
REFRESH MATERIALIZED VIEW mv_stats_summary;
REFRESH MATERIALIZED VIEW mv_timeline_yearly;

-- 4. Sanity check: no rows should remain with "0019-" date_event.
--    Wrap in a DO block so the transaction aborts if the assertion
--    fails (defensive against future regressions).
DO $$
DECLARE
    remaining INTEGER;
    min_date TEXT;
BEGIN
    SELECT COUNT(*) INTO remaining
      FROM sighting
     WHERE date_event LIKE '0019-%';
    IF remaining > 0 THEN
        RAISE EXCEPTION 'fix_year_0019 failed: % rows still match 0019-%%',
            remaining;
    END IF;

    -- Also verify the new MIN(date_event). Should be >= "1000-"
    -- (the legitimate UFOCAT pre-modern sightings) or, if those
    -- were also somehow dropped, at least not "0019-..."
    SELECT MIN(date_event) INTO min_date
      FROM sighting
     WHERE date_event IS NOT NULL;
    IF min_date LIKE '0019-%' THEN
        RAISE EXCEPTION 'fix_year_0019 failed: MIN(date_event) still begins with 0019-';
    END IF;
    RAISE NOTICE 'fix_year_0019 OK: MIN(date_event) = %', min_date;
END $$;

COMMIT;
