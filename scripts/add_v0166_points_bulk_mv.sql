-- =========================================================================
-- v0.16.6 — mv_points_bulk: the narrow source for /api/points-bulk
-- =========================================================================
-- Loading 468,251 descriptions into `sighting` took its heap from ~150 MB to
-- 836 MB. They average 645 chars, which keeps each row under PostgreSQL's
-- ~2 KB TOAST threshold, so they are stored inline. The map buffer needs
-- about thirty narrow columns, but every scan then had to drag that text
-- through memory alongside them: the ETag count took 17.3 s and the buffer
-- build 48.8 s, against the app's 25 s statement_timeout. /api/points-bulk
-- returned 500 to every visitor, which the page showed as "some data may be
-- missing" plus a silent fall back to the legacy Leaflet cluster layer.
--
-- This view holds exactly the columns _points_bulk_build_cached() selects,
-- for exactly the rows it maps: 385,211 rows, ~60 MB, scanned in 73 ms.
--
-- COLUMN SET IS LOAD-BEARING. Every name here must match what the packer's
-- select list asks for, including `latitude`/`longitude` (which come from
-- `location`, not `sighting`) and the raw `shape` and `date_event` columns.
-- A missing one is not a soft failure: the query errors with
-- "missing FROM-clause entry" and the map goes dark.
-- tests/test_v0166_points_bulk_mv.py checks this against app.py.
--
-- MUST BE REFRESHED WHENEVER SIGHTING OR LOCATION DATA CHANGES. The deploy
-- workflow runs this file every deploy and the REFRESH below is
-- unconditional, so a deploy always publishes current data. A bulk load
-- outside a deploy has to refresh it too.
-- =========================================================================

DROP MATERIALIZED VIEW IF EXISTS mv_points_bulk;

CREATE MATERIALIZED VIEW mv_points_bulk AS
SELECT s.id,
       l.latitude,
       l.longitude,
       s.source_db_id,
       s.shape,
       s.date_event,
       s.duration_seconds,
       s.num_witnesses,
       s.sighting_datetime,
       s.standardized_shape,
       s.primary_color,
       s.dominant_emotion,
       s.quality_score,
       s.richness_score,
       s.hoax_likelihood,
       s.has_description,
       s.has_media,
       s.has_movement_mentioned,
       s.movement_categories,
       s.emotion_28_dominant,
       s.emotion_28_group,
       s.emotion_7_dominant,
       s.vader_compound,
       s.roberta_sentiment,
       s.nrc_joy,
       s.nrc_fear,
       s.nrc_anger,
       s.nrc_sadness,
       s.nrc_surprise,
       s.nrc_disgust,
       s.nrc_trust,
       s.nrc_anticipation
FROM sighting s
JOIN location l ON l.id = s.location_id
WHERE l.latitude IS NOT NULL
  AND l.longitude IS NOT NULL
  AND l.latitude BETWEEN -90 AND 90
  AND l.longitude BETWEEN -180 AND 180;

CREATE INDEX IF NOT EXISTS idx_mv_points_bulk_id ON mv_points_bulk(id);

-- Covering index so the ETag's COUNT/MAX never touches the fat heap:
-- 17.3 s -> 392 ms.
CREATE INDEX IF NOT EXISTS idx_sighting_location_id_covering
  ON sighting (location_id, id);

ANALYZE mv_points_bulk;
