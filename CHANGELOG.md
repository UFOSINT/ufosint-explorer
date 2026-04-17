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

### Added
- **Place search now filters results** to the searched bounding box.
  Typing "Arizona" in the map place search applies the Nominatim
  bbox as a rectangle region filter in addition to panning the
  map. Previously the map zoomed but the sighting set was unchanged
  — users had to manually draw a polygon. One-click-clearable via
  the region chip. See `docs/V013_UX_POLISH_PLAN.md` §5.

### Changed
- **Header stats badge collapsed to a single "${total} sightings"
  chip.** The 5-chip middle-dot chain (`total · mapped · high
  quality · with movement · possible duplicates`) that wrapped to
  2-3 lines at most viewport widths is gone; click the badge for
  the detail popover, which already carried every derived count.
  Measured: header height stays at 53 px across viewports instead
  of growing to 108 px / 237 px. See §1.

### Fixed
- **Mobile filter drawer no longer collapses the map to 0 px.**
  When a user tapped the hamburger on a phone, the filter bar
  wrapped to 200+ px in the flex flow, squeezing the map
  container below the viewport. `#filters-bar` + movement row now
  become `position: fixed` overlays on mobile + touch, with a
  subtle backdrop. Map keeps its full height. See §2.
- **TimeBrush selection handles grabbable on touch.** Base handles
  are 14 px / 6 px wide; the `body.is-touch` pseudo-element hit
  overlay now reaches a proper 44 × 44 px minimum without changing
  visual size. Overview mini-map handles got the same treatment
  (previously had no touch override). See §3.
- **"READY" ghost at lower-left of the map** after the intro
  dissolve. The `#map-status` panel had "READY" as its HTML default
  value which persisted until something wrote over it. Default is
  now empty + a `.panel-status:empty { display: none }` rule. See §4.
- **Phone header overflow.** The "UFOSINT Explorer" title was
  wrapping to 2 lines in the mobile header and eating horizontal
  space from the tab bar, forcing Methodology / Insights to scroll
  off the right edge. Title is now `display: none` below 720 px;
  the Observatory tab + page `<title>` still identify the site.
- **TimeBrush bottom-edge gestures.** Handles sat 4-8 px from the
  viewport edges, so aiming for them on iOS Safari triggered the
  browser's swipe-back (left), swipe-forward (right), and home
  indicator (bottom) gestures instead. Bumped horizontal padding
  to 18 px, added `padding-bottom: calc(22px + safe-area-inset-bottom)`
  so handles sit clear of the edge-gesture zones + iPhone X notch.
  Also `overscroll-behavior-x: contain` as a belt-and-suspenders
  guard against horizontal gesture chaining.
- **`viewport-fit=cover`** added to the viewport meta tag so iOS
  actually resolves `env(safe-area-inset-*)` to non-zero on notched
  devices. Without this the above `calc()` was effectively just the
  22 px fallback on every iPhone.
- **Observatory topbar overflowed horizontally on phone.** Points /
  Heatmap / Hex + Crashes / Nuclear / Facilities + REGION + COLOR /
  SIZE + LAT/LON HUD + gear were all on one absolute-positioned row
  that ran off the right edge (~885 scrollWidth vs ~303 viewport).
  Now `flex-wrap: wrap` on mobile, with the LAT/LON/READY HUD hidden
  (not useful on thumb-on-map workflow, and the widest single element
  was the biggest space-waster). Partial fix for
  `docs/V013_UX_POLISH_PLAN.md` §10.
- **TimeBrush overview mini-map pushed off the bottom on phone.**
  The brush-header wrapped to 2-3 rows at narrow widths and the
  140 px `min-height` wasn't enough to fit header + histogram +
  overview. Bumped to 190 px on mobile and added `flex-wrap` to
  the brush-header so layout is deterministic.

## [0.12.3] — 2026-04-17 — TCP keepalive + Always On (prod resilience, hotfix)

Third prod wedge in 24 h. Root cause: `alwaysOn` was **false** on the
App Service — Azure idled the container after ~20 min of no traffic.
On wake-up the pool's TCP sockets were dead at the kernel level, but
the default Linux TCP keepalive timeout is ~2 h so the OS didn't know.
`check=SELECT 1` hung on the dead FD → same cascade as before.

### Fixed
- **TCP keepalive on all PG connections** (`keepalives=1`,
  `keepalives_idle=60`, `keepalives_interval=10`, `keepalives_count=5`).
  The OS now probes idle sockets every 60 s and declares them dead
  after ~110 s (60 + 10×5). When `check` runs on a socket the OS
  already knows is dead, it fails instantly → pool replaces it → no
  hang. This is the missing piece that makes Layer 1 (`check=`) work
  reliably even when Azure drops packets silently.

### Ops
- **Always On enabled** on `ufosint-explorer` App Service. Container
  stays warm, connections don't go stale from idle sleep. TCP keepalive
  is the belt; Always On is the braces.

## [0.12.2] — 2026-04-16 — Fail-fast health check + statement/connect timeouts (prod resilience, hotfix)

Second prod wedge on 2026-04-16 proved v0.12.1's Layer 1 alone wasn't
enough: when Azure's network path *silently drops* packets rather than
RST'ing, `check=SELECT 1` hangs on the dead socket until the OS-level
TCP timeout fires. See `docs/OPERATIONS.md` §4.

### Fixed
- **`/health` now returns HTTP 503 on DB failure** (was 200 with
  `status:"waiting"`). Azure App Service Health Check had no way to
  detect a wedged worker because the endpoint lied; the LB happily
  kept routing 30 s-timeout traffic to it. Happy path unchanged
  (`200 {"status":"ok","sightings":N}`) so the deploy smoke probe
  still works.
- **Pool `timeout` 30 s → 8 s.** Requests that can't check out a
  connection now return 503 to the LB in 8 s instead of holding
  the worker thread for 30 s. Pairs with Azure Health Check eviction.
- **`connect_timeout=5`** on pool `kwargs` — caps how long opening
  a *fresh* connection can block, so a mass eviction + refill can't
  stall getconn() indefinitely.
- **`statement_timeout=25000`** in the PG `options` string — server-
  side kill switch for any runaway query (25 s; slightly under pool
  checkout + margin so clients see the PG error not a pool timeout).

### Ops
- Azure App Service Health Check enabled at `/health` (10 min LB
  threshold). Unhealthy instances are auto-restarted instead of
  relying on manual `az webapp restart`.

## [0.12.1] — 2026-04-16 — Pool self-healing (prod resilience)

Prevents the 2026-04-16 prod incident (see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) §4) from recurring.

### Fixed
- **Wedged `psycopg_pool`** when connections go idle longer than
  Azure's network-path timeout. Every `getconn()` was handing out
  zombie sockets, queries hung 30 s, `PoolTimeout` cascaded into
  an `/api/*` 500-storm.
  - `check=ConnectionPool.check_connection` — ping each connection
    before handing it out; dead ones are discarded and replaced.
  - `max_idle=300` — close connections idle > 5 min.
  - `max_lifetime=3600` — recycle every hour as defense in depth.
- `tests/conftest.py` — `_FakePool` gains a `check_connection`
  staticmethod stub so the import line resolves without the real
  `psycopg_pool` class.

### Added
- `docs/OPERATIONS.md` — runbook, incident log, known failure
  modes. Cross-linked from README docs table and CLAUDE.md.

## [0.12.0] — 2026-04-15 — UAP Gerb curated overlay (Crashes + Nuclear + Facilities)

Ships the `feature/gerb-overlay` branch — curated research data from the
UAP Gerb project layered over the sighting corpus for proximity analysis
and visual correlation.

### Added
- **`/api/overlay` endpoint** returning three curated datasets as a
  single JSON payload (~30 KB, cached 10 minutes): 14 crash retrievals,
  35 nuclear encounters, 75 facilities.
- **Three toggleable map layers** on Observatory — Crashes, Nuclear,
  Facilities. Each marker opens a detail popup with the curated
  metadata (craft type, recovery status, weapon system, sensor
  confirmation, etc.).
- **Timeline annotation stems** on the TimeBrush for crash + nuclear
  events. Labels appear at higher zoom levels; text-stroke outlines
  make them readable against the histogram. De-overlap logic skips
  colliding labels (stems stay; hover for tooltip).
- **Nuclear proximity on every sighting** — `distance_to_nearest_nuclear_site_km`
  and `nearest_nuclear_site_name` columns computed by `gerb_overlay.py`
  via haversine against 50 nuclear-relevant facilities (396k populated).
- **NRC Lexicon word-counts** — 10 new sighting columns
  (`nrc_joy`, `nrc_fear`, `nrc_anger`, `nrc_sadness`, `nrc_surprise`,
  `nrc_disgust`, `nrc_trust`, `nrc_anticipation`, `nrc_positive`,
  `nrc_negative`) denormalized from the sentiment analysis so raw
  emotion counts stay queryable on the public DB after
  `sentiment_analysis` is dropped in the export.

### Changed
- `scripts/migrate_sqlite_to_pg.py` extended with the v0.12 column
  list and the three new overlay tables.
- `/llms.txt` and `docs/ARCHITECTURE.md` updated with the new endpoint
  and tables.

### Fixed
- **TimeBrush NaN freeze** when dragging selection handles past the
  dataset bounds. Three guards: drag handler bails on NaN/Infinity,
  pan handler same, and `_syncWindow` auto-recovers to the full range
  instead of staying frozen.
- **Overlay markers blocking point clicks** — same `preferCanvas` bug
  as the region-shape regression. Overlay layers now don't steal
  pointer events from the sighting markers underneath.
- **Ruff B905** in `api_overlay` — `strict=False` on `zip()`.

### Schema
- 3 new PG tables (`crash_retrieval`, `nuclear_encounter`, `facility`).
- 12 new `sighting` columns (10 NRC + 2 nuclear proximity).
- Applied to prod via `scripts/add_v012_gerb_nrc_columns.sql`
  (idempotent, safe to re-run).

## [0.11.9] — 2026-04-15 — Region (geofence) draw tool + Live Analytics sidebar

Large feature drop spanning v0.11.3 through v0.11.9 on the
`feature/geofencing` branch. Ships:

### Added
- **Region (geofence) draw tool** on the Observatory topbar with a
  shape picker (Rectangle / Polygon / Ellipse). The drawn shape
  filters all sightings to that spatial region across Observatory,
  Timeline, and Insights tabs.
  - **Rectangle** — click-drag corners. Dashed cyan outline with
    glow during drag; becomes a persistent Leaflet rectangle once
    applied.
  - **Polygon** — click each vertex to place, double-click or click
    the first vertex to close. Vertex markers are draggable mid-
    draw so you can fine-tune before closing. Minimum 3 vertices.
  - **Ellipse** — click-drag corner-to-corner. Rendered as a
    64-vertex polygon approximation that matches the filter math
    (standard `(x/a)² + (y/b)² ≤ 1` in lat/lng space).
- **Region ON/OFF toggle** on the TimeBrush bar. Temporarily
  disables the spatial filter without clearing the geometry, so
  you can A/B compare data with and without the region.
- **URL hash persistence** for all three shapes (2-decimal
  precision): `rect:s,w;n,e`, `ellipse:s,w;n,e`,
  `poly:lat1,lng1;...;latN,lngN`. Shapes survive page refresh and
  tab switches.
- **Observatory Live Analytics sidebar** — replaces the old dead
  Sources/Shapes rail sections with a dashboard that updates live
  on every filter change:
  - Visible count + percentage of total
  - Top Shapes horizontal bar chart (top 8)
  - By Source stacked proportional bar + per-source bars
  - Quality Score Distribution 10-bucket histogram (red / yellow
    / cyan by score range)
  - Time window (unchanged)
- **Observatory Data Quality gear icon** in the topbar matching the
  pattern already on Timeline + Insights.
- **Histogram normalization** on the TimeBrush — filtered bars
  now scale to their own in-view peak instead of the unfiltered
  max, so the SHAPE of the filtered subset is always legible even
  at tight filters (1% of rows).
- **Feature tour step** for the REGION tool.

### Changed
- Polygon vertex markers switched to Leaflet-native `L.circleMarker`
  (previously custom SVG overlay that had z-index issues with the
  Leaflet pane stack and was invisible during drawing).
- Ellipse + polygon preview shapes switched to Leaflet-native
  layers (`L.polygon`, `L.polyline`). Rectangle kept as a DOM DIV
  overlay since it always worked.
- `applyClientFilters` now passes `bbox` and `regionShape` fields;
  `_rebuildVisible` in deck.js runs the bbox cull first (cheap)
  then exact point-in-shape tests (ray-cast for polygon, standard
  ellipse formula) only on points that pass.

### Fixed
- Polygon click-to-place was being eaten by deck.gl's canvas
  handler; switched to `pointerdown`/`pointerup` pattern with a
  5px movement threshold.
- Rectangle drag preview was invisible because its DIV overlay
  was at z-index 620, below Leaflet's marker/tooltip panes.
  Raised to 1100 (above the popup pane at 700).
- Accidental point clicks while panning — now suppressed if the
  mouse moved >5px between mousedown and mouseup.

### Removed
- Dead SVG overlay (`#region-draw-svg` and related) — superseded
  by Leaflet-native preview layers.
- Observatory rail Sources + Shapes sections (duplicated the top
  filter bar and the per-source checkboxes had broken semantics).
- `window._ufoDeckPick` debug handle.

### Infra
- New staging App Service at
  `ufosint-explorer-staging.azurewebsites.net` (B1 tier, shared
  Postgres with prod). Feature branches auto-deploy to staging
  via `.github/workflows/azure-deploy-staging.yml` so production
  is never touched until a merge to `main`.

Tests: 570 total (+64 new for v0.11.4–v0.11.9 features).

## [0.11.2] — 2026-04-12 — Cinematic landing, AI readiness, mobile fixes, credits

### Added
- **Cinematic landing animation** — terminal-style counter ticks to 614,505 on first visit, then dissolves to reveal the map
- **Guided tooltip tour** — 5-step spotlight walkthrough (map, rail, TimeBrush, tabs, stats badge) with clip-path backdrop cutout
- **Help button** — `?` icon in header replays tour anytime; localStorage prevents repeat on return visits
- **AI-readiness endpoints** — `/robots.txt`, `/llms.txt`, `/llms-full.txt`, `/.well-known/mcp.json` for agent/LLM discovery
- **Schema.org JSON-LD** — `Dataset` markup in `<head>` for AI search engine discoverability
- **Credits modal** — accessible via gear menu; attributes @ufohackers (data/research), @DuelingGroks (engineering), Claude Code (AI-assisted dev)
- **`--surface-1` / `--surface-2`** CSS tokens for popup backgrounds across all three themes

### Changed
- **Connect panel** — added Claude Code CLI command (`claude mcp add`), AI Discovery card listing all discovery endpoints
- **Methodology page** — rewritten intro ("This is not raw data"), added reproducibility statement, full v0.11 emotion analysis section with 4 models documented
- **README.md** — complete rewrite for v0.11 (features, binary buffer, emotion analysis, MCP setup)

### Fixed
- **Mobile: header tabs** — horizontal scrolling row instead of vertical stacking; settings gear + help button visible on mobile (were hidden)
- **Mobile: tour tooltip** — fixed bottom-sheet positioning so Skip/Next buttons are always visible
- **Mobile: TimeBrush** — `height: auto` + `min-height` so histogram bars visible below wrapped controls
- **Mobile: modal detail grid** — collapses to 1-column below 600px
- **Mobile: filter groups** — removed min-width 140px conflict causing overflow at 375px
- **Mobile: chat popover** — capped height to 60vh on small screens
- **Mobile: touch targets** — 44px minimum on rail collapse buttons, 32px on tabs, 18px brush handles
- **Popup readability** — all popups were transparent (--surface-1 undefined); now properly opaque

## [0.11.1] — 2026-04-12 — Playback performance, DQ gear popup, progress bar

### Added
- **Data Quality gear popup** on Timeline and Insights tab headers — mirrors Observatory rail toggles without switching tabs
- **Playback progress bar** — thin accent strip on brush header showing sweep position
- **Badge counter** on gear icons showing active DQ filter count

### Changed
- **Playback performance** — `refreshTimelineCards(true)` and `refreshInsightsClientCards(true)` skip coverage strip computation (~8ms) and DOM updates (~3ms) during playback animation, cutting per-frame JS from ~20ms to ~8ms

## [0.11.0] — 2026-04-12 — Transformer emotion analysis, shared TimeBrush

### Added
- **v0.11 emotion data** — 502,985 sightings analyzed by 4 models: RoBERTa 3-class sentiment, RoBERTa 7-class emotion, GoEmotions 28-class, VADER compound
- **12 new sighting columns** — `emotion_28_dominant`, `emotion_28_group`, `emotion_7_dominant`, `vader_compound`, `roberta_sentiment`, plus score vectors
- **5 new Insights cards** — Sentiment Polarity, Emotion Distribution (7-class), GoEmotions Detail (28-class with neutral toggle), Sentiment Score Distributions, Emotion Profile by Source
- **40-byte binary schema** (v011-1) — 5 new uint8 fields packed into the bulk buffer; VADER/RoBERTa scores scaled [-1,+1] → [0,255]
- **Shared TimeBrush** — moved out of Observatory panel, visible on Observatory + Timeline + Insights tabs
- **Animated Insights/Timeline** — charts update during playback via throttled refresh (4fps)

### Changed
- Insights tab reorganized into 3 sections: Emotion & Sentiment (5 cards), Data Quality (2 cards), Movement & Shape (2 cards)
- Coverage strips on all 9 Insights cards (green/yellow/orange/red pills)

## [0.10.0] — 2026-04-12 — Cross-filtering, zoom-aware charts, live filters

### Added
- **Cross-filtering** — click any chart segment to filter all other cards to that subset; cyan chip bar shows active filter
- **Zoom-aware Timeline** — charts mirror TimeBrush zoom range with day/month/year granularity toggle
- **Live-reactive filters** — selects auto-apply at 250ms debounce, date inputs at 500ms; no Apply button needed
- **Overview mini-map** — bilinear-scale overview below TimeBrush (15% pre-1900, 85% post-1900)

### Fixed
- Source dropdown breaking Insights (read `.value` instead of `.selectedOptions[0].text`)
- Quality rail permanently disabled on first load (removed `dataset.mounted` guard)
- Filters not flowing through on Timeline/Insights tabs

## [0.9.2] — 2026-04-12 — TimeBrush adaptive granularity + live commit + Apply button

Three small improvements to the Observatory TimeBrush that make
sub-year playback actually useful:

1. **Adaptive histogram granularity.** The v0.9.0 brush always
   rendered year bars regardless of zoom level — at a 3-month
   zoom the user saw 0-1 bars, a solid useless block. v0.9.2
   adds month and day bar variants. The brush picks the right
   granularity automatically based on view span:
   - View span > 10 years → year bars (126 bars at full range)
   - View span 1-10 years → month bars (1512 at full range)
   - View span < 1 year → **day bars** (~46,000 at full range,
     ~90 visible at a 3-month zoom)

   Cost: one walk of `POINTS.dateDays` per granularity,
   cached indefinitely. Day histogram is the cheapest of the
   three (direct integer subtraction per row, no binary
   search) at ~5-10 ms. Month uses a precomputed monthStarts
   lookup table and binary search at ~15-20 ms. All cached;
   zoom changes are O(1) after the first fetch.

2. **Live commit during drag.** v0.9.0 dragged visually only
   and committed on pointerup, which meant no live map
   feedback while the user was adjusting the window. v0.9.2
   adds `_liveCommit()` that calls `UFODeck.setTimeWindow()`
   directly on every pointermove — the same code path the
   Play loop uses at 60fps. Bypasses the form-input + URL-hash
   pipeline (those still run on pointerup) so there's no
   history churn.

   Result: drag the selection, the map re-tallies
   continuously. Release, the final commit writes form
   inputs + hash.

3. **Explicit Apply button** in the brush header next to Reset
   View. Force-applies the current selection via the full
   filter pipeline. Useful when the user arrives at the brush
   via URL hash or programmatic call and the live commit
   didn't run.

Bonus: the minimum selection window dropped from 30 days to
7 days so users can set a one-week selection for playback.
Narrower than that and the histogram becomes visually
unreadable.

### Added

- **`deck.js` adaptive-granularity histograms**
  (`getHistogram(granularity)` +
  `getHistogramForGranularityVisible(granularity)`) returning
  `[{ startMs, count }]` for `year`/`month`/`day` granularities.
  Shared builder `_buildHistogram(iter, gran)` branches on
  granularity; unfiltered results cached in `_histCache`.
- **`_buildMonthStarts()`** precomputes the month-starts lookup
  table once per session (~1500 entries) so month-histogram
  binary search is cheap.
- **`TimeBrush._pickGranularity()`** — returns
  `"year"|"month"|"day"` based on `_viewSpan()`.
- **`TimeBrush._getFullBins(gran)` / `_getFilteredBins(gran)`**
  — wrap the deck.js helpers with a per-brush cache. Filtered
  slot cleared on every `retally()`.
- **`TimeBrush._binsCache`** three-slot cache
  `{ year: {full, filtered}, month: {...}, day: {...} }`.
- **`TimeBrush._liveCommit()`** — pushes the current window to
  `UFODeck.setTimeWindow(dayFrom, dayTo, {dayPrecision:true})`
  for 60fps live feedback during drag.
- **`TimeBrush.applyNow()`** — force-commits the current window
  via `_onChangeRaw`, same path as pointerup. Wired to the new
  `#brush-apply` button.
- **`#brush-apply` button** in `index.html` brush header.
- **`tests/test_v092.py`** — 21 new tests locking the
  adaptive-granularity contract, live-commit wiring, and Apply
  button.

### Changed

- **`TimeBrush._draw`** rewritten to pick granularity, fetch
  bins via `_getFullBins`/`_getFilteredBins`, and iterate
  `bins[i].startMs` instead of `bins[i].year`. All three
  granularities route through a single `_viewTimeToPx(startMs)`
  call so the math is uniform. Falls back to the legacy
  year-only path when deck.gl helpers aren't available.
- **`TimeBrush.retally(bins)`** now just sets `_hasActiveFilter`
  and invalidates the three granularity filtered slots; the
  `bins` argument is kept for backward compat but no longer
  read. Next `_draw()` recomputes from the new POINTS.visibleIdx.
- **`TimeBrush._bindEvents` onPointerMove** calls `_liveCommit()`
  after `_syncWindow()` for `move`/`l`/`r` drag modes. Pan
  mode skips the commit (pan only changes the view, not the
  selection).
- **Minimum selection window** dropped from 30 days to 7 days
  in the `l`/`r` handle clamps so sub-year playback works.
- **`mountQualityRail` / `updateQualityBiasBanner`** — no
  change, but this is the first v0.9.x release where the bias
  warning is visible in the wild because users will actually
  flip the toggle now that filters feel responsive.

### Fixed

- **3-month zoom used to show 0-1 bars.** Now shows ~90 day
  bars with visible daily variation in sighting counts.
- **Drag-to-select had no live map feedback.** Users had to
  release to see what they'd selected. Now updates in real
  time.

## [0.9.1] — 2026-04-12 — Correctness hotfix + Insights coverage panels

Informed by a UX agent review and a Science agent review run in
parallel before implementation. Both landed after v0.9.0 shipped
and agreed independently on several issues — v0.9.1 fixes the
ones that are actually wrong (not just suboptimal) plus adds
the single cheapest honesty improvement.

### Wave 1 — Correctness hotfix

The v0.8.8 build had several places where the app was silently
wrong, not just imprecise:

- **Year-0019 records.** 692 UFOCAT records with a 2-digit "19"
  sentinel (meaning "1900s, year unknown") were documented as
  set to NULL by the v0.8.3b date-fix pipeline. That fix never
  landed on Azure PG, so `/api/stats.date_range.min` returned
  "0019-01-01" and `/api/timeline` showed a `{"0019": {...}}`
  bucket — pretending 692 sightings happened in 19 AD. v0.9.1
  adds query-time guards in three endpoints (`/api/stats` live +
  MV paths, `/api/timeline` live + MV paths,
  `/api/sentiment/timeline`) and ships a one-shot
  `scripts/fix_year_0019.sql` cleanup script with audit logging,
  idempotency checks, and a post-update sanity assertion.
- **`/api/timeline?bins=monthly`** was silently ignored — the
  server returned year-level data regardless. v0.9.1 honors the
  parameter by routing monthly requests to the live path and
  returning `{"mode": "monthly"}` in the response.
- **`sources[0]` was `None`** in the bulk buffer meta sidecar.
  Client-side charts rendering a per-source category silently
  dropped orphaned-FK rows into a null bucket. v0.9.1 renames
  the slot to the literal string `"(unknown)"` so those rows
  get a labeled category, AND counts them in
  `coverage.orphaned_source` so the client can detect the
  data-integrity issue via the meta sidecar.
- **Methodology page lede** claimed "126,730 duplicate
  candidate pairs are flagged for review" — but
  `/api/stats.duplicate_candidates = 0`. That's the
  pre-v0.8.3b number; the `duplicate_candidate` table ships
  empty in the current build because dedup moved to ingest
  time. v0.9.1 strikes the claim from the lede and adds a
  correction banner right below it.
- **"Hoax likelihood"** was labelled and rendered as a
  probability. It's a keyword-match heuristic. v0.9.1 renames
  all user-facing strings to "narrative red flags" with a
  tooltip disclaimer, keeps the underlying column name for
  backward compat, and adds an Insights card subtitle
  explaining it's a heuristic, not a calibrated posterior.
- **"Had description"** toggle implied the narrative text is
  readable. In the public DB raw text is stripped. Renamed to
  "Had description (in source)" with a "classifier ran; text
  not retained" sub-label.

A new persistent **bias warning banner** appears inside the
Observatory rail whenever the "High quality only" toggle is
active. Text explains that the composite score rewards modern
MUFON-investigated reports and that downstream charts inherit
that bias.

### Wave 2 — Insights coverage panels

Every Insights card now surfaces a **coverage strip** at the
bottom:

```
Emotion classifier populated    35.4%    n = 23,814 / 67,203
```

A green/yellow/orange/red pill encodes the coverage band:

- **Green (≥ 80%)** — data is dense enough for the chart to be
  trusted without caveats
- **Yellow (50-80%)** — chart is meaningful but the user should
  know the subset
- **Orange (30-50%)** — card is dimmed to 0.72 opacity; visual
  hierarchy signals "don't over-interpret this"
- **Red (< 30%)** — card is dimmed further (0.45 opacity) and
  displays a big red "INSUFFICIENT DATA — < 30% COVERAGE"
  banner above the chart

This was the Science reviewer's single top recommendation:
"the single cheapest correction to the tool's biggest honesty
problem." A single walk of `POINTS.visibleIdx` computes coverage
for all 11 derived columns in a few milliseconds. Cards wire
into a new `_mountAllCoverageStrips()` helper called from
`refreshInsightsClientCards` so the early-return-on-chart-update
pattern in each renderer doesn't swallow the coverage call.

### Added

- **`_computeInsightsCoverage(P)`** helper in `app.js` — walks
  `POINTS.visibleIdx` once and returns `{ total, dated, quality,
  hoax, richness, shape, color, emotion, hasDescription,
  hasMedia, hasMovement, movementFlags }` each as `{ n, pct }`.
- **`_renderCoverageStrip(canvasId, covEntry, label)`** — mounts
  or updates a coverage strip at the bottom of a given card.
  Colors the pill based on percentage band and toggles
  `.is-low-coverage` / `.is-critical-coverage` classes on the
  card.
- **`_mountAllCoverageStrips()`** — calls `_renderCoverageStrip`
  for all 8 client-side cards. Runs once per
  `refreshInsightsClientCards` call.
- **`updateQualityBiasBanner()`** — shows/hides the in-rail
  warning banner based on whether `state.qualityFilter.highQuality`
  is active.
- **`scripts/fix_year_0019.sql`** — one-shot DB cleanup with
  audit logging to the `date_correction` table, idempotent via
  `NOT EXISTS` guard, refreshes `mv_stats_summary` + mv_timeline_yearly`
  and asserts no `0019-%` records remain post-cleanup.
- **`.meth-banner-note`** CSS class for the methodology current-
  build correction banner.
- **`.quality-bias-banner`** CSS class for the in-rail bias
  warning.
- **`.insight-coverage-strip`** + `.cov-pill.cov-{hi,mid,midlo,low}`
  CSS for the per-card coverage readouts.
- **`.insight-card.is-low-coverage`** / `.is-critical-coverage`
  CSS for the dimming + INSUFFICIENT DATA banner.
- **`tests/test_v091.py`** — 25 new static source-inspection
  tests locking every point of the v0.9.1 contract.

### Changed

- **`_api_stats_from_mv()`** detects a stale `date_min` starting
  with "0019-" and re-queries the live table to override with
  the correct value.
- **`_api_stats_from_live()`** adds `date_event NOT LIKE '0019-%'`
  to the MIN/MAX query.
- **`api_timeline()`** reads a `bins` query parameter, branches
  to a full-range-monthly SQL path when `bins=monthly`, adds the
  `NOT LIKE '0019-%'` guard on the live clauses, and drops the
  `"0019"` key from the MV post-processing.
- **`api_sentiment_timeline()`** adds the same `NOT LIKE '0019-%'`
  guard.
- **`/api/points-bulk?meta=1`** — `sources[0]` is now
  `"(unknown)"`; `coverage.orphaned_source` + `orphaned_shape`
  counters track rows whose FK didn't resolve at pack time.
- **Methodology lede** — struck the 126,730 duplicate-candidate
  claim; added a correction banner calling out the current-build
  state.
- **`mountQualityRail()`** — creates and manages the
  `.quality-bias-banner` element; updates it on every toggle
  change.
- **`refreshInsightsClientCards()`** — now computes coverage
  before rendering any chart and calls
  `_mountAllCoverageStrips()` after.
- **"High quality only" + "Hide likely hoaxes" toggles** —
  renamed to "Hide narrative red flags" with a "flag score"
  sub-label; the "High quality only" label stays but gains
  the bias warning banner.
- **"Hoax Likelihood Curve"** Insights card title renamed to
  "Narrative Red Flags (keyword heuristic)".
- **Detail modal** — "Hoax likelihood: 0.42" renamed to
  "Narrative red flags: 0.42" with a hover tooltip explaining
  it's a keyword-match heuristic, not a probability.
- **`tests/test_v080_bulk.py`** — updated
  `meta["sources"][0]` assertion from `is None` to
  `== "(unknown)"`.

### Fixed

- **`/api/stats.date_range.min`** no longer returns "0019-01-01".
- **`/api/timeline` response** no longer contains a
  `{"0019": {"UFOCAT": 692}}` bucket.
- **`/api/timeline?bins=monthly`** actually returns monthly
  data instead of silently downgrading to yearly.
- **Stats badge popover** shows the corrected date range.
- **Insights charts** no longer render identical layouts at
  5% coverage vs 95% coverage.

## [0.9.0] — 2026-04-11 — TimeBrush zoom/pan + mobile responsive layout

Two big UX improvements:

1. **The Observatory TimeBrush now zooms.** Scroll wheel over the
   brush zooms centered on the cursor (classic Google Maps
   pattern); click and drag on empty canvas pans the view; the
   Reset View button (plus double-click on the canvas) restores
   the full 1900—2026 range. The selection window and playback
   are **orthogonal to zoom**: you can zoom in, place a 3-month
   window with pixel-level precision, zoom back out, and Play
   still animates the same 3-month window.

   When zoomed to a narrow range, the window readout switches
   from year-only ("1997 — 1998") to year-month ("1997-06 —
   1997-08") to year-month-day ("1997-06-15 — 1997-08-14")
   depending on the span. Tells you at a glance when your
   selection has sub-year precision.

2. **The site is now mobile-responsive.** Two parallel triggers
   flip the Observatory to a one-column layout with the rail as
   a collapsible accordion above the map: `body.is-touch` from
   a `matchMedia("(hover: none) and (pointer: coarse)")` feature
   detect (catches real phones, foldables, iPads-in-landscape),
   AND `@media (max-width: 700px)` for desktop users resizing
   their browser narrow. Both branches are intentional — touch
   detection catches wide-viewport touch devices, width catches
   narrow-viewport desktops.

   On mobile the rail sections become clickable headers: Data
   Quality stays expanded by default (it's the primary filter
   path), Sources/Shapes/Visible/Time Window start collapsed.
   Tap a chevron header to expand. The filter bar wraps onto
   2-3 rows, the movement cluster scrolls horizontally, the
   stats badge drops optional chips, and the TimeBrush grows
   to 115px tall with wider handle hit areas for touch.

### Added

- **`TimeBrush` zoom state** — new `viewMinT` / `viewMaxT` on
  the class, distinct from `this.window[0]`/`[1]` (the playback
  selection). `_minViewSpanMs = 7 * 86400000` caps max zoom-in
  at 1 week of real time. Three view-aware helpers:
  `_viewSpan()`, `_pxToViewTime(px)`, `_viewTimeToPx(t)`.
- **Scroll-wheel zoom handler** on the brush canvas wrap.
  Uses cursor-centered zoom math so the time value under the
  cursor stays stationary as the view shrinks or grows around
  it. Factor 0.8 zoom-in per wheel tick, 1.25 zoom-out.
- **Drag-to-pan on empty canvas** — a new `"pan"` mode in the
  existing pointer-drag state machine. Click empty canvas area
  (not the selection window, not a handle) and drag to translate
  the view. Clamped to dataset bounds.
- **`resetView()` method** on `TimeBrush` plus `_updateResetViewBtn()`
  helper. The Reset View button in the brush header is hidden
  by default and shown whenever the view span is < 98% of the
  full range. Also wired to `dblclick` on the canvas.
- **`_formatWindowLabel(d0, d1)` helper** picks year-only /
  year-month / year-month-day formatting based on span.
- **Touch-primary feature detect** in DOMContentLoaded. Uses
  `matchMedia("(hover: none) and (pointer: coarse)")` and
  toggles `body.is-touch`. Listens for media query changes so
  orientation flips and foldable unfolds update the layout live.
- **`hydrateRailCollapsibles()`** — wires click handlers on the
  new rail collapse buttons. Uses a dual-class model
  (`.is-collapsed` for user-collapsed default-expanded sections;
  `.is-expanded` for user-expanded default-collapsed sections)
  so the CSS can encode the correct visible state regardless of
  which media query is active.
- **Stats badge `.stats-chip-optional` class** — wraps the
  high-quality / with-movement / possible-duplicates chips so
  mobile CSS can hide them without losing them from the popover.
- **`#brush-reset-view` button** in the brush header.
- **5× `.rail-collapse-btn` + `.rail-chevron` + `.rail-body`**
  wrappers on the Observatory rail sections. SVG count jumped
  7 → 12.
- **`tests/test_v090.py`** — 24 tests locking zoom state, view-
  aware draw math, wheel/pan/dblclick bindings, `resetView`
  method, feature detect, accordion handler, rail HTML, and
  responsive CSS.

### Changed

- **`TimeBrush._draw`** iterates view range only. Bars outside
  `[viewMinYear, viewMaxYear]` are skipped. Bar x-coords come
  from `_viewTimeToPx(Date.UTC(b.year, 0, 1))` instead of a
  hardcoded `(b.year - BRUSH_MIN_YEAR) / yearSpan` ratio, so
  the math works at any zoom level. Ghost (unfiltered) +
  foreground layers share a single `drawLayer()` closure.
- **`TimeBrush._syncWindow`** clips the selection rectangle to
  the current view range. When the window extends beyond the
  view on one side, the rectangle gets `extends-left` /
  `extends-right` classes. When entirely outside the view,
  the rectangle hides (selection data preserved). The readout
  label uses `_formatWindowLabel` for variable precision.
- **`TimeBrush._drawAnnotations`** skips annotations outside
  the current view. Key sighting markers (Roswell, Rendlesham,
  Phoenix Lights, etc.) only render when their year is in view.
- **Selection drag (`move`/`l`/`r` modes in `onPointerMove`)**
  now uses `_viewSpan()` instead of `(this.maxT - this.minT)`
  for the pixel-to-ms conversion. When zoomed in, dragging
  100px moves the selection by 100 view-pixels of time, not
  100 full-dataset-pixels. Users perceive this as "more
  precise at narrow zoom", which is exactly what you want.
- **`TimeBrush.reset()`** now also resets the zoom view so
  "Reset" truly means "back to starting state".
- **`mountObservatoryRail()`** calls `hydrateRailCollapsibles()`
  after the existing population code so the rail sections
  have working accordion handlers.
- **`showStats()`** wraps derived-count chips in
  `.stats-chip-optional` spans.
- **Observatory rail HTML** — every `.rail-section`
  (`rail-quality`, sources, shapes, visible, time window) gets
  a `.rail-collapse-btn` header with SVG chevron, and its
  content is wrapped in a `.rail-body` div.

### Fixed

- **`TimeBrush` 1900—2026 range was essentially unusable for
  narrow windows.** Dragging a 6-pixel handle across a
  ~1900-pixel canvas to isolate a 3-month window was visually
  impossible. v0.9.0's scroll-wheel zoom + view-aware drag
  precision makes sub-year window placement a non-issue.
- **Site was desktop-only.** No `@media (max-width: ...)` rules
  on the Observatory layout; on any viewport < 600px the 230px
  rail squeezed the map to ~270px. Now has dual-trigger mobile
  rules under both `body.is-touch` and
  `@media (max-width: 700px)`.

## [0.8.8] — 2026-04-11 — Emotion cards client-side + methodology expansion

Two tracks:

1. **The 4 Insight emotion cards were rewritten to read from
   `POINTS.emotionIdx` instead of the `/api/sentiment/*` endpoints.**
   Those endpoints had been returning empty data since the v0.8.5
   reload: the `sentiment_analysis` table was truncated, and the
   v0.8.3b public export (`ufo_public.db`) doesn't ship sentiment
   rows because they were computed from raw narrative text which
   was stripped for privacy. The cards rendered blank with a
   "No sentiment scores for these filters" message regardless of
   filter state.

   `dominant_emotion` IS populated in the bulk buffer at byte
   offset 22 (149,607 of 396,158 mapped rows), so all 4 cards can
   be computed client-side with the same walk-POINTS.visibleIdx
   pattern the v0.8.6 derived cards already use. No server trips,
   filter-reactive for free, and the Insights tab now renders
   **8 working cards** instead of 4 blank + 4 working.

2. **The methodology page gained three new sections** explaining
   the data layer that's been accumulating since v0.8.3b:

   - **How Sightings Get Mapped** — walks through the
     614,505 → 396,158 → 105,854 split, explaining why the stats
     badge used to under-count mapped sightings by ~4× (it was
     counting distinct geocoded places, not sighting rows). Covers
     the ~35% of sightings without coordinates and where they come
     from (pre-GPS historical records, free-text locations,
     structurally missing data).
   - **Movement + Quality Classification** — documents the 10
     movement categories with per-category counts and example
     narrative patterns, the composite quality_score formula and
     60-threshold for the High Quality filter, the hoax_likelihood
     heuristic, the richness_score companion, plus the
     dominant_emotion and primary_color sparse columns that drive
     the Observatory's Color/Emotion dropdowns and Insights cards.
   - **Notes on the v0.8.3b Data Pipeline** — three retirement
     notes explaining why raw text is no longer in the public DB,
     why the Duplicates tab was removed (duplicate_candidate ships
     empty because dedup moved to ingest time), and why sentiment
     had to be migrated to dominant_emotion.

### Added

- **4 new emotion renderers in `app.js`** (`renderEmotionRadar`,
  `renderEmotionOverTime`, `renderEmotionBySource`,
  `renderEmotionByShape`). All take no arguments — they read
  directly from `window.UFODeck.POINTS.emotionIdx` / `.shapeIdx` /
  `.sourceIdx` / `.dateDays` and compute via a single walk of
  `POINTS.visibleIdx`. Share two helpers: `_collectEmotionCounts`
  and `_emotionColor`.
- **Methodology page: "How Sightings Get Mapped"** section with the
  3-query explanation table and the 4-bucket breakdown of why
  sightings lack coordinates.
- **Methodology page: "Movement + Quality Classification"** section
  with the 10-category movement table, quality_score formula
  breakdown, hoax_likelihood explanation, and richness_score notes.
- **Methodology page: "Notes on the v0.8.3b Data Pipeline"**
  section covering raw text retirement, duplicates table
  emptiness, and sentiment pipeline migration.
- **`tests/test_v088.py`** — 15 tests locking the v0.8.8 contract
  (emotion cards client-side, no /api/sentiment fetches, new
  renderer signatures, methodology sections present).

### Changed

- **`loadInsights()`** no longer calls `/api/sentiment/overview`,
  `/api/sentiment/timeline`, `/api/sentiment/by-source`, or
  `/api/sentiment/by-shape`. Gates on `POINTS.ready` like
  `loadTimeline()` does, schedules a retry via `setInterval` if
  the bulk buffer isn't loaded yet.
- **`refreshInsightsClientCards()`** now calls all 8 renderers
  (the 4 v0.8.8 emotion cards + the 4 v0.8.6 derived cards).
- **Insights status bar** shows "149,607 sightings with emotion
  classification · N in view" instead of the old "X sightings
  analyzed | Avg sentiment: Y.YYY" (we don't have VADER compound
  scores anymore).
- **`renderSentimentTimeline` → `renderEmotionOverTime`**. The
  new version is a stacked-area chart with 8 emotion series
  (joy/fear/anger/sadness/surprise/disgust/trust/anticipation)
  and no sentiment-compound line. Inline yearStarts binary
  search mirrors the `deck.js` helpers.
- **Methodology page** removed the stale mention of the
  "All Coords / Original Only / Geocoded Only" dropdown that
  v0.8.7 deleted.

### Fixed

- **4 blank Insight cards on the live site.** Emotion Distribution,
  Sentiment Over Time, Emotions By Source, and Emotions By Shape
  were rendering empty because the sentiment endpoints returned
  `total_analyzed: 0`. v0.8.8's client-side rewrite uses the
  already-populated `dominant_emotion` column instead. No reload,
  no schema change, no server-side re-compute needed.

## [0.8.7] — 2026-04-11 — Filter bar cleanup + Movement cluster + Quality rail bug fix

Four changes:

1. **Five dead dropdowns deleted from the top filter bar.** Country,
   State, Hynek, Vallée, and Collection were read by
   `applyClientFilters()` but `_rebuildVisible` silently ignored
   them — the bulk buffer has no byte slot for any of them, so
   filtering required a per-row database lookup that defeated the
   v0.8.0 client-side filter architecture. Deleted entirely rather
   than backfilled with buffer columns.
2. **New Movement category cluster.** 10-pill checkbox row below the
   filter bar populated from `POINTS.movements` after `bootDeckGL()`
   completes. Multi-select with OR-mask semantics: a sighting
   matches if any checked category's bit is set in its
   `movement_flags` uint16 at offset 28. No byte-layout changes —
   the uint16 slot was added in v0.8.5 and had been sitting there
   waiting for a UI.
3. **Color and Emotion dropdowns surfaced.** Both had zombie code in
   `applyClientFilters` and `_rebuildVisible` for months but no
   HTML element. Byte slots at offsets 21 and 22 were already
   populated (coverage 145,209 and 149,607). Added the dropdowns
   and wired them into `populateFilterDropdownsFromDeck()` so they
   use the canonical standardized lists from the bulk meta.
4. **Quality rail no longer permanently bricks itself.**
   `mountQualityRail()` read `window.UFODeck.getCoverage()` at
   mount time, but it was called from `loadObservatory()` which
   ran *before* `bootDeckGL()` finished loading the bulk buffer.
   Coverage came back as `{}`, every toggle got `populated = false`,
   CSS applied `cursor: not-allowed`, and the `dataset.mounted = "1"`
   guard prevented the rail from ever re-rendering. User report:
   "the cursor turns into an X when I hover over data quality
   section and can't press any buttons." Fixed by removing the
   mount guard + re-mounting from `bootDeckGL()` completion.

### Added

- **Movement category multi-select cluster** in the filter bar.
  HTML: `.filter-movement-row > .movement-cluster >
  .movement-chip-label[]`. JS: `_mountMovementCluster(movements)`,
  `_readMovementCats()`, plus `movementCats` field on the filter
  descriptor and bit-mask resolution in `_rebuildVisible`.
- **Color dropdown** (`#filter-color`, populated from
  `POINTS.colors`).
- **Emotion dropdown** (`#filter-emotion`, populated from
  `POINTS.emotions`).
- **`populateFilterDropdownsFromDeck()`** in `app.js` — called
  from `bootDeckGL()` and `_wireTimeBrushToDeck()` to populate
  shape/color/emotion dropdowns + mount the movement cluster
  using `POINTS` metadata (canonical standardized lists).
- **`_populateLookupDropdown(id, values, placeholder)`** helper —
  clears target, adds placeholder, appends non-null values,
  preserves existing selection across re-populates.
- **Movement cluster hash restore.** URL hash
  `?movement=hovering,landed` restores the cluster's checked
  state. Defers via `state.pendingMovementFilter` if the cluster
  hasn't mounted yet (POINTS not ready), and
  `_mountMovementCluster` consumes the pending set when it runs.
- **`tests/test_v087.py`** — 28 tests locking every aspect of the
  v0.8.7 contract (dead IDs removed, new IDs present, helpers
  exist, movement mask logic in `_rebuildVisible`, backend
  pruning, Quality rail re-mount hook wired).

### Changed

- **`mountQualityRail()`**: removed the `dataset.mounted === "1"`
  idempotent guard. `host.innerHTML = ""` on every call already
  makes re-mounts safe. Also gated the coverage lookup on
  `POINTS.ready` so un-ready state renders as disabled instead of
  reading `{}`.
- **`bootDeckGL()`**: now calls `populateFilterDropdownsFromDeck()`
  and `mountQualityRail()` after `_wireTimeBrushToDeck()` so the
  filter bar and Quality rail populate with real data once
  `POINTS.ready` flips.
- **`_rebuildVisible()` in deck.js**: handles `f.movementCats`
  by resolving each category name against `POINTS.movements` and
  OR-ing the corresponding bits into a uint16 mask. Hot loop
  aliases `POINTS.movementFlags` → `mvf` for V8 optimisation.
- **`applyClientFilters()`**: passes `movementCats` into the
  filter descriptor. `_countActiveFilters()` counts the cluster
  as a single active filter regardless of how many categories
  are checked.
- **`FILTER_FIELDS`** trimmed from 10 → 6 entries. Deleted:
  `filter-collection`, `filter-country`, `filter-state`,
  `filter-hynek`, `filter-vallee`, `coords-filter`. Added:
  `filter-color`, `filter-emotion`.
- **`getFilterParams()`** rewritten to serialize the 6 surviving
  filter fields plus the movement cluster (as a comma-separated
  `movement=hovering,landed` param).
- **`clearFilters()`** now drives off `FILTER_FIELDS` in a loop
  instead of hardcoding each dead dropdown, and also resets the
  movement cluster + the Quality rail state.
- **`populateFilterDropdowns()` in `app.js`** — only populates
  the source dropdown now. Shape / color / emotion arrive later
  via `populateFilterDropdownsFromDeck()` using canonical
  standardized lists from `POINTS`.
- **`add_common_filters()` in `app.py`** — trimmed to 6 keys
  (shape, source, color, emotion, date_from, date_to). The
  `shape` key switched from `s.shape` (raw per-source mixed-case
  strings) to `s.standardized_shape` (the v0.8.3b classified
  column), so it agrees with the Observatory dropdown's canonical
  list. Country / state / hynek / vallee / collection / coords
  branches deleted — they had no client-side equivalent and the
  server-side handling was orphaned.
- **`_COMMON_FILTER_KEYS`** trimmed from 10 → 6 entries.
- **`init_filters()`** dropped 5 startup queries (hynek, vallee,
  collection, countries, states, match_methods). Shape query
  uses `standardized_shape`. Added color + emotion vocabularies
  as a defensive shim for dev tools hitting `/api/filters`
  directly.

### Removed

- `#filter-country`, `#filter-state`, `#filter-hynek`,
  `#filter-vallee`, `#filter-collection` dropdowns from
  `index.html`.
- `#btn-more-filters` button + `#filters-advanced` drawer.
- `#coords-filter` dropdown.
- `FILTER_CACHE["hynek"]`, `["vallee"]`, `["collections"]`,
  `["countries"]`, `["states"]`, `["match_methods"]` startup
  queries (~50 lines of `init_filters()`).
- `add_common_filters` handling for `collection`, `hynek`,
  `vallee`, `country`, `state`, `coords` query params.
  Scripted callers sending these now get them silently ignored
  (route still returns 200).
- CSS `#filter-country, #filter-state { max-width: 180px; }`
  rule — dead selector.

### Fixed

- **Data Quality rail permanently disabled on first load.** Root
  cause described above. After the v0.8.7 fix the rail re-renders
  on `POINTS.ready` with real coverage, toggles are clickable, and
  the cursor stays pointer (not `not-allowed`). User can finally
  use "High quality only", "Hide likely hoaxes", "Has description",
  "Has media", and "Has movement described" to filter the map.
- **Shape dropdown silently failed for mixed-case raw values.**
  `init_filters()` previously ran `SELECT DISTINCT shape FROM
  sighting` which returns "Disk", "disc", "cigar", etc. —
  whatever the raw feeds stored. But `_rebuildVisible` looks
  values up in `POINTS.shapes`, which is the **standardized**
  list from the bulk meta sidecar ("Disc", "Cigar", "Triangle",
  capitalized). Picking "Disk" fails `indexOf("Disk") === -1` in
  the standardized list. v0.8.7 populates the dropdown from
  `POINTS.shapes` directly (via
  `populateFilterDropdownsFromDeck()`) so every option
  round-trips through `_rebuildVisible` correctly.

## [0.8.6] — 2026-04-11 — Timeline redesign + Insights buildout + cleanup

Four tracks landing in one release:

1. **Timeline tab becomes a client-side dashboard.** The old single
   Chart.js bar chart is replaced with a 3-card grid that reads
   straight from the in-memory bulk buffer. Cards: stacked-by-source
   yearly histogram, median quality score per year, movement category
   share per year. Zero `/api/timeline` round trips after the initial
   Observatory mount; filter changes re-tally in a few milliseconds.
2. **Four new Insight cards.** Quality Score Distribution, Movement
   Taxonomy, Shape × Movement Matrix, Hoax Likelihood Curve — all
   computed client-side from the bulk buffer so they respect the
   current Observatory filter state and don't depend on any new
   backend endpoints.
3. **Search and Duplicates tabs deleted.** The Observatory rail + bulk
   buffer supersede the old ILIKE faceted search, and the v0.8.3b
   science-team export ships zero `duplicate_candidate` rows so the
   Duplicates panel had nothing to render anyway. Routes, panels,
   JS helpers, and nav buttons all removed.
4. **Two real bug fixes** hit during the v0.8.5 reload verification:
   the `_points_bulk_etag` stale-cache bug (content-replace reloads
   kept the same `(count, max_id, cols)` tuple so the in-process
   `lru_cache` served a stale buffer — now includes a
   `has_movement_mentioned` data-content signal), and the
   Observatory brush drag lag (300ms debounce on every `pointermove`
   — now visual-only during drag, commits on `pointerup` via a raw
   un-debounced callback).

### Added

- **`deck.js` aggregate helpers** (`getYearHistogramBySource`,
  `getYearHistogramForVisible`, `computeMedianByYear`,
  `computeMovementShareByYear`, `countVisible`). Every helper walks
  `POINTS.visibleIdx` once and runs in single-digit milliseconds on
  396k rows. These are the shared primitives the Timeline page and
  Insights cards both use.
- **`TimeBrush.retally(bins)`** method. Called from
  `applyClientFilters()` after the filter pipeline updates
  `POINTS.visibleIdx`. The brush now draws a "ghost" background of
  the unfiltered dataset with the filtered bins overlaid in the
  accent color, so the user sees the filter's shape over time
  without losing sight of the full range.
- **Insights Phase 5 cards.** Four new `renderXxx()` functions in
  `app.js`: `renderQualityDistribution` (10-bucket bar chart, 60+
  buckets highlighted in accent), `renderMovementTaxonomy`
  (horizontal bar chart of the 10 movement categories sorted by
  count), `renderShapeMovementMatrix` (stacked horizontal bar chart
  of top-10 shapes × 10 movement categories), and `renderHoaxCurve`
  (20-bucket line chart red-shifted on the right tail).
- **`refreshTimelineCards()` and `refreshInsightsClientCards()`**
  callable from `applyClientFilters()` so the two dashboards
  respond to rail changes on any tab.
- **`tests/test_v086.py`** with 28 tests locking every point of the
  v0.8.6 contract (routes gone, etag has `mv` segment, new deck.js
  helpers, TimeBrush drag split, Timeline canvas IDs, Insights
  canvas IDs, dead JS functions deleted).

### Changed

- **`_points_bulk_etag()`** now includes
  `mv{has_movement_count}` as an ingredient, reusing the indexed
  scan the `/api/stats` endpoint already runs. Cost: ~15ms per
  request (cached by `@lru_cache` on the response side). Guards
  against pre-v0.8.3 schemas with an `UndefinedColumn` rollback
  that falls back to the sentinel `mvx`.
- **`TimeBrush` drag semantics.** `pointermove` updates the window
  rectangle visually only; `pointerup` commits via the raw
  un-debounced callback. Stored `this._onChangeRaw` alongside the
  debounced `this.onChange` so callers that still want coalescing
  (programmatic hash restore, play mode) keep it.
- **`debounce()` helper upgraded.** The previous implementation had
  a no-op `.flush()`. v0.8.6 tracks `pendingArgs` so `flush()`
  actually fires the pending call synchronously and `cancel()`
  discards it cleanly.
- **`navigateToSearch()`** rewritten. Callers that used to jump to
  the Search tab now land on the Observatory with the filter
  applied via `applyFilters()`. The `q` (free-text) parameter is
  silently ignored because Observatory filtering is faceted, not
  full-text.
- **`drillToMonth()`** (Leaflet popup handler) now applies a month
  date range to the Observatory filter instead of switching to the
  deleted Timeline drill-down mode.
- **AI tool panel** "view all in Search →" link text changed to
  "view all on map →" to match the new destination.

### Removed

- `/api/search` and `/api/duplicates` routes + handlers (~270
  lines of `app.py`).
- `#panel-search` and `#panel-duplicates` panels (~50 lines of
  `index.html`), plus the two `<button data-tab>` nav entries.
- Search + Duplicates functions from `app.js`: `doSearch`,
  `executeSearch`, `renderActiveFilterChips`, `removeFilter`,
  `renderPager`, `goToPage`, `scoreColor`, `scoreLabel`,
  `loadDuplicates`, `initSearchActions`, `escapeRegExp`. The
  shared helper `disableButtonWhilePending` stays — still used by
  `applyFilters()` and the AI/map-search paths.
- `state.searchPage`, `state.searchTotal`, `state.searchSort`,
  `state.dupesPage`, `state.dupesTotal` from the app state bag.
- `window.doSearch`, `window.goToPage`, `window.removeFilter`
  global bindings.
- Timeline drill-down "back to years" button and the monthly
  drill-down flow — the new 3-card dashboard has no monthly view.
- 3 inline SVGs from `index.html` (the Search panel's CSV/JSON/
  copy-link icons). Expected `test_index_html_has_expected_svg_count`
  dropped 10 → 7.

### Fixed

- **`/api/points-bulk` stale cache after content-replace reload.**
  Hit during v0.8.5 verification: the bulk endpoint returned
  `coverage.has_movement = 0` after a reload that populated 249,217
  movement rows, because the etag's `(count, max_id, column_set)`
  tuple was unchanged. The reload was forced to a live state with
  a manual Azure App Service restart. v0.8.6's etag now includes
  `mv{has_movement_count}` so the cache invalidates automatically
  on any future content swap.
- **Brush drag visibly lagged on the first 300ms of each drag.**
  Caused by the debounced `onChange` running on every `pointermove`.
  Now visual-only during drag with a single commit on `pointerup`.

## [0.8.5] — 2026-04-11 — Movement classification + rebalanced quality scoring

App-side ship of the science team's v0.8.3b data layer. Two new
derived columns on `sighting` (`has_movement_mentioned`,
`movement_categories`) plus a rebalanced `quality_score` formula
that shifts the "High Quality" filter from ~138k rows to exactly
**118,320**. The v0.8.3b public SQLite at `data/output/ufo_public.db`
already has the raw text columns stripped, so migrating from it
means raw text (`description`, `summary`, `notes`, `raw_json`)
**never reaches Azure Postgres at all**. `strip_raw_for_public.py`
is no longer part of the normal flow — it's just a safety backstop
in case someone migrates from the private `ufo_unified.db` by
accident.

See [`docs/V085_MOVEMENT_PLAN.md`](docs/V085_MOVEMENT_PLAN.md) for
the full layout + migration walkthrough.

### Added

- **`/api/points-bulk` v083-1 binary schema.** Row grew from 28 to
  **32 bytes** (still 4-byte aligned). New field layout:
  - `flags` byte gains bit 2 = `has_movement_mentioned`
  - New `movement_flags` uint16 at offset 28: 10-bit bitmask over
    the categories in `_MOVEMENT_CATS` order (`hovering` / `linear`
    / `erratic` / `accelerating` / `rotating` / `ascending` /
    `descending` / `vanished` / `followed` / `landed`)
  - New `_reserved2` uint16 at offset 30 for future growth
  - Schema version bumped from `v082-1` → `v083-1` so every browser
    cache invalidates on next fetch.
- **`_MOVEMENT_CATS` module constant** in app.py locks the 10
  categories in the order their bit position is packed on the
  wire. Changing this order silently remaps every shipped
  buffer — tests guard the order explicitly.
- **Meta sidecar `movements` lookup + `flag_bits` map.** The
  `?meta=1` response now carries the 10 category names in bit
  order (so the client can decode `movement_flags` bit-by-bit)
  plus a `flag_bits: { has_description: 0, has_media: 1,
  has_movement: 2 }` map so the client doesn't have to guess
  which bit is which.
- **`/api/sighting/:id`** explicit SELECT now includes
  `s.has_movement_mentioned` and `s.movement_categories`. The
  response parses the TEXT JSON column into a real JSON array
  server-side so callers receive `["hovering", "vanished"]`
  instead of the JSON-encoded string.
- **deck.js typed arrays.** `POINTS.movementFlags` is a new
  `Uint16Array(N)` populated in the deserialisation loop from
  offset 28 of each 32-byte row. `POINTS.movements` is the lookup
  table from the meta sidecar.
- **deck.js filter predicate.** `_rebuildVisible` honors a new
  `hasMovement` boolean on the filter object. When set, rows are
  kept iff `(flags & FLAG_HAS_MOVEMENT) !== 0` matches the
  requested bool.
- **Quality rail "Has movement described" toggle** in the
  Observatory left rail. Same disabled-when-unpopulated pattern
  as the v0.8.2 "Has description" / "Has media" toggles —
  coverage pulled from the `has_movement` coverage counter the
  server emits. When populated, clicking filters to the 249,217
  rows whose narrative mentioned any movement.
- **Detail modal "Movement" row** in the Derived Metadata section.
  Renders one `.movement-chip` per category the pipeline detected
  (e.g. `hovering · vanished · followed`). Falls back to a
  `[ MOVEMENT ]` / `[ STATIC ]` pill when the categories array
  is empty but `has_movement_mentioned` is set. Hidden entirely
  when both are null.
- **`.movement-chip` CSS rule.** Inherits `var(--accent)` so the
  chips auto-skin for both SIGNAL (cyan) and DECLASS (burgundy)
  themes.
- **`scripts/add_v083_derived_columns.sql`** wired into the deploy
  workflow's psql step after `add_v082_derived_columns.sql`.
  Idempotent — `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX
  CONCURRENTLY IF NOT EXISTS`. Ran auto on next deploy, adds the
  two columns as NULL. Actual data population happens on the
  operator's manual reload step.

### Changed

- **`_points_bulk_build()` SELECT clause** now includes
  `has_movement_mentioned` and `movement_categories` via the
  existing `_col_expr` column-probe helper. Graceful to
  pre-v0.8.3 schemas: missing columns become `NULL AS col_name`
  and the endpoint returns the same 32-byte layout with
  `movement_flags = 0` on every row.
- **Row packer** parses `movement_categories` TEXT JSON, ORs each
  recognised category's bit into `mv_flags`, sets
  `flags |= 0x04` when `has_movement_mentioned == 1`. Unknown
  categories (shouldn't happen — science team promises only the
  10 documented values) are silently skipped for forward
  compatibility.
- **deck.js schema-size check** bumped from `bytesPerRow !== 28`
  to `bytesPerRow !== 32`. Mismatch throws loudly so stale v082-1
  servers can't silently corrupt every marker on a client running
  the v083-1 deserialiser.
- **v0.8.0–v0.8.4 regression tests** updated to the v083-1
  contract. The `_FakeBulkCursor` sample sightings now carry the
  two new fields (defaulting to 0 / None), assertions that locked
  `BYTES_PER_ROW = 28` now lock `= 32`, and the round-trip test
  checks the trailing uint16s.

### Deployment

v0.8.5 ships the app code in one commit + tag. The deploy workflow
auto-applies `add_v083_derived_columns.sql`, adding the two columns
as NULL. At that point the Quality rail's new "Has movement
described" toggle renders **disabled** (coverage = 0) until the
operator runs the manual data reload from `data/output/ufo_public.db`.
See `docs/V085_MOVEMENT_PLAN.md` for the full operator instructions.

### Not shipped

- **Category-level movement filters** (filter by "hovering"
  specifically, etc.). Scope-deferred to v0.8.6 — the binary
  layout supports it (the bitmask is on the wire), just no UI
  control yet. The detail modal shows all categories as chips so
  users can at least SEE which applied to a specific sighting.
- **Insights page cards for movement category counts.** Out of
  scope; a stretch goal.
- **Retirement of `scripts/strip_raw_for_public.py`.** Script
  stays in-tree as a safety backstop even though the normal
  migration flow from `ufo_public.db` makes it unnecessary.
- **`sighting_analysis` JSON side-fields migration.** Still v0.9
  scope per `docs/V083_BACKLOG.md` APP-1.

## [0.8.4] — 2026-04-11 — Signal / Declass theme overhaul

Finishes the theme system v0.7 started. The SIGNAL / DECLASS token
swap was already plumbed through `body.theme-signal` /
`body.theme-declass` CSS classes, `localStorage` persistence, and
a pre-paint script in `index.html`. v0.8.4 closes the remaining
three gaps: (1) the toggle was buried in the settings menu, (2)
the base map tiles were hardcoded OSM, and (3) the deck.gl layer
colors were hardcoded cyan-on-void.

See [`docs/V084_THEME_PLAN.md`](docs/V084_THEME_PLAN.md) for the
full audit + palette rationale.

### Added

- **Top-nav theme pill.** New `.theme-pill` radio group between the
  tab buttons and the settings gear icon, visible at all times.
  Two compact buttons (SIGNAL, DECLASS) with the same
  `.theme-opt` class as the settings-menu copy, so
  `initThemeToggle()` binds both instances automatically — clicking
  either one updates the other via `setTheme()`'s aria-checked
  mirror. Responsive: labels collapse to just the first letter on
  viewports narrower than 900 px.
- **`TILE_URLS` constant in `static/app.js`** with Carto basemap
  URLs:
  - SIGNAL → `https://{s}.basemaps.cartocdn.com/dark_all/...`
    ("Dark Matter": dark slate, white roads)
  - DECLASS → `https://{s}.basemaps.cartocdn.com/rastertiles/voyager/...`
    ("Voyager": warm cream paper, desaturated accents)
  Both are free for public use, retina-aware, CORS-enabled, no API
  key required. Replaces the hardcoded standard OSM tile URL in
  both the main Observatory map AND the detail-modal mini-map.
- **`THEME_PALETTES` constant in `static/deck.js`** with
  `scatter` and `hexRange` entries per theme. The SIGNAL palette
  keeps the v0.8.0 cold-plasma → cyan → hot-amber ramp; the DECLASS
  palette is a new cream → tan → rust → burgundy → deep-wine ramp
  that matches the `#B8001F` DECLASS accent and reads clearly on
  Voyager's cream tiles. ScatterplotLayer dots switch from cyan
  `[0, 240, 255, 180]` on SIGNAL to near-black `[15, 23, 42, 200]`
  on DECLASS for max contrast.
- **`UFODeck.setTheme(name)`** public API in `deck.js`. Updates
  the internal theme pointer and calls `refreshActiveLayer()` so
  the next layer instance picks up the new palette colors.
  Layer factories (Scatterplot, Hexagon, Heatmap) all read from
  `_activePalette()` instead of hardcoded RGB arrays, and their
  `updateTriggers` include `_theme` so deck.gl's GPU attribute
  cache invalidates on swap.

### Changed

- **`setTheme()` in `static/app.js`** now wires three live
  updates:
  1. CSS class swap + localStorage persist (existing v0.7 path)
  2. TimeBrush canvas redraw (existing v0.7 path)
  3. `state.tileLayer.setUrl(TILE_URLS[theme])` — Leaflet
     re-fetches the visible tile grid in place with no layer
     remove/re-add (NEW)
  4. `window.UFODeck.setTheme(theme)` — deck.gl recolors without
     a page reload (NEW)
  The effect: toggling SIGNAL ↔ DECLASS is instant and affects
  every surface simultaneously — UI chrome, map tiles, point
  colors, hex ramp, heatmap ramp, histogram accents, popup
  styling, Quality rail, Data Quality bars, etc.
- **`bootDeckGL()`** now calls `UFODeck.setTheme(_currentTheme())`
  BEFORE mounting the initial LeafletLayer, so a user loading the
  page in DECLASS doesn't briefly see cyan dots before the later
  setTheme() call re-renders with the burgundy palette.
- **Detail-modal mini-map** tile URL now uses the same
  `TILE_URLS[_currentTheme()]` helper as the main map so the
  modal matches the active theme.

### Not changed

- **No CSS audit fixes needed.** Every `.class` added since v0.8.0
  (`.popup-btn`, `.popup-desc-badge.has-desc`, `.brush-mode-btn`,
  `.brush-speed-select`, `.rail-quality`, `.rail-toggle-list`,
  `.quality-bar`, `.result-derived`, `.meta-pill.has-desc`, etc.)
  already reads from `var(--accent)` / `var(--text)` / `var(--bg)`
  tokens, so the existing body-class token swap re-skins them for
  free. A one-time grep of the v0.8.x additions confirmed zero
  hardcoded colors that would break DECLASS.
- **Existing `.theme-toggle` in the settings menu stays.** It's a
  fallback for discoverability; removing it would be a subtraction
  with no upside.
- **No prefers-color-scheme autoswitch.** Users pick the theme
  explicitly; we don't try to guess. The localStorage persist
  makes the choice sticky across sessions.

### New tests

- `tests/test_v084_theme.py` with 20 assertions covering:
  - `.theme-pill` exists at the top nav, not inside settings menu
  - CSS declares both `.theme-pill` and `.theme-opt-compact` rules
  - `body.theme-signal` / `body.theme-declass` still present
  - `TILE_URLS` has both signal + declass Carto URLs
  - `initMap()` stashes `state.tileLayer` and reads from `TILE_URLS`
  - No `tile.openstreetmap.org` left anywhere in app.js
  - `setTheme()` calls both `state.tileLayer.setUrl()` AND
    `UFODeck.setTheme()`
  - `bootDeckGL()` seeds the theme BEFORE mounting the layer
  - `THEME_PALETTES` declared in deck.js with both variants +
    scatter + hexRange fields
  - Layer factories read from `_activePalette()`, not hardcoded
    RGB arrays
  - `UFODeck.setTheme` is in the public API exports
  - `setDeckTheme()` function exists and calls `refreshActiveLayer`

## [0.8.3] — 2026-04-11 — Raw text retirement (search + detail rewire)

The preparation step for dropping the raw narrative columns from the
public Postgres. v0.8.3 removes every code path that reads
`description`, `summary`, `notes`, or `raw_json` from the sighting
table, so `scripts/strip_raw_for_public.py` can safely drop those
columns without breaking the app. The full raw text remains in the
private SQLite on the operator's machine — it's just not in the
public copy anymore.

See [`docs/V083_PLAN.md`](docs/V083_PLAN.md) for the full rationale,
column inventory, and acceptance criteria.

### Changed

- **`/api/sighting/:id`** now uses an explicit SELECT column list
  (the new `_SIGHTING_DETAIL_COLUMNS` tuple) instead of
  `SELECT s.*`. The list never references the 4 dropped columns
  so the endpoint is forward-compatible with
  `strip_raw_for_public.py`. It also adds the 9 v0.8.2 derived
  fields (`standardized_shape`, `primary_color`, `dominant_emotion`,
  `quality_score`, `richness_score`, `hoax_likelihood`,
  `has_description`, `has_media`, `sighting_datetime`) so the
  detail modal can render Data Quality + Derived Metadata sections.
  The legacy `json.loads(record["raw_json"])` parse block is gone.

- **`/api/search` `q` parameter** changes semantics. Previously it
  ran `ILIKE` against `s.description` and `s.summary` (the raw
  narrative text); now it runs a 7-column faceted match over
  `l.city`, `l.state`, `l.country`, `s.standardized_shape`,
  `s.primary_color`, `s.dominant_emotion`, and `sd.name`. So
  `q=triangle` returns every triangle-shaped sighting;
  `q=texas` returns every Texas sighting. Multi-word ANDs
  work via the faceted filters alongside `q`. Every column
  in the WHERE has a btree index (from v0.7 + v0.8.2
  migrations), so the search stays sub-second on 614k rows.

- **`/api/search` response shape** adds derived fields
  (`quality_score`, `hoax_likelihood`, `dominant_emotion`,
  `primary_color`, `has_description`, `has_media`,
  `sighting_datetime`, `standardized_shape`) and drops the
  300-char `description` snippet. Result cards now render a
  compound metadata line (`Black · Fear · quality 78`) instead
  of a description preview. The `<mark>q</mark>` regex
  highlighting is gone — nothing to highlight.

- **`/api/export.csv` and `/api/export.json`**. `EXPORT_COLUMNS`
  drops `summary` and `description`; adds `sighting_datetime`,
  `standardized_shape`, `primary_color`, `dominant_emotion`,
  `event_type`, `duration_seconds`, `quality_score`,
  `richness_score`, `hoax_likelihood`, `has_description`,
  `has_media`. `_build_export_query` uses the same faceted
  7-column `q` clause as `/api/search`.

- **`static/app.js openDetail()`** drops the Description and
  Raw JSON sections. Adds two new sections:
  - **Data Quality**: three horizontal bars rendering
    `quality_score`, `richness_score`, and `hoax_likelihood`.
    The hoax bar uses the danger color (inverted: higher = more
    red) and shows the raw REAL 0.0–1.0 value, not the 0–100
    scale.
  - **Derived Metadata**: `standardized_shape` (chip),
    `primary_color`, `dominant_emotion`, plus `[ DESC ]` /
    `[ NO DESC ]` and `[ MEDIA ]` / `[ NO MEDIA ]` badges from
    the flag fields.

  The Sentiment Analysis section (VADER compound + NRC emotion
  bars) is kept alongside Data Quality — the operator chose to
  preserve it for now. Same with the Explanation section: it's
  short structured text ("Chinese lantern", "Venus at low
  horizon") and stays in v0.8.3, flagged for science-team cleanup
  in `docs/V083_BACKLOG.md` under "Science-team cleanup of
  free-text fields".

- **`static/app.js executeSearch()`** result cards replace the
  description snippet with a compound derived-metadata line
  (`color · emotion · quality N · hoax X`) and add
  `[ DESC ]` / `[ MEDIA ]` pills to the meta row. No more
  `<mark>q</mark>` highlighting.

- **`static/style.css`** gains `.quality-bar`, `.quality-bar-fill`,
  `.quality-bar-hoax`, `.quality-bar-value`, `.result-derived`,
  `.meta-pill.has-desc`, `.meta-pill.has-media`, plus inline
  `.quality-inline` and `.hoax-inline` accent chips for the
  search result cards.

- **`scripts/strip_raw_for_public.py` `RAW_COLUMNS`** trimmed
  from 6 entries to the 4 the operator confirmed during v0.8.3
  scoping: `description`, `summary`, `notes`, `raw_json`.
  `date_event_raw` and `time_raw` are kept in the schema.
  `scripts/drop_raw_text_columns.sql` updated to match.

### Added

- **`docs/V083_PLAN.md`** — full architecture plan, column
  inventory, acceptance criteria, ship sequence, and risk
  register.

- **`tests/test_v083_no_raw_text.py`** — 20 regression
  assertions that lock the v0.8.3 contract. Every check is a
  grep-style source-level assertion so the tests fail loudly
  if a future refactor re-introduces a `s.description` /
  `s.summary` read, a `r.description` render, or bumps the
  strip script's `RAW_COLUMNS` list back up.

### Not shipped yet (deferred to the post-ship ops step)

- **`scripts/strip_raw_for_public.py` has not been run against
  Azure Postgres yet.** That's the final irreversible step and
  happens after v0.8.3 ships, deploys, and the operator signs
  off that the detail modal + search work correctly. The script
  will ask for host-name confirmation before executing and will
  refuse to run if `quality_score` coverage is below 90%.

- **`sighting_analysis` table migration**. Still SQLite-private,
  flagged as v0.9 scope in `docs/V083_BACKLOG.md` APP-1.

- **Science-team cleanup of free-text fields**
  (`explanation`, `characteristics`, `weather`, `terrain`,
  `witness_names`). Kept in the schema for now; flagged as
  v0.8.4+ scope per the operator's sign-off during v0.8.3
  scoping.

## [0.8.2] — 2026-04-10 — Derived public fields + quality rail

The science team's `ufo-dedup` pipeline delivered a batch of derived
analysis fields (`quality_score`, `hoax_likelihood`,
`standardized_shape`, `richness_score`, `primary_color`,
`dominant_emotion`, `has_description`, `has_media`,
`sighting_datetime`, `topic_id`). v0.8.2 plumbs them through the
Postgres schema, the `/api/points-bulk` binary payload, the deck.js
filter pipeline, and the Observatory left rail — without breaking
the app when the columns haven't been populated yet, and without
exposing any raw report text.

See [`docs/V082_PLAN.md`](docs/V082_PLAN.md) for the full data-flow
diagram, binary layout, coverage strategy, and legal / data-policy
note on raw-text retirement.

### Added

- **`scripts/add_v082_derived_columns.sql`** — idempotent
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for every new sighting
  column plus `CREATE INDEX CONCURRENTLY IF NOT EXISTS` on the
  indexable ones (quality_score, hoax_likelihood,
  standardized_shape, dominant_emotion, primary_color,
  sighting_datetime, has_description, has_media, topic_id). Runs
  as part of the deploy workflow between the v0.7 index step and
  the v0.7.5 MV refresh. All new columns start NULL and stay NULL
  until the user runs the ufo-dedup pipeline on their private DB
  and re-streams via `migrate_sqlite_to_pg.py`.
- **`/api/points-bulk` v082-1 binary schema.** Row size bumps from
  16 bytes to **28 bytes**. New fields: `date_days` (uint32, days
  since 1900-01-01), `quality_score` / `hoax_score` / `richness_score`
  (uint8 0–100, 255 = unknown sentinel), `color_idx`, `emotion_idx`,
  `flags` (bit 0 = has_description, bit 1 = has_media),
  `num_witnesses`, `duration_log2` (log-scale seconds). Estimated
  gzipped payload ~4 MB (up from 2.85 MB in v0.8.0) — still under
  the 5 MB budget and amortised over an entire session.
- **Meta sidecar `coverage` + `columns_present` maps.** Per-field
  populated-row counts and per-column schema-existence flags so
  the client can disable filter UI controls for unpopulated or
  missing fields with a tooltip explaining why.
- **Column probe at endpoint startup.** `_points_bulk_column_set()`
  runs one `information_schema.columns` query and the results drive
  the `SELECT` clause via `_col_expr()` — missing columns become
  `NULL AS col_name`, so the app ships v0.8.2 and works regardless
  of whether the migration has been applied yet.
- **`_epoch_days_1900()`** — fast ISO-date-string → days-since-1900
  converter. Handles `"YYYY-MM-DD"`, `"YYYY-MM"`, `"YYYY"`, and
  free-text year-prefixed strings. 0 = unknown.
- **`_duration_log2()`** — `log2(sec + 1)` rounded to uint16 so
  durations from 1 s to days fit in 2 bytes with ~1% resolution.
- **`deck.js` deserialiser + filter pipeline extensions.** Nine new
  typed-array fields (`dateDays`, `qualityScore`, `hoaxScore`,
  `richnessScore`, `colorIdx`, `emotionIdx`, `flags`,
  `numWitnesses`, `durationLog2`), plus seven new filter
  predicates in `_rebuildVisible` (`qualityMin`, `hoaxMax`,
  `richnessMin`, `colorName`, `emotionName`, `hasDescription`,
  `hasMedia`). Score filters treat the 255 sentinel as failing
  any threshold test — the right semantic for "unknown scores
  shouldn't pass quality gates".
- **Day-precision time window.** `UFODeck.setTimeWindow()` now
  accepts a `{ dayPrecision: true }` option and uses day-granular
  bounds during playback. When `POINTS.coverage.date_days > 0` the
  TimeBrush automatically switches to day precision so the PLAY
  button animates month-by-month instead of jumping by whole years.
- **Data Quality rail** in the Observatory left sidebar with four
  toggles: "High quality only" (score ≥ 60), "Hide likely hoaxes"
  (score > 0.5), "Has description", "Has media". Unpopulated
  toggles render muted/disabled with a tooltip explaining that
  the derived column exists in the schema but no rows are
  populated yet. Styled to match the existing rail aesthetic.
- **Auto-switching shape dropdown.** When
  `coverage.std_shape_idx > 0` the `/api/points-bulk` meta sidecar
  reports `shape_source: "standardized"` and the shapes lookup
  contains the ~25 canonical values from the science team's
  fuzzy-match pipeline. Otherwise it falls back to the ~200 raw
  shape values. The frontend filter dropdown picks up either
  automatically via `UFODeck.getShapes()`.
- **`scripts/drop_raw_text_columns.sql`** (unapplied) — a manual
  migration the user can run with `psql` when they're ready to
  retire the raw `description` / `summary` / `notes` / `raw_json`
  columns from the public Postgres. Gated by two safety probes:
  the v0.8.2 derived columns must exist, AND ≥ 90 % of geocoded
  rows must have `quality_score` populated. Otherwise the
  transaction rolls back. **Not wired into the deploy workflow.**

### Changed

- **`_points_bulk_etag()`** now includes the set of present derived
  columns in the etag, so when the v0.8.2 migration finally lands
  on the live schema the client-side cache invalidates
  automatically. No stale 16-byte buffers after a schema upgrade.
- **`migrate_sqlite_to_pg.py`** knows about the new sighting
  columns and intersects the TABLES column list with the actual
  Postgres schema via a new `pg_columns()` helper. Running the
  migrator against a pre-v0.8.2 Postgres prints a warning and
  skips the missing columns instead of erroring on COPY.
- **`applyClientFilters()`** (app.js) now reads `state.qualityFilter`
  and passes `qualityMin` / `hoaxMax` / `hasDescription` /
  `hasMedia` into the deck.js filter descriptor.

### Deferred

- **Raw text column drop** on the public Postgres. `/api/search`
  and `/api/sighting/:id` still rely on description/summary; those
  rewires are v0.8.3+ scope. The SQL script is in-tree and ready
  to run when you sign off.
- **`sighting_analysis` JSON side-fields** (behavior_tags,
  emotion_scores, color_list, hoax_flags). Detail-modal scope,
  not bulk-map scope.
- **`topic_id` filter.** Column shipped in the binary as a reserved
  slot but the UI doesn't expose it — waiting on the v0.9 topic
  modelling work.

## [0.8.1] — 2026-04-10 — Client-side temporal animation

v0.8.0 made pan / zoom / filter free by loading every geocoded
sighting into typed arrays and rendering on the GPU. v0.8.1 does the
same thing for the bottom-of-Observatory **time brush** — the
histogram, the drag window, and the PLAY button — so time-lapse
playback runs at the full browser frame rate instead of the 3 fps
the v0.7.6 300 ms debounce allowed.

See [`docs/V081_PLAN.md`](docs/V081_PLAN.md) for the full
architecture rationale and the risk register.

### Added

- **`UFODeck.setTimeWindow(yearFrom, yearTo, { cumulative })`** —
  new `deck.js` public API that overlays a temporal filter onto
  the current UI filter state and refreshes the active deck.gl
  layer. Designed to be called per-frame during playback. Also
  added `clearTimeWindow()`, `getYearHistogram()`, `getYearRange()`,
  and `isTimeWindowActive()`.
- **Client-computed year histogram.** `TimeBrush.ensureData()` now
  prefers `UFODeck.getYearHistogram()` over `/api/timeline?full_range=1`
  when the bulk dataset is already loaded. The histogram is computed
  from `POINTS.year` in a single pass (~3 ms for 396k rows), cached
  for the lifetime of the session. Saves one network round-trip per
  Observatory mount.
- **Cumulative replay mode.** New `#brush-mode` button next to PLAY
  cycles between `SLIDING` and `CUMULATIVE`. In cumulative mode the
  window's left edge stays pinned at the dataset minimum while the
  right edge advances, so you watch the dataset fill up over time.
  The cumulative state gets a dashed border so the toggle is
  visible at a glance.
- **Reusable scratch buffer** (`_visibleScratch`) in the filter
  pipeline. Instead of allocating a fresh `Uint32Array(N)` per
  frame (which would mean ~96 MB/sec of GC pressure at 60 fps),
  the hot loop writes into a persistent `Uint32Array` sized to
  `POINTS.count` and returns a `subarray(0, j)` view. Allocated
  once per session.

### Changed

- **Filter pipeline refactored.** `deck.js` moved its internal
  state into two module-level objects: `_activeFilter` (UI
  filter — source / shape / bbox / year range) and `_timeState`
  (timeline-driven year window). Both feed into a shared
  `_rebuildVisible()` loop that walks `POINTS` once. This lets
  `setTimeWindow()` overlay on top of an active source/shape
  filter without clobbering it.
- **`TimeBrush.togglePlay()`** gained a fast-path branch. When the
  `deckFastPath` callback is set (installed by `bootDeckGL()` after
  the bulk dataset finishes loading), the `requestAnimationFrame`
  step closure calls `UFODeck.setTimeWindow()` directly instead of
  going through the debounced `onChange → applyFilters → form
  input round-trip` pipeline. Frame rate goes from ~3 fps to
  whatever the browser gives us (60 fps on a desktop GPU).
- **`TimeBrush.reset()`** now stops any active playback and calls
  `UFODeck.clearTimeWindow()` so the map snaps back to the full
  range instantly instead of waiting 300 ms for the debounced
  filter update.
- **`applyClientFilters` no longer owns the filter loop directly.**
  It now stashes the filter descriptor in `_activeFilter` and
  defers to `_rebuildVisible()`. Same external behaviour, but
  shares code with the time-window fast path.

### Kept for one release cycle

The legacy `/api/timeline?full_range=1` fetch path is still used
when `UFODeck` isn't available (old browsers, WebGL off). Playback
on that path is still the v0.7.6 debounced behaviour — slower but
functional.

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
