-- =========================================================================
-- scripts/pg_tuning.sql
--
-- Free performance tuning for the UFOSINT Explorer PostgreSQL instance.
-- Targets Azure Database for PostgreSQL Flexible Server, Burstable B1ms
-- (1 vCPU, 2 GB RAM). Every value here is chosen to stay safely within
-- the B1ms envelope; bumping to B2ms (8 GB) is a simple scale factor.
--
-- The workload is read-heavy:
--   * /api/map          -- spatial bbox lookups on location(latitude,longitude)
--   * /api/hexbin       -- same, grouped by lat/lng buckets
--   * /api/search       -- pg_trgm GIN on description + summary
--   * /api/timeline     -- decade rollups on date_event
--   * /api/duplicates   -- joins on duplicate_candidate
-- The whole working set (~614K sightings + 2 location columns + hot indexes)
-- is only ~1-2 GB, so the single biggest win is keeping more of it resident
-- in shared_buffers and priming the buffer cache after every restart.
--
-- HOW TO APPLY
--   1) Parameters (the ALTER SYSTEM block) are set via the Azure portal on
--      Flexible Server: "Server parameters" → set each value → Save.
--      Azure applies them without a restart for the "reload" params and
--      schedules a restart for the "postmaster" params (shared_buffers,
--      max_connections). This file documents the exact values.
--   2) The pg_prewarm and ANALYZE section below IS safe to run ad hoc as a
--      superuser or as the app role (it only touches existing tables):
--          psql "$DATABASE_URL" -f scripts/pg_tuning.sql
--      Run it once after a restart, or wire it into a startup hook.
-- =========================================================================


-- -------------------------------------------------------------------------
-- 1) Memory settings. Reference values for Burstable B1ms (2 GB RAM).
--    For B2ms (8 GB) multiply each by 4 (shown in the comment).
-- -------------------------------------------------------------------------
--
-- Azure Flexible Server sets sane-ish defaults but they're conservative.
-- These are the values to paste into the portal's server-parameters page.
--
--   shared_buffers       = 768MB     -- B2ms: 3GB   (~25-35% of RAM)
--   effective_cache_size = 1500MB    -- B2ms: 6GB   (what planner thinks is cached)
--   work_mem             = 16MB      -- B2ms: 32MB  (per sort/hash operation)
--   maintenance_work_mem = 128MB     -- B2ms: 512MB (VACUUM, CREATE INDEX)
--   max_connections      = 50        -- B2ms: 100
--   random_page_cost     = 1.1       -- SSD, not spinning disk
--   effective_io_concurrency = 200   -- SSD can handle many parallel reads
--   default_statistics_target = 200  -- better row estimates on skewed cols
--   jit                  = off       -- JIT startup cost hurts us more than it helps
--
-- Azure Flexible Server does NOT accept ALTER SYSTEM from clients (managed
-- service lockdown). The ALTER SYSTEM statements below are left commented
-- out so this script still runs clean on a vanilla self-hosted Postgres
-- for local development. Uncomment for local tuning.
--
-- ALTER SYSTEM SET shared_buffers            = '768MB';
-- ALTER SYSTEM SET effective_cache_size      = '1500MB';
-- ALTER SYSTEM SET work_mem                  = '16MB';
-- ALTER SYSTEM SET maintenance_work_mem      = '128MB';
-- ALTER SYSTEM SET random_page_cost          = 1.1;
-- ALTER SYSTEM SET effective_io_concurrency  = 200;
-- ALTER SYSTEM SET default_statistics_target = 200;
-- ALTER SYSTEM SET jit                       = off;
-- SELECT pg_reload_conf();


-- -------------------------------------------------------------------------
-- 2) pg_prewarm — load hot tables + indexes into shared_buffers.
--
-- After a server restart the buffer cache is empty, so the first query
-- against each table eats a few hundred ms of disk I/O. pg_prewarm reads
-- the pages directly into the cache so the FIRST request is warm.
--
-- Azure Flexible Server ships the extension but it has to be enabled
-- via the "Server parameters" page (shared_preload_libraries) for the
-- 'buffercache' variant. The 'read' variant used here does NOT require
-- preload, only CREATE EXTENSION.
-- -------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_prewarm;

DO $$
DECLARE
    relname text;
    hot_rels text[] := ARRAY[
        -- Core tables (ordered biggest-impact first)
        'sighting',
        'location',
        'sentiment_analysis',
        'duplicate_candidate',

        -- Composite / hot indexes that every map request hits
        'idx_location_coords',
        'idx_sighting_date',
        'idx_sighting_source',
        'idx_sighting_shape',
        'idx_sighting_location',
        'idx_sighting_source_date',
        'idx_location_country',
        'idx_location_city',

        -- Search indexes (pg_trgm GIN)
        'idx_sighting_description_trgm',
        'idx_sighting_summary_trgm',

        -- Sentiment + duplicate joins
        'idx_sentiment_sighting',
        'idx_sentiment_compound',
        'idx_duplicate_a',
        'idx_duplicate_b',
        'idx_duplicate_status'
    ];
    loaded bigint;
BEGIN
    FOREACH relname IN ARRAY hot_rels LOOP
        BEGIN
            SELECT pg_prewarm(relname) INTO loaded;
            RAISE NOTICE 'prewarm %: % blocks', relname, loaded;
        EXCEPTION WHEN undefined_table THEN
            RAISE NOTICE 'prewarm %: SKIPPED (not present)', relname;
        END;
    END LOOP;
END $$;


-- -------------------------------------------------------------------------
-- 3) Refresh planner stats. Cheap, runs in a few seconds on 614K rows,
--    and gives the planner accurate histograms for the skewed columns
--    (country, source_db_id, shape).
-- -------------------------------------------------------------------------
ANALYZE sighting;
ANALYZE location;
ANALYZE sentiment_analysis;
ANALYZE duplicate_candidate;


-- -------------------------------------------------------------------------
-- 4) Sanity check — dump the current memory + buffer settings so the
--    operator can confirm the portal values actually took effect.
-- -------------------------------------------------------------------------
SELECT name, setting, unit
FROM pg_settings
WHERE name IN (
    'shared_buffers',
    'effective_cache_size',
    'work_mem',
    'maintenance_work_mem',
    'max_connections',
    'random_page_cost',
    'effective_io_concurrency',
    'default_statistics_target',
    'jit'
)
ORDER BY name;
