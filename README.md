# UFO Explorer

Interactive web interface for browsing the Unified UFO Sightings Database — **614,505 sighting records** from five major UFO/UAP databases, deduplicated and cross-referenced.

**Live:** [web-production-a941e.up.railway.app](https://web-production-a941e.up.railway.app)

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

## Deduplication

Three-tier cross-source deduplication flags **126,730 candidate pairs** for review without deleting any records:

- **Tier 1:** MUFON-NUFORC matching on date + city + state (7,694 pairs)
- **Tier 2:** All remaining cross-source pairs via location keys (51,879 pairs)
- **Tier 3:** Description fuzzy matching for date-only matches (17,157 pairs)

102,554 NUFORC records enriched with Hynek classifications from UFOCAT metadata.

## Tech Stack

- **Backend:** Python / Flask / Gunicorn
- **Database:** SQLite (1.3 GB, read-only)
- **Frontend:** Vanilla JS, Leaflet (maps), Chart.js (timeline)
- **Hosting:** Railway with persistent volume for the database

## Running Locally

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

Requires `ufo_unified.db` in the project directory. The database is built from source files using the ETL pipeline in the parent repository.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5000` | Server port (Railway sets this automatically) |
| `DB_PATH` | `./ufo_unified.db` | Path to the SQLite database file |

## Building the Database

The database is built from raw source files using a reproducible pipeline:

```bash
python rebuild_db.py
```

This runs: schema creation, 5 source imports, data quality fixes, metadata enrichment, and three-tier deduplication. Total build time: ~2 minutes.

## License

Data sourced from publicly available UFO/UAP databases. See the Methodology tab in the explorer for full attribution and provenance details.
