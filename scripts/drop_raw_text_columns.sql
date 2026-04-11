-- =========================================================================
-- v0.8.3+ — Raw text column drop (UNAPPLIED — manual trigger only)
-- =========================================================================
--
-- This script is NOT wired into the deploy workflow. It lives in the
-- repo so it's version-controlled and reviewable, but you must run it
-- by hand with psql when you've signed off on the cutover.
--
-- Before running this, verify:
--
--   1. /api/search has been rewired to not use description / summary
--      (currently uses LIKE against idx_sighting_description_trgm and
--      idx_sighting_summary_trgm — both become no-ops after the drop).
--
--   2. /api/sighting/:id has been rewired to show derived fields
--      (standardized_shape, quality_score, richness_score,
--      hoax_likelihood, dominant_emotion, primary_color) instead of
--      the raw description text.
--
--   3. The v0.8.2 derived columns are populated on every geocoded
--      row — the safety probe at the top of the transaction below
--      will bail out if quality_score is empty, because dropping the
--      raw text BEFORE the derived values land would be irreversible
--      data loss.
--
--   4. You have a backup of the current DB. The raw text columns
--      contain ~614k sighting narratives totalling several hundred
--      megabytes and cannot be reconstructed from the public DB
--      after the DROP (your private ufo-dedup SQLite on disk is the
--      only remaining copy).
--
-- Why deferred to v0.8.3+:
--   The v0.8.2 /api/points-bulk endpoint never touched the raw text
--   columns, so v0.8.2 is forward-compatible with the drop. But the
--   /api/search and /api/sighting/:id endpoints still read
--   description/summary today, so dropping now would break them.
--   Those rewires are a separate sprint.
--
-- How to run this manually:
--   export PGPASSWORD=...
--   psql "host=... dbname=ufo_unified user=ufosint_admin sslmode=require" \
--        -v ON_ERROR_STOP=1 \
--        -f scripts/drop_raw_text_columns.sql
-- =========================================================================

\echo '--- v0.8.3+ raw-text-drop script ---'
\echo 'This script is destructive. It drops ~300 MB of raw report text.'
\echo 'Ctrl-C in the next 5 seconds to abort.'
SELECT pg_sleep(5);

BEGIN;

-- Safety probe 1: derived columns must exist.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'sighting'
          AND column_name = 'quality_score'
    ) THEN
        RAISE EXCEPTION 'Derived columns not found. Run add_v082_derived_columns.sql first.';
    END IF;
END $$;

-- Safety probe 2: at least 90% of geocoded rows must have a quality_score.
-- If the ufo-dedup pipeline hasn't been run yet, this fails and the
-- whole transaction rolls back.
DO $$
DECLARE
    total     BIGINT;
    populated BIGINT;
    coverage  NUMERIC;
BEGIN
    SELECT COUNT(*) INTO total
    FROM sighting s
    JOIN location l ON l.id = s.location_id
    WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL;

    SELECT COUNT(*) INTO populated
    FROM sighting s
    JOIN location l ON l.id = s.location_id
    WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
      AND s.quality_score IS NOT NULL;

    coverage := populated::NUMERIC / NULLIF(total, 0);
    RAISE NOTICE 'quality_score coverage: % / % = %', populated, total, coverage;

    IF coverage < 0.90 THEN
        RAISE EXCEPTION
            'quality_score coverage % is below 0.90 threshold. Run the ufo-dedup pipeline and re-migrate before dropping raw text.',
            coverage;
    END IF;
END $$;

-- Drop the raw text columns. In PG 11+ this is metadata-only — the
-- column data is orphaned in the heap pages and reclaimed on the next
-- VACUUM FULL. Plan a VACUUM FULL afterwards to actually free the disk.
--
-- v0.8.3 trim: date_event_raw and time_raw are NOT dropped here
-- (operator's explicit choice). They're short structured strings and
-- the detail modal keeps showing them. Other short free-text fields
-- (explanation, characteristics, weather, terrain, witness_names) are
-- also intentionally preserved for a science-team cleanup pass in
-- v0.8.4+ — see docs/V083_BACKLOG.md.

ALTER TABLE sighting
    DROP COLUMN IF EXISTS description,
    DROP COLUMN IF EXISTS summary,
    DROP COLUMN IF EXISTS notes,
    DROP COLUMN IF EXISTS raw_json;

-- Drop the trigram indexes that relied on the text columns. CASCADE
-- would handle this automatically, but explicit is clearer.
DROP INDEX IF EXISTS idx_sighting_description_trgm;
DROP INDEX IF EXISTS idx_sighting_summary_trgm;

COMMIT;

\echo '--- Raw text columns dropped. Run VACUUM FULL sighting to reclaim disk. ---'
\echo '  (VACUUM FULL takes exclusive lock; schedule during a maintenance window.)'

-- To reclaim the disk space (optional, but the whole point of this
-- migration is making the public DB smaller):
--
--   VACUUM FULL sighting;
--
-- This takes an AccessExclusiveLock on sighting for several minutes.
-- On B1ms with ~614k rows it's typically 30-120 seconds. Run during
-- a low-traffic window.
