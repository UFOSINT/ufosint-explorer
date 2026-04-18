"""
Surgical Reddit + geocoding import — zero-downtime alternative to the
full TRUNCATE+COPY pattern in migrate_sqlite_to_pg.py.

Reads the latest SQLite export (ufo_public.db) and applies exactly
three deltas to the live PG:

  1. INSERT new location rows (Reddit's 2,777 new geo rows, ids
     214,783..218,593 — no conflict with existing PG ids).
  2. UPDATE existing location rows where SQLite has coordinates that
     PG doesn't yet have (~31k rows from the v0.13 geocoding
     upgrade to cities1000).
  3. INSERT the 3,811 new Reddit sighting rows (ids 614,506..618,316).

Runs inside a single transaction so an error at any step rolls the
whole thing back. No TRUNCATE, no DROP — prod stays fully available
throughout. Safe to re-run: the sighting INSERT uses ON CONFLICT on
reddit_post_id so a partial run can resume cleanly.

Usage:
    export DATABASE_URL=<postgres-uri>
    python scripts/import_reddit_surgical.py \\
           --sqlite C:/dev/dg/UFOSINT/data/output/ufo_public.db \\
           [--dry-run]

Post-import, run `REFRESH MATERIALIZED VIEW CONCURRENTLY ...` on the
v0.7.5 MVs so /api/stats, /api/timeline, /api/sentiment/overview
reflect the new totals. `az webapp restart` rebuilds the in-process
lru_cache for /api/points-bulk and the FILTER_CACHE so r/UFOs appears
in the source dropdown.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import psycopg


# Columns to copy on Reddit sighting INSERT. Must match what the
# SQLite export actually has AND what PG accepts. We intersect at
# runtime so a mismatch (e.g. SQLite has a column PG doesn't, or
# vice versa) is handled gracefully — any SQLite column not in PG
# is dropped from the INSERT, any PG column not in SQLite stays NULL.
SIGHTING_COPY_COLUMNS = [
    # Identity + provenance
    "id", "source_db_id", "source_record_id",
    "origin_id", "origin_record_id",
    # Dates
    "date_event", "date_event_raw", "date_end",
    "time_raw", "timezone",
    "date_reported", "date_posted",
    # Location
    "location_id",
    # Observation (structured)
    "shape", "color", "size_estimated", "angular_size", "distance",
    "duration", "duration_seconds", "num_objects", "num_witnesses",
    "sound", "direction", "elevation_angle", "viewed_from",
    # Witness
    "witness_age", "witness_sex", "witness_names",
    # Classification
    "hynek", "vallee", "event_type", "svp_rating",
    # Resolution
    "explanation", "characteristics", "weather", "terrain",
    # Derived (v0.8.2+)
    "lat", "lng", "sighting_datetime",
    "standardized_shape", "primary_color", "dominant_emotion",
    "quality_score", "richness_score", "hoax_likelihood",
    "has_description", "has_media", "topic_id",
    # Movement (v0.8.3b)
    "has_movement_mentioned", "movement_categories",
    # Emotion (v0.11)
    "emotion_28_dominant", "emotion_28_group", "emotion_7_dominant",
    "vader_compound", "roberta_sentiment",
    "emotion_7_surprise", "emotion_7_fear", "emotion_7_neutral",
    "emotion_7_anger", "emotion_7_disgust", "emotion_7_sadness",
    "emotion_7_joy",
    # NRC (v0.12)
    "nrc_joy", "nrc_fear", "nrc_anger", "nrc_sadness",
    "nrc_surprise", "nrc_disgust", "nrc_trust", "nrc_anticipation",
    "nrc_positive", "nrc_negative",
    # Nuclear proximity (v0.12)
    "distance_to_nearest_nuclear_site_km",
    "nearest_nuclear_site_name",
    # v0.13 — Reddit + LLM
    "reddit_post_id", "reddit_url",
    "llm_confidence", "llm_anomaly_assessment",
    "llm_prosaic_candidate", "llm_strangeness_rating", "llm_model",
    "description",  # LLM summary for Reddit rows
    "has_photo", "has_video",
]

LOCATION_COLUMNS = [
    "id", "raw_text", "city", "county", "state", "country", "region",
    "latitude", "longitude", "geoname_id", "geocode_src",
]


def intersect_with_pg(pg_conn, table, columns):
    """Return columns that exist in the target PG table."""
    cur = pg_conn.cursor()
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    pg_cols = {r[0] for r in cur.fetchall()}
    filtered = [c for c in columns if c in pg_cols]
    skipped = [c for c in columns if c not in pg_cols]
    if skipped:
        print(f"  [{table}] PG missing columns, skipping: {skipped}")
    return filtered


def intersect_with_sqlite(sq_conn, table, columns):
    """Return columns that exist in the source SQLite table."""
    cur = sq_conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    sq_cols = {r[1] for r in cur.fetchall()}
    filtered = [c for c in columns if c in sq_cols]
    skipped = [c for c in columns if c not in sq_cols]
    if skipped:
        print(f"  [{table}] SQLite missing columns, skipping: {skipped}")
    return filtered


def insert_new_locations(sq_conn, pg_cur, dry_run=False):
    """INSERT location rows with id > max(pg.location.id).

    Safe because Reddit's new locations are at the tail of the id
    sequence and the location table uses explicit ids (not SERIAL).
    """
    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM location")
    pg_max_id = pg_cur.fetchone()[0]

    sq_cur = sq_conn.cursor()
    sq_cur.execute(
        "SELECT COUNT(*) FROM location WHERE id > ?", (pg_max_id,)
    )
    total = sq_cur.fetchone()[0]
    if total == 0:
        print(f"  No new locations to insert (PG max id = {pg_max_id})")
        return 0

    cols = LOCATION_COLUMNS
    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO location ({col_list}) VALUES ({placeholders})"

    print(f"  Inserting {total:,} new location rows (ids > {pg_max_id})...")
    if dry_run:
        print("  [DRY RUN] not executing")
        return total

    t0 = time.perf_counter()
    sq_cur.execute(
        f"SELECT {col_list} FROM location WHERE id > ? ORDER BY id",
        (pg_max_id,),
    )
    batch = []
    inserted = 0
    for row in sq_cur:
        batch.append(row)
        if len(batch) >= 1000:
            pg_cur.executemany(sql, batch)
            inserted += len(batch)
            batch = []
    if batch:
        pg_cur.executemany(sql, batch)
        inserted += len(batch)

    print(f"  Done: {inserted:,} inserted in {time.perf_counter() - t0:.1f}s")
    return inserted


def update_location_coords(sq_conn, pg_cur, dry_run=False):
    """UPDATE existing location rows where SQLite has better coords.

    Uses a temporary staging table + UPDATE-FROM so we're not firing
    31k individual UPDATEs over the wire.
    """
    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM location")
    pg_max_id = pg_cur.fetchone()[0]

    sq_cur = sq_conn.cursor()
    sq_cur.execute(
        "SELECT id, latitude, longitude, geoname_id, geocode_src "
        "FROM location WHERE latitude IS NOT NULL AND id <= ?",
        (pg_max_id,),
    )
    rows = sq_cur.fetchall()
    print(f"  Candidates (SQLite has coords, id <= PG max): {len(rows):,}")

    # Filter: only rows where PG coords differ from SQLite
    pg_cur.execute(
        "SELECT id, latitude, longitude FROM location WHERE id <= %s",
        (pg_max_id,),
    )
    pg_state = {r[0]: (r[1], r[2]) for r in pg_cur.fetchall()}

    to_update = []
    for lid, lat, lng, geoname_id, geocode_src in rows:
        pg_row = pg_state.get(lid)
        if pg_row is None:
            continue
        pg_lat, pg_lng = pg_row
        if pg_lat is None or pg_lng is None:
            to_update.append((lid, lat, lng, geoname_id, geocode_src))
        elif (pg_lat, pg_lng) != (lat, lng):
            # Existing row has coords but they're different — trust
            # SQLite (fresher geocode). Don't clobber if SQLite is
            # null, only update when SQLite has something.
            to_update.append((lid, lat, lng, geoname_id, geocode_src))

    if not to_update:
        print("  No location coord updates needed")
        return 0

    print(f"  Updating coords on {len(to_update):,} rows...")
    if dry_run:
        print("  [DRY RUN] not executing")
        return len(to_update)

    t0 = time.perf_counter()
    # Staging table + UPDATE FROM
    pg_cur.execute(
        "CREATE TEMP TABLE _loc_updates ("
        "id BIGINT PRIMARY KEY, "
        "latitude DOUBLE PRECISION, "
        "longitude DOUBLE PRECISION, "
        "geoname_id BIGINT, "
        "geocode_src TEXT) ON COMMIT DROP"
    )
    pg_cur.executemany(
        "INSERT INTO _loc_updates VALUES (%s, %s, %s, %s, %s)",
        to_update,
    )
    pg_cur.execute(
        """
        UPDATE location l
           SET latitude   = u.latitude,
               longitude  = u.longitude,
               geoname_id = COALESCE(u.geoname_id, l.geoname_id),
               geocode_src = COALESCE(u.geocode_src, l.geocode_src)
          FROM _loc_updates u
         WHERE l.id = u.id
        """
    )
    updated = pg_cur.rowcount
    print(f"  Done: {updated:,} updated in {time.perf_counter() - t0:.1f}s")
    return updated


def insert_reddit_sightings(sq_conn, pg_cur, dry_run=False):
    """INSERT Reddit sighting rows with ON CONFLICT DO NOTHING on
    reddit_post_id so re-runs are idempotent."""
    cols_pg = intersect_with_pg(pg_cur.connection, "sighting", SIGHTING_COPY_COLUMNS)
    cols_sq = intersect_with_sqlite(sq_conn, "sighting", cols_pg)
    cols = cols_sq

    # SQLite stores booleans as 0/1/None. PG has has_photo / has_video
    # as BOOLEAN. psycopg doesn't auto-cast, so we convert at the
    # Python boundary before INSERT. Same treatment for any other
    # BOOLEAN column we might add in the future.
    bool_cols = _pg_boolean_columns(pg_cur.connection, "sighting")
    bool_idx = [i for i, c in enumerate(cols) if c in bool_cols]

    def _to_bool(v):
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, int | float):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "0", "no", "")
        return bool(v)

    sq_cur = sq_conn.cursor()
    sq_cur.execute(
        "SELECT COUNT(*) FROM sighting WHERE source_db_id = 6"
    )
    total = sq_cur.fetchone()[0]
    print(f"  Reddit rows to insert: {total:,}")
    if total == 0:
        return 0

    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    # The v0.13 migration creates a PARTIAL unique index on
    # reddit_post_id WHERE reddit_post_id IS NOT NULL. ON CONFLICT
    # must include the same predicate so PostgreSQL can match it
    # to the index (otherwise: InvalidColumnReference).
    sql = (
        f"INSERT INTO sighting ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (reddit_post_id) "
        f"WHERE reddit_post_id IS NOT NULL "
        f"DO NOTHING"
    )

    if dry_run:
        print("  [DRY RUN] not executing")
        return total

    t0 = time.perf_counter()
    sq_cur.execute(
        f"SELECT {col_list} FROM sighting WHERE source_db_id = 6 ORDER BY id"
    )
    batch = []
    inserted = 0
    for row in sq_cur:
        if bool_idx:
            row = list(row)
            for idx in bool_idx:
                row[idx] = _to_bool(row[idx])
            row = tuple(row)
        batch.append(row)
        if len(batch) >= 500:
            pg_cur.executemany(sql, batch)
            inserted += len(batch)
            batch = []
    if batch:
        pg_cur.executemany(sql, batch)
        inserted += len(batch)

    print(f"  Done: {inserted:,} processed in {time.perf_counter() - t0:.1f}s")
    return inserted


def _pg_boolean_columns(pg_conn, table):
    """Return the set of BOOLEAN columns on `table`."""
    cur = pg_conn.cursor()
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
          AND data_type='boolean'
        """,
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--pg", default=None,
                        help="Postgres URI (defaults to $DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run inside a transaction and ROLLBACK at the end")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite).resolve()
    if not sqlite_path.exists():
        sys.exit(f"SQLite not found: {sqlite_path}")

    pg_url = args.pg or os.environ.get("DATABASE_URL")
    if not pg_url:
        sys.exit("Set DATABASE_URL or pass --pg")

    print(f"SQLite: {sqlite_path}")
    print(f"PG:     {pg_url.split('@')[-1]}")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'LIVE WRITE'}")
    print()

    sq_conn = sqlite3.connect(str(sqlite_path))
    sq_conn.execute("PRAGMA query_only = ON")

    with psycopg.connect(pg_url, autocommit=False) as pg_conn:
        with pg_conn.cursor() as pg_cur:
            try:
                print("=== Step 1: INSERT new locations ===")
                new_locs = insert_new_locations(sq_conn, pg_cur, args.dry_run)

                print()
                print("=== Step 2: UPDATE existing location coords ===")
                updated_locs = update_location_coords(sq_conn, pg_cur, args.dry_run)

                print()
                print("=== Step 3: INSERT Reddit sightings ===")
                new_sightings = insert_reddit_sightings(sq_conn, pg_cur, args.dry_run)

                print()
                if args.dry_run:
                    print("DRY RUN — rolling back")
                    pg_conn.rollback()
                else:
                    print("Committing transaction...")
                    pg_conn.commit()

                print()
                print("Summary:")
                print(f"  New locations:     {new_locs:,}")
                print(f"  Updated locations: {updated_locs:,}")
                print(f"  New sightings:     {new_sightings:,}")
            except Exception:
                pg_conn.rollback()
                raise

    sq_conn.close()


if __name__ == "__main__":
    main()
