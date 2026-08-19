# CLAUDE.md — Agent handoff for `ufosint-explorer`

If you're a Claude Code session starting cold in this repo, read this
first. It's deliberately short — the real docs are what it points at.

## Who works here

- **This repo (`ufosint-explorer/`)** — Flask web app + vanilla-JS frontend.
  Live at https://ufosint.com.
- **Sibling repo (`../ufo-dedup/`)** — ETL + deduplication pipeline. Builds
  the SQLite files this app's Postgres is loaded from. See its own
  `CLAUDE.md`.
- **`../UFO-UX/`** — shared design sandbox.

**One owner, both repos** (as of 2026-08-19). Earlier revisions of this file
described a split where a separate agent owned each side and cross-repo edits
needed explicit permission — that no longer applies. Edit either.

Workspace root is `/Users/thomhastings/Documents/UFO_Files/UFOSINT.com/`.

## Where the actual documentation lives

| You want… | Read… |
|-----------|-------|
| How the app fits together (request flow, cache, DB access, conventions) | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| How to deploy, env vars, Azure setup, rollback | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Known failure modes, restart runbook, incident log | [`docs/OPERATIONS.md`](docs/OPERATIONS.md) |
| How to run + extend the test suite | [`docs/TESTING.md`](docs/TESTING.md) |
| What shipped when (SemVer history) | [`CHANGELOG.md`](CHANGELOG.md) |
| Feature-sprint plan docs | `docs/V08*_PLAN.md` |

## What makes this codebase unusual

- **No frontend build step.** `static/app.js` is vanilla JS loaded
  directly by the browser. Don't add Vite/Webpack without a very good
  reason — the user has explicitly pushed back on bundling.
- **Single-file Flask backend.** `app.py` is ~3,300 lines by design.
  Split only when a clear subsystem emerges.
- **Pre-substituted HTML.** `static/index.html` is read once at import
  time, `{{ASSET_VERSION}}` is replaced, and the result is cached as
  `_INDEX_HTML`. Don't add per-request Jinja rendering.
- **No staging environment (removed in the Aug 2026 hosting migration).**
  Prod moved to a separate Azure account (owner: Thom Hastings) and the
  B1 staging App Service was retired to save cost. There is now a single
  deploy target. Test locally before merging; there is no shared staging
  URL to smoke-test against anymore.
- **Single-target deploy pipeline.** `main` push (or a `v*` tag) → prod
  via `.github/workflows/azure-deploy.yml`, which deploys to the
  `ufosint-explorer-app` App Service and owns DB migrations. `feature/**`
  branches no longer auto-deploy anywhere. See also `docs/DEPLOYMENT.md`.
- **Schema-change ordering still applies.** Since the deploy workflow
  runs migrations against the prod Postgres before/at deploy, a schema
  change must be compatible with the code being deployed in the same
  push, or the app will 500.
- **Migrations must not seed rows.** Every migration in the workflow's list
  runs on *every* deploy, so a row a migration writes is a row a deploy can
  resurrect. `add_v013_reddit_columns.sql` seeded a `source_database` row
  behind an `IF NOT EXISTS` guard — idempotent only while the row exists, so
  the deploy carrying the v0.16 purge re-created what it had just deleted.
  Schema migrations create structure, not content;
  `tests/test_v016_purge.py` enforces this across the whole migration set.
- **The SQLite source can outrank prod.** `../ufo-dedup/` builds the files
  `scripts/migrate_sqlite_to_pg.py` and `scripts/reload_from_public_db.py`
  load from. If those files are older than prod, a reload silently reverts
  prod. Both were realigned to 476,195 rows on 2026-08-19; check before any
  reload.

## Conventions the user cares about

- Terse, option-laden responses; no trailing "here's what I did" summaries.
- Comments explain *why*, not *what*.
- Changelog entries land under `## [Unreleased]`, moved down on tag cut.
- Ruff is the lint gate. Run `ruff check .` locally before pushing.
- After feature work, bundle the docs updates (CHANGELOG + README +
  `/llms.txt` in `app.py` + `docs/ARCHITECTURE.md`) as a single commit
  before merging to main.

## Before you act on a recalled memory

Memory may be stale — branch names, commit SHAs, row counts and
uncommitted file lists all change quickly. Always verify with
`git status` / `git log`, or a live query, before repeating a claim the
memory makes about current state. Row counts in particular moved twice in
August 2026.
