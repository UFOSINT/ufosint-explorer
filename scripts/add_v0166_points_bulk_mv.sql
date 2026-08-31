-- =========================================================================
-- v0.16.6 — mv_points_bulk: the narrow source for /api/points-bulk
-- =========================================================================
-- Loading 468,251 descriptions into `sighting` took its heap from ~150 MB to
-- 836 MB. The map buffer needs about 30 narrow columns, but every scan then
-- had to drag the inline description text through memory with them. The
-- build query went from inside the app's 25 s statement_timeout to 48.8 s,
-- so /api/points-bulk returned 500 to every visitor -- which the page showed
-- as "some data may be missing" and a silent fall back to the legacy Leaflet
-- cluster layer, because bootDeckGL() throws when that fetch fails.
--
-- This view holds exactly the columns the buffer packs, for exactly the rows
-- it maps: 385,211 rows, 57 MB, scanned in 73 ms rather than 48.8 s.
--
-- IT MUST BE REFRESHED WHENEVER SIGHTING OR LOCATION DATA CHANGES. The
-- deploy workflow runs this file on every deploy and the REFRESH at the
-- bottom is unconditional, so a deploy always publishes current data. A bulk
-- load outside a deploy (reload_from_public_db.py) refreshes it too.
--
-- Idempotent: safe to run on every deploy.
-- =========================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_points_bulk AS
SELECT s.id, s.lat, s.lng, s.sighting_datetime, s.source_db_id,
       s.standardized_shape, s.quality_score, s.hoax_likelihood,
       s.richness_score, s.primary_color, s.dominant_emotion,
       s.has_description, s.has_media, s.has_movement_mentioned,
       s.movement_categories, s.num_witnesses, s.duration_seconds, s.topic_id,
       s.emotion_28_dominant, s.emotion_28_group, s.emotion_7_dominant,
       s.vader_compound, s.roberta_sentiment,
       s.nrc_joy, s.nrc_fear, s.nrc_anger, s.nrc_sadness,
       s.nrc_surprise, s.nrc_disgust, s.nrc_trust, s.nrc_anticipation
FROM sighting s
JOIN location l ON l.id = s.location_id
WHERE l.latitude IS NOT NULL
  AND l.longitude IS NOT NULL
  AND l.latitude BETWEEN -90 AND 90
  AND l.longitude BETWEEN -180 AND 180;

CREATE INDEX IF NOT EXISTS idx_mv_points_bulk_id ON mv_points_bulk(id);

-- A covering index for the ETag's COUNT/MAX, so it never touches the heap.
-- The equivalent on `sighting` (location_id, id) is what took that query
-- from 17.3 s to 392 ms while the fat heap was still in play.
CREATE INDEX IF NOT EXISTS idx_sighting_location_id_covering
  ON sighting (location_id, id);

REFRESH MATERIALIZED VIEW mv_points_bulk;
ANALYZE mv_points_bulk;
