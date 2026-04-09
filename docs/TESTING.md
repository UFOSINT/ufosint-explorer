# Testing

The repo ships a pytest suite plus a ruff lint gate. Both run on every
push to `main` via `.github/workflows/azure-deploy.yml` before the
build job. The tests are designed to be cheap (whole suite finishes in
< 1 second locally) and catch the specific class of bug that hit us in
Sprint 4: HTML/CSS/JS skew, missing cache-busting, broken asset URLs.

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

# Skip the slower ones (we don't really have any yet)
pytest -m "not slow"

# Lint
ruff check .

# Auto-fix what's fixable
ruff check . --fix
```

No database is required — `tests/conftest.py` stubs
`psycopg_pool.ConnectionPool` with a no-op so the app module imports
cleanly without a live Postgres server.

## What each file covers

### `tests/conftest.py`
Session-scoped fixture that stubs the connection pool, then imports the
app module once. Exposes `flask_app`, `client`, and `asset_version`
fixtures.

### `tests/test_static_assets.py` (17 tests)
Pure file-read checks on the frontend:

- **SVG sizing** — every inline `<svg>` in `static/index.html` and
  `static/app.js` must carry explicit `width` and `height` attributes.
  This is the defensive fallback that prevents the Sprint 4 stale-cache
  bug from recurring.
- **SVG count** — locked at 10 in `index.html` so accidental
  duplication shows up in diffs.
- **Cache-bust pattern** — `index.html` must reference
  `style.css?v={{ASSET_VERSION}}` and `app.js?v={{ASSET_VERSION}}`.
- **No emoji** — Sprint 4 replaced every emoji with an SVG; the allow
  list is `✓` and `✗` (used as inline feedback on the copy button).
- **Required design tokens** — `style.css` must define
  `--text-strong`, `--font-mono`, `--font-sans`, `--cat-1`,
  `--success`, `--warning`, `--danger`, `--shadow-sm`.
- **`.icon` base rule** — must exist, must size via `1em`.
- **No rogue `#6c5ce7` purple** — the Sprint 4 chip unification killed
  this; a regression means a new chip rule was added without reusing
  the shared tokens.
- **JS syntax** — delegates to `node -c static/app.js` when node is on
  PATH. Skipped on bare-bones dev machines; CI always has node.

### `tests/test_routes.py` (5 tests)
Flask route registration + HTML shell:

- Every expected route is in `app.url_map`.
- `GET /` returns HTML with the `{{ASSET_VERSION}}` placeholder fully
  substituted (no leakage).
- `GET /` has `Content-Type: text/html` and `Cache-Control: max-age=60`.
- `ASSET_VERSION` is a non-empty hex-ish string of reasonable length.

### `tests/test_cache_headers.py` (5 tests)
The four-tier Cache-Control policy from `add_cache_headers`:

| URL pattern               | Expected Cache-Control                    |
|---------------------------|-------------------------------------------|
| `/static/style.css?v=x`   | `public, max-age=31536000, immutable`     |
| `/static/app.js?v=x`      | `public, max-age=31536000, immutable`     |
| `/static/style.css`       | `public, max-age=3600, must-revalidate`   |
| `/static/app.js`          | `public, max-age=3600, must-revalidate`   |
| `/`                       | `public, max-age=60`                      |

If you change these, update both the tests and `docs/ARCHITECTURE.md`
section 3 in the same commit.

### `tests/test_mcp_catalog.py` (7 tests)
MCP / BYOK tool-catalog invariants:

- `tools_catalog` module imports cleanly.
- Exactly 6 tools defined.
- Every tool has `name`, `description`, `parameters` dict, callable
  `handler`.
- Tool names are unique.
- `/api/tools-catalog` returns the same set of names as
  `tools_catalog.TOOLS`.
- `list_tools_mcp()` uses `inputSchema` (camelCase MCP format), not
  `parameters` (OpenAI function format).
- `/mcp` blueprint is mounted.

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
