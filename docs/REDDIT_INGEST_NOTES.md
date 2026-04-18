# Reddit r/UFOs → PG Ingest — Notes for the `ufo-dedup` Team

**Audience:** the agent / person building the ETL step that writes the
r/UFOs extraction output into the shared Postgres.

**Status:** schema is drafted and waiting for sign-off. Nothing in prod
references these columns yet — safe to land the migration now, safe to
start writing data as soon as it lands.

**Contract:** [PR #10 — v0.13 schema](https://github.com/UFOSINT/ufosint-explorer/pull/10)
defines the exact column names, types, and constraints. If something
below diverges from the PR, the PR wins.

---

## 1. What's new on the sighting table

| Bucket | Columns |
|--------|---------|
| **Reddit-specific** | `reddit_post_id` (UNIQUE), `reddit_url` |
| **LLM-derivative** | `llm_confidence`, `llm_anomaly_assessment`, `llm_prosaic_candidate`, `llm_strangeness_rating`, `llm_model` |
| **Universal (new)** | `has_photo`, `has_video` |
| **Universal (was in Python list, now real PG columns)** | `duration_seconds`, `num_witnesses`, `num_objects` |

Plus `source_collection('Reddit')` and `source_database('r/UFOs')` are
seeded by the migration. Reference them by name, not by hardcoded id.

**Canonical ids after the 2026-04-18 seed** (for debugging / spot checks;
still resolve by name in pipeline code):

```
source_collection  id=4  name='Reddit'
source_database    id=6  name='r/UFOs'
```

---

## 2. Content policy — what goes in, what stays out

**The rule:** we publish **transformative LLM output** about the sighting
plus a permalink back to Reddit. We do **not** republish Reddit-owned
content.

### Safe to store (transformative, derivative, or factual-about-the-sighting)

- LLM summary paragraph (goes in `description`)
- LLM-extracted factual fields (shape, color, duration, num_witnesses…)
- LLM ratings (`llm_confidence`, `llm_anomaly_assessment`,
  `llm_strangeness_rating`, `llm_prosaic_candidate`)
- LLM model provenance string (`llm_model`)
- The permalink URL (`reddit_url`)
- The stable post ID for dedup only (`reddit_post_id`)
- Geocoded lat/lng derived from mentioned place names

### Do NOT store

- `selftext` / `selftext_html` / `title` — verbatim user speech
- `author` — privacy, not needed
- `op_comments` / `question_answer_pairs` — verbatim user speech
- `score` / `upvote_ratio` / `num_comments` / `flair` — engagement
  metadata that isn't transformative
- `data_quality_note` — risks restating the OP's content in the LLM's
  own voice; the ratings carry the same information safely
- LLM pipeline metadata (`_tokens_in`, `_tokens_out`) — useful in your
  side of the pipeline for cost tracking, not useful in PG

---

## 3. Column mapping: `extracted.json` → `sighting` row

Given the example at `ufo-dedup/data/raw/reddit/extracted/1d6vfeh.json`:

| Source field | Target column | Notes |
|--------------|---------------|-------|
| `post_id` | `reddit_post_id` | UNIQUE; use for ON CONFLICT key |
| `reddit_url` | `reddit_url` | — |
| `date_event` | `date_event` | Existing column; same format as other sources |
| `time_of_day` | `time_raw` | Existing column; hh:mm string |
| `city` / `state` / `country` | → `location` row | Create-or-reuse via normal dedup path |
| `latitude` / `longitude` | → `location.latitude` / `.longitude` | Null in extraction; your geocoding step fills these |
| `color` | `primary_color` | Also populate raw `color` if you have it |
| `shape` | `shape` + `standardized_shape` | Run through the same shape classifier as other sources so the filter dropdowns stay unified |
| `duration` | `duration` | Free text |
| `duration_seconds` | `duration_seconds` | Parsed int |
| `num_witnesses` | `num_witnesses` | — |
| `num_objects` | `num_objects` | — |
| `has_photo` | `has_photo` | — |
| `has_video` | `has_video` | — |
| `description` | `description` | LLM summary paragraph, NOT raw selftext |
| `confidence` | `llm_confidence` | Must be `high` / `medium` / `low` |
| `anomaly_assessment` | `llm_anomaly_assessment` | Must be `anomalous` / `prosaic` / `ambiguous` |
| `prosaic_candidate` | `llm_prosaic_candidate` | Free text; null unless assessed prosaic |
| `strangeness_rating` | `llm_strangeness_rating` | smallint 1–5 |
| `_model` | `llm_model` | Free text |
| *(nothing)* | `source_db_id` | FK to `source_database` where `name='r/UFOs'` |
| *(nothing)* | `summary` | Can leave NULL or pull first sentence of description |

### Fields in `extracted.json` we have NO column for (yet)

- `sound` — audible descriptor (e.g. "humming")
- `direction` — cardinal direction of travel
- `elevation` — angular elevation
- `movement` — verb phrase
- `timezone` — most rows are null in your example; could store with `time_raw`

Two options:
1. **Drop them** from the ingest for now. If the UI later wants them,
   we do a schema update.
2. **Stuff them into `notes` or `characteristics`** (existing columns
   already on `sighting`) as a compact JSON blob.

I'd lean toward option 1 (drop for now, explicit schema later) to keep
the ingest clean. Let me know if any of those four are likely to matter
for v1 and I'll add columns.

---

## 4. CHECK constraint vocabularies — must match exactly

The v0.13 migration puts CHECK constraints on these. If your pipeline
emits a value not in the list, INSERT fails loudly.

| Column | Allowed values |
|--------|----------------|
| `llm_confidence` | `high`, `medium`, `low`, or NULL |
| `llm_anomaly_assessment` | `anomalous`, `prosaic`, `ambiguous`, or NULL |
| `llm_strangeness_rating` | integer 1–5 inclusive, or NULL |

If your prompt emits anything else (`very high`, `uncertain`, `0` for
"not rated", etc.), either:
- Normalize before INSERT, or
- Let me know and we relax the constraint before the migration merges.

---

## 5. Dedup — within Reddit and across sources

### Within Reddit (easy)

- `reddit_post_id` is UNIQUE. Use `INSERT … ON CONFLICT (reddit_post_id)
  DO UPDATE SET …` for idempotent incremental ingest.
- Re-running the ETL over the same 4k batch should be a no-op (or
  update-only for rows where the LLM extraction has been re-run).

### Reddit vs legacy sources (harder, opportunity)

A significant fraction of r/UFOs posts will be people re-reporting a
case that's already in NUFORC/MUFON, or describing an event that shows
up across multiple sources.

**Recommended posture:** keep Reddit rows SEPARATE from legacy rows;
link them via the existing `duplicate_candidate` table.

```
sighting (NUFORC #189234)
    ↔ duplicate_candidate (match_method='text+geo+time', similarity=0.87)
sighting (r/UFOs #1d6vfeh)
```

The web UI can then render a "also reported on r/UFOs" chip on the
NUFORC popup (or vice versa) without either record losing its distinct
analysis.

Auto-merging would destroy the per-source context (Reddit's strangeness
rating, NUFORC's canonical case number, different witness counts). The
duplicate_candidate link preserves both.

**Match heuristic suggestion:** (date_event within ±1 day) AND
(haversine distance < 50 km) AND (cosine similarity on description
> 0.7). Tune as needed.

---

## 6. Event clustering — an opportunity to flag

The Lake Powell example shows **the same event** observed from **three
states** (Utah, Arizona, California). The three witnesses coordinate in
the Q&A pairs (`"I saw the same thing from Yucca Valley, CA"`).

If your pipeline can detect these intra-source multi-witness events —
same date, same time window, visible arc of witness locations, similar
description — flag them with a shared `event_id` (new column? or a new
`event_cluster` table?). The website can then render them as a single
"3-state event" with connecting lines on the map.

This is **the killer feature** Reddit enables that legacy DBs don't
have. It's not blocking v0.13 but worth thinking about while the
pipeline is still mutable.

---

## 7. Incremental ingest pattern

For the first 4k: one-shot bulk COPY is fine.

For ongoing updates (if you add a weekly pull of new posts):

```sql
INSERT INTO sighting (reddit_post_id, …)
VALUES (…)
ON CONFLICT (reddit_post_id) DO UPDATE SET
    description              = EXCLUDED.description,
    llm_strangeness_rating   = EXCLUDED.llm_strangeness_rating,
    llm_anomaly_assessment   = EXCLUDED.llm_anomaly_assessment,
    -- ...etc, only fields that can legitimately change
    ;
```

After a batch:

1. `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_timeline_yearly;`
2. `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_stats_summary;`
3. The web app's `FILTER_CACHE` (in-process per worker) will pick up
   new shapes/colors/emotions the next time the worker restarts.
   Acceptable staleness for now; will get a TTL in a later PR.

---

## 8. Post-removal policy (please handle in pipeline)

If a post is removed by the author or a subreddit mod after we've
ingested it, we should honor that.

**Proposal:** weekly job hits each `reddit_url` with a HEAD request.
If 404 or Reddit's "removed" placeholder:

- Set `description = NULL`, `llm_strangeness_rating = NULL`, etc.
  (scrub the transformative fields so nothing remains surfacing)
- Keep the row + `reddit_post_id` for audit
- Add a new column (tbd) `removed_upstream_at TIMESTAMPTZ`
- The web app will skip rendering rows with `description IS NULL`

Not required for v0.13 launch, but should be on the roadmap before we
cross ~10k Reddit rows.

---

## 9. Thumbnails — status: SPEC PENDING

User has thumbnails for many of the sightings but we haven't locked
down the policy or hosting plan. Questions outstanding:

1. Are they AI-generated, user-uploaded, or frame grabs of linked
   external media?
2. Where will they be hosted (Azure Blob, git artifact, external CDN)?
3. What dimensions / format?

Schema placeholder if we go ahead:

```sql
ALTER TABLE sighting
    ADD COLUMN IF NOT EXISTS thumbnail_url    TEXT,
    ADD COLUMN IF NOT EXISTS thumbnail_width  SMALLINT,
    ADD COLUMN IF NOT EXISTS thumbnail_height SMALLINT;
```

Don't populate these yet — wait for the policy decision.

---

## 10. Open questions I need your team to answer

Please tag me / drop a note on PR #10 with answers:

1. **Column vocabulary.** Do `high`/`medium`/`low` and `anomalous`/
   `prosaic`/`ambiguous` match what your LLM extractor actually emits?
   If not, I relax the CHECK constraints before the PR merges.

2. **Sound / direction / elevation / movement.** Drop from ingest for
   v1, or pack into `notes` / `characteristics`?

3. **Event clustering.** Worth building into v1, or defer to a follow-
   on sprint?

4. **Thumbnails.** Answer the three questions above when you can.

5. **Reddit content refresh cadence.** Is this a one-shot 4k import,
   or will you pull new posts periodically?

---

## 11. Files to look at on my side

- [`scripts/add_v013_reddit_columns.sql`](../scripts/add_v013_reddit_columns.sql)
  — the migration. Read the header comment; it's the canonical doc.
- [`scripts/migrate_sqlite_to_pg.py`](../scripts/migrate_sqlite_to_pg.py)
  — column list for COPY; extend if you add more columns.
- [`CLAUDE.md`](../CLAUDE.md) — two-agent workspace conventions.
- [`docs/FAILURE_MODES.md`](FAILURE_MODES.md) — known issues; nothing
  blocks Reddit ingest but the `FILTER_CACHE` staleness is relevant.

---

## 12. Deploy sequencing

Land in this order or the web app will 500 at the wrong moment:

1. **Merge PR #10** → prod deploy workflow applies v0.13 migration →
   new columns exist, all NULL for legacy rows
2. **Your pipeline writes** the first batch of r/UFOs rows
3. **Materialized views refresh** (manual for now; auto later)
4. **Web app frontend PR** (separate, not yet drafted) adds the
   Reddit badge, "View on r/UFOs" button, strangeness filter, etc.
5. **Users see it** on ufosint.com

Steps 1–3 have no user-visible impact. Step 4 is when things appear
on the site.

---

*Last updated by the website-dev agent. If you change anything about
the schema contract, please update this doc and the PR description in
the same commit so the two stay in sync.*
