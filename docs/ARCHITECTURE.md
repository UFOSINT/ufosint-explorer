# Architecture

A working reference for anyone (human or agent) picking up this codebase
cold. The goal is to explain *why* things are the way they are, not just
*what* they do — the "what" is easy to read from the source.

## 1. System at a glance

```
                            ┌──────────────────────────────┐
                            │   Azure App Service (B1)     │
                            │   Python 3.12 / gunicorn     │
                            │   2 workers × 4 threads      │
                            │                              │
 Browser  ──── HTTPS ───────▶│   Flask app (app.py)         │
                            │   ├── @app.after_request     │
                            │   │    sets Cache-Control    │
                            │   ├── /static/ (Flask)       │
                            │   ├── /api/* (psycopg pool)  │
                            │   ├── /mcp   (JSON-RPC)      │
                            │   └── /       → index.html   │
                            │                              │
                            │   psycopg_pool.ConnectionPool│
                            │    (min 1 / max 8)           │
                            └───────────┬──────────────────┘
                                        │ TLS (sslmode=require)
                                        ▼
                     ┌───────────────────────────────────┐
                     │  Azure Database for PostgreSQL    │
                     │  Flexible Server, Burstable B1ms  │
                     │  (read-only workload)             │
                     │                                   │
                     │  Tables:                          │
                     │    sighting         (614,505)     │
                     │    location         (geocoded)    │
                     │    source_database  (5)           │
                     │    source_collection (3)          │
                     │    duplicate_candidate (126,730)  │
                     │    sighting_sentiment             │
                     └───────────────────────────────────┘
```

**Frontend** is vanilla JS + CSS with no build step. Four files in
`static/`: `index.html` (~1,500 lines), `app.js` (~7,500 lines),
`deck.js` (~1,400 lines, deck.gl bulk buffer + GPU layer adapter),
and `style.css` (~5,000 lines). Libraries come from unpkg/jsdelivr
CDNs: Leaflet + markercluster + heat for the map, deck.gl 9.x for
GPU-accelerated point/heatmap/hexbin rendering, Chart.js for charts,
Inter from Google Fonts.

**Backend** is a single Flask module (`app.py`, ~3,300 lines) plus two
adapters: `mcp_http.py` (MCP-over-HTTP JSON-RPC blueprint, ~290 lines)
and `tools_catalog.py` (shared tool definitions for both BYOK chat and
MCP, ~540 lines). Also serves AI-readiness discovery files (`/llms.txt`,
`/llms-full.txt`, `/.well-known/mcp.json`, `/robots.txt`).

**DB** is Azure Database for PostgreSQL Flexible Server, Burstable B1ms.
Read-only from the app's perspective — writes happen out-of-band via
`scripts/migrate_sqlite_to_pg.py` when the `ufo-dedup` pipeline rebuilds
the canonical SQLite snapshot.

## 2. Request flow for a typical page load

1. Browser requests `GET /`.
2. Flask's `/` route returns the pre-substituted `_INDEX_HTML` string
   (computed once at startup from `static/index.html` with
   `{{ASSET_VERSION}}` replaced).
3. `add_cache_headers` sets `Cache-Control: public, max-age=60`.
4. Browser parses HTML, requests `/static/style.css?v=<version>` and
   `/static/app.js?v=<version>`.
5. Flask serves both from `static/`.  Because the URL carries `?v=…`,
   `add_cache_headers` tags them
   `Cache-Control: public, max-age=31536000, immutable`.
6. `app.js` runs `DOMContentLoaded`:
   - `Promise.all([fetchJSON("/api/filters"), fetchJSON("/api/stats")])`
   - `populateFilterDropdowns` + `showStats`
   - Wires up click/submit handlers for tabs, filters, modal, search
   - `initMap()` creates the Leaflet map, `loadMapMarkers()` hits
     `/api/map` with the viewport bbox
7. `/api/map` checks `flask_caching.SimpleCache` (per-worker, 5-minute
   TTL), falls through to `psycopg_pool.ConnectionPool.getconn()` for a
   parameterised query against `sighting JOIN location`, returns JSON.
8. Browser renders clusters or heatmap based on `state.mapMode`.

## 3. Cache strategy (critical for stale-cache regressions)

Four tiers, matched top-to-bottom by `add_cache_headers`:

| Tier | Match                            | Cache-Control                                       | Why |
|------|----------------------------------|-----------------------------------------------------|-----|
| 1    | `/static/*?v=<any>`              | `public, max-age=31536000, immutable`               | Versioned URL; content-addressed by `?v=`, can never go stale. |
| 2    | `/static/*` (no `?v=`)           | `public, max-age=3600, must-revalidate`             | Safety net: if something direct-links an unversioned URL, it can be at most one hour behind a deploy. |
| 3    | `/api/stats`, `/api/filters`, `/api/map`, `/api/heatmap`, `/api/timeline`, `/api/search`, `/api/sentiment/*`, `/api/duplicates` | `public, max-age=300` | Browser + CDN can cache for 5 minutes during a pan/zoom session. Server-side `flask_caching.SimpleCache` with the same 300s TTL backs it up. |
| 4    | `/`                              | `public, max-age=60`                                | HTML shell is tiny; a 60s cache lets deploys propagate the new version string fast. |
| —    | everything else (`/health`, `/api/sighting/<id>`, `/mcp`) | no header set | Flask default; browsers will not cache aggressively. |

**The Sprint 4 stale-cache bug**: before commit `7377087`, `/static/*`
used `max-age=604800` (7 days) and `index.html` referenced
`/static/style.css` without a version query. A user's browser served
the pre-Sprint 4 CSS against the post-Sprint 4 HTML for up to a week,
breaking SVG sizing. The fix is the versioned-URL pattern in tier 1.

## 4. Asset versioning (`ASSET_VERSION`)

Computed once at module load in `app.py`:

```python
def _compute_asset_version() -> str:
    # 1. explicit env var (set by the GH Actions workflow)
    env_ver = os.environ.get("ASSET_VERSION") or os.environ.get("GITHUB_SHA")
    if env_ver: return env_ver[:12]
    # 2. git SHA (works in dev)
    sha = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"])
    if sha: return sha
    # 3. mtime hash fallback (works even when neither env var nor git exist)
    h = hashlib.md5()
    for name in ("static/style.css", "static/app.js", "static/index.html"):
        ...
    return h.hexdigest()[:12]
```

The resulting string is substituted into `index.html` once at import
time and cached in `_INDEX_HTML`. Every request to `/` returns the same
string — no per-request template rendering, no filesystem I/O.

On Azure, the GitHub Actions workflow doesn't set `ASSET_VERSION` (git
isn't available in the App Service container), so the mtime-hash
fallback runs. This is fine — mtime changes on every deploy because the
zip extracts into a fresh wwwroot.

**Invariant**: if `index.html` is ever templated via Jinja or served
some other way, the `{{ASSET_VERSION}}` placeholder still has to be
substituted before the HTML reaches the client. Test:
`tests/test_routes.py::test_index_route_substitutes_asset_version`.

## 5. Database access

```
                  Flask request
                       │
                       ▼
               get_db()  ─── returns ──▶  _PooledConn
                                              │
                                              │ .cursor() / .execute()
                                              ▼
                              psycopg_pool.ConnectionPool
                                              │
                                              │ min=1, max=8
                                              │ autocommit=True
                                              │ read-only transactions
                                              ▼
                                 Azure Database for PostgreSQL
```

- **Pool size (`max=8`)** is exactly `workers × threads` from the
  Procfile (2 × 4 = 8). One connection per gunicorn slot.
- **`autocommit=True`** avoids per-query `BEGIN`/`COMMIT` overhead.
- **`default_transaction_read_only=on`** is a safety net — any code
  that ever tries to write will fail loudly instead of corrupting the
  mirror.
- **`_PooledConn`** is a tiny proxy. Existing call sites use the
  `conn = get_db(); ...; conn.close()` pattern; the proxy makes
  `.close()` return the connection to the pool instead of tearing it
  down. Don't rewrite all call sites to `with pool.connection() as ...`
  unless you're changing every route in the same commit.

### Query caching

Two layers:

1. **`flask_caching.SimpleCache`** — per-worker in-process LRU with
   `CACHE_THRESHOLD=500`, `CACHE_DEFAULT_TIMEOUT=300`. Applied via
   `@cache.cached(timeout=300)` on expensive query endpoints.
2. **`FILTER_CACHE`** — a plain Python dict populated once at import
   time by `init_filters()`. Distinct shapes, hynek codes, vallee codes,
   sources, collections, top countries — things that change only when
   the DB is rebuilt. `/api/filters` returns this dict directly.

Neither cache is shared across gunicorn workers. That's fine — with 2
workers a cold-cache hit happens at most twice per deploy, and the
responses are small enough that the browser cache (tier 3 above) covers
repeat views.

## 6. Tool catalog (`tools_catalog.py`)

Single source of truth for the 6 tools exposed to AI clients. Each
entry is a dict:

```python
{
    "name": "search_sightings",
    "description": "Search the unified UFO sightings database...",
    "parameters": {"type": "object", "properties": {...}},
    "handler": search_sightings,  # callable
}
```

Three consumers:

- `list_tools_openai()` — wraps in OpenAI function-calling format
  (`{"type": "function", "function": {...}}`). Returned by
  `/api/tools-catalog`. Consumed by the BYOK chat in `static/app.js`.
- `list_tools_mcp()` — wraps in MCP `tools/list` format (uses
  camelCase `inputSchema`, not OpenAI's `parameters`). Returned by the
  `/mcp` blueprint in `mcp_http.py`.
- `call_tool(name, arguments)` — direct dispatch. Used by both
  `/api/tool/<name>` (BYOK) and `/mcp` (MCP clients).

`tests/test_mcp_catalog.py` locks the invariants: every tool has a
name / description / parameters / handler, the names are unique, and
`/api/tools-catalog` returns the same set as `tools_catalog.TOOLS`.

## 7. Frontend state model

`static/app.js` uses a single global `state` object plus a URL hash
for persistence:

```js
const state = {
    activeTab: "map",
    filters: {},
    map: null, markerLayer: null, heatLayer: null, chart: null,
    mapMode: "clusters",
    timelineYear: null,
    searchPage: 0, searchTotal: 0, searchSort: "date_desc",
    dupesPage: 0, dupesTotal: 0,
    insightsCharts: {},
    hashLoading: false,  // prevents hash→filter→hash loops
};
```

URL hash format: `#/<tab>?<filter=value>&…`. `readHash()` parses it,
`writeHash()` writes it. Back/forward navigation re-runs
`applyHashToFilters()`.

Tab switching uses `visibility: hidden; opacity: 0` on inactive panels
instead of `display: none`, which keeps focus behavior correct and
allows fade transitions.

## 8. Directory layout

```
ufosint-explorer/
├── app.py                       # Flask app (single file)
├── mcp_http.py                  # /mcp blueprint (JSON-RPC)
├── mcp_server.py                # stdio MCP via FastMCP (for local clients)
├── tools_catalog.py             # shared tool definitions
├── Procfile                     # gunicorn entry point
├── requirements.txt             # production deps
├── requirements-dev.txt         # adds pytest + ruff
├── pyproject.toml               # ruff + pytest config
├── README.md
├── CHANGELOG.md
│
├── static/
│   ├── index.html               # ~860 lines, self-contained
│   ├── app.js                   # ~2700 lines, vanilla JS + Leaflet
│   └── style.css                # ~2500 lines, design tokens + components
│
├── scripts/
│   ├── pg_schema.sql            # canonical PostgreSQL DDL
│   └── migrate_sqlite_to_pg.py  # SQLite → Postgres loader
│
├── tests/
│   ├── conftest.py              # stubs psycopg pool, loads app once
│   ├── test_static_assets.py    # SVG dims, cache-bust pattern, tokens
│   ├── test_routes.py           # route registration, HTML shell
│   ├── test_cache_headers.py    # 4-tier Cache-Control policy
│   └── test_mcp_catalog.py      # tool-catalog invariants
│
├── docs/
│   ├── ARCHITECTURE.md          # this file
│   ├── DEPLOYMENT.md            # Azure setup, secrets, CI/CD
│   └── TESTING.md               # how to run tests
│
└── .github/
    └── workflows/
        └── azure-deploy.yml     # test → build → deploy → smoke
```

## 9. Conventions

- **Comments explain *why*, not *what*.** The code is the *what*.
- **Comments that survive a refactor are the valuable ones.** Keep them
  pinned to invariants, gotchas, and "why the obvious thing doesn't
  work here", not to line-by-line narration.
- **No build step on the frontend.** Adding Vite/Webpack/etc. is a
  commitment — don't do it casually. If something needs bundling, first
  ask whether a single inline `<script>` tag would do.
- **DB queries go through `get_db()` and `_PooledConn`.** Don't call
  `psycopg.connect(...)` directly from a route handler — you'll leak
  connections out of the pool.
- **Tests live in `tests/` and are imported by the app via the
  `_stub_database_and_load_app` session fixture.** Don't touch the
  real database from a test unless you're adding an integration suite
  that explicitly opts in.
- **Ruff is the lint gate.** Config in `pyproject.toml`. Run
  `ruff check . --fix` locally before pushing.
- **Changelog entries go in `CHANGELOG.md` under `## [Unreleased]`**,
  moved down into the released section when a tag is cut.

## 10. Known gotchas

- **Windows line endings**: git normalizes LF→CRLF on Windows checkouts.
  That's fine for `*.py` but you may see `warning: in the working copy
  ... LF will be replaced by CRLF` on commits — it's benign.
- **`mcp_http` imports from `app`**, so `app.register_blueprint(mcp_bp)`
  must happen *after* the `Flask(...)` instance is created. Ruff flags
  this as E402; the import carries `# noqa: E402`.
- **`/api/sighting/<id>` is intentionally not cached.** If you wrap it
  in `@cache.cached` you'll serve stale detail views after a DB rebuild.
- **`/api/timeline` and `/api/search` must LEFT JOIN `location`** when
  `coords=geocoded` is passed. This was silently broken before Sprint
  2 — see commit `de3f335`. The test `test_every_expected_route_is_registered`
  only catches the route-level regression; there's no query-level test yet.
- **`DATABASE_URL` is required at import time**, not request time. If
  you `from app import app` in a context where the pool can't open, the
  import raises. Tests work around this with a `_FakePool` stub — see
  `tests/conftest.py`.
