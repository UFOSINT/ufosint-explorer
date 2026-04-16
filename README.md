# UFOSINT Explorer

Interactive web interface for browsing the Unified UFO Sightings Database — **614,505 sighting records** from five major UFO/UAP databases, deduplicated and cross-referenced.

**Live:** [ufosint.com](https://ufosint.com)

> This repo is the successor to `UFOSINT/ufo-explorer`, which was a Railway-hosted proof of concept. Hosting has moved to Azure App Service. The POC repo is archived for reference.

## Features

### Observatory
- **GPU-accelerated map** — 396,158 geocoded sightings rendered via deck.gl on a Leaflet base. Points, heatmap, and hex-bin views. Click any sighting for a full detail modal.
- **Data Quality rail** — Five toggles (high quality, narrative red flags, has description, has media, has movement) filter the map in real time. Bias warning when the high-quality subset is active.
- **Live filters** — Shape, color, source, emotion, date range, and 10 movement-category checkboxes. No Apply button — selections take effect at 250ms debounce.
- **UAP Gerb overlays** (v0.12) — Three toggleable curated layers on top of the sighting map:
  - **Crashes** — 14 documented crash-retrieval events with craft type, recovery status, and biologics flags.
  - **Nuclear** — 35 nuclear-weapon encounters with weapon system, sensor confirmation, and witness credibility.
  - **Facilities** — 75 nuclear-relevant facilities (bases, labs, storage sites) used to compute proximity for every sighting.
  Click any overlay marker for a detail popup. Crash + nuclear events also appear as annotation stems on the TimeBrush so you can spot temporal clusters.

### TimeBrush
- **Shared across all tabs** — a zoomable, pannable histogram of sightings over time (1900 — present). Scroll to zoom; drag to pan; drag handles to select a playback window.
- **Adaptive granularity** — year, month, or day bars depending on zoom level, with a bilinear-scale overview mini-map (15% pre-1900, 85% post-1900).
- **Playback animation** — Hit Play to sweep a time window across the dataset. 10 speed options (0.5 day/sec to 10 year/sec). Sliding and cumulative modes. Timeline and Insights charts animate in sync.

### Timeline
- **Three-card dashboard** — All sightings stacked by source, quality score over time (median per year), and movement-category share over time.
- **Zoom-aware** — Charts reflect the TimeBrush zoom range. Day/Month/Year toggle matches the brush's adaptive granularity.
- **Cross-filtering** — Click any bar segment to isolate that source/year across all three charts.

### Insights
- **9 client-side cards** in 3 sections:
  - **Emotion & Sentiment** (5 cards) — Sentiment polarity, 7-class RoBERTa emotion distribution, 28-class GoEmotions detail (with neutral toggle), VADER/RoBERTa score distributions, emotion profile by source.
  - **Data Quality** (2 cards) — Quality score distribution, narrative red flags curve.
  - **Movement & Shape** (2 cards) — Movement taxonomy, shape-by-movement matrix.
- **Coverage strips** — Every card shows an N/total coverage indicator with color-coded pill (green/yellow/orange/red). Cards below 50% coverage are dimmed; below 30% show an "INSUFFICIENT DATA" banner.
- **Cross-filtering** — Click any chart segment to filter all other cards to that subset.

### AI Integration
- **BYOK AI chat** — Bring your own OpenAI, Anthropic, or OpenRouter API key. Chat with the database using 6 function-calling tools. Runs entirely in the browser; keys never leave localStorage.
- **MCP server** — `/mcp` exposes the same 6 tools over JSON-RPC 2.0 for Claude Desktop, Cursor, Cline, Continue, Windsurf, and any MCP-compatible client.
- **Data Quality gear popup** — Timeline and Insights tabs have a gear icon for adjusting quality filters without switching to Observatory.

### First-Visit Experience
- **Cinematic landing** — Terminal-style counter ticks up to the total sighting count, then dissolves to reveal the map.
- **Guided tour** — 5-step tooltip walkthrough highlighting the map, rail, TimeBrush, tabs, and stats badge. Respects `prefers-reduced-motion`.
- **Help button** — `?` icon in the header replays the tour anytime.

### Other
- **Methodology** — Full documentation of the import pipeline, deduplication strategy, scoring methodology, and transformer emotion analysis.
- **Export** — CSV and JSON download of filtered result sets.
- **Mobile responsive** — Accordion rail, wrapping filter bar, touch-friendly TimeBrush on phones and tablets.
- **Themes** — Signal (dark cyan) and Declass (warm amber) color schemes.

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

## Download the Database

The public SQLite snapshot is attached to every versioned release:

**[📦 Latest release download (ufo_public.db, 508 MB)](https://github.com/UFOSINT/ufosint-explorer/releases/latest/download/ufo_public.db)**

Or via the GitHub CLI:

```bash
gh release download --latest -R UFOSINT/ufosint-explorer -p ufo_public.db
```

Or direct curl:

```bash
curl -LO https://github.com/UFOSINT/ufosint-explorer/releases/latest/download/ufo_public.db
```

What's inside:

- **614,505 deduplicated sightings** from NUFORC, MUFON, UFOCAT, UPDB, UFO-search
- **502,985 emotion-analyzed** rows (4 transformer models — see next section)
- **396,158 geocoded** with lat/lng coordinates
- Derived columns: quality score, richness score, hoax likelihood, movement categorization, standardized shape
- SQLite format — works with any SQLite client, the `sqlite3` CLI, or Python's built-in `sqlite3`

Quick inspection:

```bash
sqlite3 ufo_public.db ".tables"
sqlite3 ufo_public.db "SELECT COUNT(*) FROM sighting;"
sqlite3 ufo_public.db "SELECT shape, COUNT(*) FROM sighting GROUP BY shape ORDER BY 2 DESC LIMIT 10;"
```

**Privacy note:** the public database has raw narrative text stripped (description / summary / notes columns are NULL). All derived columns (emotion, quality, movement) were computed from the private corpus before the strip and ship as structured fields. See the [Methodology tab](https://ufosint.com#/methodology) on the live site for the full pipeline.

## Emotion & Sentiment Analysis (v0.11)

Three transformer models + VADER run against 502,985 sightings with narrative text:

| Model | Type | Output | Coverage |
|---|---|---|---|
| **RoBERTa** (cardiffnlp) | 3-class sentiment | positive / negative / neutral | 502,985 rows |
| **RoBERTa** (j-hartmann) | 7-class emotion | anger, disgust, fear, joy, neutral, sadness, surprise | 502,985 rows |
| **GoEmotions** (SamLowe) | 28-class emotion | admiration, amusement, anger, ... (28 labels) | 502,985 rows |
| **VADER** | Rule-based sentiment | compound score (-1 to +1) | 502,985 rows |

All models run offline via the science team's analysis pipeline. Results are stored as 12 new columns in the sighting table and packed into the 40-byte binary bulk buffer for client-side rendering.

## Binary Bulk Buffer

The frontend loads all 396,158 mapped sightings in a single 15MB binary fetch (`/api/points-bulk`). Each sighting is packed into **40 bytes** (v011-1 schema):

```
<IffIBBBBBBBBBBHHHBBBBBBBB   (26 fields, 40 bytes per row)
```

Fields include: sighting ID, lat/lon, date (day-index), source/shape/color/emotion indices, quality/hoax/richness scores, flags (has_description, has_media, has_movement), movement bitmask, and 5 emotion/sentiment fields (emotion_28_group, emotion_28_dominant, emotion_7_dominant, vader_compound, roberta_sentiment).

All client-side filtering, histogramming, and chart rendering runs against these typed arrays — typically completing in <5ms for 396k rows.

## Tech Stack

- **Backend:** Python 3.12 / Flask / Gunicorn (2 workers x 4 threads)
- **Database:** Azure Database for PostgreSQL Flexible Server (Burstable B1ms), accessed via `psycopg` 3 + `psycopg_pool`
- **Frontend:** Vanilla JS (~7,300 lines), Leaflet + deck.gl (map), Chart.js (charts), Inter font
- **Binary protocol:** 40-byte packed struct per sighting, ~15MB raw / ~6MB gzipped
- **AI:** BYOK chat (OpenAI/Anthropic/OpenRouter), MCP server (JSON-RPC 2.0 + stdio)
- **Hosting:** Azure App Service (Linux, B1 tier)
- **CI/CD:** GitHub Actions -> `azure/webapps-deploy@v3` on push to `main` or tag push
- **Testing:** pytest (500+ static analysis tests) + ruff, enforced in CI before every deploy

## MCP Tools

Six read-only tools exposed via `/mcp` (JSON-RPC 2.0) and `/api/tools-catalog` (OpenAI format):

| Tool | Description |
|------|-------------|
| `search_sightings` | Free-text + filter search (up to 200 records). Filters: shape, source, state, country, date range, Hynek class. |
| `get_sighting` | Full record for a single sighting by ID. |
| `get_stats` | Top-level database statistics: totals, per-source counts, date range, geocoded count. |
| `get_timeline` | Counts grouped by year or month. Optional source/shape filter. |
| `find_duplicates_for` | Cross-source duplicate candidates for a given sighting ID. |
| `count_by` | Top-N rankings by categorical field (shape, hynek, vallee, source, country, state). |

### Claude Desktop Setup

```json
{
  "mcpServers": {
    "ufosint": {
      "url": "https://ufosint.com/mcp",
      "transport": "http"
    }
  }
}
```

### Claude Code Setup

```json
{
  "mcpServers": {
    "ufosint": {
      "url": "https://ufosint.com/mcp",
      "transport": "http"
    }
  }
}
```

Add to `.claude/settings.json` or your global Claude Code MCP config.

## Documentation

| Doc | For |
|-----|-----|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | App structure, request flow, cache strategy, DB access patterns, tool catalog, conventions. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Azure infrastructure, environment variables, CI/CD pipeline, release procedures. |
| [`docs/TESTING.md`](docs/TESTING.md) | Test suite coverage, how to run locally, how to add new tests. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history (SemVer). |

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
# Full suite (< 2 seconds, 500+ tests)
pytest

# Lint
ruff check .

# JS syntax check
node -c static/app.js && node -c static/deck.js
```

See [`docs/TESTING.md`](docs/TESTING.md) for the full breakdown of what each test file covers.

## Deploying

Push to `main` — that's it. The GitHub Actions workflow runs `test -> build -> deploy -> smoke`. The `smoke` stage verifies the live HTML ships with versioned asset URLs; if it doesn't, the deploy fails.

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full pipeline breakdown, Azure setup, and rollback procedure.

## Environment Variables

| Variable | Required? | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | **Yes** | — | psycopg connection string, must include `sslmode=require`. App refuses to start without it. |
| `PORT` | No | `5000` | Server port. App Service sets this automatically. |
| `ASSET_VERSION` | No | auto | Override the auto-computed asset version string. |

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md#5-environment-variables-reference) for the full list.

## Feedback & Issues

Found a bug or have an idea? [Open a GitHub Issue](https://github.com/UFOSINT/ufosint-explorer/issues). We read every one.

- **Bug reports** — include your browser, OS, and what you expected vs. what happened
- **Feature ideas** — describe the use case, not just the solution
- **Data questions** — if something looks wrong in the database, mention the sighting ID

We're not accepting pull requests at this time — all development is handled internally. But issues and suggestions are very welcome.

## License

Code in this repo is released under a permissive license (to be finalized — likely MIT). Data sourced from publicly available UFO/UAP databases; see the Methodology tab in the explorer for full attribution and provenance details. Redistribution of the raw source datasets is subject to each source's individual terms.
