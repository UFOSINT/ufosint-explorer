# Testing

The repo ships a pytest suite plus a ruff lint gate. Both run on every
push to `main` via `.github/workflows/azure-deploy.yml` before the
build job. The tests are designed to be cheap (whole suite finishes in
< 2 seconds locally, **506 tests**) and catch structural regressions:
HTML/CSS/JS skew, missing cache-busting, broken asset URLs, and
feature invariants for each sprint.

## Running locally

```bash
# Install dev deps (includes pytest + ruff)
pip install -r requirements-dev.txt

# Run everything
pytest

# Run a single file
pytest tests/test_cache_headers.py -v

# Run a single test
pytest tests/test_routes.py::test_index_route_substitutes_asset_version -v

# JS syntax check
node -c static/app.js && node -c static/deck.js

# Lint
ruff check .

# Auto-fix what's fixable
ruff check . --fix
```

No database is required — `tests/conftest.py` stubs
`psycopg_pool.ConnectionPool` with a no-op so the app module imports
cleanly without a live Postgres server.

## Test files

### Core infrastructure (5 files)

| File | Tests | What it covers |
|------|-------|---------------|
| `conftest.py` | — | Session-scoped fixture: stubs connection pool, exposes `flask_app`, `client`, `asset_version` |
| `test_static_assets.py` | 17 | SVG sizing/count, cache-bust pattern, no emoji, design tokens, JS syntax |
| `test_routes.py` | 5 | Route registration, `{{ASSET_VERSION}}` substitution, content-type, cache-control |
| `test_cache_headers.py` | 5 | Four-tier Cache-Control policy for static assets vs HTML |
| `test_mcp_catalog.py` | 7 | Tool catalog invariants: 6 tools, unique names, MCP format, blueprint mounted |

### Sprint-level feature tests (18 files)

Each sprint ships a test file that locks its feature invariants so future
changes don't silently break them. These are static analysis tests —
they read `app.py`, `app.js`, `deck.js`, `index.html`, and `style.css`
and assert structural properties (function signatures, CSS rules, HTML
elements) without running a server.

| File | Tests | Sprint | What it covers |
|------|-------|--------|---------------|
| `test_v07.py` | 20 | v0.7 | Observatory tab, theme toggle, settings menu, stats badge |
| `test_v075_mv.py` | 8 | v0.7.5 | Materialized view, points-bulk etag, year-stats |
| `test_v080_bulk.py` | 33 | v0.8.0 | 40-byte binary schema, deck.gl layer, scatterplot/heatmap/hex |
| `test_v081_timeline.py` | 12 | v0.8.1 | TimeBrush playback, speed options, cumulative mode |
| `test_v082_derived.py` | 25 | v0.8.2 | Derived columns (quality, hoax, richness, emotion, color) |
| `test_v083_no_raw_text.py` | 8 | v0.8.3 | Public DB strips raw text, has_description flag |
| `test_v084_theme.py` | 18 | v0.8.4 | Signal/Declass themes, CSS tokens, Carto basemap |
| `test_v085_movement.py` | 22 | v0.8.5 | Movement categories, bitmask, 10-category taxonomy |
| `test_v086.py` | 30 | v0.8.6 | Timeline redesign, Insights client-side, Search/Duplicates removed |
| `test_v087.py` | 15 | v0.8.7 | Filter bar cleanup, movement cluster, quality rail |
| `test_v088.py` | 28 | v0.8.8 | Emotion cards from POINTS, methodology expansion |
| `test_v090.py` | 22 | v0.9.0 | TimeBrush zoom/pan, mobile responsive, accordion rail |
| `test_v091.py` | 18 | v0.9.1 | Coverage strips, year-0019 fix, source null handling |
| `test_v092.py` | 16 | v0.9.2 | Adaptive granularity, live commit, day/month/year toggle |
| `test_perf_infra.py` | 12 | v0.8.0+ | Performance invariants: typed arrays, binary packing |
| `test_loading_system.py` | 10 | v0.8.0+ | Progressive loading, skeleton states |
| `test_progressive_loading.py` | 8 | v0.8.6+ | Chart.update("none"), no destroy in refresh path |
| `test_v0111.py` | 19 | v0.11.1 | Playback performance, DQ gear popup, progress bar |
| `test_v0112.py` | 34 | v0.11.2 | Cinematic intro, guided tour, help button, AI discovery |

**Total: 506 tests across 23 files.**

## Adding a new test

1. Decide which file it belongs in. If none fit, create a new
   `tests/test_<topic>.py` — pytest picks it up automatically.
2. Use the existing fixtures where possible:
   - `flask_app` — the loaded Flask app object
   - `client` — a Flask test client
   - `asset_version` — the computed asset version string
3. Keep tests fast. Anything that touches the network or the real DB
   should be marked `@pytest.mark.slow` and excluded from the default
   run.
4. If you're adding an invariant (not just a regression test), update
   the relevant section of `docs/ARCHITECTURE.md` too.

## Adding a test that hits the real database

None of the existing tests do this. If you need to:

1. Add `DATABASE_URL` to the test runner env via a `.env.test` file
   that's gitignored.
2. Mark the test `@pytest.mark.db` so it can be opted out of with
   `pytest -m "not db"`.
3. In the GitHub Actions workflow, add a secondary `test-integration`
   job that runs only against a staging / ephemeral database. Don't
   hit production from CI.

## Ignored failures

None. Every test in the suite blocks deploy when it fails. If a test
starts producing false positives, fix it or delete it — don't `xfail`
it and forget.
