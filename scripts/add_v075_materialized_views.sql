-- =========================================================================
-- scripts/add_v075_materialized_views.sql
--
-- v0.7.5 — Materialized views for the three slowest landing-page endpoints:
--
--   /api/stats              -> mv_stats_summary + mv_stats_by_source + mv_stats_by_collection
--   /api/timeline (yearly)  -> mv_timeline_yearly
--   /api/sentiment/overview -> mv_sentiment_overview
--
-- All three of those queries aggregate across the entire 614k-row sighting
-- table. Even on the post-v0.7.4 tuned B1ms they take ~1-2 seconds warm.
-- The landing page fires five of them in parallel, so first-visit latency
-- is dominated by the slowest one (~2s) plus pool contention. Pre-computing
-- the no-filter case collapses each of those to a ~5 ms index scan of a
-- tiny MV.
--
-- IMPORTANT CONTRACT
--   * These MVs ONLY answer the no-filter case. When a user applies a
--     shape / source / country / date-range filter, the Python endpoint
--     MUST fall back to the original live query. That fallback path is
--     unchanged from v0.7.4 and uses the existing Flask-Caching layer.
--   * REFRESH MATERIALIZED VIEW (non-CONCURRENT) is run by the deploy
--     workflow in .github/workflows/azure-deploy.yml after the index
--     migration step. It takes an ACCESS EXCLUSIVE lock for ~5-15 s
--     during refresh, which is acceptable at deploy time.
--
-- This file is IDEMPOTENT: re-running it is a no-op on existing views.
-- The GitHub Actions deploy step runs it on every push, same pattern as
-- scripts/add_v07_indexes.sql.
-- =========================================================================


-- -------------------------------------------------------------------------
-- /api/stats — summary counts (single row)
-- -------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_stats_summary AS
SELECT
    (SELECT COUNT(*)::bigint FROM sighting)                             AS total_sightings,
    (SELECT MIN(date_event)  FROM sighting WHERE date_event IS NOT NULL) AS date_min,
    (SELECT MAX(date_event)  FROM sighting WHERE date_event IS NOT NULL) AS date_max,
    (SELECT COUNT(*)::bigint FROM location
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL)            AS geocoded_locations,
    (SELECT COUNT(*)::bigint FROM location
        WHERE latitude IS NOT NULL AND geocode_src IS NULL)              AS geocoded_original,
    (SELECT COUNT(*)::bigint FROM location
        WHERE geocode_src IS NOT NULL)                                   AS geocoded_geonames,
    (SELECT COUNT(*)::bigint FROM duplicate_candidate)                   AS duplicate_candidates,
    NOW()                                                                AS refreshed_at;

-- Only one row, but we still add a unique index so future REFRESH ...
-- CONCURRENTLY calls would work without a rebuild.
CREATE UNIQUE INDEX IF NOT EXISTS mv_stats_summary_singleton
    ON mv_stats_summary ((1));


-- -------------------------------------------------------------------------
-- /api/stats — per-source counts (one row per source_database)
-- -------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_stats_by_source AS
SELECT
    sd.id                              AS source_db_id,
    sd.name                            AS name,
    COALESCE(sc.name, 'Unknown')       AS collection,
    COUNT(s.id)::bigint                AS count
FROM source_database sd
LEFT JOIN source_collection sc ON sd.collection_id = sc.id
LEFT JOIN sighting s           ON s.source_db_id   = sd.id
GROUP BY sd.id, sd.name, sc.name
ORDER BY count DESC;

CREATE UNIQUE INDEX IF NOT EXISTS mv_stats_by_source_pk
    ON mv_stats_by_source (source_db_id);


-- -------------------------------------------------------------------------
-- /api/stats — per-collection counts (one row per source_collection)
-- -------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_stats_by_collection AS
SELECT
    sc.id                AS collection_id,
    sc.name              AS name,
    COUNT(s.id)::bigint  AS count
FROM source_collection sc
JOIN source_database sd ON sd.collection_id = sc.id
LEFT JOIN sighting s    ON s.source_db_id    = sd.id
GROUP BY sc.id, sc.name
ORDER BY count DESC;

CREATE UNIQUE INDEX IF NOT EXISTS mv_stats_by_collection_pk
    ON mv_stats_by_collection (collection_id);


-- -------------------------------------------------------------------------
-- /api/timeline?mode=yearly (no filters) — (period, source_name, cnt)
--
-- The original endpoint SUBSTR's a text date_event, which is why the
-- planner can't use the idx_sighting_date b-tree: the index is on the
-- full text, not its 4-char prefix. The MV freezes the SUBSTR result
-- as a column, and we add a btree on period so any per-year slice is
-- an index scan.
-- -------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_timeline_yearly AS
SELECT
    SUBSTR(s.date_event, 1, 4) AS period,
    sd.name                    AS source_name,
    COUNT(*)::bigint           AS cnt
FROM sighting s
JOIN source_database sd ON s.source_db_id = sd.id
WHERE s.date_event IS NOT NULL
  AND LENGTH(s.date_event) >= 4
GROUP BY SUBSTR(s.date_event, 1, 4), sd.name;

CREATE UNIQUE INDEX IF NOT EXISTS mv_timeline_yearly_pk
    ON mv_timeline_yearly (period, source_name);
CREATE INDEX IF NOT EXISTS mv_timeline_yearly_period
    ON mv_timeline_yearly (period);


-- -------------------------------------------------------------------------
-- /api/sentiment/overview (no filters) — single row of aggregates
--
-- This is the 23-second query from the v0.7.4 boot logs. With the MV
-- it becomes a ~1 ms single-row read.
-- -------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_sentiment_overview AS
SELECT
    COUNT(*)::bigint              AS total_analyzed,
    AVG(sa.vader_compound)        AS avg_compound,
    AVG(sa.vader_positive)        AS avg_positive,
    AVG(sa.vader_negative)        AS avg_negative,
    AVG(sa.vader_neutral)         AS avg_neutral,
    SUM(sa.emo_joy)::bigint       AS joy,
    SUM(sa.emo_fear)::bigint      AS fear,
    SUM(sa.emo_anger)::bigint     AS anger,
    SUM(sa.emo_sadness)::bigint   AS sadness,
    SUM(sa.emo_surprise)::bigint  AS surprise,
    SUM(sa.emo_disgust)::bigint   AS disgust,
    SUM(sa.emo_trust)::bigint     AS trust,
    SUM(sa.emo_anticipation)::bigint AS anticipation,
    NOW()                         AS refreshed_at
FROM sentiment_analysis sa
JOIN sighting s ON s.id = sa.sighting_id;

CREATE UNIQUE INDEX IF NOT EXISTS mv_sentiment_overview_singleton
    ON mv_sentiment_overview ((1));


-- -------------------------------------------------------------------------
-- Initial population. If the MV was just created above, it's already
-- populated by the CREATE statement. If it already existed from a prior
-- deploy, we refresh it to pick up new rows. Both paths are safe.
--
-- We use plain REFRESH (not CONCURRENTLY) so this script doesn't require
-- a long-lived connection or need to worry about the CONCURRENTLY
-- non-empty-MV precondition. Takes an ACCESS EXCLUSIVE lock for ~5-15 s
-- per view, which is fine at deploy time.
-- -------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW mv_stats_summary;
REFRESH MATERIALIZED VIEW mv_stats_by_source;
REFRESH MATERIALIZED VIEW mv_stats_by_collection;
REFRESH MATERIALIZED VIEW mv_timeline_yearly;
REFRESH MATERIALIZED VIEW mv_sentiment_overview;


-- -------------------------------------------------------------------------
-- Sanity check — dump row counts so the deploy log shows populations.
-- -------------------------------------------------------------------------
SELECT 'mv_stats_summary'       AS mv, COUNT(*) AS rows FROM mv_stats_summary
UNION ALL SELECT 'mv_stats_by_source',        COUNT(*) FROM mv_stats_by_source
UNION ALL SELECT 'mv_stats_by_collection',    COUNT(*) FROM mv_stats_by_collection
UNION ALL SELECT 'mv_timeline_yearly',        COUNT(*) FROM mv_timeline_yearly
UNION ALL SELECT 'mv_sentiment_overview',     COUNT(*) FROM mv_sentiment_overview;
