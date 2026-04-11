# v0.8.5 — Movement classification + rebalanced quality scoring

## TL;DR

Science team delivered v0.8.3b: two new sighting columns
(`has_movement_mentioned`, `movement_categories`) plus a rebalanced
`quality_score` formula that shifts the High Quality filter from
~139k to **118,320** rows. The data is in a clean public SQLite at
`data/output/ufo_public.db` (raw text already stripped).

v0.8.5 (app side) ships the wire-format changes and UI controls to
surface the new fields. The data reload itself is a manual one-time
step by the operator — v0.8.5 is graceful to the NULL / NOT-NULL
state of the new columns via the existing `/api/points-bulk`
column-probe pattern from v0.8.2.

**Critical property**: migrating from `ufo_public.db` instead of
`ufo_unified.db` means raw text (`description`, `summary`, `notes`,
`raw_json`) never reaches Azure Postgres at all. This is **stronger
than v0.8.3's `strip_raw_for_public.py` plan**: v0.8.3 stripped
raw text POST-migration; v0.8.5 prevents it from ever being there
in the first place. `strip_raw_for_public.py` is still available as
a safety backstop but is no longer part of the normal pipeline.

## What the science team shipped (v0.8.3b data layer)

| Field                       | Type           | Semantics                                                                 |
| --------------------------- | -------------- | ------------------------------------------------------------------------- |
| `has_movement_mentioned`    | SMALLINT 0/1   | 1 if narrative mentioned any movement signal; 249,217 rows (40.6%)       |
| `movement_categories`       | TEXT (JSON)    | Deduped JSON array of matched categories, e.g. `["hovering","vanished"]` |

Ten movement categories (order fixed so we can bit-pack):

| Bit | Category     |
| --- | ------------ |
| 0   | hovering     |
| 1   | linear       |
| 2   | erratic      |
| 3   | accelerating |
| 4   | rotating     |
| 5   | ascending    |
| 6   | descending   |
| 7   | vanished     |
| 8   | followed     |
| 9   | landed       |

Rebalanced `quality_score` formula weights `has_description`,
`has_media`, `num_witnesses`, and `has_movement_mentioned` more
heavily. NULL-date rows with rich text get a relaxed UNKNOWN_DATE_CAP
for the NICAP / historical carve-out. Distribution shifts:

| Bucket  | v0.8.2      | v0.8.3b      |
| ------- | ----------- | ------------ |
| 0–19    | ~171,000    | 142,072      |
| 20–39   | ~155,000    | 151,375      |
| 40–59   | ~178,000    | 202,738      |
| 60–79   | ~95,000     | **108,117**  |
| 80–100  | ~16,000     | 10,203       |
| **≥60** | ~138,863    | **118,320**  |

That **118,320** is the headline number to verify post-deploy.

## Binary schema bump — v082-1 → v083-1

Current v082-1 row is **28 bytes** (aligned to 4 at row boundaries
because 28 = 7 × 4). We need to add:

- `has_movement_mentioned` — a single bit; fits in the existing
  `flags` byte (bit 0 = has_desc, bit 1 = has_media, **bit 2 = has_movement**)
- `movement_categories` — 10-bit bitmask packed into a uint16

Adding a uint16 at the end would make the row 30 bytes, which is
**not** 4-byte aligned (30 % 4 = 2). The next row's `id` at offset
30 would fall on an unaligned address, hurting V8's optimized
Uint32Array reads. Options:

1. **Pad to 32 bytes** — adds 2 bytes of reserved space for future
   growth (topic_id, duration_bucket when SCI-4 lands, etc.).
   Stays 4-byte aligned.
2. **Drop `duration_log2`** (currently 0% coverage because
   importers never write `duration_seconds`, see SCI-4 in
   `docs/V083_BACKLOG.md`) and use those 2 bytes for
   `movement_flags`. Keeps 28 bytes.

Going with option 1 — **32 bytes per row**. `duration_log2` stays
in place even though it's empty today, because the importer fix is
tracked and will populate it eventually. The extra 2 reserved bytes
accommodate `topic_id` (v0.9) without another binary bump.

### New 32-byte layout

```
Offset  Size  Type      Name                Notes
0       4     uint32    id                  sighting.id
4       4     float32   lat
8       4     float32   lng
12      4     uint32    date_days           days since 1900-01-01, 0 = unknown
16      1     uint8     source_idx          into sources[]
17      1     uint8     shape_idx           into shapes[] (standardized)
18      1     uint8     quality_score       0-100, 255 = unknown
19      1     uint8     hoax_score          0-100, 255 = unknown
20      1     uint8     richness_score      0-100, 255 = unknown
21      1     uint8     color_idx           into colors[]
22      1     uint8     emotion_idx         into emotions[]
23      1     uint8     flags               bit0=has_desc, bit1=has_media, bit2=has_movement
24      1     uint8     num_witnesses       clamped 0-255
25      1     uint8     _reserved
26      2     uint16    duration_log2       log2(sec+1) rounded, 0 = unknown
28      2     uint16    movement_flags      NEW: 10-bit bitmask of movement categories
30      2     uint16    _reserved2          future: topic_id or another flag byte
```

Struct format: `<IffIBBBBBBBBBBHHH` (was `<IffIBBBBBBBBBBH`) — 32 bytes.

At 396,240 rows: **12.7 MB raw**, estimated **~5 MB gzipped**.
v0.8.0 plan had a 5 MB budget; we're at the ceiling.

### Server-side packing

`_points_bulk_build()` needs:

1. Add `has_movement_mentioned` + `movement_categories` to the
   `_POINTS_BULK_DERIVED_COLS` tuple so the column probe picks them
   up.
2. SELECT the new columns via `_col_expr()` — which already handles
   `NULL AS col_name` for schemas that don't have the columns yet.
3. Parse `movement_categories` (TEXT JSON) into a Python list, map
   each category to its bit via a module-level `_MOVEMENT_CATS`
   tuple, OR the bits together.
4. Set `flags |= 0x04` (bit 2) when `has_movement_mentioned == 1`.
5. Pack the resulting uint16 + reserved uint16 into the struct.
6. Bump `_POINTS_BULK_SCHEMA_VERSION` from `"v082-1"` → `"v083-1"`
   so every browser cache invalidates.
7. Bump `_POINTS_BULK_BYTES_PER_ROW` from 28 to 32.
8. Update the meta sidecar's `fields` descriptor to include the
   new `movement_flags` entry + the `movements` lookup (the 10
   category names).
9. Update the coverage map: new `has_movement` and `movement_flags`
   keys.

### Client-side deserialisation

`deck.js loadBulkPoints()` needs new typed arrays:

```js
POINTS.hasMovement    = new Uint8Array(N);  // bit 2 of flags, extracted for fast filter
POINTS.movementFlags  = new Uint16Array(N); // bit-packed categories
POINTS.movements      = meta.movements;     // lookup: ["hovering", "linear", ...]
```

The hot deserialisation loop gets one more `getUint16(o + 28, true)`
call per row. At 396k rows that's +3 ms over the current ~40 ms
deserialise — negligible.

### Client-side filter predicate

`_rebuildVisible` gets two new filter fields:

- `hasMovement: true | false | null` — bit 2 of `flags`
- `movementAny: number[] | null` — array of category names; the
  loop checks `(movementFlags[i] & mask) !== 0` where `mask` is
  OR'd from the resolved indices at filter-apply time
- `movementAll: number[] | null` — same but `&` mask equality

v0.8.5 ships `hasMovement` only. `movementAny` / `movementAll`
land in v0.8.6 if the operator wants category-level filters.

## UI — Quality rail "Movement" section

New section added below the "Data Quality" rail section:

```
┌─ MOVEMENT ─────────────────────────────┐
│ ☐ Has movement described               │
│   narrative mentions hovering /         │
│   landing / erratic / ...               │
└─────────────────────────────────────────┘
```

Just one toggle for v0.8.5. The coverage indicator (249,217 rows
= 40.6% when the data migration has run, 0 otherwise) lights up /
greys out the toggle the same way v0.8.2 handles the Quality /
Hoax toggles.

The full 10-category picker would add 10 checkboxes to the rail
and would need collapse UI — scope-deferred.

## UI — Detail modal movement chips

Extend the Derived Metadata section in `openDetail()` with a
"Movement" row that shows chips for each category in the array:

```
Movement:   hovering · vanished · followed
```

Rendered as `<span class="movement-chip">` elements with the
same token-based `--accent` styling as the existing shape-tag +
has-desc badges. Empty array → hide the row.

## `/api/sighting/:id` — explicit SELECT update

Add `s.has_movement_mentioned` and `s.movement_categories` to the
`_SIGHTING_DETAIL_COLUMNS` tuple. Parse the JSON server-side and
return as a real JSON array on the wire:

```json
{
  ...
  "has_movement_mentioned": 1,
  "movement_categories": ["hovering", "followed", "vanished"]
}
```

## Deploy workflow

Add `scripts/add_v083_derived_columns.sql` to the sparse-checkout
list and add a new step after "Apply v0.8.2 derived-field migration":

```yaml
- name: Apply v0.8.3b movement-fields migration
  env:
    DATABASE_URL: ${{ secrets.DATABASE_URL }}
  run: |
    set -euo pipefail
    if [ -z "${DATABASE_URL:-}" ]; then
      echo "::warning::DATABASE_URL not set — skipping v0.8.3b migration"
      exit 0
    fi
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/scripts/add_v083_derived_columns.sql
```

This means the schema migration runs automatically on next deploy.
The data reload (TRUNCATE + migrate) is still a manual operator step.

## Deploy sequence (operator-facing)

1. **Ship v0.8.5 app code** (this plan) — the deploy workflow auto-
   applies `add_v083_derived_columns.sql`, adding the 2 new columns
   to PG as NULL. Existing v082-1 browser caches keep working
   because the new `/api/points-bulk` endpoint ships the v083-1
   binary schema with ALL rows showing `has_movement_mentioned = 0`
   and `movement_flags = 0` until step 2 runs.

2. **Operator runs the manual data reload** from
   `data/output/ufo_public.db` (science team's clean public SQLite):

   ```bash
   export DATABASE_URL='postgresql://ufosint_admin:<password>@ufosint-pg.postgres.database.azure.com:5432/ufo_unified?sslmode=require'

   # Drop the date_correction FK so we can TRUNCATE sighting
   psql "$DATABASE_URL" <<'SQL'
   BEGIN;
   ALTER TABLE date_correction DROP CONSTRAINT date_correction_sighting_id_fkey;
   TRUNCATE
       source_collection, source_database, source_origin,
       location, reference, sighting, attachment,
       sighting_reference, duplicate_candidate, sentiment_analysis
   RESTART IDENTITY CASCADE;
   COMMIT;
   SQL

   # Migrate the clean public SQLite → PG (~15 minutes on B2)
   cd ufosint-explorer
   python scripts/migrate_sqlite_to_pg.py \
       --sqlite ../../data/output/ufo_public.db \
       --pg "$DATABASE_URL"

   # Re-add the FK as NOT VALID (preserves the 714 historical corrections
   # without re-validating against the new sighting IDs)
   psql "$DATABASE_URL" -c "
   ALTER TABLE date_correction
       ADD CONSTRAINT date_correction_sighting_id_fkey
       FOREIGN KEY (sighting_id) REFERENCES sighting(id)
       NOT VALID;"
   ```

3. **Verify headline numbers**:
   ```sql
   SELECT COUNT(*) AS total                   FROM sighting;                              -- 614,505
   SELECT COUNT(*) AS high_quality            FROM sighting WHERE quality_score >= 60;    -- 118,320
   SELECT COUNT(*) AS with_movement           FROM sighting WHERE has_movement_mentioned = 1; -- 249,217
   SELECT COUNT(*) AS with_coords             FROM sighting WHERE lat IS NOT NULL AND lng IS NOT NULL; -- 396,240
   SELECT COUNT(*) AS with_standardized_shape FROM sighting WHERE standardized_shape IS NOT NULL; -- 236,463
   ```

4. **Bump the data ETag** — happens automatically. The
   `_points_bulk_etag()` function reads the current row count + max
   id + column set, all of which change after the reload. Browser
   caches invalidate on next fetch.

5. **Verify live** — hard-refresh Observatory, toggle the new
   "Has movement described" rail toggle, confirm marker count drops
   from ~396k to ~249k (matches `has_movement_mentioned = 1` count).

6. **Strip script status**: `scripts/strip_raw_for_public.py` is
   NO LONGER NEEDED for the v0.8.3b cutover. Migrating from
   `ufo_public.db` means the raw text columns never get into PG in
   the first place. The script is kept in-tree as a safety
   backstop in case someone accidentally migrates from
   `ufo_unified.db` (the private one).

## Answers to the science team's questions

**Q1: Migrate from `ufo_public.db` as the long-term workflow?**

Yes — strictly safer. The private `ufo_unified.db` has raw text
that shouldn't reach PG; the public one doesn't. Always migrate
from the public copy. I'll update `docs/ARCHITECTURE.md` and
`docs/DEPLOYMENT.md` to note this.

**Q2: Front-end filter for movement fields?**

v0.8.5 ships the "Has movement described" toggle in the Quality
rail. Category-level filters (filter by "hovering" specifically,
etc.) require a more complex UI and are deferred to v0.8.6 unless
the operator wants them sooner. The detail modal shows the full
category array as chips so users can at least SEE which
categories applied to a specific sighting.

## Non-goals

- **No changes to quality-score logic in the app.** The
  rebalanced formula is entirely server-side (in
  `ufo-dedup/analyze.py`); the app just reads whatever integer is
  in the column.
- **No changes to the `strip_raw_for_public.py` safety script.**
  It still works, still has the coverage safety probe, and is
  still the right tool if someone ever needs it. Just not part of
  the normal v0.8.5 flow.
- **No changes to the CHANGELOG sync with ufo-dedup.** The
  science-team repo has its own changelog; this one tracks the app
  side. A cross-reference note is fine.
- **No `sighting_analysis` table migration.** Still v0.9 scope per
  `docs/V083_BACKLOG.md` APP-1.
- **No new Insights page cards for movement counts.** Stretch goal;
  easier to ship separately once we know what resonates with users.

## Tests

New `tests/test_v085_movement.py` with:

- Plan doc exists + covers key concepts
- CHANGELOG has [0.8.5] section
- `scripts/add_v083_derived_columns.sql` is wired into
  `azure-deploy.yml` and runs after `add_v082_derived_columns.sql`
- `_POINTS_BULK_SCHEMA_VERSION == "v083-1"`
- `_POINTS_BULK_BYTES_PER_ROW == 32`
- `_POINTS_BULK_STRUCT` is the new 32-byte format string
- `_POINTS_BULK_DERIVED_COLS` includes `has_movement_mentioned`
  and `movement_categories`
- `_MOVEMENT_CATS` module constant exists with the 10 categories
  in bit order
- `_SIGHTING_DETAIL_COLUMNS` includes the two new fields
- Binary round-trip against fake DB: size is count × 32,
  `movement_flags` bit-mask matches expected values for scripted
  rows
- `deck.js` declares `POINTS.hasMovement` and `POINTS.movementFlags`
  typed arrays
- `deck.js` filter predicate reads `hasMovement`
- `app.js` Quality rail has a "Has movement" entry in the `toggles`
  array
- `openDetail()` detail modal renders movement category chips
- CSS has a `.movement-chip` rule

## Risks

| Risk | Mitigation |
|---|---|
| Operator runs migration from wrong SQLite file (`ufo_unified.db` instead of `ufo_public.db`) | Raw text would reach PG; `strip_raw_for_public.py` is available as the cleanup path |
| Binary schema bump breaks v082-1 cached clients | ETag change forces cache invalidation; existing cache-control headers handle the rest |
| Row count at 32 bytes pushes past 5 MB gzipped | At 12.7 MB raw, estimated ~4.5 MB gzipped. Under budget. |
| `movement_categories` JSON parse fails on a row | Defensive try/except in the packer; failed rows get `movement_flags = 0` and continue |
| New Quality rail toggle is confusing for users | Clear label + subtitle ("narrative mentions hovering / landing / ...") |
| Unknown movement category in the source data | Packer logs a warning but keeps going; the bitmask just won't have that bit set |

## Acceptance criteria

1. `curl /api/points-bulk?meta=1 | jq '.schema_version'` returns `"v083-1"`.
2. `curl /api/points-bulk?meta=1 | jq '.schema.bytes_per_row'` returns `32`.
3. `curl /api/points-bulk?meta=1 | jq '.movements'` returns the 10-category array (when columns populated).
4. Binary payload is exactly `count * 32` bytes after gunzip.
5. After the data reload: `curl /api/points-bulk?meta=1 | jq '.coverage.has_movement'` ≈ 249,217 (expect some drift from non-geocoded rows).
6. Browser: "Has movement described" toggle in the Quality rail enables once coverage > 0.
7. Browser: detail modal shows movement chips for a sighting with `movement_categories = ["hovering", "vanished"]`.
8. 285+ tests green (v0.8.4's 285 + v0.8.5's new ones).
