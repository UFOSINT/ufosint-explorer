# UFOSINT Explorer

Interactive web interface for browsing the Unified UFO Sightings Database — **614,505 sighting records** from five major UFO/UAP databases, deduplicated and cross-referenced.

**Live:** [ufosint-explorer.azurewebsites.net](https://ufosint-explorer.azurewebsites.net) · *(custom domain `ufosint.com` in progress)*

> This repo is the successor to `UFOSINT/ufo-explorer`, which was a Railway-hosted proof of concept. We have moved hosting to Azure App Service and rebranded around the ufosint.com domain. The POC repo is archived for reference.

## Features

- **Interactive Map** — Clustered markers and heatmap view of geocoded sightings with bounding-box filtering
- **Timeline** — Stacked bar chart of sightings by year, drill down into monthly view
- **Search** — Full-text search across 614K descriptions with filters for shape, source, Hynek/Vallee classification, date range, country, state, and collection
- **Duplicates Browser** — Browse 126,730 cross-source duplicate candidate pairs with confidence scores
- **Insights** — Sentiment and emotion analytics dashboard
- **Export** — CSV and JSON download of filtered result sets
- **Methodology** — Full documentation of the import pipeline, deduplication strategy, and scoring methodology
- **BYOK AI chat** — Bring your own OpenAI / Anthropic / OpenRouter key and chat with the database using function calling
- **MCP server** — `/mcp` exposes the same 6 tools over JSON-RPC for Claude Desktop, Cursor, Cline, Continue, Windsurf, etc.

## Data Sources

| Collection | Source | Records | Description |
|---|---|---|---|
| PUBLIUS | MUFON | 138,310 | Mutual UFO Network case reports |
| PUBLIUS | NUFORC | 159,320 | National UFO Reporting Center |
| UFOCAT | UFOCAT | 197,108 | CUFOS academic catalog (2023) |
| CAPELLA | UPDB | 65,016 | Jacques Vallee's Unified Phenomena Database |
| GELDREICH | UFO-search | 54,751 | Majestic Timeline (19+ historical compilations) |

Total raw records across all sources: ~2.56 million. After removing known overlaps at import time: **614,505**.

All source datasets were paid for / licensed by UFOSINT. This repository contains only code; the raw sources and the built database live outside the repo — see the [`ufo-dedup`](https://github.com/UFOSINT/ufo-dedup) pipeline repo for details.

## Deduplication

Three-tier cross-source deduplication flags **126,730 candidate pairs** for review without deleting any records:

- **Tier 1:** MUFON–NUFORC matching on date + city + state (7,694 pairs)
- **Tier 2:** All remaining cross-source pairs via location keys (51,879 pairs)
- **Tier 3:** Description fuzzy matching for date-only matches (17,157 pairs)

102,554 NUFORC records enriched with Hynek classifications from UFOCAT metadata.

## Tech Stack

- **Backend:** Python 3.12 / Flask / Gunicorn (2 workers × 4 threads)
- **Database:** Azure Database for PostgreSQL Flexible Server (Burstable B1ms), accessed via `psycopg` 3 + `psycopg_pool`
- **Frontend:** Vanilla JS, Leaflet (maps + markercluster + heat), Chart.js (timeline), Inter font
- **Hosting:** Azure App Service (Linux, B1 tier)
- **CI/CD:** GitHub Actions → `azure/webapps-deploy@v3` on push to `main` or tag push
- **Testing:** pytest + ruff, enforced in CI before every deploy

## Documentation

| Doc | For |
|-----|-----|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the app is structured, request flow, cache strategy, DB access patterns, tool catalog, conventions, and known gotchas. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Azure infrastructure setup, environment variables, CI/CD pipeline, how to cut a release, and how to debug the live site. |
| [`docs/TESTING.md`](docs/TESTING.md) | What the test suite covers, how to run it locally, and how to add new tests. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history, following SemVer. |

## Running Locally

```bash
# Install deps (dev includes pytest + ruff)
pip install -r requirements-dev.txt

# Point at a Postgres instance
export DATABASE_URL='postgresql://user:pass@host:5432/ufo_unified?sslmode=require'

# Start the server
python app.py
# http://localhost:5000
```

The database is built from raw source files by the ETL pipeline in the sibling repo [`UFOSINT/ufo-dedup`](https://github.com/UFOSINT/ufo-dedup). Use `scripts/migrate_sqlite_to_pg.py` to load the resulting SQLite snapshot into PostgreSQL.

## Running Tests

```bash
# Full suite (< 1 second)
pytest

# Lint
ruff check .

# Auto-fix lint issues
ruff check . --fix
```

See [`docs/TESTING.md`](docs/TESTING.md) for the full breakdown of what each test file covers and how to add new ones.

## Deploying

Push to `main` — that's it. The GitHub Actions workflow runs `test → build → deploy → smoke`. The `smoke` stage verifies the live HTML ships with versioned asset URLs (`style.css?v=<version>`); if it doesn't, the deploy is marked failed.

To cut a tagged release:

```bash
# Move the CHANGELOG entry out of [Unreleased]
vim CHANGELOG.md
git commit -am "Prep v0.4.2 release"

git tag -a v0.4.2 -m "v0.4.2 — <summary>"
git push origin main v0.4.2
```

Pushing the tag re-triggers the workflow because the trigger list includes `tags: 'v*'`.

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full pipeline breakdown, Azure setup, and rollback procedure.

## Environment Variables

| Variable | Required? | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | **Yes** | — | psycopg connection string, must include `sslmode=require`. App refuses to start without it. |
| `PORT` | No | `5000` | Server port. App Service sets this automatically. |
| `ASSET_VERSION` | No | auto | Override the auto-computed asset version string. Only set this when debugging the versioning system. |

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#5-environment-variables-reference) for the full list.

## License

Code in this repo is released under a permissive license (to be finalized — likely MIT). Data sourced from publicly available UFO/UAP databases; see the Methodology tab in the explorer for full attribution and provenance details. Redistribution of the raw source datasets is subject to each source's individual terms.
