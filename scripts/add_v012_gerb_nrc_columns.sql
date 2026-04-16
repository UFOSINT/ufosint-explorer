-- =========================================================================
-- v0.12 — NRC Lexicon word-counts + UAP Gerb nuclear proximity overlay
-- =========================================================================
--
-- Adds:
--   10 NRC lexicon word-count columns on sighting (denormalized from
--   sentiment_analysis — makes the raw emotion counts queryable on the
--   public DB after sentiment_analysis is dropped in the export)
--
--   2 nuclear proximity columns on sighting (distance_to_nearest_nuclear_site_km,
--   nearest_nuclear_site_name — computed by gerb_overlay.py via haversine
--   against 50 nuclear-relevant facilities)
--
--   3 overlay tables (crash_retrieval, nuclear_encounter, facility —
--   curated UAP Gerb research data, separate from the main sighting corpus)
--
-- Fully idempotent. Safe to run on a live DB.
-- Apply with psql (CONCURRENTLY cannot run inside a transaction block):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -f scripts/add_v012_gerb_nrc_columns.sql
-- =========================================================================

-- NRC Lexicon word-counts
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS nrc_joy             SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_fear            SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_anger           SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_sadness         SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_surprise        SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_disgust         SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_trust           SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_anticipation    SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_positive        SMALLINT,
    ADD COLUMN IF NOT EXISTS nrc_negative        SMALLINT;

-- Nuclear proximity
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS distance_to_nearest_nuclear_site_km REAL,
    ADD COLUMN IF NOT EXISTS nearest_nuclear_site_name           TEXT;

-- Overlay tables
CREATE TABLE IF NOT EXISTS crash_retrieval (
    id              TEXT PRIMARY KEY,
    page_name       TEXT NOT NULL,
    year            INTEGER,
    date_event      TEXT,
    city            TEXT,
    region          TEXT,
    country         TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    precision       TEXT,
    craft_type      TEXT,
    craft_size_m    REAL,
    recovery_status TEXT,
    has_biologics   SMALLINT,
    crew_count      TEXT,
    evidence_quality TEXT,
    source_confidence TEXT,
    short_summary   TEXT,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS nuclear_encounter (
    id              SERIAL PRIMARY KEY,
    page_name       TEXT NOT NULL,
    year            INTEGER,
    date_event      TEXT,
    base            TEXT,
    city            TEXT,
    region          TEXT,
    country         TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    weapon_system   TEXT,
    incident_type   TEXT,
    missiles_affected INTEGER,
    sensor_confirmation TEXT,
    witness_credibility TEXT,
    evidence_quality TEXT,
    source_confidence TEXT,
    summary         TEXT,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS facility (
    id              SERIAL PRIMARY KEY,
    name            TEXT,
    facility_type   TEXT,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    source          TEXT
);

-- Index for proximity filtering
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_nuclear_dist
    ON sighting(distance_to_nearest_nuclear_site_km);

-- =========================================================================
-- Verification
-- =========================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'sighting'
--   AND (column_name LIKE 'nrc_%'
--        OR column_name LIKE 'distance_to_%'
--        OR column_name = 'nearest_nuclear_site_name')
-- ORDER BY column_name;
--
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
--   AND table_name IN ('crash_retrieval', 'nuclear_encounter', 'facility');
