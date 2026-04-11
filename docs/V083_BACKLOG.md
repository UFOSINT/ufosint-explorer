# v0.8.3 / v0.9 backlog

Items identified by the science team during the v0.8.2 rebuild +
migration cycle, plus the v0.8.3-specific rewires that were
explicitly deferred when v0.8.2 shipped. Ordered roughly by
priority. Nothing in here is committed work yet — this is the
staging area for the next sprint.

## v0.8.3 scope — raw-text retirement prerequisites

These must land (and be verified live) before `drop_raw_text_columns.sql`
can be applied to the public Postgres. Both endpoints currently
depend on `description` / `summary` and would return zero results
or broken detail pages after the drop.

### 1. `/api/search` rewire — faceted, not text-based

**Current state:** `ILIKE` + `pg_trgm` against `description` and
`summary`. Lives behind `idx_sighting_description_trgm` and
`idx_sighting_summary_trgm` GIN indexes, which become dead weight
after the drop.

**New design:** A faceted search against the derived fields and
the existing structured metadata.

Search inputs:
- `q` — free-text match against city / state / country / source /
  standardized_shape / primary_color / dominant_emotion. No
  description/summary fallback.
- `quality_min` — inclusive `quality_score` threshold
- `hoax_max` — inclusive `hoax_likelihood` threshold (REAL 0.0–1.0)
- `has_description` — `true`/`false`/null
- `has_media` — `true`/`false`/null
- `standardized_shape` — exact match
- `primary_color` — exact match
- `dominant_emotion` — exact match
- `date_from` / `date_to` — ISO date range
- `country`, `state`, `city` — existing structured filters

Response shape stays the same (id + location + date + derived
metadata). Drop `description_snippet` from the response entirely.

The search UI needs a full rebuild — the current free-text input
becomes a compound filter panel. Most of the controls already exist
in the Observatory left rail, so we can lift that component.

### 2. `/api/sighting/:id` rewire — derived-only detail modal

**Current state:** Returns the full raw `description`, `summary`,
`notes`, `raw_json`, `date_event_raw`, `time_raw`, and
`witness_names`. The detail modal in `static/app.js` renders the
description text as the primary content of the popup.

**New design:** The modal shows only derived metadata + location +
links. Rough layout:

```
┌──────────────────────────────────────────┐
│ SIGHTING #136613        [ ✕ close ]      │
├──────────────────────────────────────────┤
│ Shape:    Triangle                       │
│ Color:    Black                          │
│ Emotion:  Fear                           │
│ Duration: 5 minutes                      │
│                                          │
│ Quality:  ██████████░  78 / 100          │
│ Hoax:     █░░░░░░░░░░  0.12              │
│                                          │
│ Date:     1978-11-09 (local time)        │
│ Location: Los Alamos, New Mexico, USA    │
│           35.8801°N  106.3001°W          │
│ Source:   NUFORC                         │
│                                          │
│ 📝 Has written description (private)     │
│ 📷 Has media reference                   │
│                                          │
│ [ Related: see all in Los Alamos → ]     │
│ [ Related: see 1978 on timeline → ]      │
└──────────────────────────────────────────┘
```

Key design choices:
- **Quality/hoax bars** instead of raw numbers, for instant
  readability.
- **Has description / has media indicators** — rendered as "exists
  privately" with no link, since the text itself is not in the
  public DB anymore. Sets the right legal story.
- **No raw fields** anywhere in the response. The private SQLite
  on the user's machine is the only place the narratives live.
- **Sighting-analysis JSON side-fields** (behavior_tags,
  emotion_scores, color_list, hoax_flags, raw_shape_matched_via)
  would land here if we decide to migrate the `sighting_analysis`
  table to Postgres — see item #3 in the science-team backlog
  below.

### 3. Sentiment / Insights rewire

`/api/sentiment/overview` and the Insights tab's emotion charts
currently read from the `sentiment_analysis` table (VADER +
NRCLex). That table is unaffected by the raw-text drop — the
derived scores stay. But the Insights tab also renders
"representative sightings" cards that pull the description text.
Those cards need the same treatment as the detail modal (derived
metadata only).

Tests to write for v0.8.3:
- `/api/search` returns 200 with every combination of the new filter params
- `/api/sighting/:id` response never contains `description`, `summary`, `notes`, `raw_json`
- Detail modal HTML never renders `description` into the DOM
- Insights tab's representative-sightings cards use derived fields only

---

## Science team backlog (from the v0.8.2 cycle)

Items the science team flagged during the rebuild. Logged here so
they don't get lost. Some belong in `ufo-dedup`, some belong in
`ufosint-explorer`.

### Repo: `ufo-dedup`

#### SCI-1. Silent exception swallowing in `sentiment.py`

**Priority: HIGH.** `sentiment.py:78-82` does
`except Exception: emo = {}`, which silently produced all-zero
emotion data for months because NLTK corpora weren't available.
Nobody noticed until the v0.8.2 rebuild surfaced it.

Fix:
- Replace the bare `except` with a logged warning that includes
  the exception type + message
- After N consecutive failures (suggest 100), `sys.exit(1)` so the
  next operator can't silently ship a corrupt sentiment table
- Add a smoke test at the top of `sentiment.py` that verifies the
  NLTK corpora load before processing any rows

This affects every downstream derived field computed from the
NRC emotion columns — historically that included `dominant_emotion`
on the `sighting` table, which would have been NULL on every row
until the fix.

#### SCI-2. `copy_to_explorer()` hardcoded to nonexistent path

`rebuild_db.py` copies the final SQLite to `ufo-dedup/ufo-explorer/`
which doesn't exist. Actual destination is `data/output/ufo_unified.db`.
Fix: update the hardcoded path, or replace with a configurable
target (env var or `paths.py` module constant).

#### SCI-3. Importer paths hardcoded to `../data/raw/`

Already patched in the current session but should become a config
constant so the layout isn't baked into every importer. Suggest a
new `ufo_dedup/paths.py` module with:

```python
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("UFO_DATA_ROOT", REPO_ROOT.parent / "data"))
RAW_ROOT  = DATA_ROOT / "raw"
OUTPUT_ROOT = DATA_ROOT / "output"
```

And every importer does `from ufo_dedup.paths import RAW_ROOT`.

#### SCI-4. `duration_seconds` never populated by importers

The free-text `duration` column gets parsed but the integer
`duration_seconds` column is always NULL in the SQLite output.
As a result `duration_bucket` is 0% coverage, and the v0.8.2
`duration_log2` field in the binary payload is always 0.

Fix: regex parser in `analyze.py` (not the importers — this is
derivation, not ingestion) that converts `"5 min"`, `"30 sec"`,
`"few hours"`, `"around 2 minutes"`, etc. into integer seconds.
Worth writing a test file with ~50 canonical strings from the
dataset so the regex doesn't silently regress.

#### SCI-5. Topic modelling is stubbed

`run_topic_modeling()` in `analyze.py` is a no-op. `topic_id`
column is reserved on the public schema (v0.8.2) but always
NULL. v0.9 scope.

#### SCI-6. `date_correction` FK drift

The `date_correction` table stores `(sighting_id, original_date,
corrected_date)` rows that are validated metadata, but every
rebuild re-keys the sighting IDs (AUTOINCREMENT), so the FK
points at different rows on every run. Three possible fixes:

- **(a)** Hash-based stable IDs: use a SHA256 of
  `(source_db_id, source_record_id)` as the sighting primary key
  instead of AUTOINCREMENT. Biggest change but permanently fixes
  the drift.
- **(b)** Re-key `date_correction` by `(source_db_id,
  source_record_id)` instead of `sighting.id`. Smaller change but
  adds a lookup to every date correction join.
- **(c)** Re-author the corrections after every rebuild. Doesn't
  actually fix the problem, just kicks the can.

Recommend (a) for long-term data integrity, but it's a v0.9+
change (every FK in the schema has to be audited).

### Repo: `ufosint-explorer` (migration script gaps)

#### APP-1. `migrate_sqlite_to_pg.py` doesn't migrate `sighting_analysis`

The new `sighting_analysis` table (`behavior_tags`, `color_list`,
`emotion_scores`, `hoax_flags`, `raw_shape_matched_via`) is
currently SQLite-private. If we want the JSON drill-downs in the
public detail modal (v0.8.3 scope above), we need to:

1. Add `sighting_analysis` to `scripts/pg_schema.sql` and a new
   `scripts/add_v083_sighting_analysis.sql` migration
2. Add it to the `TABLES` list in `migrate_sqlite_to_pg.py` (in
   dependency order after `sighting`)
3. Wire it into `/api/sighting/:id` so the detail modal can
   render the behavior_tags and color_list JSON

Ships naturally as part of the v0.8.3 detail-modal rewire.

#### APP-2. `migrate_sqlite_to_pg.py` missing 3 sighting columns

`sentiment_score`, `duration_bucket`, `movement_type` exist on the
ufo-dedup SQLite `sighting` table but aren't in the migration
script's column list (and aren't in the Azure Postgres schema yet
either). All three would need:

1. `ALTER TABLE sighting ADD COLUMN` in an `add_v083_*.sql`
   migration
2. Entry in the migrator's column list
3. Optionally, new filter predicates in `deck.js` and UI toggles

`duration_bucket` is blocked on SCI-4 (duration_seconds is empty,
so the bucket is always NULL). `movement_type` is usable now.
`sentiment_score` is usable now if we want another filter.

#### APP-3. `date_correction` migration "MISMATCH" is a false positive

The migrator's `verify_counts()` raises a MISMATCH on
`date_correction` because SQLite has 0 rows but Postgres has 714
(the PG copy preserves hand-curated corrections that don't exist
in the SQLite source). Not a bug — intentional preservation — but
the verify_counts step should special-case the table or add a
`--ignore-table date_correction` flag so the script doesn't
`exit(1)` on a clean migration.

---

## v0.9+ — not scope for v0.8.3

Stashed here so they're not lost.

- **Topic modelling** (SCI-5). Fills in `topic_id`. BERTopic or
  LDA over `standardized_shape + primary_color + dominant_emotion
  + num_witnesses + duration_bucket` features, capped at ~25 topics.
- **Hash-based stable sighting IDs** (SCI-6 option a). Affects
  every foreign-key table; major surgery.
- **Duration bucket UI** — blocked on SCI-4.
- **sighting_analysis JSON side-fields surfaced in detail modal**
  (APP-1).
