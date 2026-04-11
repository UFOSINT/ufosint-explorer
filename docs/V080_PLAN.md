# v0.8.0 — Bulk client-side rendering

## TL;DR

Stop re-querying the database on every pan/zoom. Download the entire
geocoded dataset as a binary blob once per session, render it with
`deck.gl` on the GPU, filter it in the browser, and only talk to the
server again when the user *clicks a point* for details.

Expected impact:

| Metric                          | Before (v0.7.7)      | After (v0.8.0) |
| ------------------------------- | -------------------- | -------------- |
| First map paint                 | 1–3 s                | 1–2 s          |
| Subsequent pan/zoom             | 200–800 ms per move  | **free** (GPU) |
| Hex/heat mode toggle            | 200–600 ms           | **free** (GPU) |
| Filter change                   | 500–1500 ms          | **free** (CPU) |
| Points visible at once          | 25 000 (sampled)     | **105 836** (all) |
| DB queries per pan/zoom         | 1–3                  | 0              |
| Payload per pan/zoom            | 3–5 MB JSON          | 0              |

## Why the current approach is slow

`/api/map` samples up to 25 000 markers per request, serialises them as
JSON (~3–5 MB), and does this on *every* viewport change. Then Leaflet
clusters them on the CPU. The same query gets re-run for `/api/heatmap`
and `/api/hexbin` when the user switches modes. Filter changes re-trigger
all of that again.

Even with the v0.7.4 `pg_prewarm` + v0.7.5 materialized views, the
per-pan query path is the wrong shape:

1. `s ⨝ l ⨝ sd ⨝ sc` joins on every move (even if buffered by a bbox).
2. `ROW_NUMBER() OVER (PARTITION BY ...)` grid sampling on every move.
3. JSON encode 25k rows, gzip, send over the wire.
4. Parse 25k JSON objects in JS.
5. Leaflet builds 25k `L.marker` objects and a cluster tree.

None of that needs to happen more than once. The entire *point* of a
density visualisation is that the underlying positions never change —
what changes is the *viewport* and the *filter*, both of which are
cheap operations on a typed array sitting in browser memory.

## The new architecture

```
                                ┌────────────────────────────────┐
                                │  /api/points-bulk              │
  Browser  ── GET once ────────▶│  Returns ~700 KB gzipped       │
                                │  ETag = rowcount + max(u_at)   │
  Store in Float32Array         │  Cache-Control: 1 day          │
  in window.POINTS              └────────────────────────────────┘

  Pan/zoom/filter ──────────────▶ deck.gl ScatterplotLayer
                                  HexagonLayer (GPU aggregation)
                                  ScreenGridLayer / HeatmapLayer
                                  — no network, no DB —

  Click a point ────────────────▶ /api/sighting/:id (existing)
```

## The binary format

One packed row = **16 bytes**:

| Offset | Size | Type     | Field        | Notes                             |
| ------ | ---- | -------- | ------------ | --------------------------------- |
| 0      | 4    | uint32   | `id`         | sighting.id                       |
| 4      | 4    | float32  | `lat`        | degrees                           |
| 8      | 4    | float32  | `lng`        | degrees                           |
| 12     | 1    | uint8    | `source_idx` | index into `sources` lookup       |
| 13     | 1    | uint8    | `shape_idx`  | index into `shapes` lookup (0 = none) |
| 14     | 2    | uint16   | `year`       | 0 if unknown                      |

At 105 836 geocoded rows: **1.69 MB** raw, **~700 KB** gzipped. One HTTP
fetch, cached in the browser and on the Flask side.

float32 gives ~7 decimal digits of precision, which is ~1 cm at the
equator. For a sighting map at zoom 18 that's more than enough.

### Lookup tables

A small JSON sidecar with human-readable names for the packed indices:

```json
{
  "count": 105836,
  "etag": "v0.8.0-614505-20260410",
  "sources": ["MUFON", "NUFORC", "UFOCAT", "UFO-search", "UPDB", ...],
  "shapes":  [null, "Light", "Circle", "Triangle", "Disk", ...],
  "min_year": 34,
  "max_year": 2026,
  "schema": {
    "bytes_per_row": 16,
    "fields": [
      { "name": "id",         "offset": 0,  "type": "uint32",  "len": 4 },
      { "name": "lat",        "offset": 4,  "type": "float32", "len": 4 },
      { "name": "lng",        "offset": 8,  "type": "float32", "len": 4 },
      { "name": "source_idx", "offset": 12, "type": "uint8",   "len": 1 },
      { "name": "shape_idx",  "offset": 13, "type": "uint8",   "len": 1 },
      { "name": "year",       "offset": 14, "type": "uint16",  "len": 2 }
    ]
  }
}
```

The browser fetches `/api/points-bulk?meta=1` once to get the sidecar,
then fetches the binary buffer as an `ArrayBuffer` via
`fetch(url).then(r => r.arrayBuffer())`.

### Why not GeoJSON / JSON?

- GeoJSON of 105k points: ~25 MB. Dead on arrival.
- Numeric JSON (arrays of numbers): ~10 MB. Too big.
- MessagePack: smaller but still ~4 MB and needs a library.
- Raw Float32Array: 1.7 MB uncompressed, 700 KB gzipped. No dependencies.

### Why not vector tiles?

Vector tiles (`pg_tileserv`, Mapbox, Azure Maps) are the correct answer
for **millions** of points or for 3D terrain. For 100k points they're
infrastructure overhead with no win — the entire dataset already fits
in a single request smaller than most hero images on the internet.

## Caching strategy

### Server-side

```python
@app.route("/api/points-bulk")
def api_points_bulk():
    etag = _points_bulk_etag()  # cheap: COUNT + MAX(updated_at)
    if request.headers.get("If-None-Match") == etag:
        return "", 304

    if request.args.get("meta") == "1":
        return jsonify(_points_bulk_meta(etag))

    buf = _points_bulk_buffer(etag)  # @lru_cache keyed on etag
    resp = Response(buf, mimetype="application/octet-stream")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Content-Encoding"] = "gzip"  # gzip ourselves
    return resp
```

The `@lru_cache(maxsize=4)` keeps the packed buffer hot in gunicorn
worker memory between requests. The buffer itself is ~1.7 MB
uncompressed, gzipped to ~700 KB — so one worker holds maybe 2 MB of
map data total. That's nothing.

The ETag is derived from:
- `SELECT COUNT(*) FROM sighting WHERE latitude IS NOT NULL`
- `SELECT MAX(updated_at) FROM sighting` (or `max(id)` if no `updated_at`)

Both are O(1) with existing indexes. Combined into a string like
`v080-105836-20260410`. When either changes, the ETag changes, and
`@lru_cache` naturally invalidates because the key changes.

### Client-side

The browser gets `Cache-Control: public, max-age=3600` so a hard refresh
within an hour hits the browser cache and never touches the server.

Across sessions, we also mirror the buffer into `IndexedDB` keyed on
ETag, so a returning user gets instant cold start (no network at all if
the ETag matches).

### CDN / Azure

Azure App Service streams the gzipped bytes as-is — no CDN needed for
this size. Later we can move the bulk buffer to Azure Blob Storage + a
CDN edge if the App Service starts sweating from cold hits.

## Client-side rendering with deck.gl

`deck.gl` has a first-class Leaflet integration
(`@deck.gl/leaflet`) that renders WebGL layers inside the existing
`state.map` without replacing Leaflet. All marker clustering, heatmap
rendering, and hex binning move to the GPU.

### Layers

```js
import { DeckGLLayer } from "@deck.gl/leaflet";
import { ScatterplotLayer, HexagonLayer, HeatmapLayer } from "@deck.gl/layers";
```

- **Points mode** → `ScatterplotLayer({
    data: filteredIndices,
    getPosition: (i) => [lng[i], lat[i]],
    getRadius: 4,
    radiusMinPixels: 2,
    getFillColor: (i) => sourceColor[source_idx[i]],
    pickable: true,
    onClick: (info) => openDetail(id[info.index]),
  })`
  100k points render at 60 FPS on a Chromebook.

- **Heatmap mode** → `HeatmapLayer({
    data: filteredIndices,
    getPosition: (i) => [lng[i], lat[i]],
    radiusPixels: 30,
    intensity: 1,
    threshold: 0.05,
  })`
  GPU density estimation, recomputes on every frame, zero latency.

- **Hex mode** → `HexagonLayer({
    data: filteredIndices,
    getPosition: (i) => [lng[i], lat[i]],
    radius: 50000,  // meters, adapts to zoom
    elevationRange: [0, 0],  // flat, no 3D
    extruded: false,
    coverage: 0.95,
    colorRange: [
      [0, 59, 92], [0, 140, 180], [0, 240, 255],
      [255, 179, 0], [255, 78, 0],
    ],
  })`
  **deck.gl computes the honeycomb tessellation on the GPU**, sizes it
  to the current zoom, and re-aggregates on every pan. No server, no
  bucket SQL, no cell-center math, no manual offset-row layout. The
  hex tessellation problem we fought for v0.7.5 → v0.7.7 goes away
  entirely because deck.gl does it natively and correctly in screen
  space (meters, not degrees, so no Mercator stretch at high
  latitudes).

### Typed arrays as the source of truth

The filter pipeline is:

```js
// Raw buffers, immutable, loaded once.
const N = meta.count;                          // 105836
const idArr     = new Uint32Array(buf, 0, N);  // not contiguous — see note
const latArr    = new Float32Array(buf, ...);
const lngArr    = new Float32Array(buf, ...);
const srcArr    = new Uint8Array(buf, ...);
const shapeArr  = new Uint8Array(buf, ...);
const yearArr   = new Uint16Array(buf, ...);

// Filter state → index array.
let visibleIdx = new Uint32Array(N);
function rebuildVisibleIdx() {
    let j = 0;
    for (let i = 0; i < N; i++) {
        if (filterYear && (yearArr[i] < filterYear[0] || yearArr[i] > filterYear[1])) continue;
        if (filterSrc && !(filterSrcMask & (1 << srcArr[i]))) continue;
        if (filterShape && !(filterShapeMask & (1 << shapeArr[i]))) continue;
        visibleIdx[j++] = i;
    }
    visibleIdx = visibleIdx.subarray(0, j);
    deckLayer.setProps({ data: visibleIdx });
}
```

One linear scan over 105k entries is ~1 ms in V8. The entire filter
pipeline runs on the main thread with zero jank.

**Note on struct packing:** because the row is 16 bytes and `uint32`
requires 4-byte alignment, `Uint32Array` views work directly. `float32`
also requires 4-byte alignment, which we preserve. `uint16` requires
2-byte alignment, also preserved. The 1-byte fields don't need
alignment. So we can create typed array views over the same underlying
ArrayBuffer with strides:

```js
// Actually simpler: deserialize once into 6 tight typed arrays.
const raw = new DataView(buf);
const id    = new Uint32Array(N);
const lat   = new Float32Array(N);
const lng   = new Float32Array(N);
const src   = new Uint8Array(N);
const shape = new Uint8Array(N);
const year  = new Uint16Array(N);
for (let i = 0; i < N; i++) {
    const o = i * 16;
    id[i]    = raw.getUint32(o,  true);
    lat[i]   = raw.getFloat32(o + 4,  true);
    lng[i]   = raw.getFloat32(o + 8,  true);
    src[i]   = raw.getUint8(o + 12);
    shape[i] = raw.getUint8(o + 13);
    year[i]  = raw.getUint16(o + 14, true);
}
```

Costs ~3–5 ms to deserialize 105k rows. One-time.

## Filters that stay server-side

Not every filter can move to the client. These still hit the server:

- **Full-text search** (`/api/search?q=...`) — the packed buffer has no
  text. Search results return a list of IDs; the client intersects
  that with its in-memory set to highlight matches.
- **State / county / city filters** — not in the packed row. Could be
  added as a `uint16` state index if the user wants them.
- **Duplicate clusters** — already a separate endpoint.

Everything else (date range, source, shape, country, year) moves to
the browser.

## Removing the per-pan endpoints

After v0.8.0 ships and the deck.gl path is proven, the following
endpoints become dead code and can be retired:

- `/api/map` — replaced by `/api/points-bulk` + deck.gl
- `/api/heatmap` — replaced by deck.gl HeatmapLayer
- `/api/hexbin` — replaced by deck.gl HexagonLayer

**We keep them for one release cycle** as a graceful fallback in case
deck.gl fails to load (ancient browsers, WebGL disabled, etc.). The
client probes WebGL support on boot and picks the backend:

```js
const hasWebGL = (() => {
    try {
        const c = document.createElement("canvas");
        return !!(c.getContext("webgl2") || c.getContext("webgl"));
    } catch { return false; }
})();

if (hasWebGL) {
    loadBulkDataset().then(mountDeckGL);
} else {
    // v0.7 behaviour — slow but universal.
    mountLeafletLegacy();
}
```

v0.8.1 will remove the legacy path once we've confirmed no real users
hit it.

## Implementation plan

### Phase 1 — Server (day 1)

1. Add `_points_bulk_etag()` — cheap COUNT + MAX.
2. Add `_points_bulk_meta(etag)` — lookup tables + schema descriptor.
3. Add `_points_bulk_buffer(etag)` — query all geocoded rows, pack
   into `bytearray`, gzip, return `bytes`. `@lru_cache(maxsize=4)`.
4. Add `/api/points-bulk` route with ETag 304 handling.
5. Tests:
   - Route is registered
   - Meta endpoint returns valid schema
   - Binary endpoint returns `bytes_per_row * count` bytes after gunzip
   - ETag header present and stable across requests
   - `If-None-Match` returns 304
   - `@lru_cache` doesn't leak across ETag changes

### Phase 2 — Client bootstrap (day 1)

6. Add `@deck.gl/core`, `@deck.gl/layers`, `@deck.gl/leaflet` to the
   static vendored bundle. (We ship vendored static JS to keep the
   build pipeline dead simple — no npm in CI.)
7. Add `static/js/bulk.js` — fetches the bulk dataset, deserialises
   into typed arrays, exposes `window.POINTS = { id, lat, lng, ... }`.
8. Add WebGL probe + fallback branch to `initObservatory()`.

### Phase 3 — Rendering (day 2)

9. Add `static/js/deck.js` — wraps `DeckGLLayer`, mounts the three
   layers on `state.map`.
10. Wire `toggleMapMode` to swap the active deck.gl layer instead of
    hitting `/api/map|heatmap|hexbin`.
11. Wire point click → `openDetail(id)` via `pickable` + `onClick`.
12. Rip out the legacy `loadMapMarkers()`, `loadHeatmap()`,
    `loadHexBins()` code paths (behind the WebGL probe).

### Phase 4 — Filter pipeline (day 2)

13. Add `applyClientFilters()` that reads filter state and rebuilds
    `visibleIdx`.
14. Wire every existing filter input to call it instead of
    `applyFilters()` → `loadMapMarkers()`.
15. Date range → year filter on `yearArr`. The existing date picker
    still hits `/api/stats` etc. for the side counts, so no UI change.

### Phase 5 — Ship (day 3)

16. Update `CHANGELOG.md` with the v0.8.0 entry.
17. Update `docs/ARCHITECTURE.md` — new data flow diagram.
18. Smoke tests update: probe `/api/points-bulk` for a sane binary
    payload.
19. Commit, tag v0.8.0, push, watch the deploy.
20. Manually verify in browser: hex mode tessellates, heatmap
    responds to zoom, filter toggles are instant, click → modal
    works, payload size is <1 MB.

## What we're NOT doing in v0.8.0

- **No PostGIS migration.** Existing btree on (latitude, longitude) is
  plenty for the detail-on-click path. We can add PostGIS in a
  future sprint if we want ST_DWithin radius search or polygon
  containment tests.
- **No pg_tileserv.** Not needed at 105k points.
- **No Redis cache.** v0.7.4 mentioned Redis as an optional
  accelerator. With v0.8.0, the DB only sees clicks and landing-page
  stats, so even the existing Flask-Caching + pg_prewarm combo is
  overkill.
- **No App Service upgrade.** We're going *down* in CPU usage, not
  up. B1 stays.
- **No npm build pipeline.** Vendored deck.gl as a single UMD bundle
  in `static/vendor/`. Same approach as the current Leaflet install.
- **No WebWorker.** The deserialisation + filter loops run in under
  5 ms on the main thread. Not worth the complexity.

## Risk register

| Risk                                   | Mitigation                                        |
| -------------------------------------- | ------------------------------------------------- |
| deck.gl bundle is 200+ KB              | Acceptable vs. the 3-5 MB / pan we're removing    |
| Browser runs out of memory on mobile   | 1.7 MB ArrayBuffer + 6 typed arrays ≈ 4 MB total. Fine on any device made after 2010. |
| WebGL disabled                         | Legacy Leaflet path kept for one release          |
| ETag cache poisoning                   | ETag includes the schema version, so a code change invalidates old caches |
| Data catalog grows past 1 M points     | Revisit vector tiles at that point                |
| Full-text search can't be client-side  | Server still serves `/api/search`; client intersects results with its in-memory index |

## Success metrics

After v0.8.0 is live we should see:

1. **Observatory first-paint TTI** at or below current (bulk fetch +
   deserialise happens in parallel with the initial Leaflet mount).
2. **Pan/zoom frame time** well under 16 ms (60 FPS). Measure with
   `performance.now()` in a dev build.
3. **DB query count** from the App Service metric: drops to
   effectively zero per map-mode switch. Only landing stats and
   detail modal queries remain.
4. **Gunicorn worker CPU** during a user-pan session: near-idle.
5. **Total gzipped transfer** for a fresh session: under 1 MB
   (bundle + bulk data).
