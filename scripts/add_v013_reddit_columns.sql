-- =========================================================================
-- v0.13 — Reddit r/UFOs ingest schema + universal LLM-extraction columns
-- =========================================================================
--
-- Adds the columns needed for the first r/UFOs batch (~4k curated posts)
-- plus a set of universally-useful fields that the LLM extraction pipeline
-- populates — which are initially NULL for legacy sources (NUFORC, MUFON,
-- UFOCAT, UPDB, UFO-search) but can be backfilled later by running the
-- same pipeline against their narratives.
--
-- Source content policy
--   We do NOT store raw Reddit post text, usernames, or user comments.
--   Only transformative LLM output (summary description, ratings,
--   classifications) plus the permalink URL for attribution. See
--   docs/SOURCES.md for the full policy.
--
-- Columns added to `sighting`
--
--   Reddit-specific (2):
--     reddit_post_id      — unique stable identifier (e.g. "1d6vfeh")
--     reddit_url          — for "View original on r/UFOs" link
--
--   LLM-derivative (5):
--     llm_confidence           — 'high' | 'medium' | 'low'
--     llm_anomaly_assessment   — 'anomalous' | 'prosaic' | 'ambiguous'
--     llm_prosaic_candidate    — free text, null unless assessed prosaic
--     llm_strangeness_rating   — 1..5 (smallint)
--     llm_model                — model provenance (e.g. "google/gemini-2.0-flash-001")
--
--   Universal-but-Reddit-first (5):
--     duration_seconds   — parsed from free text to seconds
--     num_witnesses      — hard count
--     num_objects        — hard count
--     has_photo          — boolean
--     has_video          — boolean
--
-- Fully idempotent. Safe to run on a live DB. Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -f scripts/add_v013_reddit_columns.sql
-- =========================================================================

-- Reddit-specific columns
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS reddit_post_id    TEXT,
    ADD COLUMN IF NOT EXISTS reddit_url        TEXT;

-- LLM-derivative columns. Generic naming (no `reddit_` prefix) because
-- the same pipeline can later backfill these for legacy sources.
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS llm_confidence          TEXT,
    ADD COLUMN IF NOT EXISTS llm_anomaly_assessment  TEXT,
    ADD COLUMN IF NOT EXISTS llm_prosaic_candidate   TEXT,
    ADD COLUMN IF NOT EXISTS llm_strangeness_rating  SMALLINT,
    ADD COLUMN IF NOT EXISTS llm_model               TEXT;

-- Universal fields. Initially populated only for Reddit rows, but the
-- schema is source-agnostic so backfilling other sources is purely a
-- pipeline-side job.
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS duration_seconds  INTEGER,
    ADD COLUMN IF NOT EXISTS num_witnesses     SMALLINT,
    ADD COLUMN IF NOT EXISTS num_objects       SMALLINT,
    ADD COLUMN IF NOT EXISTS has_photo         BOOLEAN,
    ADD COLUMN IF NOT EXISTS has_video         BOOLEAN;

-- ---------------------------------------------------------------------------
-- Constraints on the enum-like text columns. CHECK constraints instead of
-- real PG enum types so pipeline code can add values without a migration.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sighting_llm_confidence_check'
    ) THEN
        ALTER TABLE sighting
            ADD CONSTRAINT sighting_llm_confidence_check
            CHECK (llm_confidence IS NULL OR llm_confidence IN ('high', 'medium', 'low'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sighting_llm_anomaly_assessment_check'
    ) THEN
        ALTER TABLE sighting
            ADD CONSTRAINT sighting_llm_anomaly_assessment_check
            CHECK (llm_anomaly_assessment IS NULL OR llm_anomaly_assessment IN ('anomalous', 'prosaic', 'ambiguous'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sighting_llm_strangeness_rating_check'
    ) THEN
        ALTER TABLE sighting
            ADD CONSTRAINT sighting_llm_strangeness_rating_check
            CHECK (llm_strangeness_rating IS NULL OR (llm_strangeness_rating BETWEEN 1 AND 5));
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
-- reddit_post_id is the unique key for within-Reddit dedup. The migration
-- pipeline should use ON CONFLICT (reddit_post_id) DO UPDATE for idempotent
-- incremental ingest. Partial index skips the 614k legacy rows where it's null.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_reddit_post_id
    ON sighting(reddit_post_id)
    WHERE reddit_post_id IS NOT NULL;

-- Strangeness filter is a new filterable axis the UI will expose as a
-- chip or slider. Partial index keeps size small — most rows are null until
-- backfill.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_llm_strangeness
    ON sighting(llm_strangeness_rating)
    WHERE llm_strangeness_rating IS NOT NULL;

-- Anomaly/prosaic filter — research-grade "show me only unexplained" toggle.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_llm_anomaly_assessment
    ON sighting(llm_anomaly_assessment)
    WHERE llm_anomaly_assessment IS NOT NULL;

-- Composite: num_witnesses ≥ N filters ("multi-witness cases only").
-- Covers both Reddit and future backfilled rows.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_num_witnesses
    ON sighting(num_witnesses)
    WHERE num_witnesses IS NOT NULL;

-- ---------------------------------------------------------------------------
-- source_database seed rows (idempotent insert-or-update)
-- ---------------------------------------------------------------------------
-- The ufo-dedup pipeline owns the canonical list of sources, but we insert
-- the Reddit row here as a fallback so the schema migration can land before
-- the first data batch arrives. If the pipeline uses a different id it will
-- conflict-update the name.
INSERT INTO source_collection (name)
    VALUES ('Reddit')
ON CONFLICT DO NOTHING;

INSERT INTO source_database (name, collection_id)
    SELECT 'r/UFOs', id FROM source_collection WHERE name = 'Reddit'
ON CONFLICT DO NOTHING;

-- =========================================================================
-- Verification
-- =========================================================================
-- Run these to confirm the migration applied cleanly:
--
-- SELECT column_name, data_type, is_nullable
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'sighting'
--   AND (column_name LIKE 'reddit_%'
--        OR column_name LIKE 'llm_%'
--        OR column_name IN ('duration_seconds', 'num_witnesses',
--                           'num_objects', 'has_photo', 'has_video'))
-- ORDER BY column_name;
--
-- SELECT indexname FROM pg_indexes
-- WHERE tablename = 'sighting'
--   AND (indexname LIKE 'idx_sighting_reddit%'
--        OR indexname LIKE 'idx_sighting_llm%'
--        OR indexname = 'idx_sighting_num_witnesses');
--
-- SELECT sc.name AS collection, sd.name AS source, sd.id
-- FROM source_database sd
-- LEFT JOIN source_collection sc ON sd.collection_id = sc.id
-- WHERE sc.name = 'Reddit' OR sd.name = 'r/UFOs';
