"""
Surgical v0.14 import — zero-downtime data sync from the science team's
SQLite export (ufo_public.db) into the live Azure Postgres.

v0.14 delta vs v0.13 prod:
  - 9 new audit_* columns on sighting (applied via
    add_v014_audit_columns.sql BEFORE running this script).
  - 78,518 locations with corrected coords (audit Tier A + B fixes).
  - ~380,000 sighting rows with newly-extracted fields (shape, color,
    duration, sound, direction, standardized_shape, etc.) from the
    LLM audit pass.
  - ~30,000 wrong-country geocodes removed (set to NULL coords).

The script takes the same three-step approach as v0.13 but with a
fourth step added for the wide sighting UPDATE. Runs inside ONE
transaction so an error anywhere rolls the whole thing back. No
TRUNCATE, no DROP — prod stays fully available throughout.

Usage:
    export DATABASE_URL=<postgres-uri>
    python scripts/import_v014_surgical.py \\
           --sqlite C:/dev/dg/UFOSINT/ufo-dedup/data/output/ufo_public.db \\
           [--dry-run]

Post-import:
  - REFRESH MATERIALIZED VIEW CONCURRENTLY on the 5 v0.7.5 MVs.
  - az webapp restart to bust /api/points-bulk lru_cache +
    FILTER_CACHE (new shapes "Crescent", "Cloud", "Dome" will appear).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import psycopg


# ---------------------------------------------------------------------------
# Column lists
# ---------------------------------------------------------------------------

# Columns to INSERT on a NEW sighting row (Reddit inserts, already landed
# in v0.13 — this stays here for completeness / future recurring imports).
SIGHTING_INSERT_COLUMNS = [
    "id", "source_db_id", "source_record_id",
    "origin_id", "origin_record_id",
    "date_event", "date_event_raw", "date_end",
    "time_raw", "timezone",
    "date_reported", "date_posted",
    "location_id",
    "shape", "color", "size_estimated", "angular_size", "distance",
    "duration", "duration_seconds", "num_objects", "num_witnesses",
    "sound", "direction", "elevation_angle", "viewed_from",
    "witness_age", "witness_sex", "witness_names",
    "hynek", "vallee", "event_type", "svp_rating",
    "explanation", "characteristics", "weather", "terrain",
    "lat", "lng", "sighting_datetime",
    "standardized_shape", "primary_color", "dominant_emotion",
    "quality_score", "richness_score", "hoax_likelihood",
    "has_description", "has_media", "topic_id",
    "has_movement_mentioned", "movement_categories",
    "emotion_28_dominant", "emotion_28_group", "emotion_7_dominant",
    "vader_compound", "roberta_sentiment",
    "emotion_7_surprise", "emotion_7_fear", "emotion_7_neutral",
    "emotion_7_anger", "emotion_7_disgust", "emotion_7_sadness",
    "emotion_7_joy",
    "nrc_joy", "nrc_fear", "nrc_anger", "nrc_sadness",
    "nrc_surprise", "nrc_disgust", "nrc_trust", "nrc_anticipation",
    "nrc_positive", "nrc_negative",
    "distance_to_nearest_nuclear_site_km",
    "nearest_nuclear_site_name",
    "reddit_post_id", "reddit_url",
    "llm_confidence", "llm_anomaly_assessment",
    "llm_prosaic_candidate", "llm_strangeness_rating", "llm_model",
    "description",
    "has_photo", "has_video",
    "audit_status", "audit_location_check", "audit_location_fix",
    "audit_geocode_check", "audit_data_extracted",
    "audit_quality_notes", "audit_batch_id",
    "audit_model", "audit_timestamp",
]

# Columns we UPDATE on existing sighting rows. Excludes primary key (id),
# source provenance fields that never change (source_db_id, *_record_id),
# created_at, and raw date fields. Everything else the audit pipeline
# might have touched is here.
SIGHTING_UPDATE_COLUMNS = [
    # Observation (LLM extracted)
    "shape", "color", "size_estimated", "angular_size", "distance",
    "duration", "duration_seconds", "num_objects", "num_witnesses",
    "sound", "direction", "elevation_angle", "viewed_from",
    # Derived (shape classifier may have run again)
    "standardized_shape", "primary_color", "dominant_emotion",
    "quality_score", "richness_score", "hoax_likelihood",
    "has_description", "has_media", "topic_id",
    # Movement (may have re-classified)
    "has_movement_mentioned", "movement_categories",
    # Location corrections show up in the `location` table, not here —
    # but lat/lng on sighting are denormalized copies that need sync.
    "lat", "lng",
    # v0.14 — NEW audit_* columns
    "audit_status", "audit_location_check", "audit_location_fix",
    "audit_geocode_check", "audit_data_extracted",
    "audit_quality_notes", "audit_batch_id",
    "audit_model", "audit_timestamp",
]

LOCATION_COLUMNS = [
    "id", "raw_text", "city", "county", "state", "country", "region",
    "latitude", "longitude", "geoname_id", "geocode_src",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def intersect_with_pg(pg_conn, table, columns):
    cur = pg_conn.cursor()
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (table,),
    )
    pg_cols = {r[0] for r in cur.fetchall()}
    return [c for c in columns if c in pg_cols]


def intersect_with_sqlite(sq_conn, table, columns):
    cur = sq_conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    sq_cols = {r[1] for r in cur.fetchall()}
    return [c for c in columns if c in sq_cols]


def _pg_boolean_columns(pg_conn, table):
    cur = pg_conn.cursor()
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND data_type='boolean'
        """,
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


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


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def insert_new_locations(sq_conn, pg_cur, dry_run=False):
    """Insert location rows with id > PG's current max(id)."""
    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM location")
    pg_max_id = pg_cur.fetchone()[0]

    sq_cur = sq_conn.cursor()
    sq_cur.execute("SELECT COUNT(*) FROM location WHERE id > ?", (pg_max_id,))
    total = sq_cur.fetchone()[0]
    if total == 0:
        print(f"  No new locations (PG max id = {pg_max_id:,})")
        return 0

    cols = ", ".join(LOCATION_COLUMNS)
    placeholders = ", ".join(["%s"] * len(LOCATION_COLUMNS))
    sql = f"INSERT INTO location ({cols}) VALUES ({placeholders})"

    print(f"  Inserting {total:,} new location rows (ids > {pg_max_id:,})")
    if dry_run:
        print("  [DRY RUN] not executing")
        return total

    t0 = time.perf_counter()
    sq_cur.execute(
        f"SELECT {cols} FROM location WHERE id > ? ORDER BY id",
        (pg_max_id,),
    )
    batch, inserted = [], 0
    for row in sq_cur:
        batch.append(row)
        if len(batch) >= 1000:
            pg_cur.executemany(sql, batch)
            inserted += len(batch)
            batch = []
    if batch:
        pg_cur.executemany(sql, batch)
        inserted += len(batch)
    print(f"  Done: {inserted:,} in {time.perf_counter() - t0:.1f}s")
    return inserted


def update_location_coords(sq_conn, pg_cur, dry_run=False):
    """UPDATE existing location rows where SQLite has corrected coords.

    v0.14 expands this from v0.13's ~31k to ~78k rows because of the
    audit Tier A/B geocoding fixes. Uses a temp staging table + UPDATE
    FROM to avoid 78k individual round-trips.
    """
    pg_cur.execute("SELECT COALESCE(MAX(id), 0) FROM location")
    pg_max_id = pg_cur.fetchone()[0]

    sq_cur = sq_conn.cursor()
    # Include ALL existing locations (even where SQLite now has NULL
    # coords — Tier A fixes WIPE wrong geocodes). So we can't filter
    # "WHERE latitude IS NOT NULL" like v0.13 did.
    sq_cur.execute(
        "SELECT id, latitude, longitude, geoname_id, geocode_src "
        "FROM location WHERE id <= ?",
        (pg_max_id,),
    )
    sq_rows = {r[0]: (r[1], r[2], r[3], r[4]) for r in sq_cur.fetchall()}

    pg_cur.execute(
        "SELECT id, latitude, longitude, geoname_id, geocode_src "
        "FROM location WHERE id <= %s",
        (pg_max_id,),
    )
    pg_rows = {r[0]: (r[1], r[2], r[3], r[4]) for r in pg_cur.fetchall()}

    to_update = []
    for lid, sq_row in sq_rows.items():
        pg_row = pg_rows.get(lid)
        if pg_row is None:
            continue
        # If anything differs, update.
        if sq_row != pg_row:
            to_update.append((lid,) + sq_row)

    if not to_update:
        print("  No location coord updates needed")
        return 0

    print(f"  Updating {len(to_update):,} location rows")
    if dry_run:
        print("  [DRY RUN] not executing")
        return len(to_update)

    t0 = time.perf_counter()
    pg_cur.execute(
        "CREATE TEMP TABLE _loc_updates ("
        "id BIGINT PRIMARY KEY, latitude DOUBLE PRECISION, "
        "longitude DOUBLE PRECISION, geoname_id BIGINT, geocode_src TEXT"
        ") ON COMMIT DROP"
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
               geoname_id = u.geoname_id,
               geocode_src = u.geocode_src
          FROM _loc_updates u
         WHERE l.id = u.id
        """
    )
    updated = pg_cur.rowcount
    print(f"  Done: {updated:,} in {time.perf_counter() - t0:.1f}s")
    return updated


def update_existing_sightings(sq_conn, pg_cur, dry_run=False):
    """v0.14 — bulk UPDATE on existing sighting rows.

    The audit pipeline touched ~378k of 618k rows, rewriting shape /
    color / duration / standardized_shape / audit_* / etc. Strategy:
    stream all sighting rows from SQLite into a TEMP staging table,
    then one UPDATE FROM merges them into the live table. Any row
    whose values haven't changed is a PG no-op (MVCC writes a new
    tuple only when the SET list actually differs from the current
    row).

    Takes ~2-4 min on B1ms PG for 618k rows.

    Uses only SIGHTING_UPDATE_COLUMNS — excludes id/source_db_id/
    *_record_id/date_event_raw/witness_names/etc that never change.
    """
    cols = intersect_with_pg(pg_cur.connection, "sighting", SIGHTING_UPDATE_COLUMNS)
    cols = intersect_with_sqlite(sq_conn, "sighting", cols)

    sq_cur = sq_conn.cursor()
    sq_cur.execute("SELECT COUNT(*) FROM sighting")
    total = sq_cur.fetchone()[0]
    print(f"  Sightings to sync (all sources incl. Reddit): {total:,}")
    print(f"  Columns to update: {len(cols)} — {cols[:6]}...")
    if dry_run:
        print("  [DRY RUN] not executing")
        return total

    t0 = time.perf_counter()

    # Build temp table schema. All TEXT/INTEGER/REAL — we're going to
    # UPDATE FROM it, so precise types don't matter as long as PG can
    # cast them back into the target columns.
    temp_cols_ddl = ",\n    ".join(f"{c} TEXT" for c in cols)
    pg_cur.execute(
        f"""
        CREATE TEMP TABLE _sight_updates (
            id BIGINT PRIMARY KEY,
            {temp_cols_ddl}
        ) ON COMMIT DROP
        """
    )

    # Stream from SQLite. TEXT is permissive enough for any column value.
    bool_cols = _pg_boolean_columns(pg_cur.connection, "sighting")
    bool_idx = [i for i, c in enumerate(cols) if c in bool_cols]

    col_list = ", ".join(["id"] + cols)
    placeholders = ", ".join(["%s"] * (1 + len(cols)))
    insert_sql = f"INSERT INTO _sight_updates ({col_list}) VALUES ({placeholders})"

    sq_cur.execute(
        f"SELECT id, {', '.join(cols)} FROM sighting ORDER BY id"
    )
    batch, streamed = [], 0
    for row in sq_cur:
        if bool_idx:
            row = list(row)
            for idx in bool_idx:
                # idx is position in `cols`; +1 for the leading id
                row[idx + 1] = _to_bool(row[idx + 1])
            row = tuple(row)
        batch.append(row)
        if len(batch) >= 2000:
            pg_cur.executemany(insert_sql, batch)
            streamed += len(batch)
            batch = []
    if batch:
        pg_cur.executemany(insert_sql, batch)
        streamed += len(batch)
    t1 = time.perf_counter()
    print(f"  Staged {streamed:,} rows into _sight_updates in {t1 - t0:.1f}s")

    # One big UPDATE ... FROM ... WHERE id = id. With CAST(NULLIF())
    # for TEXT->target type so empty strings and nulls behave cleanly.
    # We trust SQLite's raw values because the target column accepts
    # what the v0.14 export produced.
    set_clauses = []
    for c in cols:
        # SMALLINT / INTEGER / REAL / DOUBLE PRECISION columns need an
        # explicit CAST from TEXT; TEXT columns pass through.
        pg_cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='sighting' "
            "AND column_name=%s",
            (c,),
        )
        row = pg_cur.fetchone()
        if not row:
            continue
        dtype = row[0]
        if dtype == "text":
            set_clauses.append(f"{c} = NULLIF(u.{c}, '')")
        elif dtype == "boolean":
            set_clauses.append(
                f"{c} = CASE "
                f"WHEN u.{c} IS NULL OR u.{c} = '' THEN NULL "
                f"WHEN u.{c} IN ('1', 'true', 'True') THEN TRUE "
                f"ELSE FALSE END"
            )
        else:
            # integer/smallint/real/double precision/bigint
            set_clauses.append(f"{c} = CAST(NULLIF(u.{c}, '') AS {dtype})")
    set_sql = ",\n               ".join(set_clauses)

    update_sql = f"""
        UPDATE sighting s
           SET {set_sql}
          FROM _sight_updates u
         WHERE s.id = u.id
    """
    t2 = time.perf_counter()
    print("  Running UPDATE FROM...")
    pg_cur.execute(update_sql)
    updated = pg_cur.rowcount
    t3 = time.perf_counter()
    print(f"  Done: {updated:,} sighting rows updated in {t3 - t2:.1f}s "
          f"(total step: {t3 - t0:.1f}s)")
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--pg", default=None,
                        help="Postgres URI (defaults to $DATABASE_URL)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-sighting-update", action="store_true",
                        help="Skip step 3 (legacy sighting UPDATEs) — "
                             "useful for testing the location/temp-table "
                             "infra in isolation")
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

                if not args.skip_sighting_update:
                    print()
                    print("=== Step 3: UPDATE existing sighting rows (v0.14) ===")
                    updated_sightings = update_existing_sightings(
                        sq_conn, pg_cur, args.dry_run
                    )
                else:
                    updated_sightings = 0
                    print("\n=== Step 3 skipped (--skip-sighting-update) ===")

                print()
                if args.dry_run:
                    print("DRY RUN — rolling back")
                    pg_conn.rollback()
                else:
                    print("Committing transaction...")
                    pg_conn.commit()

                print()
                print("Summary:")
                print(f"  New locations:      {new_locs:,}")
                print(f"  Updated locations:  {updated_locs:,}")
                print(f"  Updated sightings:  {updated_sightings:,}")
            except Exception:
                pg_conn.rollback()
                raise

    sq_conn.close()


if __name__ == "__main__":
    main()
