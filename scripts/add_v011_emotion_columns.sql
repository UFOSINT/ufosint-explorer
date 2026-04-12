-- =========================================================================
-- v0.11 — Emotion classification columns
-- =========================================================================
--
-- Adds the 12 new emotion columns produced by ufo-dedup/emotions.py.
-- Three transformer models (GoEmotions 28-class, 7-class RoBERTa emotion,
-- RoBERTa-large sentiment) + VADER produce pre-computed labels and scores
-- on every sighting with narrative text (~503k rows of 614k total).
--
-- The /api/points-bulk packer reads these columns and encodes them into
-- the 40-byte binary row format (see the v0.11 schema spec for byte
-- offsets and scaling conventions).
--
-- Fully idempotent:
--   * ADD COLUMN IF NOT EXISTS — safe to re-run
--   * CREATE INDEX CONCURRENTLY IF NOT EXISTS — no write-blocking
--
-- Apply with psql (CONCURRENTLY cannot run inside a transaction block):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -f scripts/add_v011_emotion_columns.sql
-- =========================================================================

-- P1: GoEmotions 28-class dominant label + sentiment group
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS emotion_28_dominant TEXT,
    ADD COLUMN IF NOT EXISTS emotion_28_group    TEXT;

-- P1: 7-class RoBERTa dominant label
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS emotion_7_dominant  TEXT;

-- P2: VADER compound and RoBERTa sentiment scores
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS vader_compound      REAL,
    ADD COLUMN IF NOT EXISTS roberta_sentiment   REAL;

-- P3: 7-class softmax probability vector (optional — 7 bytes in buffer)
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS emotion_7_surprise  REAL,
    ADD COLUMN IF NOT EXISTS emotion_7_fear      REAL,
    ADD COLUMN IF NOT EXISTS emotion_7_neutral   REAL,
    ADD COLUMN IF NOT EXISTS emotion_7_anger     REAL,
    ADD COLUMN IF NOT EXISTS emotion_7_disgust   REAL,
    ADD COLUMN IF NOT EXISTS emotion_7_sadness   REAL,
    ADD COLUMN IF NOT EXISTS emotion_7_joy       REAL;

-- Indexes for the label columns (filterable in the app)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_emo28
    ON sighting(emotion_28_dominant);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_emo28g
    ON sighting(emotion_28_group);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_emo7
    ON sighting(emotion_7_dominant);

-- =========================================================================
-- Verification query
-- =========================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_schema = 'public'
--   AND table_name = 'sighting'
--   AND column_name LIKE 'emotion_%' OR column_name IN ('vader_compound','roberta_sentiment')
-- ORDER BY column_name;
