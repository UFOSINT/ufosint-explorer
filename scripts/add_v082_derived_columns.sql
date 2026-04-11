-- =========================================================================
-- v0.8.2 — Derived public fields migration
-- =========================================================================
--
-- Adds the analysis fields the ufo-dedup science-team pipeline delivers
-- (quality_score, hoax_likelihood, standardized_shape, primary_color,
-- dominant_emotion, richness_score, sighting_datetime, has_description,
-- has_media, topic_id) plus denormalised lat/lng on the sighting row.
--
-- Fully idempotent:
--   * ADD COLUMN IF NOT EXISTS — safe on schemas that already have
--     the columns from a prior run.
--   * CREATE INDEX CONCURRENTLY IF NOT EXISTS — won't block writes
--     during deploys; won't error on a re-run.
--
-- Design notes:
--   * All new columns start NULL. They'll stay NULL until the user
--     runs ufo-dedup/rebuild_db.py on their private machine and then
--     streams the result via scripts/migrate_sqlite_to_pg.py. The
--     /api/points-bulk endpoint ships with a column-probe helper that
--     encodes NULL as a sentinel value in the binary payload, so the
--     app works immediately after this migration runs even if none of
--     the derived values are populated yet.
--   * hoax_likelihood is REAL (PG float4) so the SQLite source's
--     0.0-1.0 range round-trips exactly. The /api/points-bulk packer
--     scales it to 0-100 uint8 for the wire format.
--   * quality_score and richness_score are SMALLINT (0-100), matching
--     the SQLite INTEGER with a tighter domain.
--   * has_description / has_media are SMALLINT (0 or 1) to match the
--     SQLite INTEGER bool idiom; could be BOOLEAN but SMALLINT lets
--     the migrate_sqlite_to_pg.py streamer move the raw bytes with no
--     type coercion.
--   * topic_id stays INTEGER so v0.9 topic modelling can use the
--     full 2^31 namespace if needed.
--   * sighting_datetime is TEXT — the ufo-dedup side stores ISO 8601
--     strings and the public PG schema keeps text-based dates for
--     consistency with the existing date_event column.
-- =========================================================================

ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS lat                DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS lng                DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS sighting_datetime  TEXT,
    ADD COLUMN IF NOT EXISTS standardized_shape TEXT,
    ADD COLUMN IF NOT EXISTS primary_color      TEXT,
    ADD COLUMN IF NOT EXISTS dominant_emotion   TEXT,
    ADD COLUMN IF NOT EXISTS quality_score      SMALLINT,
    ADD COLUMN IF NOT EXISTS richness_score     SMALLINT,
    ADD COLUMN IF NOT EXISTS hoax_likelihood    REAL,
    ADD COLUMN IF NOT EXISTS has_description    SMALLINT,
    ADD COLUMN IF NOT EXISTS has_media          SMALLINT,
    ADD COLUMN IF NOT EXISTS topic_id           INTEGER;

-- Indexes for the common filter paths. CONCURRENTLY so a deploy that
-- applies this migration during traffic doesn't block writes. Each
-- index is guarded by IF NOT EXISTS so a second deploy is a no-op.
-- Note: CONCURRENTLY cannot run inside a transaction block, so this
-- migration must be applied with psql -f (not inside a BEGIN/COMMIT).

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_quality
    ON sighting(quality_score);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_hoax
    ON sighting(hoax_likelihood);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_std_shape
    ON sighting(standardized_shape);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_dom_emotion
    ON sighting(dominant_emotion);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_datetime_derived
    ON sighting(sighting_datetime);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_has_desc_new
    ON sighting(has_description);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_has_media
    ON sighting(has_media);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_topic
    ON sighting(topic_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_primary_color
    ON sighting(primary_color);

-- =========================================================================
-- Verification query (harmless SELECT — confirms the columns exist)
-- =========================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'sighting'
--   AND column_name IN (
--       'lat','lng','sighting_datetime','standardized_shape','primary_color',
--       'dominant_emotion','quality_score','richness_score','hoax_likelihood',
--       'has_description','has_media','topic_id'
--   )
-- ORDER BY column_name;
