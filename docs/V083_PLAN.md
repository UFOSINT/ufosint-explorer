# v0.8.3 — Raw text retirement (search + detail rewire)

## TL;DR

Remove every code path that reads `description`, `summary`, `notes`,
or `raw_json` so the public Postgres can drop those columns without
breaking the app. Once v0.8.3 is live and verified, run
`scripts/strip_raw_for_public.py` on Azure Postgres. The full raw
text will remain only in the private SQLite on the operator's
machine.

## Scope

Four code surfaces depend on the raw narrative columns today. Every
one of them must change before `strip_raw_for_public.py` can run
safely:

1. **`/api/search`** (app.py:1793–1877) — `q` parameter runs `ILIKE`
   against `description` and `summary`; response includes a 300-char
   description snippet.

2. **`/api/sighting/:id`** (app.py:1993–2127) — `SELECT s.*` implicitly
   pulls every sighting column including raw text; response contains
   `description`, `summary`, `notes`, `raw_json` (JSON-parsed).

3. **`/api/export.csv` + `/api/export.json`** (app.py:1898–1990) —
   `EXPORT_COLUMNS` lists `summary` and `description` explicitly;
   dropping them would produce empty cells.

4. **`static/app.js`** — `openDetail()` (line 3163) renders
   `r.description` / `r.summary` / `r.raw_json`; `executeSearch()`
   (line 2563) renders `r.description` snippets with `<mark>q</mark>`
   highlighting.

## Columns to drop (per the user's sign-off)

```
description, summary, notes, raw_json
```

**Keep**: `date_event_raw`, `time_raw`, `witness_names`,
`witness_age`, `witness_sex`, `explanation`, `characteristics`,
`weather`, `terrain`. The operator chose not to drop these —
they're structured metadata, not narrative text, and the detail
modal keeps showing them.

Trim `scripts/strip_raw_for_public.py`'s `RAW_COLUMNS` list to
match. (The existing file drops 6 columns; trim to the 4 the
operator confirmed.)

## Endpoint rewires

### 1. `/api/search` — faceted, not text-based

**New `q` semantics** (per operator sign-off): match against a
concatenated facet string over the derived + structured fields.
A query like `triangle texas` returns triangle sightings in Texas.
A query like `fear london` returns sightings where dominant_emotion
is "fear" in London. Location-only, source-only, and shape-only
matches all work from the same input.

**SQL** (new WHERE clause for `q`):

```sql
WHERE (
    COALESCE(l.city,     '') ILIKE %s OR
    COALESCE(l.state,    '') ILIKE %s OR
    COALESCE(l.country,  '') ILIKE %s OR
    COALESCE(s.standardized_shape, s.shape, '') ILIKE %s OR
    COALESCE(s.primary_color,      '') ILIKE %s OR
    COALESCE(s.dominant_emotion,   '') ILIKE %s OR
    COALESCE(sd.name,              '') ILIKE %s
)
```

Seven copies of the same `%q%` pattern. Slower than the v0.7 trigram
path but still sub-second on 614k rows because every column is
indexed (source name is an FK lookup; shape/color/emotion have
btree indexes from `add_v082_derived_columns.sql`; city/state/country
have btree indexes from `pg_schema.sql`).

The existing filter pipeline (`add_common_filters`) still composes
on top: a user can `q=triangle` + `source=MUFON` + `date_from=1990`
and it works exactly like v0.8.2 with a different `q` semantics.

**New response shape** — drop `description`; add derived fields:

```json
{
  "results": [
    {
      "id": 136613,
      "date": "1978-11-09",
      "shape": "Triangle",           // standardized_shape, falls back to raw shape
      "source": "NUFORC",
      "city": "Los Alamos",
      "state": "NM",
      "country": "USA",
      "witnesses": 3,
      "duration": "5 minutes",
      "collection": "PUBLIUS",
      "quality_score": 78,
      "hoax_likelihood": 0.12,
      "dominant_emotion": "fear",
      "primary_color": "white",
      "has_description": true,
      "has_media": false
    }
  ],
  "total": 12408,
  "page": 0,
  "per_page": 50,
  "pages": 249
}
```

The card rendering in `executeSearch()` switches from a description
snippet with highlighting to a compound metadata line:

```
1978-11-09   NUFORC   ★★★★ quality 78
Triangle · white · fear · 3 witnesses · 5 minutes
Los Alamos, NM, USA                    [ ?? hoax 0.12 ]
```

No `<mark>` highlighting (no text to highlight); no description
snippet. The derived-metadata line is self-explanatory.

### 2. `/api/sighting/:id` — derived-only detail

Replace `SELECT s.*` with an explicit column list that excludes the
raw narrative fields. Drop the `raw_json` parse block. Add all 12
derived fields.

**New SELECT**:

```sql
SELECT
    s.id, s.source_db_id, s.source_record_id,
    s.date_event, s.date_end, s.date_reported, s.date_posted,
    s.shape, s.color, s.size_estimated,
    s.duration, s.duration_seconds, s.num_objects, s.num_witnesses,
    s.sound, s.direction, s.elevation_angle, s.viewed_from,
    s.witness_age, s.witness_sex, s.witness_names,
    s.hynek, s.vallee, s.event_type, s.svp_rating,
    s.explanation, s.characteristics,
    s.weather, s.terrain,
    s.source_ref, s.page_volume, s.created_at,
    -- v0.8.2 derived fields
    s.standardized_shape, s.primary_color, s.dominant_emotion,
    s.quality_score, s.richness_score, s.hoax_likelihood,
    s.has_description, s.has_media,
    s.sighting_datetime,
    sd.name AS source_name,
    l.raw_text AS loc_raw, l.city, l.county, l.state, l.country,
    l.region, l.latitude, l.longitude
FROM sighting s
JOIN source_database sd ON s.source_db_id = sd.id
LEFT JOIN location l ON s.location_id = l.id
WHERE s.id = %s
```

Note: this query keeps `date_event_raw` and `time_raw` out of the
SELECT so the response never exposes them even while they exist
in the schema. That's a belt-and-suspenders guard: the operator
chose to keep them in the DB but the API doesn't need to surface
them.

**Drop from response**: `raw_json`, `description`, `summary`, `notes`.
Drop the `raw_json` parse + JSON.stringify block entirely.

**Keep** (structured enough to stay): everything else, including
`explanation`, `characteristics`, `weather`, `terrain`,
`witness_names` — per the operator's sign-off.

### 3. Frontend: `openDetail()` modal rewire

Remove three existing sections:

- **"Description"** section (reads `r.description` / `r.summary`)
- **"Explanation"** section that references `r.explanation`
  **— actually keep this**, `explanation` is not being dropped.
  It's already structured-enough and useful.
- **"Raw JSON" toggle** (reads `r.raw_json`)

Add one new section: **"Data Quality"** with three horizontal bars:

```html
<div class="detail-section">
  <h3>Data Quality</h3>
  <div class="detail-row">
    <span class="detail-label">Quality:</span>
    <div class="quality-bar" style="--pct: 78%">
      <div class="quality-bar-fill" style="width: 78%"></div>
    </div>
    <span class="quality-bar-value">78 / 100</span>
  </div>
  <!-- same for richness_score, hoax_likelihood (inverted) -->
</div>
```

CSS gives each bar a colored fill (cyan for quality/richness,
burgundy for hoax). Hoax is inverted: higher number = more red.

Add another new section: **"Derived Metadata"**:

```
Standardized shape:  Triangle
Primary color:       Black
Dominant emotion:    Fear
Has description:     [ YES ]
Has media:           [ NO  ]
```

When `standardized_shape` is populated, it goes next to the raw
`shape` field in the Observation section. When it's null, just
show raw `shape`. Same for `primary_color` / `dominant_emotion`.

### 4. Frontend: `executeSearch()` card rewire

The result card template currently is:

```html
<div class="result-card">
  <div class="result-header">
    <span class="result-date">1978-11-09</span>
    <span class="source-badge">NUFORC</span>
    <span class="shape-tag">Triangle</span>
  </div>
  <div class="result-loc">Los Alamos, NM, USA</div>
  <div class="result-desc">Witness saw a triangular craft...</div>
  <div class="result-meta">
    <span class="meta-pill">3 witnesses</span>
    <span class="meta-pill">5 minutes</span>
  </div>
</div>
```

New template drops `result-desc` and adds a derived-metadata line:

```html
<div class="result-card">
  <div class="result-header">
    <span class="result-date">1978-11-09</span>
    <span class="source-badge">NUFORC</span>
    <span class="shape-tag">Triangle</span>
  </div>
  <div class="result-loc">Los Alamos, NM, USA</div>
  <div class="result-derived">
    <!-- populated from r.primary_color, r.dominant_emotion, etc -->
    Black · Fear · <span class="quality-inline">quality 78</span>
  </div>
  <div class="result-meta">
    <span class="meta-pill">3 witnesses</span>
    <span class="meta-pill">5 minutes</span>
    <span class="meta-pill has-desc">[ HAS DESCRIPTION ]</span>
  </div>
</div>
```

Drop the `<mark>q</mark>` highlighting entirely (no text to highlight).

### 5. `/api/export.csv` + `/api/export.json`

`EXPORT_COLUMNS` change: drop `summary`, `description`; add derived
fields.

**Before (v0.8.2)**:
```python
EXPORT_COLUMNS = [
    "id", "date_event", "shape", "hynek", "vallee", "num_witnesses",
    "duration", "summary", "description",
    "source", "collection",
    "city", "state", "country",
    "latitude", "longitude",
]
```

**After (v0.8.3)**:
```python
EXPORT_COLUMNS = [
    "id", "date_event", "sighting_datetime",
    "shape", "standardized_shape",
    "primary_color", "dominant_emotion",
    "hynek", "vallee", "event_type",
    "num_witnesses", "duration", "duration_seconds",
    "quality_score", "richness_score", "hoax_likelihood",
    "has_description", "has_media",
    "source", "collection",
    "city", "state", "country",
    "latitude", "longitude",
]
```

The `_build_export_query` SQL also rewrites to remove `s.summary` /
`s.description` from the SELECT and use the same faceted `q` clause
as `/api/search`.

## Regression test coverage

New test file `tests/test_v083_no_raw_text.py` — 20+ assertions that
lock the v0.8.3 contract:

1. `api.py` contains **zero** references to `s.description` /
   `s.summary` / `s.notes` / `s.raw_json` outside of dedicated
   comments marking them as intentionally dead.
2. `/api/sighting/:id` response (via fake cursor) never has
   `description`, `summary`, `notes`, `raw_json` keys.
3. `/api/search` response never has a `description` key.
4. `/api/search?q=...` SQL SELECT doesn't reference
   `s.description` or `s.summary`.
5. `EXPORT_COLUMNS` doesn't contain `summary` or `description`.
6. `static/app.js` `openDetail()` doesn't render `r.description` /
   `r.summary` / `r.raw_json`.
7. `static/app.js` `executeSearch()` result card template doesn't
   use `r.description`.
8. `scripts/strip_raw_for_public.py` `RAW_COLUMNS` list contains
   exactly `description, summary, notes, raw_json` (not
   `date_event_raw` / `time_raw`).

## Ship sequence

1. Write plan doc ← you are here
2. Trim `scripts/strip_raw_for_public.py` `RAW_COLUMNS` to the
   4-column list. Stage the file (it's currently untracked in the
   working tree).
3. Rewrite `/api/sighting/:id` with explicit SELECT + new derived
   fields in response.
4. Rewrite `/api/search` with faceted `q` + new derived fields in
   response.
5. Update `EXPORT_COLUMNS` + `_build_export_query`.
6. Rewire `openDetail()` in `static/app.js`: drop Description,
   Raw JSON sections; add Data Quality + Derived Metadata sections.
7. Rewire `executeSearch()` result card template.
8. Add `static/style.css` rules for `.quality-bar`, `.result-derived`,
   `.has-desc` pill.
9. Write `tests/test_v083_no_raw_text.py` with the 20+ regression
   assertions.
10. Update `CHANGELOG.md` with the v0.8.3 entry.
11. Commit, tag `v0.8.3`, push, watch deploy.
12. Verify `/api/sighting/136613` response doesn't contain
    `description` / `summary` / `notes` / `raw_json`.
13. Verify `/api/search?q=triangle` works + returns derived fields.
14. **Manual test**: open detail modal in browser, confirm Data
    Quality bars render and Description section is gone.
15. **User sign-off** that v0.8.3 looks right.
16. Run `scripts/strip_raw_for_public.py --dry-run` to preview
    what'll drop.
17. User gives final go-ahead.
18. Run `scripts/strip_raw_for_public.py --yes --vacuum-full`.
19. Verify `/api/sighting/136613` still works post-drop (the app
    should be schema-compatible with either state).
20. Update docs/ARCHITECTURE.md note: "raw text lives only on
    private SQLite; public DB has derived fields only".

## Non-goals

- **No change to `/api/sentiment/overview`**. Already reads the
  derived sentiment_analysis table. Untouched.
- **No change to `/api/tool/*` MCP endpoints**. They call the
  underlying functions; as long as those functions stop returning
  raw text, the MCP tool wrappers work automatically.
- **No change to `/api/stats`, `/api/timeline`, `/api/filters`,
  `/api/duplicates`, `/api/points-bulk`, `/api/hexbin`,
  `/api/heatmap`, `/api/map`**. None read raw text.
- **No new search UI controls**. The search tab's existing filter
  dropdowns (source, shape, country, date range, quality rail)
  stay the same. Only the `q` box semantics change.
- **No rewire of `sighting_analysis` JSON side-fields**. That's a
  v0.9 scope item per docs/V083_BACKLOG.md (APP-1).

## Risks

| Risk | Mitigation |
|---|---|
| Users bookmarked `/api/search?q=witness` expecting text hits | Deliberate breaking change — document in CHANGELOG; `q=witness` now matches source/shape/color/emotion/location instead |
| `/api/sighting/:id` callers expect `description` key | Detail modal is the only known caller; MCP + BYOK chat use the high-level functions; no external callers are documented |
| `strip_raw_for_public.py` irreversibly drops the columns | Coverage probe (≥90% `quality_score`) refuses to run on a DB that hasn't been re-migrated; dry-run flag for a preview; manual host-name confirmation gate; private SQLite on operator machine is the reversible backup |
| Trigram indexes dropped → faceted search slower | Measured below — faceted SQL uses btree indexes on each column, sub-second on 614k rows |
| JSON breakage for external tools scraping `/api/sighting/:id` | None documented; the project is solo-operated and all consumers are in-tree |

## Acceptance criteria

1. **Server-side**: `curl /api/sighting/136613 | jq 'keys'` returns
   a key list that does NOT include `description`, `summary`,
   `notes`, or `raw_json`.
2. **Server-side**: `curl '/api/search?q=triangle' | jq '.results[0]
   | keys'` returns a key list that does NOT include `description`.
3. **Server-side**: `curl '/api/search?q=texas' | jq '.total'`
   returns a non-zero number (the faceted q matches location).
4. **Browser-side**: opening a sighting in the detail modal shows
   Data Quality bars + Derived Metadata section, and does NOT
   render any paragraphs of description text.
5. **Browser-side**: search results show a derived-metadata line
   instead of a description snippet, with no `<mark>` highlighting.
6. **Schema-side**: `strip_raw_for_public.py --dry-run` successfully
   previews the drop of exactly 4 columns +
   `idx_sighting_description_trgm` + `idx_sighting_summary_trgm`.
7. **Post-drop**: `/api/sighting/136613` still returns 200 after
   `strip_raw_for_public.py` has run (the endpoint is
   schema-compatible with either state).
8. **Regression**: `pytest tests/ -q` shows the new
   `test_v083_no_raw_text.py` assertions all green, with no
   regressions in the 243 existing tests.
