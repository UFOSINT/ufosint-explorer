# v0.8.2 — Derived public fields + raw-text retirement

## TL;DR

The science team extended the private `ufo-dedup` SQLite schema with a
batch of derived analysis fields (`quality_score`, `hoax_likelihood`,
`standardized_shape`, `primary_color`, `dominant_emotion`,
`richness_score`, `sighting_datetime`, `has_description`, `has_media`,
`topic_id`). v0.8.2 plumbs those fields through the Azure Postgres
schema, the `/api/points-bulk` binary payload, `deck.js`, and the
Observatory UI — without breaking the app when the columns haven't
been populated yet, and without exposing any raw report text.

## The data flow

```
  ┌───────────────────────────────┐       ┌──────────────────────────────┐
  │  Private machine (user-only)  │       │  Azure Postgres (public)     │
  │  • ufo-dedup                  │       │  • ufosint-explorer          │
  │  • rebuild_db.py              │       │  • /api/points-bulk          │
  │  • analyze.py (sentiment,     │       │  • deck.gl client            │
  │    NRCLex, hoax, quality)     │       │  • Legal-clean: NO raw       │
  │  • SQLite with raw text       │──┐    │    description/summary/notes │
  │    + every derived field      │  │    │                              │
  └───────────────────────────────┘  │    └──────────────────────────────┘
                                     │                ▲
                          strip_raw_for_public.py     │
                                     │                │
                                     ▼                │
                          ┌─────────────────────┐     │
                          │  "public" SQLite    │     │
                          │  • Raw text DROPPED │     │
                          │  • Derived fields   │     │
                          │    kept             │     │
                          └─────────────────────┘     │
                                     │                │
                     migrate_sqlite_to_pg.py ─────────┘
                          (runs the ALTER TABLE
                           migration first, then
                           streams rows via COPY)
```

The Postgres side **never holds** the raw `description` / `summary` /
`notes` / `raw_json` columns after the cutover. They're not in the
wire format, not in the SQL SELECT lists, not in the Kudu-stored
artifacts, not in any deploy log. The legal story is simple: the
public infrastructure literally doesn't have the raw reports.

## Which fields actually exist where

Audited the live `/api/sighting/136613` response on production, plus
the source files. Status as of v0.8.1:

| Field                | In ufo-dedup SQLite | In Azure Postgres | In /api/points-bulk | In deck.js filter |
| -------------------- | :-----------------: | :---------------: | :-----------------: | :---------------: |
| id                   | ✅                  | ✅                | ✅ (v0.8.0)         | ✅                |
| lat, lng             | ✅ (denorm)         | ✅ (via location) | ✅ (v0.8.0)         | ✅                |
| source_id            | ✅ as source_db_id  | ✅                | ✅ (v0.8.0)         | ✅                |
| sighting_datetime    | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| standardized_shape   | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| quality_score        | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| richness_score       | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| has_description      | ✅ (new)            | ✅ (computed inline) | ❌ (year byte only) | ❌            |
| hoax_likelihood      | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| primary_color        | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| dominant_emotion     | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| duration_seconds     | ✅                  | ✅                | ❌                  | ❌                |
| num_witnesses        | ✅                  | ✅                | ❌                  | ❌                |
| has_media            | ✅ (new)            | **❌**            | **❌**              | **❌**            |
| topic_id (reserved)  | ✅ (new, empty)     | ❌                | ❌                  | ❌                |
| description (raw)    | ✅                  | ✅                | n/a (never shipped) | n/a               |
| summary (raw)        | ✅                  | ✅                | n/a                 | n/a               |

**Nine new sighting columns** need to land in Postgres. **Four raw
text columns** need to be dropped when the user signs off.

## The plan

### Phase 1 — Postgres schema migration (server)

**New file: `scripts/add_v082_derived_columns.sql`**

Idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for every new
sighting column, plus the supporting indexes. The migration is safe
to run on a schema that's already been upgraded (it's a no-op) and on
a schema that hasn't (adds the columns with NULL defaults).

```sql
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS lat                 DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS lng                 DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS sighting_datetime   TEXT,
    ADD COLUMN IF NOT EXISTS standardized_shape  TEXT,
    ADD COLUMN IF NOT EXISTS primary_color       TEXT,
    ADD COLUMN IF NOT EXISTS dominant_emotion    TEXT,
    ADD COLUMN IF NOT EXISTS quality_score       SMALLINT,
    ADD COLUMN IF NOT EXISTS richness_score      SMALLINT,
    ADD COLUMN IF NOT EXISTS hoax_likelihood     REAL,
    ADD COLUMN IF NOT EXISTS has_description     SMALLINT,
    ADD COLUMN IF NOT EXISTS has_media           SMALLINT,
    ADD COLUMN IF NOT EXISTS topic_id            INTEGER;

CREATE INDEX IF NOT EXISTS idx_sighting_quality      ON sighting(quality_score);
CREATE INDEX IF NOT EXISTS idx_sighting_hoax         ON sighting(hoax_likelihood);
CREATE INDEX IF NOT EXISTS idx_sighting_std_shape    ON sighting(standardized_shape);
CREATE INDEX IF NOT EXISTS idx_sighting_dom_emotion  ON sighting(dominant_emotion);
CREATE INDEX IF NOT EXISTS idx_sighting_datetime     ON sighting(sighting_datetime);
CREATE INDEX IF NOT EXISTS idx_sighting_has_desc_new ON sighting(has_description);
CREATE INDEX IF NOT EXISTS idx_sighting_has_media    ON sighting(has_media);
CREATE INDEX IF NOT EXISTS idx_sighting_topic        ON sighting(topic_id);
```

**Deploy workflow integration:** the existing `.github/workflows/azure-deploy.yml`
already runs `add_v075_materialized_views.sql`. We add the
v0.8.2 migration to the sparse-checkout step and apply it right
after the v0.7 index migration (before the MV refresh).

The migration **does not** populate the new columns. They start
NULL and stay NULL until the next `migrate_sqlite_to_pg.py` run from
the user's private machine. That's by design — the app must be
robust to NULL values in these columns.

### Phase 2 — Application robustness to NULL-or-missing columns

The key observation: `/api/points-bulk` SHOULD ship v2 of the binary
schema regardless of whether the Postgres columns are populated.
When columns are NULL:

- `quality_score`, `richness_score`, `hoax_likelihood` → encoded as
  `255` in the uint8 slot (sentinel meaning "unknown")
- `standardized_shape` → idx 0 (unknown)
- `primary_color`, `dominant_emotion` → idx 0 (unknown)
- `sighting_datetime` → `date_days = 0` (unknown), falls back to
  `year` from `date_event` parsing
- `has_description`, `has_media` → bit 0 in the `flags` byte

The meta sidecar reports per-field **coverage** (e.g.
`"quality_score_populated": 0` when nothing's populated yet, or
`"quality_score_populated": 396100` when everything is). The
frontend checks coverage before showing the filter UI — a filter
toggle that would hide every point is disabled with a tooltip
explaining "no rows have this field populated yet".

**Column probe at startup:** on first call, the endpoint runs one
query:

```sql
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'sighting'
  AND column_name IN (
    'lat','lng','sighting_datetime','standardized_shape',
    'primary_color','dominant_emotion','quality_score',
    'richness_score','hoax_likelihood','has_description',
    'has_media','topic_id'
  )
```

The result is cached in a module-level set and drives the SELECT
clause construction. Columns that don't exist yet get `NULL AS
col_name` inserted into the SELECT so the row tuple stays the same
shape regardless of schema state.

This means **the app ships v0.8.2 without the migration running**
and stays functional. When the migration finally lands (on the next
tag deploy that includes it), the existing `@lru_cache` invalidates
on the new ETag and the next request picks up the new columns
seamlessly.

### Phase 3 — Binary schema v2

Row size: **28 bytes**, little-endian, 4-byte-aligned id/lat/lng/
date_days at the front.

| Offset | Size | Type     | Name            | Notes                                       |
| ------ | ---- | -------- | --------------- | ------------------------------------------- |
| 0      | 4    | uint32   | id              | sighting.id                                 |
| 4      | 4    | float32  | lat             | WGS84                                       |
| 8      | 4    | float32  | lng             | WGS84                                       |
| 12     | 4    | uint32   | date_days       | days since 1900-01-01 (0 = unknown)         |
| 16     | 1    | uint8    | source_idx      | into `sources` lookup (0 = unknown)         |
| 17     | 1    | uint8    | std_shape_idx   | into `shapes` lookup (0 = unknown)          |
| 18     | 1    | uint8    | quality_score   | 0–100; **255 = unknown**                    |
| 19     | 1    | uint8    | hoax_score      | 0–100; **255 = unknown** (from 0.0–1.0 × 100)|
| 20     | 1    | uint8    | richness_score  | 0–100; **255 = unknown**                    |
| 21     | 1    | uint8    | color_idx       | into `colors` lookup (0 = unknown)          |
| 22     | 1    | uint8    | emotion_idx     | into `emotions` lookup (0 = unknown)        |
| 23     | 1    | uint8    | flags           | bit0=has_desc bit1=has_media bit2-7=reserved|
| 24     | 1    | uint8    | num_witnesses   | clamped 0–255                               |
| 25     | 1    | uint8    | reserved        | (for future growth)                         |
| 26     | 2    | uint16   | duration_log2   | log2(sec+1) rounded; 0 = unknown            |

Row size: 28 bytes. At 396k rows: **10.9 MB raw**, estimated
**~4 MB gzipped** (slightly bigger than v0.8.0's 2.85 MB but still
well under the v0.8.0 plan's 5 MB target).

**Why log-scale duration?** Durations range from 1 second to days.
Linear uint32 wastes resolution where users care (1s–10min). A
log2 scale gives ~1% precision across the whole span in 16 bits.
`JS: seconds = Math.pow(2, duration_log2) - 1`.

**Why 255-sentinel for scores?** uint8 has a clean out-of-band
value (scores only go 0–100). Keeps the hot filter loop branchless
(`if (quality[i] < threshold) continue` — rows with `255` are
automatically excluded by any `>= threshold` test, which is exactly
the semantic we want: unknown scores fail quality gates).

### Phase 4 — Meta sidecar v2

The `?meta=1` response gains new fields:

```json
{
  "count": 396100,
  "etag": "v082-1-396100-614491-cols=12",
  "schema_version": "v082-1",
  "sources": [null, "MUFON", "NUFORC", "UFOCAT", "UFO-search", "UPDB"],
  "shapes":  [null, "circle", "triangle", "disk", "light", ...],
  "colors":  [null, "white", "red", "orange", "blue", ...],
  "emotions": [null, "fear", "surprise", "joy", "anger", "sadness", ...],
  "coverage": {
    "date_days":        396080,
    "quality_score":         0,
    "hoax_score":            0,
    "richness_score":        0,
    "std_shape_idx":         0,
    "color_idx":             0,
    "emotion_idx":           0,
    "has_description":  396100,
    "has_media":        396100,
    "num_witnesses":    187200,
    "duration_log2":    142300
  },
  "columns_present": {
    "standardized_shape": false,
    "quality_score":      false,
    "...":               "..."
  },
  "raw_size": 11090800,
  "gzip_size": 4200000,
  "schema": { ... layout descriptor ... }
}
```

The frontend uses `coverage` to decide which filter UI controls to
enable and which to gray out with a tooltip. `columns_present`
tells us whether the column exists at all (vs exists but empty).

### Phase 5 — deck.js v2

The deserialiser reads all 12 fields into typed arrays:

```js
POINTS = {
    id, lat, lng,           // existing
    dateDays,               // NEW Uint32Array
    sourceIdx,              // existing
    shapeIdx,               // existing (now standardized)
    qualityScore,           // NEW Uint8Array (255 = unknown)
    hoaxScore,              // NEW Uint8Array (255 = unknown)
    richnessScore,          // NEW Uint8Array
    colorIdx,               // NEW Uint8Array
    emotionIdx,             // NEW Uint8Array
    flags,                  // NEW Uint8Array
    numWitnesses,           // NEW Uint8Array
    durationLog2,           // NEW Uint16Array
    // lookups
    sources, shapes, colors, emotions,
    // metadata
    coverage, columnsPresent,
};
```

`applyClientFilters` / `_rebuildVisible` grows to handle:

- `qualityMin: number | null`           → `quality >= qualityMin && quality !== 255`
- `hoaxMax: number | null`              → `hoax <= hoaxMax && hoax !== 255`
- `stdShapeName: string | null`         → resolved to `stdShapeIdxTarget`
- `colorName: string | null`            → resolved to `colorIdxTarget`
- `emotionName: string | null`          → resolved to `emotionIdxTarget`
- `hasDescription: bool | null`         → flag bit 0 check
- `hasMedia: bool | null`               → flag bit 1 check
- `dateFrom / dateTo` (ISO days)        → `dateDays >= from && dateDays <= to`
- (existing) `sourceName`, `yearFrom/To`, `bbox`

Time window API bumps to day precision:

```js
UFODeck.setTimeWindow(dayStart, dayEnd, { cumulative })
```

Still works with year values by converting `year × 365.25` under
the hood so existing TimeBrush callers don't break.

### Phase 6 — UI toggles

Three new sidebar controls, placed above the existing filter dropdowns
in the Observatory left rail (`mountObservatoryRail`):

```
┌─ DATA QUALITY ─────────────────────────┐
│ ☐ High quality only (score ≥ 60)       │
│ ☐ Hide likely hoaxes (score > 0.5)     │
│ ☐ Has description                      │
│ ☐ Has media                            │
└────────────────────────────────────────┘
```

Toggles that reference unpopulated fields are disabled with a
tooltip: *"This filter needs the v0.8.2 derived-fields pipeline to
run. The field is present in the schema but no rows are populated
yet."*

The existing raw `shape` dropdown gets a flag: when
`coverage.std_shape_idx > 0`, it shows standardized shapes from the
v0.8.2 lookup. Otherwise it falls back to the existing raw shape
list. The dropdown label updates to "Shape" either way.

### Phase 7 — TimeBrush day precision

`TimeBrush` currently operates in milliseconds since epoch and
derives year integers for the GPU filter. v0.8.2 extends the fast
path to pass day-resolution bounds when the bulk data has
`dateDays` populated:

```js
fastPath(dayStart, dayEnd, cumulative);  // day-granular
```

`setTimeWindow()` checks which field is available:
- If `POINTS.dateDays` is populated and has coverage > 0 → day filter.
- Otherwise → year filter (v0.8.1 behaviour).

Playback step size scales inversely with span so a full sweep still
takes ~4 seconds: `stepSize = totalDays × 0.004`. At 126 years
× 365.25 = 46,000 days × 0.004 = 184 days/frame × 60 fps = 11,000
days/sec, full sweep in ~4.2 seconds. Good.

### Phase 8 — migrate_sqlite_to_pg.py update

Add the new columns to the sighting TABLES entry. The existing
`stream_table` helper already handles missing SQLite columns by
inserting `NULL`, so the script is forward-compatible with an
old SQLite DB that doesn't have the new fields.

The ufo-dedup side already adds the columns to fresh SQLite via
`create_schema.py`. So after the user runs `rebuild_db.py` on
their private machine, the resulting SQLite has the columns
populated, and the next `migrate_sqlite_to_pg.py` run streams
everything into Postgres.

### Phase 9 — Raw text column drop (deferred)

The user said *"once we sign off"*. v0.8.2 **does not** drop the
raw text columns. It just:

1. Stops reading them in /api/points-bulk (never did anyway — has_desc
   was computed inline).
2. Leaves `/api/sighting/:id` and `/api/search` untouched — they still
   use description / summary.
3. Ships `scripts/drop_raw_text_columns.sql` as an **unapplied**
   migration the user can run manually when they decide to cut over.
   It's `ALTER TABLE sighting DROP COLUMN description, summary, notes,
   raw_json, date_event_raw, time_raw`, wrapped in a BEGIN/COMMIT
   with a `SELECT has_description FROM sighting LIMIT 1` safety probe
   so it bails out if the derived fields aren't populated.
4. Adds a follow-up in `docs/V082_PLAN.md` listing the endpoints that
   need rewiring before the drop is safe:
   - `/api/search` — needs to switch to full-text-search against the
     derived fields or be disabled entirely.
   - `/api/sighting/:id` — the detail modal today shows description
     text; needs to show standardized_shape, quality_score,
     richness_score, hoax_likelihood, dominant_emotion, primary_color
     instead.
   - `/api/sentiment/overview` — the MV already stores the aggregated
     VADER / NRC scores so the underlying sentiment_analysis table is
     derived, not raw. Safe.

This is a **v0.8.3 or v0.9.0** follow-up. Out of scope for v0.8.2.

## Implementation order

1. Plan doc (this file) ✓
2. `scripts/add_v082_derived_columns.sql` — the PG ALTER migration
3. `scripts/migrate_sqlite_to_pg.py` — add new columns to the
   sighting TABLES entry
4. `.github/workflows/azure-deploy.yml` — checkout + apply the new
   migration before MV refresh
5. `app.py`:
   - `_POINTS_BULK_SCHEMA_VERSION = "v082-1"`
   - `_POINTS_BULK_BYTES_PER_ROW = 28`
   - `_POINTS_BULK_STRUCT = "<IffIBBBBBBBBBBH"` (16 fields, 28 bytes)
   - Rename to `_POINTS_BULK_COLUMNS` set + column probe helper
   - Rewrite `_points_bulk_build()` to SELECT all new columns (or
     `NULL AS ...` when the column doesn't exist)
   - Rewrite the row packer to compute `date_days`, pack scores
     with 255-sentinel, etc.
   - Extend meta sidecar with `coverage` + `columns_present` +
     new lookups
6. `static/deck.js`:
   - Bump `loadBulkPoints()` to deserialise 28-byte rows into
     the new typed arrays
   - Extend `_rebuildVisible` hot loop with the new filter
     predicates
   - Add `getCoverage()` and `getColumnsPresent()` public helpers
   - Bump `setTimeWindow()` to accept day-granular bounds
7. `static/app.js`:
   - New `mountQualityRail()` helper that builds the four
     quality/hoax/description/media checkboxes and binds them
     to `applyClientFilters`
   - Wire `coverage` into the rail so unpopulated toggles render
     as disabled
   - Update `applyClientFilters` to read the new toggles
   - Update the TimeBrush day-precision fast path
8. `static/index.html`: new `<div class="rail-quality">` section
9. `static/style.css`: styles for the quality rail
10. `tests/test_v082_derived.py`: full contract coverage
11. `CHANGELOG.md`: v0.8.2 entry
12. Commit, tag, ship

## What we're NOT doing in v0.8.2

- **No raw text drop on the public DB.** Separate migration, needs
  sign-off. Endpoints that still rely on description/summary keep
  working.
- **No sighting_analysis JSON side-fields.** Those are detail-modal
  work (`/api/sighting/:id`), not bulk-map work.
- **No topic_id filter** — the field is reserved but empty until
  v0.9. We ship it in the binary anyway so v0.9 doesn't need
  another schema bump.
- **No running the data pipeline for the user.** That's on their
  private machine. v0.8.2 is ready-to-populate infrastructure.
- **No deleting v0.8.0 code paths.** Legacy `/api/map`,
  `/api/heatmap`, `/api/hexbin` stay for one more release cycle.

## Risk register

| Risk                                          | Mitigation                                                      |
| --------------------------------------------- | --------------------------------------------------------------- |
| ALTER TABLE blocks on a large busy table      | ADD COLUMN NULL is metadata-only in PG ≥ 11. Instant.           |
| Deploy runs migration before columns exist in ufo-dedup export | Migration is IF NOT EXISTS + idempotent; columns sit NULL until next migrate_sqlite_to_pg.py |
| Binary payload grows past 5 MB gzipped        | 28-byte rows × 396k = 10.9 MB raw, estimated ~4 MB gzipped. Under budget. |
| Client shows filters for unpopulated fields   | `coverage` in meta sidecar drives per-filter enable/disable     |
| 255-sentinel collides with a legit 255 score  | Scores are 0–100; 255 is guaranteed out-of-range                |
| `hoax_likelihood` REAL in PG → uint8 conversion loses precision | 100 levels of precision on a 0.0–1.0 scale is enough for UI filtering; the raw REAL stays in PG for detail lookups |
| A future column addition requires another schema bump | v0.8.2 reserves byte 25 and designs the row around 4-byte alignment for easy extension |
| Deploy workflow race between main branch + tag push both running the migration in parallel | `IF NOT EXISTS` makes it safe; both runs end up at the same state |

## Success metrics

- **`/api/points-bulk?meta=1`** returns `schema_version: "v082-1"`
  and a `coverage` object on the live deploy.
- **Binary payload** is a multiple of 28 bytes, first-row round-trip
  is exact.
- **`coverage.quality_score`** is 0 before the user runs the data
  pipeline, full after.
- **"High quality only" toggle** hides/shows markers in under 16 ms
  (measured via `performance.now()`).
- **Timeline playback** with `dateDays` populated moves at 60 fps and
  visibly animates month-by-month (not year-by-year) on slow
  playback speeds.
- **Legacy browser** (no WebGL) still works.
