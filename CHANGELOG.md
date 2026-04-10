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
