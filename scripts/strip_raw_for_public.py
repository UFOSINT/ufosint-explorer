#!/usr/bin/env python3
"""
Strip raw text columns from the PUBLIC PostgreSQL database.

This is the irreversible cutover step that makes the public copy of the
UFO sightings DB legally distributable: it drops the original narrative
columns (description, summary, notes, raw_json, date_event_raw, time_raw)
and the two trigram indexes that depended on them, leaving only the
v0.8.2 derived public fields (quality_score, standardized_shape, etc.).

The full raw text remains in the private SQLite at:
    C:\\dev\\dg\\UFOSINT\\data\\output\\ufo_unified.db
That file is the ONLY remaining copy after this script runs successfully.

Companion to scripts/drop_raw_text_columns.sql — both do the same DROP
COLUMN work. This wrapper adds:
  - CLI safety rails (host echo, never the password)
  - Before/after table-size measurement
  - Per-column byte estimate so you can see what you're freeing
  - Optional VACUUM FULL to actually reclaim disk
  - --dry-run that prints the plan and exits without writing
  - Refusal to run if quality_score coverage < threshold

Usage:
    export DATABASE_URL='postgresql://...'
    python scripts/strip_raw_for_public.py --dry-run        # plan only
    python scripts/strip_raw_for_public.py --yes            # do it
    python scripts/strip_raw_for_public.py --yes --vacuum-full  # do it + reclaim disk

This will REFUSE TO RUN against:
  - A non-Postgres URI
  - A DB whose sighting.quality_score coverage is below the threshold
    (default 90%) — this catches the case of stripping raw text from a
    DB that hasn't been re-migrated with the v0.8.2 derived fields yet
  - An empty sighting table
"""
import argparse
import os
import sys
import time

import psycopg

# Columns to drop. Keep in sync with scripts/drop_raw_text_columns.sql.
#
# v0.8.3 trim: the operator explicitly chose NOT to drop
# `date_event_raw` and `time_raw` during the v0.8.3 scoping
# (see docs/V083_PLAN.md). Those two fields are short structured
# strings, not narrative text, and the detail modal keeps showing
# them. The surviving 4 columns are the true narrative blobs.
#
# Other free-text fields the operator chose to keep for now:
# witness_names, explanation, characteristics, weather, terrain.
# These are short enough to remain structured-adjacent and are
# earmarked for a science-team cleanup pass in v0.8.4+ (see
# docs/V083_BACKLOG.md "Science-team cleanup of free-text fields").
RAW_COLUMNS = [
    "description",
    "summary",
    "notes",
    "raw_json",
]

# Trigram indexes that depend on the text columns and would otherwise
# CASCADE away silently. Drop them explicitly so the action is visible.
TRGM_INDEXES = [
    "idx_sighting_description_trgm",
    "idx_sighting_summary_trgm",
]

DEFAULT_COVERAGE_THRESHOLD = 0.90


# ============================================================
# Pre-flight checks
# ============================================================

def check_pg_uri(uri):
    """Refuse non-Postgres URIs. Returns the host:port for display."""
    if not uri:
        sys.exit("ERROR: no Postgres URI provided. Set DATABASE_URL or pass --pg.")
    if not (uri.startswith("postgresql://") or uri.startswith("postgres://")):
        sys.exit(f"ERROR: --pg must be a postgres:// URI. Got: {uri[:30]}...")
    # Extract host:port for display, never the password
    after_at = uri.split("@", 1)[-1]
    host = after_at.split("/", 1)[0]
    return host


def existing_columns(cur, table, candidates):
    """Return the subset of `candidates` that actually exist on `table`."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    have = {row[0] for row in cur.fetchall()}
    return [c for c in candidates if c in have]


def existing_indexes(cur, candidates):
    """Return the subset of index names from `candidates` that exist."""
    cur.execute(
        """
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = ANY(%s)
        """,
        (candidates,),
    )
    return [row[0] for row in cur.fetchall()]


def coverage_check(cur, threshold):
    """Verify quality_score coverage on geocoded rows is above threshold.

    Mirrors the safety probe in drop_raw_text_columns.sql lines 68-93.
    Returns (populated, total_geocoded, coverage_fraction).
    """
    # Use the denormalized lat/lng columns if present (v0.8.2+),
    # otherwise fall back to JOIN against location.
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'sighting' AND column_name = 'lat'
        )
        """
    )
    has_latlng = cur.fetchone()[0]

    if has_latlng:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE quality_score IS NOT NULL),
                COUNT(*)
            FROM sighting
            WHERE lat IS NOT NULL AND lng IS NOT NULL
            """
        )
    else:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE s.quality_score IS NOT NULL),
                COUNT(*)
            FROM sighting s
            JOIN location l ON l.id = s.location_id
            WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
            """
        )

    populated, total = cur.fetchone()
    coverage = (populated / total) if total else 0.0
    return populated, total, coverage


def per_column_bytes(cur, columns):
    """Estimate disk bytes occupied by each raw column (sum of octet_length).

    Reflects logical size, not on-disk TOAST overhead. Run as a single
    aggregate query to keep it cheap.
    """
    if not columns:
        return {}
    parts = ", ".join(
        f"COALESCE(SUM(octet_length({c})), 0)" for c in columns
    )
    cur.execute(f"SELECT {parts} FROM sighting")
    row = cur.fetchone()
    return dict(zip(columns, row, strict=False))


def table_size_bytes(cur, table):
    """Total relation size including indexes and TOAST."""
    cur.execute("SELECT pg_total_relation_size(%s::regclass)", (table,))
    return cur.fetchone()[0]


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ============================================================
# Plan + execute
# ============================================================

def print_plan(host, raw_present, indexes_present, populated, total, coverage, table_size, col_bytes):
    print()
    print("=" * 70)
    print("  PLAN: strip raw text columns from PUBLIC Postgres")
    print("=" * 70)
    print(f"  Target host:    {host}")
    print(f"  Sighting rows:  {coverage_summary_total(populated, total)}")
    print(f"  Coverage:       {populated:,} / {total:,} geocoded rows have quality_score "
          f"({coverage*100:.1f}%)")
    print(f"  Current table:  {fmt_bytes(table_size)} (sighting + indexes + TOAST)")
    print()

    print("  Columns to DROP from sighting:")
    if not raw_present:
        print("    (none — all 6 raw columns are already gone)")
    else:
        total_col_bytes = sum(col_bytes.get(c, 0) for c in raw_present)
        for c in raw_present:
            b = col_bytes.get(c, 0)
            print(f"    - {c:<18}  {fmt_bytes(b):>10}")
        print(f"    {'-'*18}  {'-'*10}")
        print(f"    {'TOTAL':<18}  {fmt_bytes(total_col_bytes):>10}")
        print("    (logical bytes — actual disk reclaim happens on VACUUM FULL)")
    print()

    print("  Indexes to DROP:")
    if not indexes_present:
        print("    (none — both trgm indexes are already gone)")
    else:
        for idx in indexes_present:
            print(f"    - {idx}")
    print()


def coverage_summary_total(populated, total):
    """Format the row count summary for the plan header."""
    return f"(checking {total:,} geocoded rows)"


def execute_drop(conn, raw_present, indexes_present):
    """Single-transaction DROP COLUMN + DROP INDEX. Mirrors the SQL file."""
    with conn.cursor() as cur:
        if raw_present:
            cols_sql = ",\n    ".join(f"DROP COLUMN IF EXISTS {c}" for c in raw_present)
            cur.execute(f"ALTER TABLE sighting\n    {cols_sql}")
        for idx in indexes_present:
            cur.execute(f"DROP INDEX IF EXISTS {idx}")
    conn.commit()


def vacuum_full_sighting(uri):
    """Run VACUUM FULL sighting on a fresh autocommit connection.

    VACUUM FULL cannot run inside an explicit transaction, hence the
    separate connection with autocommit=True. Takes an ACCESS EXCLUSIVE
    lock — schedule during a maintenance window.
    """
    print()
    print("Running VACUUM FULL sighting (takes ACCESS EXCLUSIVE lock)...")
    t0 = time.perf_counter()
    with psycopg.connect(uri, autocommit=True, connect_timeout=15) as v_conn:
        with v_conn.cursor() as v_cur:
            v_cur.execute("VACUUM FULL sighting")
    elapsed = time.perf_counter() - t0
    print(f"  done in {elapsed:.1f}s")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Drop raw text columns from the PUBLIC Postgres copy. Irreversible."
    )
    parser.add_argument(
        "--pg",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres URI. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and exit without writing.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation. Required for non-interactive use.",
    )
    parser.add_argument(
        "--vacuum-full",
        action="store_true",
        help="Run VACUUM FULL sighting after the drop to reclaim disk. "
             "Takes an ACCESS EXCLUSIVE lock; schedule during a maintenance window.",
    )
    parser.add_argument(
        "--coverage-threshold",
        type=float,
        default=DEFAULT_COVERAGE_THRESHOLD,
        help=f"Refuse to run if quality_score coverage on geocoded rows is "
             f"below this fraction (default {DEFAULT_COVERAGE_THRESHOLD}).",
    )
    args = parser.parse_args()

    host = check_pg_uri(args.pg)

    # Connect read-only first to gather plan info
    with psycopg.connect(args.pg, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            # Sanity: are we actually on Postgres and is the table here?
            cur.execute("SELECT version()")
            pg_version = cur.fetchone()[0].split(" on ")[0]

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'sighting'
                )
                """
            )
            if not cur.fetchone()[0]:
                sys.exit(f"ERROR: no public.sighting table on {host}")

            cur.execute("SELECT COUNT(*) FROM sighting")
            sighting_total = cur.fetchone()[0]
            if sighting_total == 0:
                sys.exit(f"ERROR: sighting table is empty on {host}. Refusing to drop.")

            # Pre-flight: derived columns must exist (mirrors SQL safety probe 1)
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'sighting'
                      AND column_name = 'quality_score'
                )
                """
            )
            if not cur.fetchone()[0]:
                sys.exit(
                    "ERROR: sighting.quality_score does not exist. "
                    "Run scripts/add_v082_derived_columns.sql before this script."
                )

            # Pre-flight: coverage must be above threshold (mirrors SQL safety probe 2)
            populated, total, coverage = coverage_check(cur, args.coverage_threshold)
            if coverage < args.coverage_threshold:
                sys.exit(
                    f"ERROR: quality_score coverage {coverage*100:.1f}% is below "
                    f"threshold {args.coverage_threshold*100:.0f}%. "
                    f"Re-run the ufo-dedup pipeline and re-migrate before stripping."
                )

            raw_present = existing_columns(cur, "sighting", RAW_COLUMNS)
            already_gone = [c for c in RAW_COLUMNS if c not in raw_present]
            indexes_present = existing_indexes(cur, TRGM_INDEXES)
            col_bytes = per_column_bytes(cur, raw_present)
            current_size = table_size_bytes(cur, "sighting")

        print(f"Connected: {pg_version}")
        print(f"Target:    {host}")
        if already_gone:
            print(f"Note:      already-dropped columns will be skipped: {', '.join(already_gone)}")

        print_plan(host, raw_present, indexes_present, populated, total, coverage, current_size, col_bytes)

        if not raw_present and not indexes_present:
            print("Nothing to do. The sighting table is already stripped.")
            return 0

        if args.dry_run:
            print("--dry-run: stopping before any write.")
            return 0

        # Confirmation gate. Refuse non-interactive runs without --yes.
        if not args.yes:
            print("This is IRREVERSIBLE on the public Postgres copy.")
            print("The full raw text remains only in the private SQLite at:")
            print("    C:\\dev\\dg\\UFOSINT\\data\\output\\ufo_unified.db")
            print()
            try:
                typed = input(f'Type the host to confirm ({host}): ').strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return 1
            if typed != host:
                sys.exit(f"Host mismatch ('{typed}' != '{host}'). Aborted, no writes.")

        # Execute. Single transaction.
        print()
        print("Executing DROP COLUMN + DROP INDEX in one transaction...")
        t0 = time.perf_counter()
        execute_drop(conn, raw_present, indexes_present)
        print(f"  committed in {time.perf_counter() - t0:.1f}s")

        # Verify
        with conn.cursor() as cur:
            still_present = existing_columns(cur, "sighting", RAW_COLUMNS)
            if still_present:
                sys.exit(
                    f"ERROR: columns still present after drop: {still_present}. "
                    f"Investigate."
                )
            still_indexes = existing_indexes(cur, TRGM_INDEXES)
            if still_indexes:
                print(f"WARNING: indexes still present after drop: {still_indexes}")

            new_size = table_size_bytes(cur, "sighting")

        print()
        print("Sighting table size:")
        print(f"  before:  {fmt_bytes(current_size):>12}")
        print(f"  after:   {fmt_bytes(new_size):>12}  "
              f"(delta {fmt_bytes(current_size - new_size)})")
        if new_size >= current_size * 0.95:
            print("  Note: disk size barely changed because PG keeps dead tuples in")
            print("  the heap until VACUUM FULL. Re-run with --vacuum-full to actually")
            print("  reclaim space, or run VACUUM FULL sighting manually later.")

    if args.vacuum_full:
        vacuum_full_sighting(args.pg)
        # Re-measure with a fresh connection
        with psycopg.connect(args.pg, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                final_size = table_size_bytes(cur, "sighting")
        print()
        print(f"Sighting table size after VACUUM FULL: {fmt_bytes(final_size)}")
        print(f"  total reclaimed vs. before-drop:     {fmt_bytes(current_size - final_size)}")

    print()
    print("=" * 70)
    print("  STRIP COMPLETE")
    print("=" * 70)
    print("  Bump the app's data ETag so client caches re-fetch the new schema.")
    print("  The full raw text is preserved only in the private SQLite at:")
    print("    C:\\dev\\dg\\UFOSINT\\data\\output\\ufo_unified.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
