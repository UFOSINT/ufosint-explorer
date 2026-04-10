#!/usr/bin/env python3
"""
Compute H3 hex-bin aggregates for the UFO sightings DB.

Usage:
    DATABASE_URL='postgresql://...' python scripts/compute_hex_bins.py

Why this exists
---------------
The `/api/hexbin` endpoint in app.py serves pre-computed hex-bin counts
so the Observatory map can render Points / Heatmap / HexBin modes at
any zoom level in milliseconds. Computing H3 cells on the fly would
require the Postgres `h3` extension — NOT available on Azure Database
for PostgreSQL Flexible Server (B1ms). So we compute cells in Python
at deploy time, store them in a support table + materialized view,
and the runtime container never imports h3.

Flow
----
1. Create `location_hex` table if missing — one row per `location`
   row, carrying H3 cell IDs and boundaries at resolutions 2..6.
2. Incremental populate: only process rows that don't already have
   an entry in `location_hex`. Re-running is cheap.
3. Drop and rebuild the `hex_bin_counts` materialized view that
   aggregates sightings per (res, cell, source, shape, decade).
4. Create supporting indexes on the MV.
5. Print row counts for the smoke output.

This script is only invoked by .github/workflows/compute-hex-bins.yml
(manual workflow_dispatch) or by a developer running it locally. It
requires the `h3` Python library (installed via requirements-deploy.txt),
which the runtime container does not have.
"""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterable

try:
    import h3
except ImportError as e:  # pragma: no cover - explicit error for the runner
    sys.stderr.write(
        "Error: the `h3` Python library is required. Run:\n"
        "    pip install -r requirements-deploy.txt\n"
    )
    raise SystemExit(1) from e

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        "Error: psycopg is required. Run:\n"
        "    pip install -r requirements-deploy.txt\n"
    )
    raise SystemExit(1) from e

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RESOLUTIONS: tuple[int, ...] = (2, 3, 4, 5, 6)
BATCH_SIZE = 2000

LOCATION_HEX_DDL = """
CREATE TABLE IF NOT EXISTS location_hex (
    location_id     BIGINT PRIMARY KEY REFERENCES location(id) ON DELETE CASCADE,
    res_2           TEXT NOT NULL,
    res_3           TEXT NOT NULL,
    res_4           TEXT NOT NULL,
    res_5           TEXT NOT NULL,
    res_6           TEXT NOT NULL,
    boundary_json_2 JSONB,
    boundary_json_3 JSONB,
    boundary_json_4 JSONB,
    boundary_json_5 JSONB,
    boundary_json_6 JSONB,
    computed_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_location_hex_r2 ON location_hex(res_2);
CREATE INDEX IF NOT EXISTS idx_location_hex_r3 ON location_hex(res_3);
CREATE INDEX IF NOT EXISTS idx_location_hex_r4 ON location_hex(res_4);
CREATE INDEX IF NOT EXISTS idx_location_hex_r5 ON location_hex(res_5);
CREATE INDEX IF NOT EXISTS idx_location_hex_r6 ON location_hex(res_6);
"""

# The MV is rebuilt from scratch on every run, so it picks up any new
# sightings loaded since the last compute. CROSS JOIN LATERAL VALUES is
# the cleanest way to turn the wide `location_hex` row (one row with 5
# cell columns) into 5 rows (one per resolution).
HEX_BIN_COUNTS_DDL = """
DROP MATERIALIZED VIEW IF EXISTS hex_bin_counts;
CREATE MATERIALIZED VIEW hex_bin_counts AS
SELECT r.res,
       r.h3_cell,
       s.source_db_id,
       s.shape,
       CASE
           WHEN LENGTH(s.date_event) >= 4
                AND substring(s.date_event FROM 1 FOR 4) ~ '^[0-9]{4}$'
           THEN (substring(s.date_event FROM 1 FOR 4)::int / 10) * 10
           ELSE NULL
       END AS decade,
       COUNT(*)::int                         AS cnt,
       AVG(l.latitude)::double precision     AS lat,
       AVG(l.longitude)::double precision    AS lng,
       (array_agg(r.boundary_json))[1]       AS boundary_json
  FROM sighting s
  JOIN location l ON l.id = s.location_id
  JOIN location_hex lh ON lh.location_id = l.id
  CROSS JOIN LATERAL (VALUES
    (2, lh.res_2, lh.boundary_json_2),
    (3, lh.res_3, lh.boundary_json_3),
    (4, lh.res_4, lh.boundary_json_4),
    (5, lh.res_5, lh.boundary_json_5),
    (6, lh.res_6, lh.boundary_json_6)
  ) AS r(res, h3_cell, boundary_json)
  WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
    AND s.date_event IS NOT NULL
 GROUP BY r.res, r.h3_cell, s.source_db_id, s.shape, decade;

CREATE INDEX idx_hex_bin_res_cell   ON hex_bin_counts(res, h3_cell);
CREATE INDEX idx_hex_bin_res_source ON hex_bin_counts(res, source_db_id);
CREATE INDEX idx_hex_bin_res_shape  ON hex_bin_counts(res, shape);
CREATE INDEX idx_hex_bin_res_decade ON hex_bin_counts(res, decade);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_row(location_id: int, lat: float, lng: float) -> tuple:
    """Compute H3 cells + boundaries for one (lat, lng) at all resolutions.

    Returns a tuple matching the location_hex column order. Boundaries
    are stored as JSON arrays of [lat, lng] pairs (6 points per hex).
    """
    cells = {}
    boundaries = {}
    for r in RESOLUTIONS:
        cell = h3.latlng_to_cell(lat, lng, r)
        cells[r] = cell
        # cell_to_boundary returns a tuple of (lat, lng) points
        boundaries[r] = [list(pt) for pt in h3.cell_to_boundary(cell)]

    return (
        location_id,
        cells[2], cells[3], cells[4], cells[5], cells[6],
        Jsonb(boundaries[2]),
        Jsonb(boundaries[3]),
        Jsonb(boundaries[4]),
        Jsonb(boundaries[5]),
        Jsonb(boundaries[6]),
    )


def _batched(rows: Iterable, n: int):
    """Yield successive n-sized chunks from an iterable."""
    batch: list = []
    for row in rows:
        batch.append(row)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL env var is required", file=sys.stderr)
        return 1

    print("Connecting to database…")
    with psycopg.connect(db_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            # 1. Ensure support table exists
            print("Ensuring location_hex table exists…")
            cur.execute(LOCATION_HEX_DDL)
            conn.commit()

            # 2. Find locations that still need cells
            print("Counting locations to process…")
            cur.execute("""
                SELECT COUNT(*)
                  FROM location l
                 WHERE l.latitude IS NOT NULL
                   AND l.longitude IS NOT NULL
                   AND l.latitude BETWEEN -90 AND 90
                   AND l.longitude BETWEEN -180 AND 180
                   AND NOT EXISTS (
                       SELECT 1 FROM location_hex h WHERE h.location_id = l.id
                   )
            """)
            todo = cur.fetchone()[0]
            print(f"  locations still needing H3 computation: {todo:,}")

            if todo == 0:
                print("  nothing to do for location_hex — skipping compute step")
            else:
                # Use a server-side cursor for memory safety on large row counts
                t_start = time.time()
                processed = 0

                with conn.cursor(name="loc_stream") as stream:
                    stream.itersize = 5000
                    stream.execute("""
                        SELECT l.id, l.latitude, l.longitude
                          FROM location l
                         WHERE l.latitude IS NOT NULL
                           AND l.longitude IS NOT NULL
                           AND l.latitude BETWEEN -90 AND 90
                           AND l.longitude BETWEEN -180 AND 180
                           AND NOT EXISTS (
                               SELECT 1 FROM location_hex h WHERE h.location_id = l.id
                           )
                    """)

                    def row_gen():
                        for (lid, lat, lng) in stream:
                            yield _compute_row(lid, float(lat), float(lng))

                    insert_sql = """
                        INSERT INTO location_hex
                            (location_id, res_2, res_3, res_4, res_5, res_6,
                             boundary_json_2, boundary_json_3, boundary_json_4,
                             boundary_json_5, boundary_json_6)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (location_id) DO NOTHING
                    """

                    with conn.cursor() as writer:
                        for batch in _batched(row_gen(), BATCH_SIZE):
                            writer.executemany(insert_sql, batch)
                            processed += len(batch)
                            pct = processed / todo * 100
                            rate = processed / max(time.time() - t_start, 0.001)
                            print(
                                f"  {processed:>7,} / {todo:,} "
                                f"({pct:5.1f}%)  {rate:,.0f} rows/s"
                            )
                            conn.commit()

                print(f"  location_hex populated in {time.time() - t_start:.1f}s")

            # 3. Rebuild the materialized view
            print("Rebuilding hex_bin_counts materialized view…")
            t_mv = time.time()
            cur.execute(HEX_BIN_COUNTS_DDL)
            conn.commit()
            print(f"  MV rebuilt in {time.time() - t_mv:.1f}s")

            # 4. Report row counts
            cur.execute("SELECT COUNT(*) FROM location_hex")
            print(f"  location_hex rows:    {cur.fetchone()[0]:,}")

            cur.execute("SELECT COUNT(*) FROM hex_bin_counts")
            print(f"  hex_bin_counts rows:  {cur.fetchone()[0]:,}")

            cur.execute(
                "SELECT res, COUNT(*) FROM hex_bin_counts GROUP BY res ORDER BY res"
            )
            for res, n in cur.fetchall():
                print(f"    res {res}: {n:,} cell/source/shape/decade rows")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
