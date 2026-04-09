# UFOSINT Explorer

Interactive web interface for browsing the Unified UFO Sightings Database — **614,505 sighting records** from five major UFO/UAP databases, deduplicated and cross-referenced.

**Live:** [ufosint.com](https://ufosint.com) *(deployment in progress)*

> This repo is the successor to `UFOSINT/ufo-explorer`, which was a Railway-hosted proof of concept. We have moved hosting to Azure App Service and rebranded around the ufosint.com domain. The POC repo is archived for reference.

## Features

- **Interactive Map** — Clustered markers and heatmap view of geocoded sightings with bounding-box filtering
- **Timeline** — Stacked bar chart of sightings by year, drill down into monthly view by clicking a year
- **Search** — Full-text search across 614K sighting descriptions with filters for shape, source, Hynek/Vallee classification, date range, and collection
- **Duplicates Browser** — Browse 126,730 cross-source duplicate candidate pairs with confidence scores, filterable by method, score range, and source
- **Collection Filtering** — Filter by data provenance: CAPELLA (Vallee), PUBLIUS (MUFON/NUFORC), GELDREICH (Majestic Timeline), UFOCAT
- **Methodology** — Full documentation of the import pipeline, deduplication strategy, and scoring methodology

## Data Sources

| Collection | Source | Records | Description |
|---|---|---|---|
| PUBLIUS | MUFON | 138,310 | Mutual UFO Network case reports |
| PUBLIUS | NUFORC | 159,320 | National UFO Reporting Center |
| UFOCAT | UFOCAT | 197,108 | CUFOS academic catalog (2023) |
| CAPELLA | UPDB | 65,016 | Jacques Vallee's Unified Phenomena Database |
| GELDREICH | UFO-search | 54,751 | Majestic Timeline (19+ historical compilations) |

Total raw records across all sources: ~2.56 million. After removing known overlaps at import time: **614,505**.

All source datasets were paid for / licensed by UFOSINT. This repository contains only code; the raw sources and the built database live outside the repo — see the `ufo-dedup` pipeline repo for details.

## Deduplication

Three-tier cross-source deduplication flags **126,730 candidate pairs** for review without deleting any records:

- **Tier 1:** MUFON-NUFORC matching on date + city + state (7,694 pairs)
- **Tier 2:** All remaining cross-source pairs via location keys (51,879 pairs)
- **Tier 3:** Description fuzzy matching for date-only matches (17,157 pairs)

102,554 NUFORC records enriched with Hynek classifications from UFOCAT metadata.

## Tech Stack

- **Backend:** Python 3.12 / Flask / Gunicorn (2 workers, 4 threads)
- **Database:** SQLite (~1.4 GB, read-only, mmap-backed)
- **Frontend:** Vanilla JS, Leaflet (maps), Chart.js (timeline)
- **Hosting:** Azure App Service (Linux, B1 tier) — custom domain via App Service Managed Certificate
- **CI/CD:** GitHub Actions → `azure/webapps-deploy@v3` on push to `main`

## Running Locally

```bash
pip install -r requirements.txt
export DB_PATH=../data/output/ufo_unified.db   # or wherever your DB lives
python app.py
# Open http://localhost:5000
```

The database is built from raw source files by the ETL pipeline in the sibling repo [`UFOSINT/ufo-dedup`](https://github.com/UFOSINT/ufo-dedup).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5000` | Server port (App Service sets this automatically) |
| `DB_PATH` | `./ufo_unified.db` | Absolute path to the SQLite database file. In Azure App Service we set this to `/home/data/ufo_unified.db` so it points at the persistent `/home` volume. |

## Deploying to Azure App Service

Automated via `.github/workflows/azure-deploy.yml`. The flow is:

1. **Prerequisites** (one-time, done in the Azure portal or with `az`):
   - Create a resource group, e.g. `rg-ufosint-prod`
   - Create a Linux App Service Plan, B1 tier
   - Create a Web App named `ufosint-explorer` with Python 3.12 runtime
   - Set app setting `DB_PATH=/home/data/ufo_unified.db`
   - Set startup command: leave blank (respects the `Procfile`) or set to `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 180`
2. **Upload the database** (one-time) to `/home/data/ufo_unified.db` via FTPS or `az webapp deploy`
3. **Add the publish profile** to GitHub: repo Settings → Secrets → Actions → `AZUREAPPSERVICE_PUBLISHPROFILE` (paste the full XML from the portal's "Get publish profile" download)
4. **Push to `main`** — the workflow builds, zips, and deploys automatically
5. **Custom domain**: in the App Service → Custom domains → add `ufosint.com`, validate via DNS TXT + A records, then enable App Service Managed Certificate for free HTTPS

When the deduplication pipeline rebuilds the database, upload the new file to `/home/data/ufo_unified.db` (via FTPS or `az webapp deploy --type static`) and restart the Web App to pick it up.

## Building the Database

The database is built from raw source files using the reproducible pipeline in the [`UFOSINT/ufo-dedup`](https://github.com/UFOSINT/ufo-dedup) repo:

```bash
cd ../ufo-dedup
python rebuild_db.py
```

This runs: schema creation, 5 source imports, data quality fixes, metadata enrichment, and three-tier deduplication. Total build time: ~2 minutes on a developer laptop.

## License

Code in this repo is released under a permissive license (to be finalized — likely MIT). Data sourced from publicly available UFO/UAP databases; see the Methodology tab in the explorer for full attribution and provenance details. Redistribution of the raw source datasets is subject to each source's individual terms — see the migration notes in the parent workspace for the per-source redistribution matrix.
