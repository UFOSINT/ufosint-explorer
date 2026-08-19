-- =========================================================================
-- Purge the mufon.csv import and all Reddit r/UFOs sightings.
--
-- Scope, deliberately narrow:
--   * source_database 'MUFON'  in collection 'PUBLIUS'  — the mufon.csv import
--   * source_database 'r/UFOs' in collection 'Reddit'   — the r/UFOs ingest
--
-- MUFON-originated records that arrived through UFOCAT are NOT touched.
-- Those carry source_db_id = UFOCAT with origin_id = MUFON; every predicate
-- here keys on source_db_id, never on origin_id.
--
-- Rows are copied into archive_* tables before deletion so a targeted undo
-- does not require a whole-server point-in-time restore. Drop the archives
-- once you're satisfied (see the bottom of this file).
--
-- No FK into sighting uses ON DELETE CASCADE, so children are deleted
-- explicitly and in dependency order. Runs as one transaction: it either
-- lands completely or not at all.
--
-- Usage: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f this_file.sql
-- =========================================================================

\set ON_ERROR_STOP on
\timing on

BEGIN;

-- -------------------------------------------------------------------------
-- 1. Resolve targets by name, then refuse to continue if the shape is wrong.
--    Hardcoded ids (MUFON, r/UFOs=6) are avoided on purpose — REDDIT_INGEST_
--    NOTES.md says resolve by name, and a wrong id here deletes the wrong
--    source entirely.
-- -------------------------------------------------------------------------
CREATE TEMP TABLE purge_src ON COMMIT DROP AS
SELECT sd.id, sd.name, sc.name AS collection
FROM source_database sd
LEFT JOIN source_collection sc ON sc.id = sd.collection_id
WHERE (sd.name = 'MUFON'  AND sc.name = 'PUBLIUS')
   OR (sd.name = 'r/UFOs' AND sc.name = 'Reddit');

DO $$
DECLARE
    n int;
    got text;
BEGIN
    SELECT COUNT(*), string_agg(name || '/' || COALESCE(collection,'?'), ', ' ORDER BY name)
      INTO n, got FROM purge_src;
    IF n <> 2 THEN
        RAISE EXCEPTION
            'Expected exactly 2 target source_database rows (MUFON/PUBLIUS, r/UFOs/Reddit); found %: [%]', n, got;
    END IF;
    RAISE NOTICE 'Targets resolved: %', got;
END $$;

-- Guard: a UFOCAT-borne MUFON record must never be in scope. If UFOCAT ever
-- got folded into the PUBLIUS collection this assertion catches it.
DO $$
DECLARE n int;
BEGIN
    SELECT COUNT(*) INTO n
    FROM source_database sd JOIN purge_src p ON p.id = sd.id
    WHERE sd.name NOT IN ('MUFON','r/UFOs');
    IF n > 0 THEN
        RAISE EXCEPTION 'Target set contains an unexpected source_database row';
    END IF;
END $$;

-- -------------------------------------------------------------------------
-- 2. Archive. CREATE TABLE (not IF NOT EXISTS) doubles as the re-run guard:
--    a second run fails here rather than silently re-deleting.
-- -------------------------------------------------------------------------
CREATE TABLE archive_sighting AS
SELECT s.* FROM sighting s JOIN purge_src p ON p.id = s.source_db_id;

CREATE UNIQUE INDEX archive_sighting_pkey ON archive_sighting(id);
ANALYZE archive_sighting;

CREATE TABLE archive_attachment AS
SELECT a.* FROM attachment a WHERE a.sighting_id IN (SELECT id FROM archive_sighting);

CREATE TABLE archive_sighting_reference AS
SELECT r.* FROM sighting_reference r WHERE r.sighting_id IN (SELECT id FROM archive_sighting);

-- Both sides matter: a pair is dead if EITHER sighting is going away.
CREATE TABLE archive_duplicate_candidate AS
SELECT d.* FROM duplicate_candidate d
WHERE d.sighting_id_a IN (SELECT id FROM archive_sighting)
   OR d.sighting_id_b IN (SELECT id FROM archive_sighting);

CREATE TABLE archive_sentiment_analysis AS
SELECT x.* FROM sentiment_analysis x WHERE x.sighting_id IN (SELECT id FROM archive_sighting);

CREATE TABLE archive_date_correction AS
SELECT x.* FROM date_correction x WHERE x.sighting_id IN (SELECT id FROM archive_sighting);

-- -------------------------------------------------------------------------
-- 3. Delete children first, then the sightings.
-- -------------------------------------------------------------------------
DELETE FROM attachment          WHERE sighting_id   IN (SELECT id FROM archive_sighting);
DELETE FROM sighting_reference  WHERE sighting_id   IN (SELECT id FROM archive_sighting);
DELETE FROM duplicate_candidate WHERE sighting_id_a IN (SELECT id FROM archive_sighting)
                                   OR sighting_id_b IN (SELECT id FROM archive_sighting);
DELETE FROM sentiment_analysis  WHERE sighting_id   IN (SELECT id FROM archive_sighting);
DELETE FROM date_correction     WHERE sighting_id   IN (SELECT id FROM archive_sighting);
DELETE FROM sighting            WHERE id            IN (SELECT id FROM archive_sighting);

-- -------------------------------------------------------------------------
-- 4. Locations orphaned by the delete. Reddit rows were geocoded, so this is
--    not a no-op. Only rows no surviving sighting points at are removed.
-- -------------------------------------------------------------------------
--    Restricted to locations the DELETED sightings pointed at. Locations that
--    were already orphaned before this purge are somebody else's problem and
--    are left alone — widening this would delete rows unrelated to the ask.
CREATE TABLE archive_location AS
SELECT l.* FROM location l
WHERE l.id IN (SELECT DISTINCT location_id FROM archive_sighting WHERE location_id IS NOT NULL)
  AND NOT EXISTS (SELECT 1 FROM sighting s WHERE s.location_id = l.id);

DELETE FROM location WHERE id IN (SELECT id FROM archive_location);

-- Same restriction for references: only those the purged sightings cited.
CREATE TABLE archive_reference AS
SELECT r.* FROM reference r
WHERE r.id IN (SELECT DISTINCT reference_id FROM archive_sighting_reference)
  AND NOT EXISTS (SELECT 1 FROM sighting_reference sr WHERE sr.reference_id = r.id);

DELETE FROM reference WHERE id IN (SELECT id FROM archive_reference);

-- -------------------------------------------------------------------------
-- 5. Retire the source_database rows and the now-empty Reddit collection.
--    PUBLIUS survives — NUFORC still lives there.
-- -------------------------------------------------------------------------
CREATE TABLE archive_source_database AS
SELECT sd.* FROM source_database sd JOIN purge_src p ON p.id = sd.id;

DELETE FROM source_database WHERE id IN (SELECT id FROM purge_src);

CREATE TABLE archive_source_collection AS
SELECT sc.* FROM source_collection sc
WHERE sc.name = 'Reddit'
  AND NOT EXISTS (SELECT 1 FROM source_database sd WHERE sd.collection_id = sc.id);

DELETE FROM source_collection WHERE id IN (SELECT id FROM archive_source_collection);

-- -------------------------------------------------------------------------
-- 6. Re-derive record_count for every surviving source.
-- -------------------------------------------------------------------------
UPDATE source_database sd
SET record_count = c.n
FROM (SELECT source_db_id, COUNT(*)::int AS n FROM sighting GROUP BY source_db_id) c
WHERE c.source_db_id = sd.id;

UPDATE source_database SET record_count = 0
WHERE id NOT IN (SELECT DISTINCT source_db_id FROM sighting);

-- -------------------------------------------------------------------------
-- 7. Post-conditions. Any failure here rolls the whole thing back.
-- -------------------------------------------------------------------------
DO $$
DECLARE
    leftover int;
    total    int;
BEGIN
    SELECT COUNT(*) INTO leftover FROM sighting s
    WHERE s.source_db_id NOT IN (SELECT id FROM source_database);
    IF leftover > 0 THEN
        RAISE EXCEPTION 'Orphaned sighting.source_db_id rows remain: %', leftover;
    END IF;

    SELECT COUNT(*) INTO total FROM sighting;
    RAISE NOTICE 'Surviving sightings: %', total;
    IF total < 400000 OR total > 550000 THEN
        RAISE EXCEPTION 'Surviving sighting count % is outside the expected 400k-550k band - refusing to commit', total;
    END IF;
END $$;

COMMIT;

-- -------------------------------------------------------------------------
-- 8. Rebuild the materialized views the site reads. Outside the transaction:
--    each REFRESH takes an ACCESS EXCLUSIVE lock and they are independent.
--    hex_bin_counts is NOT rebuilt here - it needs the h3 library, so run
--    the "Compute H3 hex bins" workflow afterwards.
-- -------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW mv_stats_summary;
REFRESH MATERIALIZED VIEW mv_stats_by_source;
REFRESH MATERIALIZED VIEW mv_stats_by_collection;
REFRESH MATERIALIZED VIEW mv_timeline_yearly;
REFRESH MATERIALIZED VIEW mv_sentiment_overview;

ANALYZE sighting;
ANALYZE location;

-- -------------------------------------------------------------------------
-- 9. Report.
-- -------------------------------------------------------------------------
\echo '--- Surviving sources ---'
SELECT sd.name AS source, sc.name AS collection, sd.record_count
FROM source_database sd LEFT JOIN source_collection sc ON sc.id = sd.collection_id
ORDER BY sd.record_count DESC NULLS LAST;

\echo '--- Totals ---'
SELECT (SELECT COUNT(*) FROM sighting)                                   AS sightings,
       (SELECT COUNT(*) FROM sighting WHERE lat IS NOT NULL)             AS mapped,
       (SELECT COUNT(*) FROM archive_sighting)                           AS archived,
       (SELECT COUNT(*) FROM location)                                   AS locations;

-- -------------------------------------------------------------------------
-- Once verified, reclaim the space:
--   DROP TABLE archive_sighting, archive_attachment, archive_sighting_reference,
--              archive_duplicate_candidate, archive_sentiment_analysis,
--              archive_date_correction, archive_location, archive_reference,
--              archive_source_database, archive_source_collection;
--   VACUUM (ANALYZE) sighting;
-- -------------------------------------------------------------------------
