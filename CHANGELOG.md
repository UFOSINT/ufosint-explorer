# Changelog

All notable changes to ufosint-explorer are documented here. The format
is loosely [Keep a Changelog](https://keepachangelog.com/), the version
scheme is [SemVer](https://semver.org/).

- **MAJOR** (`v1.x`) is reserved for the `ufosint.com` cutover + a stable
  public API. We're not there yet.
- **MINOR** (`vN.M.0`) bumps each time a sprint ships a coherent feature
  set. Sprints 1–4 were the "UX review" wave in April 2026.
- **PATCH** (`vN.M.X`) bumps for bugfixes that don't change the feature
  surface.

Tags push automatically to Azure via `.github/workflows/azure-deploy.yml`.

## [Unreleased]

Nothing yet.

## [0.8.0] — 2026-04-10 — Bulk client-side rendering (deck.gl)

The Observatory map has been querying the DB on every pan since v0.6.
At 614k sightings / 105k geocoded / 25k samples per viewport that's a
lot of wasted gunicorn cycles, a lot of 3–5 MB JSON payloads, and a
visibly sluggish pan/zoom. v0.8.0 throws the whole per-pan loop away.

The client now downloads **every geocoded sighting** as a packed
binary buffer **once** (~700 KB gzipped), deserialises it into typed
arrays, renders it on the GPU via `deck.gl`, and filters it entirely
in the browser. Pan, zoom, mode switch, and filter change are all
zero-network operations after the initial load.

See [`docs/V080_PLAN.md`](docs/V080_PLAN.md) for the full architecture
rationale and the risk register.

### Added

- **`/api/points-bulk`** — new endpoint that returns every geocoded
  sighting in a 16-byte packed row layout
  `(uint32 id, float32 lat, float32 lng, uint8 source_idx,
  uint8 shape_idx, uint16 year)`. Little-endian so the JS `DataView`
  reads it directly. The endpoint supports three response shapes:
  - `?meta=1` → JSON sidecar with lookup tables + schema descriptor
  - default → `application/octet-stream` pre-gzipped packed rows
  - `If-None-Match` with matching ETag → `304 Not Modified`
  The packed buffer is `@functools.lru_cache(maxsize=2)`'d on the
  ETag so every gunicorn worker holds at most ~4 MB of map data
  across versions. ETag is derived from the schema version + row
  count + `MAX(id)`, all O(1) under existing indexes.
- **`static/deck.js`** — client-side module that fetches the bulk
  dataset, deserialises it into six typed arrays, exposes a
  `applyClientFilters(filter)` hot loop (~1 ms for 105k rows), and
  mounts a `deck.gl` `LeafletLayer` on top of the existing Leaflet
  map with three built-in modes:
  - Points → `ScatterplotLayer` (GPU, 60 FPS on 100k points)
  - Hex → `HexagonLayer` (GPU aggregation in screen-space meters,
    uniform tessellation regardless of latitude — finally closes the
    hex-geometry saga from v0.7.5 → v0.7.7 by letting the library do
    the math correctly in the first place)
  - Heat → `HeatmapLayer` (GPU density estimation)
- **deck.gl bundle** loaded from unpkg (`deck.gl@9.0.38` +
  `@deck.gl/leaflet@9.0.38`) as `<script defer>` tags. Same
  vendor-CDN pattern the existing Leaflet install uses — no npm, no
  bundler, no build step.
- **WebGL capability probe + legacy fallback.** Browsers without
  WebGL (or where either deck.gl script fails to load) stay on the
  v0.7 `loadMapMarkers()` / `loadHeatmap()` / `loadHexBins()`
  path without noticing the difference.
- **`docs/V080_PLAN.md`** — architecture plan, binary layout, cache
  strategy, filter pipeline, risk register, and explicit list of
  what we're *not* doing (no PostGIS migration, no pg_tileserv, no
  Redis, no App Service upgrade).
- **22 new tests in `tests/test_v080_bulk.py`** covering the
  endpoint contract, the binary round-trip, ETag 304 handling, the
  LRU cache behaviour, and the frontend wire-up (fetch, DataView
  offsets, deck.gl layer types, client-filter pipeline, WebGL
  probe, index.html bundle loading).

### Changed

- `initMap()` tries to boot the GPU path before falling back to the
  legacy Leaflet marker cluster layer. When the GPU path succeeds
  it clears the legacy layers to avoid double-drawing but leaves
  their DOM nodes in place as an emergency fallback.
- `toggleMapMode()` short-circuits to `UFODeck.setDeckMode()` when
  the GPU path is active — mode switches become a single
  `setProps({ layers: [...] })` call with no server round-trip.
- `applyFilters()` now tries `applyClientFilters()` first for the
  Observatory tab. The typed-array filter walk runs in ~1 ms; only
  the legacy path falls through to `loadMapMarkers()` etc.
- `scheduleMapReload()` early-returns when `state.useDeckGL` is
  true — the deck.gl layer handles pan/zoom natively so we don't
  need the debounced re-fetch loop.

### Kept for one release cycle

`/api/map`, `/api/heatmap`, and `/api/hexbin` stay in place as the
legacy fallback for browsers without WebGL. They'll be deleted in
v0.8.1 once the GPU path is proven in production.

## [0.7.7] — 2026-04-10 — True honeycomb hex tessellation

v0.7.6 fixed the overlapping / random-sized hex bins by inscribing
each hex in its square bucket with `r = sizeDeg / 2`. That got rid of
the overlap but left small diagonal gaps between adjacent cells — the
hexes tessellated *inside* a square grid, not as a proper honeycomb.
v0.7.7 switches to offset-row bucketing on the backend so adjacent
hexes share edges.

### Fixed

- Hex cells now tile as a true honeycomb with no gaps between
  adjacent cells. `/api/hexbin` buckets longitude with a half-cell
  horizontal shift on odd rows (the standard "offset-r" hex grid
  layout), vertical row spacing is `sizeDeg * sqrt(3)/2`, and the
  cell center formula bakes the odd-row shift back in. On the client
  `_hexPolygonAround` now uses the correct circumradius
  `R = sizeDeg / sqrt(3)` for a pointy-top hex whose flat-to-flat
  width equals `sizeDeg` — so every hex's right edge sits exactly on
  its neighbor's left edge. The grid still has no Mercator
  correction, so at high latitudes hexes read as slightly
  horizontally compressed, but they tessellate uniformly across the
  viewport.

## [0.7.6] — 2026-04-10 — Marker popup polish + hex tessellation + brush playback

Three small UX bugs from the v0.7.5 round of feedback. None of them
needed schema or pipeline work — pure UI fixes.

### Added

- Marker popup now renders a real **View Details** button (was a plain
  text link), and shows a `[ DESC ]` / `[ NO DESC ]` badge so you can
  tell at a glance which sightings carry a written narrative versus
  coordinates-only entries. `/api/map` returns a new `has_desc`
  boolean computed inline (`s.description IS NOT NULL AND LENGTH > 0`)
  with no extra index needed — the field rides along with every
  marker for free.
- New `.popup-btn`, `.popup-tags`, `.popup-links`, `.popup-desc-row`,
  and `.popup-desc-badge` styles in `style.css`. The button uses the
  active theme's `--accent` colour so SIGNAL and DECLASS both look
  right.

### Fixed

- **Hex bins overlapped and rendered at apparently random sizes.**
  Two interacting bugs: (a) `/api/hexbin` returned the data centroid
  (`AVG(lat)`, `AVG(lng)`) of each bucket rather than the geometric
  cell center, so adjacent buckets drew their hexes at offset
  positions; (b) `_hexPolygonAround` stretched longitude by
  `1/cos(lat)` to make the hex equilateral on Mercator, which pushed
  each hex past its grid cell at higher latitudes (UFOSINT data
  clusters around 35–50°N where the stretch was most visible).
  v0.7.6 returns `(south + (row+0.5)*size, west + (col+0.5)*size)`
  from the backend and uses `r = sizeDeg / 2` with no Mercator
  correction in the frontend, so every hex is inscribed in its own
  grid cell. The result tessellates uniformly with small diagonal
  gaps and no overlap.
- **Time brush PLAY button appeared dead.** Hitting PLAY before
  narrowing the window left `winSpan == span`, so the slide loop's
  `b = a + winSpan` always exceeded `maxT`, the loop reset `a` back
  to `minT`, and visually nothing happened. PLAY now auto-narrows
  the window to a 5-year span starting from the dataset minimum
  before the slide begins, so the playback sweeps forward visibly
  on the first click.

## [0.7.5] — 2026-04-10 — Materialized views for landing-page aggregates

The v0.7.4 free tuning (`pg_prewarm` + parameter bumps) cut cold-start
times 7–20x, but the three heaviest landing-page endpoints still did
full-table aggregates on every cache miss. v0.7.5 pre-computes the
no-filter case of those endpoints into materialized views and has the
Python routes read from the MV first, falling back transparently to the
live query for any filtered request. Warm-path latency for the
unfiltered case drops from ~1–2 s per endpoint to ~5 ms.

### Added

- **`scripts/add_v075_materialized_views.sql`** — idempotent migration
  that creates five materialized views covering the three hottest
  routes:
  - `mv_stats_summary` (single-row aggregates for `/api/stats`)
  - `mv_stats_by_source` (per-source counts)
  - `mv_stats_by_collection` (per-collection counts)
  - `mv_timeline_yearly` (period + source_name + cnt for
    `/api/timeline?mode=yearly`)
  - `mv_sentiment_overview` (13-column single-row aggregate for
    `/api/sentiment/overview`, previously the 23-second query from the
    v0.7.4 boot logs)

  Every MV has a unique index so we can upgrade to
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` later without a rebuild. The
  script ends with a non-concurrent `REFRESH` of all five, which takes
  an ACCESS EXCLUSIVE lock for ~5-15 s per view — acceptable at deploy
  time.
- **MV fast-path + live-query fallback** in `app.py` for `/api/stats`,
  `/api/timeline`, and `/api/sentiment/overview`. Each route checks
  `_has_common_filters(request.args)` first: if no filters are set, it
  runs the MV query (tiny index scan), otherwise it drops through to
  the original live-query path and the existing Flask-Caching layer.
  Both paths return the same JSON shape, so the frontend is unchanged.
- **`_has_common_filters()` + `_COMMON_FILTER_KEYS`** — a single source
  of truth for "is this request MV-eligible?". Covered by
  `test_common_filter_keys_match_add_common_filters` which keeps the
  set in lockstep with `add_common_filters()` via regex extraction.
- **Automatic MV missing fallback** — each fast path is wrapped in a
  `try`/`except psycopg.errors.UndefinedTable` so a fresh clone that
  hasn't run the migration, or a local dev DB that drops the MV for a
  schema change, still serves the route correctly via the live query.
  The catch logs a `[api_*] mv_* missing, falling back to live query`
  line so operators can spot drift.
- **Deploy-time MV refresh step** in `.github/workflows/azure-deploy.yml`
  — the sparse checkout now also pulls
  `scripts/add_v075_materialized_views.sql`, and a new step runs it via
  `psql -v ON_ERROR_STOP=1 -f …` after the v0.7 index migration step.
  Because the script is `CREATE … IF NOT EXISTS` + `REFRESH`, running
  it on every deploy is safe and has the welcome side effect of
  picking up rows imported since the last deploy.
- **`tests/test_v075_mv.py`** (23 tests) — locks the v0.7.5 contract:
  - Migration SQL creates all five MVs with unique indexes and is
    idempotent; the deploy workflow applies it after the index step.
  - Source-level assertions that `_has_common_filters` exists, that
    each route has the MV fast path and the live-query fallback.
  - Functional tests using a `_FakeCursor`/`_FakeConn` pair that
    monkeypatches `get_db()` to inject scripted responses: MV happy
    path returns the MV-shape payload without running any live query,
    `UndefinedTable` triggers the fallback, filtered requests
    (`shape=disk`, `year=1975`, `country=US`) correctly bypass the MV.

### Changed

- `/api/stats` refactored into `_api_stats_from_mv(conn)` +
  `_api_stats_from_live(conn)` helpers with the route picking between
  them. The live helper is the unchanged v0.7.4 code path.
- `/api/sentiment/overview` gained a module-level
  `_SENTIMENT_OVERVIEW_COLS` tuple used by both the MV read
  (`SELECT {...} FROM mv_sentiment_overview`) and the live-query `dict(zip(...))`
  assembly. Single source of truth for the column list.

## [0.7.4] — 2026-04-10 — Performance infrastructure (pg_prewarm + Redis cache)

### Added

- **`scripts/pg_tuning.sql`** — documents the Azure Flexible Server
  parameter values for the B1ms tier (`shared_buffers=768MB`,
  `effective_cache_size=1500MB`, `work_mem=16MB`,
  `random_page_cost=1.1`, `jit=off`, …) and calls `pg_prewarm()` on
  every hot table + index so the buffer cache is populated after a
  restart. Safe to run as the app user. Apply with
  `psql "$DATABASE_URL" -f scripts/pg_tuning.sql` after a DB-side
  restart.
- **Startup `pg_prewarm` hook** in `app.py` — `_pg_prewarm_relations()`
  runs in the prewarm background thread and loads `sighting`,
  `location`, and the hot composite indexes into `shared_buffers`
  before the HTTP warmup hits the same queries. Silently skips when
  the extension isn't installed, so local dev and CI are unaffected.
- **Optional Redis cache backend** for Flask-Caching. When
  `REDIS_URL` is set, the app configures `CACHE_TYPE=RedisCache` with
  key prefix `ufosint:<ASSET_VERSION>:` so every gunicorn worker
  shares one warm cache and new deploys auto-invalidate the previous
  version's keys. When `REDIS_URL` is unset, the existing per-worker
  `SimpleCache` path is preserved — zero impact on local dev, CI, or
  anyone who doesn't want to pay for Redis. `redis==5.2.1` added to
  `requirements.txt` so the client library is available when the env
  var is set.
- **`docs/DEPLOYMENT.md §7 Performance tuning`** — operator playbook
  with the exact Azure portal values, the `pg_prewarm` one-time
  setup, the `az redis create` + `az webapp config appsettings set`
  commands for wiring an Azure Cache for Redis Basic C0 (~$16/mo),
  and the scale-up decision tree for when free tuning runs out.
- **`tests/test_perf_infra.py`** — locks the perf contract. Verifies
  `pg_tuning.sql` exists and prewarms the critical relations, that
  `app.py` reads `REDIS_URL` and switches cache backends accordingly,
  that `CACHE_KEY_PREFIX` is versioned, that the SimpleCache fallback
  is still the default, and that `_pg_prewarm_relations()` tolerates
  a missing extension.

## [0.7.3] — 2026-04-10 — Hex bins work out of the box (runtime SQL bucketing)

### Fixed

- **Hex Bins mode always fell back to Heatmap** because the v0.7.0
  implementation read pre-computed H3 cells from a materialized view
  that was never populated. The MV required a one-time
  `DATABASE_URL` GitHub secret + manual `compute-hex-bins.yml`
  workflow trigger + the `h3` Python library on the runner, and none
  of that one-time setup had ever been done. The endpoint always
  returned 503, and `loadHexBins()` silently disabled the toggle.

### Changed

- **`/api/hexbin` now computes bins on the fly in SQL** —
  `FLOOR((lat - south) / size)` and `FLOOR((lng - west) / size)`
  against the existing `idx_location_coords` composite index, no
  extensions, no materialized view, no pre-compute step. Works out
  of the box on any fresh deploy.
- **New `_hex_cell_size(zoom)` helper** maps every Leaflet zoom
  level (0..18) to a bucket side length in degrees — world view
  gets 20° cells, city view gets 0.008° cells. Tuned so a desktop
  viewport at each zoom yields roughly 200–1200 cells, which Leaflet
  can plot in under 100 ms.
- **`/api/hexbin` accepts `south`/`north`/`west`/`east` bbox params**
  (the client sends the current viewport), so aggregation only runs
  against sightings inside the visible window. An inverted bbox
  returns an empty `cells` list, never a 500.
- **All standard filters now work with Hex Bins mode** — source,
  shape, country, date range. The old auto-fallback to Heatmap when
  a country filter was set is gone, because `add_common_filters()`
  is wired into the runtime query just like `/api/map` and
  `/api/heatmap`.
- **Client hex polygons are computed from centroids via
  `_hexPolygonAround(lat, lng, size)`** — a flat-top hexagon with
  longitude stretched by `1/cos(lat)` so the shape stays roughly
  equilateral on Mercator. No H3 library needed client-side.
- **`loadHexBins()` no longer has a 503 fallback branch.** The
  endpoint never returns 503 now (except on complete pool failure,
  which is the correct signal). The country-filter auto-fallback
  was also removed.

### Deprecated (but kept)

- `scripts/compute_hex_bins.py`, `scripts/add_v07_indexes.sql`'s
  hex-related indexes, `requirements-deploy.txt`,
  `.github/workflows/compute-hex-bins.yml`, and
  `.github/workflows/refresh-hex-bins.yml` are still in the tree
  as dead code. They're harmless and could become useful again if
  we ever want to swap the runtime bucketing for a real H3 MV
  (e.g. to cache large viewports cross-worker). No action needed.

### Tests

- **`test_api_hexbin_does_not_return_500`** — endpoint returns 200
  on a real DB or 503 on a stubbed pool, never 500. Payload always
  carries a `cells` list.
- **`test_api_hexbin_accepts_bbox_params`** — inverted bbox returns
  an empty cells list, not an error.
- **`test_hex_cell_size_mapping`** — `_hex_cell_size` is monotonically
  non-increasing across zoom 0..18, zoom 0 has a reasonable world-view
  size, zoom 18 has a reasonable city-view size, out-of-range zooms clamp.
- **`test_load_hex_bins_sends_bbox_and_has_no_fallback`** — client
  sends `south`/`north`/`west`/`east`, has `_hexPolygonAround` helper,
  no `toggleMapMode("heatmap")` fallback inside `loadHexBins()`, no
  `resp.status === 503` branch.
- **`test_zoom_to_res_legacy_mapping_kept`** — the legacy H3 resolution
  helper is preserved for backwards compatibility but no longer used at
  runtime.
- Deprecated **`test_api_hexbin_handles_missing_mv`** (replaced by
  the above) and **`test_zoom_to_res_mapping`** (replaced by the
  cell-size test + legacy compat test).

Suite is **139 tests** (was 136), still under 0.5 s.

### Smoke probe

`azure-deploy.yml`'s `/api/hexbin` probe now passes a continental-US
bbox (`south=25&north=50&west=-125&east=-65`) and asserts the
response contains a **non-empty** `cells` array. Previously it
accepted 200 OR 503 — now 503 fails the workflow.

## [0.7.2] — 2026-04-10 — Timeline loader regression fix + 1900-baseline defaults

### Fixed

- **Timeline tab rendered a blank chart.** When v0.7.1 de-aliased
  Timeline from Observatory, the corresponding `else if (tab ===
  "timeline") loadTimeline()` branch was missing from `switchTab()`,
  so clicking Timeline activated the panel but never called the
  chart renderer. Network panel showed `/api/map` firing instead of
  `/api/timeline`. Restored the branch + pinned it with a regression
  test (`test_switch_tab_has_timeline_branch`).

### Changed

- **Default date range is now 1900 → current year.** New
  `applyDefaultDateRange()` helper seeds `filter-date-from=1900` and
  `filter-date-to=<current year>` on fresh page loads, before
  `applyHashToFilters()` runs so deep-link hashes still win. Only
  fills fields that are empty, so the Clear button still resets to
  an empty range for users who want pre-1900 data. Applied across
  Map, Timeline, and Search — the modern sighting era is now the
  default view instead of the full 34 AD → 2026 span.
- **`TimeBrush.BRUSH_MIN_YEAR` moved from 1947 → 1900.** Keeps the
  pre-Roswell context visible on the histogram (1896 airship wave,
  foo fighters, etc.) so users see continuity rather than a hard
  floor at WWII.

### Tests

Three new regression tests in `tests/test_v07.py`:
- `test_switch_tab_has_timeline_branch` — asserts the missing branch
  is back. This is the invariant that failed in v0.7.1.
- `test_default_date_range_helper_exists_and_is_called_at_boot` —
  pins `applyDefaultDateRange()` + its call in `DOMContentLoaded`.
- `test_time_brush_min_year_is_1900` — pins the brush floor constant.

Suite is now **136 tests** (was 133), still under 0.5s.

## [0.7.1] — 2026-04-10 — UFOSINT rename, place search reflow, Timeline restored

Small polish patch on top of v0.7.0 based on a round of immediate feedback.

### Changed

- **Header H1 is now "UFOSINT Explorer"** (was "UFO Explorer") — matches
  the domain, the repo name, and the HTML `<title>`.
- **Place search moved from top-left to bottom-middle of the map
  canvas.** The top-left placement from v0.6 was colliding with the
  Observatory topbar's Points/Heatmap/Hex Bins mode toggle, so the
  mode buttons were hidden behind the search input. The search pod now
  sits 36 px above the Leaflet attribution strip, centered via
  `left: 50%; transform: translateX(-50%)`, with a `max-width` clamp
  so narrow viewports don't stretch it edge-to-edge.
- **`.coords-toggle`** (All / Original / Geocoded coord-source
  dropdown) follows the place search to bottom-middle — parked just
  to the right of the search pod on desktop, stacked above it on
  mobile so neither control covers the mode toggle.
- **Timeline is a first-class tab again.** v0.7.0 folded both Map and
  Timeline into the Observatory dashboard, but users still want the
  full Chart.js drill-down view for year → month exploration — the
  Observatory time brush is a compact filter, not a replacement for
  the full chart. The `switchTab()` alias branch now maps only
  `map` → `observatory`; Timeline resolves to its own
  `#panel-timeline` with the existing `loadTimeline()` render path.
- **DECLASS "TOP SECRET // PLOTTED" classification stamp removed.**
  The rotated `position: fixed` pseudo-element overlapped the gear
  icon on narrow viewports and the novelty wore thin fast. The
  DECLASS theme is now defined purely by its palette (burgundy accent,
  cream background, Courier Prime body font) plus the paper-gradient
  canvas wrap. If we ever want it back we can scope a new stamp to
  the Observatory canvas instead of the global body.

### Tests

- New tests in `tests/test_v07.py`:
  - `test_h1_is_ufosint_explorer` — locks the rename
  - `test_timeline_tab_is_restored_and_visible` — no `hidden`, no
    `legacy-tab` on the Timeline button
  - `test_map_tab_stays_hidden_as_legacy_alias` — Map stays in DOM
    for `#/map?...` deep-link compatibility but remains invisible
  - `test_switch_tab_no_longer_aliases_timeline` — the old
    `tab === "map" || tab === "timeline"` alias is gone
  - `test_map_place_search_is_at_bottom_middle` — CSS must use
    `bottom:`, `left: 50%`, and `translateX(-50%)`, and must NOT
    carry the old `top: var(--s-3)` positioning
- Updated `test_declass_has_classification_stamp` →
  `test_declass_stamp_overlay_removed` — now pins the stamp's
  absence instead of its presence.

Suite is now **133 tests** (was 128), still runs in under 0.5 s.

## [0.7.0] — 2026-04-10 — Observatory redesign, H3 hex bins, 504 fix

This is the biggest UX change since v0.1 — a second UX team reviewed the
v0.5/v0.6 interface and asked for three concrete changes:

1. **Stop covering map content with loading chrome.** The rotating radar
   sweep and marker pane dim from v0.5/v0.6 were obscuring the data users
   were trying to read.
2. **Make the map smaller and surround it with data panels.** A unified
   "Observatory" dashboard with a left rail, center canvas, and bottom
   time brush — closer to an intelligence analyst console than a
   full-screen Google Maps clone.
3. **Add a draggable time window that scrubs the visible markers** live,
   with key sighting events annotated inline on the histogram.

Plus one bug: **`/api/sighting/<id>` was returning HTTP 504 on cold
cache** because the duplicate-candidate lookup ran an `OR` across two
un-indexed columns and fell back to a sequential scan of 126,730 rows.

### Fixed

- **HTTP 504 on `/api/sighting/<id>`.** Two changes:
  - `@cache.cached(timeout=600)` decorator on the route so warm queries
    hit the per-worker LRU.
  - Duplicate-candidate query rewritten as a `UNION ALL` of two equality
    scans so the planner can use two new btree indexes
    (`idx_duplicate_a`, `idx_duplicate_b`) independently instead of
    falling back to seqscan over `OR`.
  - Migration file `scripts/add_v07_indexes.sql` runs `CREATE INDEX IF
    NOT EXISTS` so every deploy is idempotent; the GitHub Actions
    `deploy` job executes it before the app code ships.
- **Rotating radar sweep removed from the map loading state.** Markers
  stay fully visible during refresh now. The `@keyframes map-radar-spin`
  rule is kept (no longer attached to anything) for test stability.
- **Marker pane dim disabled during progressive reloads.** The Leaflet
  pane stays at `opacity: 1` — the "loading" affordance is now the HUD
  status pill in the Observatory topbar instead of a content dim.
- **Content-blocking search overlay removed** from `executeSearch()`.
  Previous result cards stay interactive during a refresh; new cards
  swap in with the stagger fade-in from v0.6.

### Added

- **Observatory tab** — a unified dashboard that replaces the legacy
  Map and Timeline tabs. Layout: 230 px left rail (Sources / Shapes /
  Visible count / Time window), center canvas wrapping the Leaflet map
  with a Points/Heatmap/Hex Bins mode toggle and a live LAT/LON/STATUS
  HUD, and a 110 px bottom time brush with draggable window, play/reset
  buttons, and key sighting annotations (Roswell, Washington Flap, Hill
  Abduction, Rendlesham, Phoenix Lights, Tic-Tac, Gimbal, Grusch).
  Legacy Map and Timeline tab buttons are hidden (not deleted) so
  `#/map?...` and `#/timeline?...` deep links still resolve to the
  Observatory via `switchTab()`'s alias branch.
- **`/api/hexbin` endpoint** — returns pre-computed H3 hex-bin cells
  from the new `hex_bin_counts` materialized view. Query params: `zoom`
  (mapped to H3 resolution 2–6 via a zoom-to-res lookup), `source`,
  `shape`, `decade_from`, `decade_to`. Cached 300 s. Graceful 503 fall-
  back when the MV hasn't been populated yet, so the client can disable
  the HexBin mode toggle without a user-visible error.
- **H3 pre-compute pipeline** — `scripts/compute_hex_bins.py` reads
  every geocoded `location` row, computes H3 cells + cell boundaries at
  resolutions 2–6 using the `h3` Python library, stores them in a new
  `location_hex` support table, and rebuilds the `hex_bin_counts`
  materialized view that aggregates sightings per (res, cell, source,
  shape, decade). Runtime container stays lean — `h3` lives in a new
  `requirements-deploy.txt` that only the GitHub Actions runner
  installs.
- **Two new GitHub workflows**:
  - `compute-hex-bins.yml` — `workflow_dispatch` only, runs the Python
    pre-compute script against the live DB. Manual trigger so we don't
    re-run the ~5 min job on every deploy.
  - `refresh-hex-bins.yml` — `workflow_dispatch` only, runs `REFRESH
    MATERIALIZED VIEW hex_bin_counts` via `psql`. Faster than the full
    compute when only new sightings have been added (existing locations
    are already H3-indexed).
- **SIGNAL + DECLASS theme toggle** in the gear menu.
  - SIGNAL (default) — cyan `#00F0FF` on void `#030710`. Observatory-
    console aesthetic matching the UX mockup.
  - DECLASS — burgundy `#B8001F` on cream paper `#EEE8D2` with a
    CSS-only rotated "TOP SECRET // PLOTTED" classification stamp in
    the top-right corner, plus a Courier Prime monospace body font.
  - Both themes defined as CSS variable overrides on `body.theme-signal`
    and `body.theme-declass`. Choice persists in localStorage and is
    applied via an inline pre-paint script in `<head>` so there's no
    flash of the default theme on refresh.
  - The time brush histogram re-draws with the current accent color
    when the theme flips.
- **TimeBrush class** (`static/app.js`) — canvas-based year histogram
  fetched from `/api/timeline?bins=monthly&full_range=1`, draggable
  window with handles for left/right resize and middle-drag translate,
  play button that auto-scrubs the window forward, reset button, and
  annotation lines for entries in `static/data/key_sightings.json`.
  Debounces `onChange` to 300 ms so play-mode doesn't saturate
  `applyFilters()`.
- **`static/data/key_sightings.json`** — 8 canonical events with year,
  label, and short description. Hand-curated, checked in, can evolve
  without a DB touch.
- **HUD corner brackets** on the map canvas (pure CSS edges, no
  content coverage).
- **`_zoom_to_res()` helper** in `app.py` mapping Leaflet zoom 0–18 to
  H3 resolution 2–6.
- **29 new tests in `tests/test_v07.py`** locking the full v0.7
  contract:
  - `idx_duplicate_a` + `idx_duplicate_b` in both `add_v07_indexes.sql`
    and `pg_schema.sql`
  - `@cache.cached` on `api_sighting`
  - `UNION ALL` rewrite of the duplicate query
  - `/api/hexbin` route registered, returns 200 or 503 never 500
  - `_zoom_to_res` covers all Leaflet zoom levels
  - Observatory panel + rail + mode toggle + time brush markup
  - `body.theme-signal` + `body.theme-declass` CSS blocks with
    `--accent`
  - DECLASS classification stamp pseudo-element
  - `loadObservatory`, `loadHexBins`, `TimeBrush`, `initThemeToggle`,
    `setTheme` functions exist
  - `showProgressiveLoading(resultsEl` callsite removed
  - `#map.is-loading::before` no longer animates `map-radar-spin`
  - `key_sightings.json` valid + canonical entries present

Suite is now **128 tests**, still runs in under 0.5 seconds.

### Changed

- **`toggleMapMode()` handles three modes**: `points` (clustered
  markers — renamed from `clusters`), `heatmap`, `hexbin`. The v0.7
  Observatory mode toggle uses `.mode-btn` instead of the old
  `.map-mode-btn`; both selectors are handled for transition
  compatibility.
- **`switchTab()` has a whitelist (`VALID_TABS`)** that catches garbage
  input (missing, `undefined`, string `"undefined"` from a polluted URL
  hash) and falls back to the Observatory. Defence in depth after an
  initial testing session found the gear icon's shared `.tab` class
  was accidentally triggering the generic tab listener.
- **Tab click listener is scoped to `.tab[data-tab]`** with an explicit
  `settings-btn` exclusion so the gear menu button can never fire
  `switchTab(undefined)` again.
- **`azure-deploy.yml` `deploy` job** checks out the repo, installs
  `postgresql-client`, and runs `psql -f scripts/add_v07_indexes.sql`
  before the code deploy. Skipped with a warning if the new
  `DATABASE_URL` GitHub secret isn't configured.
- **`azure-deploy.yml` `smoke` job** gains two new probes:
  - `/api/hexbin?zoom=4` — expects 200 or 503, fails the workflow on
    500/404/timeout.
  - `/api/sighting/136613` — regression probe for the 504 bug. Fails
    if cold queries still hit the Azure timeout.

### Deployment steps for this release

1. Push the branch → CI runs all gates (ruff + pytest + node -c).
2. `deploy` job runs `add_v07_indexes.sql` via `psql`.
3. Code deploys to App Service.
4. `smoke` job verifies `/health`, `/`, `/api/filters`, `/api/stats`,
   `/api/hexbin` (accepts 503), `/api/sighting/136613`.
5. **One-time manual step**: trigger `compute-hex-bins.yml` from the
   GitHub Actions UI to populate `location_hex` + `hex_bin_counts`.
   Takes ~5–10 minutes for 105K locations. After this runs,
   `/api/hexbin?zoom=4` returns 200 and the HexBin toggle on the
   Observatory becomes functional.
6. Tag `v0.7.0`.

### Known limitations (deferred to v0.8)

- **HexBin + country filter don't mix.** The `hex_bin_counts` MV
  doesn't carry country because adding it would roughly 6× the row
  count. When a user selects HexBin mode with a country filter, the
  client auto-falls-back to Heatmap mode and shows a small toast.
- **`REFRESH MATERIALIZED VIEW` is non-concurrent.** Reads see a brief
  lock during refresh. Adding a `UNIQUE INDEX` on the MV to unblock
  `REFRESH ... CONCURRENTLY` is a v0.8 task.
- **DECLASS theme ships CSS-only stamps.** The UX mockup included a
  paper-noise PNG and a Courier Prime stamp glyph; v0.7 uses pure-CSS
  approximations (radial-gradient background + pseudo-element border).
  Asset integration is deferred.
- **Antimeridian hex cells** at low H3 resolutions can render as wide
  horizontal polygons in Leaflet. `loadHexBins()` detects boundaries
  crossing ±180° longitude and skips them for now; splitting into two
  polygons is a v0.8 polish item.

## [0.6.0] — 2026-04-09 — Progressive loading (keep content, dim in place)

### Changed
- **Timeline refresh is now a live chart update, not a destroy + recreate.**
  `loadTimeline()` no longer calls `state.chart.destroy()` on every
  reload — instead it mutates `state.chart.data.labels` and
  `state.chart.data.datasets` in place and calls
  `state.chart.update("active")`, which animates the bars smoothly
  between old and new values. Chart.js interpolates bar heights over
  600 ms with `easeOutQuart`, so changing the date range no longer
  flashes a blank canvas.
- **Search keeps previous results visible while the new query runs.**
  `executeSearch()` only renders skeleton cards on a cold start
  (empty results list); every subsequent search leaves the existing
  cards on screen, dims them via `.is-loading-progressive`, and
  overlays a centered terminal. When the new data arrives, the cards
  swap and each new one fades in with a staggered 22ms delay via
  `.is-new` + `--i`.
- **Map markers stay visible while panning / filtering.** Both
  `loadMapMarkers()` and `loadHeatmap()` add `.is-loading-progressive`
  on entry, which dims the Leaflet marker + overlay panes via CSS
  (`opacity: 0.45; filter: saturate(0.6)`) instead of clearing them
  immediately. The old markers stay on screen until the new batch is
  ready — no more "flash of empty map" while the request is in
  flight.
- **Boot parallelism: filters render the instant they arrive.** The
  `DOMContentLoaded` handler no longer uses `Promise.all` to block on
  both `/api/filters` and `/api/stats`. Each promise has its own
  `.then()` — `populateFilterDropdowns` fires as soon as filters land
  (usually milliseconds), while the slower `/api/stats` keeps cycling
  the badge boot sequence until its response arrives.

### Added
- **`.loading-terminal` progressive overlay** — new CSS that places a
  compact terminal card absolutely-centered over a container without
  removing its existing content. Three new base classes:
  - `.is-progressive` — opts a container into progressive loading
    (just sets `position: relative` on descendants).
  - `.is-loading-progressive` — active flag; dims children
    (`opacity: 0.4; filter: blur(0.6px) saturate(0.7)`) and fades in
    the `.progressive-overlay`.
  - `.progressive-overlay` — absolutely-positioned child that hosts
    the centered terminal. Pointer-events off so the user can still
    interact with the dimmed content underneath.
- **`showProgressiveLoading(container, bank, opts)`** /
  **`hideProgressiveLoading(container)`** — JS helpers that wrap the
  CSS class toggling + terminal mount/unmount. The hide helper
  removes the overlay node on a 240 ms timer so the CSS opacity fade
  finishes cleanly, and bails if a new load started before the fade
  finished (prevents the overlay from getting removed mid-transition).
- **`staggerNewChildren(parent, selector)`** — tags freshly rendered
  children with `.is-new` and a `--i` custom property so they fade in
  sequentially via the CSS `stagger-fade-in` keyframe.
- **`@keyframes stagger-fade-in`** — 320 ms opacity + translateY +
  blur fade, per-child delay via `calc(var(--i, 0) * 22ms)`. Applied
  to new search result cards.
- **Map pane dim rules** — `#map.is-loading-progressive
  .leaflet-marker-pane` and `.leaflet-overlay-pane` drop to 45%
  opacity + desaturate.
- **20 new tests in `tests/test_progressive_loading.py`** covering:
  - 9 required CSS selectors (`.is-progressive`, `.progressive-overlay`,
    etc.)
  - `stagger-fade-in` keyframe + `calc(var(--i ...))` delay
  - `prefers-reduced-motion` block neutralizes the stagger + blur
  - `.chart-container` has `position: relative` (overlay anchor)
  - `showProgressiveLoading` + `hideProgressiveLoading` +
    `staggerNewChildren` helpers exist
  - ≥ 2 callsites for `showProgressiveLoading` (timeline + search)
  - ≥ 3 callsites for `hideProgressiveLoading` (success + catch +
    timeline finally)
  - `loadTimeline` uses `chart.update("active")` and does NOT call
    `chart.destroy()`
  - `executeSearch` has the `hasExistingResults` gate
  - Search result cards render with `.is-new` + `--i`
  - Both map loaders add `is-loading-progressive`
  - Boot block stores `filtersPromise` / `statsPromise` separately
    and calls `populateFilterDropdowns` inside `filtersPromise.then()`

Suite is now **99 tests**, still runs in under 0.5 seconds.

### Accessibility
- **`prefers-reduced-motion: reduce`** freezes the stagger fade-in
  and kills the blur filter on dimmed content; the content still
  dims so the "stale" affordance stays, but there's no vestibular
  trigger from the blur/translate animations. Map pane filter
  saturation also normalizes under reduced motion.

## [0.5.0] — 2026-04-09 — Hackery loading system

### Added
- **`.loading-terminal` component** — reusable monospace terminal card
  with a drifting scanline, blinking cursor, glowing green prompt, and
  a bottom-edge progress bar that slides. Two variants: full
  (header + progress bar) and `.compact` (single-line, no chrome) for
  tight spaces like the search info bar.
- **`TERMINAL_MESSAGE_BANKS`** in `static/app.js` — seven themed banks
  of hacker-flavored status messages: `generic`, `search`, `map`,
  `timeline`, `duplicates`, `insights`, `boot`. Every loading site
  picks a bank that matches the tab the user is on.
- **`mountLoadingTerminal(el, bank, opts)`** / **`unmountLoadingTerminal(el)`**
  helpers. The mount helper renders the terminal markup, cycles through
  the chosen message bank every 900 ms, and restarts the typewriter
  animation on each tick. Tracks active terminals in a `WeakMap` so a
  second mount into the same container cleans up the first timer.
- **Map loading HUD** — `#map.is-loading` now renders:
  - A `conic-gradient` radar sweep rotating around the viewport
    (`mix-blend-mode: screen`, 3.2 s linear spin).
  - Four corner brackets (`.map-scanframe > .mscf-tl/tr/bl/br`) drawn
    with pure CSS borders, `ensureMapScanframe(label)` / `clearMapScanframe()`
    are called from both `loadMapMarkers` and `loadHeatmap`.
  - A glowing monospace HUD label (`PLOTTING / GRID LIVE` or
    `HEATMAP / THERMAL`) with a pulsing green bullet.
  - The v0.3 top-edge progress bar is kept as the peripheral indicator
    with a brighter box-shadow.
- **Stats-badge boot sequence** — the `Loading…` placeholder in
  `index.html` now ships as a monospace terminal line
  (`> BOOT SEQUENCE INITIATED_`). `startStatsBadgeBoot()` cycles
  through `TERMINAL_MESSAGE_BANKS.boot` every 600 ms until
  `/api/stats` resolves and `showStats()` replaces the innerHTML.
- **Skeleton scanline pass** — `.result-card.skeleton` and
  `.detail-skeleton` now layer a bright diagonal scanline
  (`mix-blend-mode: screen`, `--term-scan` tint) on top of the
  existing shimmer gradient so the cards read as "incoming
  transmission".
- **Glitch-pulse `.loading-pulse`** — the old 1.5 s opacity pulse now
  runs alongside a 5 s `hack-glitch` animation that flashes a
  two-channel RGB split (cyan + danger) with a 1 px transform offset
  on about 5% of frames. Only perceptible at ~20 fps which dodges the
  "is this broken?" uncanny-valley effect.
- **Terminal palette tokens** — `--term-green`, `--term-amber`,
  `--term-cyan`, `--term-glow`, `--term-scan`, `--term-bg`,
  `--term-border` in `:root`. Referenced everywhere in the loading
  system so a theme switch lands in one place.
- **45 new tests in `tests/test_loading_system.py`** locking the
  CSS/HTML/JS contract: every required design token, every required
  selector, every keyframe, prefers-reduced-motion fallback block,
  boot-sequence markup, scanframe helper wiring, message bank
  presence for all 7 banks. Suite is now 79 tests and still runs in
  under a second.

### Changed
- **Every loading site now uses the terminal** (or at least the
  updated glitch-pulse):
  - `/api/search` → `mountLoadingTerminal(info, "search", {compact: true})`
  - `/api/map` → `ensureMapScanframe("PLOTTING / GRID LIVE")` +
    `loading-pulse PLOTTING SIGHTINGS` in the status pill
  - `/api/heatmap` → `ensureMapScanframe("HEATMAP / THERMAL")` +
    `loading-pulse COMPUTING HEATMAP`
  - `/api/duplicates` → `mountLoadingTerminal(info, "duplicates", {compact: true})`
  - `/api/sentiment/*` → `mountLoadingTerminal(statusEl, "insights", {compact: true})`
  - AI chat thinking state → `loading-pulse ANALYZING QUERY` + cursor

### Accessibility
- **`prefers-reduced-motion: reduce`** freezes the drifting scanline,
  skeleton scan pass, typewriter, and progress-bar slide. The cursor
  still blinks (opacity-only, no transform) and `loading-pulse` runs
  at a slower 2 s cycle. No vestibular triggers at reduced-motion.
- **`role="status"` + `aria-live="polite"`** on every terminal
  container so screen readers announce the current message instead
  of reading "loading" silently forever.

## [0.4.1] — 2026-04-09 — Stale-cache hotfix + test suite

### Fixed
- **Stale browser caches serving old CSS against new HTML.** Sprint 4
  shipped inline `<svg>` icons sized only via a new CSS `.icon` class.
  Browsers holding the pre-Sprint 4 `style.css` (which had
  `Cache-Control: public, max-age=604800` = 7 days) rendered SVGs at
  their default intrinsic size (~300×300 px), pushing the map
  off-screen. The class of bug is often called "HTML/CSS skew"
  ([commit `7377087`](https://github.com/UFOSINT/ufosint-explorer/commit/7377087)).

### Added
- **Asset versioning (`ASSET_VERSION`).** `app.py` computes a version
  string at startup (`GITHUB_SHA` env → `git rev-parse HEAD` →
  mtime-hash fallback) and substitutes a `{{ASSET_VERSION}}` placeholder
  in `index.html` on boot. Every deploy ships a fresh
  `/static/style.css?v=<sha>` URL that cannot collide with a stale
  cache entry.
- **Two-tier Cache-Control for static assets.** Versioned requests
  (`?v=…`) get `max-age=31536000, immutable`; unversioned requests fall
  back to `max-age=3600, must-revalidate`. A direct-link hit on
  `/static/style.css` can now be at most one hour stale.
- **Defensive `width`/`height` attributes on every inline SVG.** Even
  if the CSS fails to load, icons render at the intended size instead
  of the browser's default 300×150.
- **Pytest test suite (`tests/`).** 34 tests covering:
  - Every inline SVG has explicit width/height
  - HTML shell references versioned asset URLs
  - No emoji in static files (they must be SVG icons)
  - Required CSS design tokens are present
  - Two-tier Cache-Control policy is applied correctly
  - Every expected Flask route is registered
  - `/api/tools-catalog` stays in sync with `tools_catalog.TOOLS`
  - `node -c static/app.js` parses cleanly
  - Lint (`ruff check .`) passes
- **4-stage CI/CD pipeline** (`.github/workflows/azure-deploy.yml`):
  `test` → `build` → `deploy` → `smoke`. The smoke stage curls `/health`
  and `/` against the live deployment and fails the workflow if the
  HTML shell is missing its versioned asset URLs.
- **`requirements-dev.txt`, `pyproject.toml`** for ruff + pytest config.
- **Documentation**: `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`,
  `docs/TESTING.md`, and this `CHANGELOG.md`. The README now links out
  to them instead of trying to be everything.

### Changed
- `/` now returns a pre-substituted in-memory HTML string instead of
  `send_from_directory("static", "index.html")`. Flask serves static
  files the normal way for every other path.

## [0.4.0] — 2026-04-09 — Sprint 4: Visual identity

### Changed
- **Accent de-emphasis.** Dates and secondary headings moved from
  `--accent` to `--text-strong` / `--text-muted`. Hierarchy now comes
  from type treatment (uppercase, tracking, weight) rather than
  color spam. H1 stays accent as the brand exception.
- **Unified chip styling.** `.meta-pill`, `.shape-tag`, and
  `.collection-tag` now share one rule (pill radius, `--bg-card`,
  `--text-muted`, `--border`). The rogue `#6c5ce7` purple is gone.
- **Cards gain soft `--shadow-sm` at rest** on `.result-card`,
  `.dupe-card`, `.insight-card`, `.connect-card`, `.detail-section`.
  `.dupe-card`, `.dupe-row`, `.dupe-card-side`, and `.connect-card`
  now share the same `translateY + shadow-md` hover lift.
- **Monospace consolidation.** Five scattered `font-family` stacks
  now point at `var(--font-mono)`.

### Added
- **Design tokens**: `--text-strong`, `--font-sans`, `--font-mono`,
  categorical `--cat-1..8` palette (Tableau-10 desaturated), semantic
  `--success`/`--warning`/`--danger`/`--info` plus legacy aliases.
- **9 emoji replaced with inline Lucide-style SVG icons**: gear,
  message-square, plug, map-pin, download, link, custom UFO disc,
  alert-triangle. Shared `.icon` base class with `.icon-md`/`.icon-lg`/
  `.icon-xl` size modifiers.

Commit: [`a7b5d2f`](https://github.com/UFOSINT/ufosint-explorer/commit/a7b5d2f)

## [0.3.0] — 2026-04-09 — Sprint 3: Feel pass

### Changed
- **Tab-switch transitions** use visibility + opacity fades instead of
  hard `display: none` teleports. Respects `prefers-reduced-motion`.
- **Modal open/close** fades in and scales up from 0.96 for a cleaner
  "slam".
- **Filter bar density pass**: mobile filter drawer, "More filters"
  expandable drawer, is-dirty indicator on text inputs, auto-apply on
  select changes.

### Added
- Global `:active` pressed state on buttons so clicks have instant
  visual feedback before the request resolves.
- `<mark>` highlighting for search-term matches inside result cards.
- Filter-count badges next to Filters and More filters buttons.

Commit: [`2092218`](https://github.com/UFOSINT/ufosint-explorer/commit/2092218)

## [0.2.0] — 2026-04-09 — Sprint 2: Geographic completeness

### Added
- **Country and State/Region filter dropdowns** backed by
  `/api/filters`. Top-60 countries by sighting count.
- **Map place search** powered by Nominatim (CORS-enabled, free). Type
  a city or country and jump there.
- **"Near me" button** — browser geolocation with graceful permission
  fallback.
- **Export**: `/api/export.csv` and `/api/export.json` download the
  current filter set as files (capped at 5,000 rows).
- **Copy link** button copies a shareable URL with the current filters
  encoded in the hash.

### Fixed
- `/api/timeline` and `/api/search` count queries now LEFT JOIN
  `location` so `coords=geocoded` filters work on those endpoints.

Commit: [`de3f335`](https://github.com/UFOSINT/ufosint-explorer/commit/de3f335)

## [0.1.0] — 2026-04-09 — Sprint 1: Polish weekend

### Added
- **Accessibility pass**: `:focus-visible` ring on all interactive
  elements, modal focus trap + return-focus-to-trigger, `aria-live`
  regions on loading states, WCAG AA contrast for `--text-faint`.
- **Label associations** with `<legend>` / `<label for=…>` across
  the filter bar.
- **Keyboard escape** closes the sighting detail modal.
- **Search skeleton loaders** match the real result card shape so the
  page doesn't jump when results arrive.

Commit: [`7ecb46f`](https://github.com/UFOSINT/ufosint-explorer/commit/7ecb46f)

## [0.0.x] — Pre-sprint prehistory

Unversioned snapshot of the POC era. Major milestones:

- **MCP server + BYOK chat** ([`ed020b8`](https://github.com/UFOSINT/ufosint-explorer/commit/ed020b8))
  — one shared `tools_catalog.py` exposes six tools via `/mcp`
  (JSON-RPC over HTTP for Claude Desktop, Cursor, etc.) and
  `/api/tools-catalog` (OpenAI function format, used by the in-browser
  BYOK chat).
- **PostgreSQL migration** ([`3a6f15d`](https://github.com/UFOSINT/ufosint-explorer/commit/3a6f15d))
  — replaced SQLite-on-Azure-Files (196s cold queries) with Azure
  Database for PostgreSQL Flexible Server (B1ms). ~100–2000× speedup.
- **Railway → Azure migration** ([`4653856`](https://github.com/UFOSINT/ufosint-explorer/commit/4653856))
  — App Service B1 Linux, GitHub Actions deploy.
- **Insights tab** ([`fdae0ee`](https://github.com/UFOSINT/ufosint-explorer/commit/fdae0ee))
  — sentiment and emotion dashboard.
- **Geocoding support** ([`4a49803`](https://github.com/UFOSINT/ufosint-explorer/commit/4a49803))
  — coord-source toggle (All / Original / GeoNames).
