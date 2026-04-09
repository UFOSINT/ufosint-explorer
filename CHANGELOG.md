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
