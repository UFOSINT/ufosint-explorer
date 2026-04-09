#!/usr/bin/env python3
"""
Migrate the unified UFO sightings DB from SQLite into PostgreSQL.

Usage:
    python migrate_sqlite_to_pg.py \
        --sqlite ../../data/output/ufo_unified.db \
        --pg "postgresql://ufosint_admin:PASSWORD@ufosint-pg.postgres.database.azure.com:5432/ufo_unified?sslmode=require" \
        [--apply-schema scripts/pg_schema.sql]

Reads each table from SQLite in primary-key order and bulk-inserts into the
matching PostgreSQL table using COPY (psycopg's fast binary path). The
original SQLite primary keys are preserved verbatim, so foreign key chains
work without remapping.

Tables are migrated in dependency order so foreign keys never break:
    source_collection -> source_database -> source_origin -> location ->
    reference -> sighting -> attachment -> sighting_reference ->
    duplicate_candidate -> sentiment_analysis -> date_correction
"""
import argparse
import csv
import io
import sqlite3
import sys
import time
from pathlib import Path

import psycopg

# Tables in dependency order. Each entry is (table_name, columns).
# Columns must match the PG schema; missing columns in SQLite are filled
# with None.
TABLES = [
    ("source_collection", ["id", "name", "display_name", "description", "url"]),
    ("source_database", ["id", "name", "collection_id", "description", "url", "copyright", "record_count"]),
    ("source_origin", ["id", "name", "description"]),
    ("location", ["id", "raw_text", "city", "county", "state", "country", "region",
                  "latitude", "longitude", "geoname_id", "geocode_src"]),
    ("reference", ["id", "text", "hash"]),
    ("sighting", [
        "id", "source_db_id", "source_record_id", "origin_id", "origin_record_id",
        "date_event", "date_event_raw", "date_end", "time_raw", "timezone",
        "date_reported", "date_posted", "location_id",
        "summary", "description",
        "shape", "color", "size_estimated", "angular_size", "distance",
        "duration", "duration_seconds", "num_objects", "num_witnesses",
        "sound", "direction", "elevation_angle", "viewed_from",
        "witness_age", "witness_sex", "witness_names",
        "hynek", "vallee", "event_type", "svp_rating",
        "explanation", "characteristics",
        "weather", "terrain",
        "source_ref", "page_volume",
        "notes", "raw_json",
    ]),
    ("attachment", ["id", "sighting_id", "url", "file_type", "description"]),
    ("sighting_reference", ["sighting_id", "reference_id"]),
    ("duplicate_candidate", [
        "id", "sighting_id_a", "sighting_id_b", "similarity_score",
        "match_method", "status", "resolved_at",
    ]),
    ("sentiment_analysis", [
        "id", "sighting_id",
        "vader_compound", "vader_positive", "vader_negative", "vader_neutral",
        "emo_joy", "emo_fear", "emo_anger", "emo_sadness",
        "emo_surprise", "emo_disgust", "emo_trust", "emo_anticipation",
        "text_source", "text_length",
    ]),
    ("date_correction", [
        "id", "sighting_id", "source_name",
        "original_date", "corrected_date", "correction_type", "reason",
    ]),
]

BATCH_SIZE = 5000  # rows per COPY chunk


def sqlite_columns(sq_conn, table):
    """Return the actual column names of `table` in the SQLite DB."""
    cur = sq_conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def stream_table(sq_conn, table, columns):
    """Yield rows from SQLite, with missing columns filled with None and
    in the order specified by `columns`."""
    actual = sqlite_columns(sq_conn, table)
    selected = [c if c in actual else "NULL" for c in columns]
    sql = f"SELECT {', '.join(selected)} FROM {table} ORDER BY ROWID"
    cur = sq_conn.cursor()
    cur.execute(sql)
    while True:
        chunk = cur.fetchmany(BATCH_SIZE)
        if not chunk:
            break
        yield chunk


def copy_table(sq_conn, pg_conn, table, columns):
    """COPY a single table from SQLite into PostgreSQL."""
    # Quick row count for the progress meter
    cur = sq_conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        total = cur.fetchone()[0]
    except sqlite3.OperationalError:
        total = 0
    if total == 0:
        print(f"  {table}: 0 rows (skipping)")
        return 0

    print(f"  {table}: {total:,} rows...", end=" ", flush=True)
    t0 = time.perf_counter()

    cols_sql = ", ".join(columns)
    copy_sql = f"COPY {table} ({cols_sql}) FROM STDIN"

    rows_done = 0
    with pg_conn.cursor() as pg_cur:
        with pg_cur.copy(copy_sql) as cp:
            for chunk in stream_table(sq_conn, table, columns):
                for row in chunk:
                    cp.write_row(row)
                rows_done += len(chunk)

    elapsed = time.perf_counter() - t0
    rate = rows_done / elapsed if elapsed > 0 else 0
    print(f"done in {elapsed:.1f}s ({rate:,.0f} rows/s)")
    return rows_done


def apply_schema(pg_conn, schema_path):
    """Drop and re-create the schema. Destroys any existing data."""
    print(f"Applying schema from {schema_path} (drops existing tables)...")
    with open(schema_path, encoding="utf-8") as f:
        ddl = f.read()
    with pg_conn.cursor() as cur:
        # Drop in reverse dependency order (or just CASCADE everything)
        for table_name, _ in reversed(TABLES):
            cur.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
        cur.execute(ddl)
    pg_conn.commit()
    print("Schema applied.")


def verify_counts(sq_conn, pg_conn):
    """Compare row counts between SQLite and PostgreSQL after migration."""
    print("\nRow count verification:")
    print(f"  {'table':<22} {'sqlite':>12}  {'postgres':>12}  {'match':>6}")
    all_match = True
    for table_name, _ in TABLES:
        try:
            sq_count = sq_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        except sqlite3.OperationalError:
            sq_count = 0
        with pg_conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            pg_count = cur.fetchone()[0]
        ok = sq_count == pg_count
        if not ok:
            all_match = False
        print(f"  {table_name:<22} {sq_count:>12,}  {pg_count:>12,}  {'OK' if ok else 'MISMATCH':>6}")
    return all_match


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", required=True, help="Path to source SQLite DB")
    parser.add_argument("--pg", required=True, help="Postgres connection URI")
    parser.add_argument("--apply-schema", help="Apply this schema SQL file before migrating (drops tables!)")
    parser.add_argument("--verify-only", action="store_true", help="Only verify row counts, do not migrate")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite).resolve()
    if not sqlite_path.exists():
        sys.exit(f"SQLite DB not found at {sqlite_path}")

    print(f"SQLite source: {sqlite_path}")
    print(f"PostgreSQL target: {args.pg.split('@')[-1]}")  # hide password
    print()

    sq_conn = sqlite3.connect(str(sqlite_path))
    sq_conn.execute("PRAGMA query_only=ON")

    with psycopg.connect(args.pg) as pg_conn:
        if args.apply_schema:
            apply_schema(pg_conn, args.apply_schema)

        if not args.verify_only:
            print("Migrating tables (dependency order):")
            t0 = time.perf_counter()
            grand_total = 0
            for table, columns in TABLES:
                grand_total += copy_table(sq_conn, pg_conn, table, columns)
                pg_conn.commit()
            elapsed = time.perf_counter() - t0
            print(f"\nTotal: {grand_total:,} rows in {elapsed:.1f}s ({grand_total/elapsed:,.0f} rows/s avg)")

        all_match = verify_counts(sq_conn, pg_conn)
        if not all_match:
            print("\nWARNING: Row counts don't match. Investigate before using the PG database.")
            sys.exit(1)
        else:
            print("\nAll row counts match.")

    sq_conn.close()


if __name__ == "__main__":
    main()
