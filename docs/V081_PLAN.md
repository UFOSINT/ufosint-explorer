# v0.8.1 — Client-side temporal animation

## TL;DR

Drive the bottom Observatory time brush — the histogram, the drag
window, and the PLAY button — entirely from the typed arrays that
v0.8.0 already loads into the browser. Playback becomes a pure
`requestAnimationFrame` loop that calls a GPU filter per frame. No
network, no debounce, no form-input round-trip, no `applyFilters()`
button spinner.

Expected impact:

| Metric                          | v0.8.0                | v0.8.1         |
| ------------------------------- | --------------------- | -------------- |
| PLAY button frame rate          | ~3 fps (300 ms debounce) | **~60 fps**  |
| Network calls during playback   | 0 (since v0.8.0)      | 0              |
| CPU per playback frame          | ~5 ms filter + form round-trip | ~2 ms filter |
| Filter → map latency while playing | 300 ms             | **~16 ms**     |
| Histogram fetch on Observatory mount | `/api/timeline?full_range=1` (~150 ms) | 0 ms (client-computed) |
| Supports cumulative replay mode | no                    | **yes**        |

## Why the current PLAY button feels sluggish

In v0.7.6 I fixed the PLAY button's "does nothing on first click" bug
by auto-narrowing the window to 5 years. That made it *move*, but
didn't make it smooth — every frame of the slide fires the brush's
`onChange`, which is wrapped in `debounce(onBrushWindowChange, 300)`.
So the fastest the map can actually update during playback is
**3 fps**, not whatever `requestAnimationFrame` is giving us.

Even if I drop the debounce, the current path is still heavy:

1. `onBrushWindowChange` writes ISO date strings into two
   `<input>` elements.
2. `applyFilters()` fires, which shows a button spinner, calls
   `applyClientFilters()`, writes the URL hash, and re-enables
   the button.
3. `applyClientFilters()` parses the date inputs back to year
   integers, builds a filter object, and calls the hot loop.

All of that per frame is ~10–20 ms of main-thread work that the
filter walk itself doesn't need.

And the histogram we draw on the brush is fetched from
`/api/timeline?full_range=1` — a monthly breakdown of every
sighting — even though we already have `POINTS.year` as a
`Uint16Array` sitting in memory. One extra network round-trip on
Observatory mount that v0.8.0 made redundant.

## The fix

### 1. `deck.js` grows a time-aware filter pipeline

```js
// New module state in deck.js
const _timeState = {
    enabled: false,        // off = year filter comes only from the UI date inputs
    yearFrom: 0,
    yearTo: 65535,
    cumulative: false,     // false = sliding window, true = [minYear, yearTo]
};

let _activeFilter = {};    // stashed by the most recent applyClientFilters() call
let _yearStats = { min: null, max: null, histogram: null };
let _visibleScratch = null; // reusable Uint32Array backing buffer
```

New public API:

```js
UFODeck.setTimeWindow(yearFrom, yearTo, { cumulative })
UFODeck.clearTimeWindow()
UFODeck.getYearHistogram()  // [{ year, count }], cached, O(1) after first call
UFODeck.getYearRange()      // { min, max }, from non-zero years only
```

`setTimeWindow()` is designed to be called per-frame during playback.
It overwrites `_timeState`, walks the typed arrays against
`_activeFilter ∩ _timeState`, writes the filtered indices into
`_visibleScratch.subarray(0, j)`, and calls `refreshActiveLayer()`.
All hot work is tight `for` loops over contiguous memory. No
allocation inside the loop.

`applyClientFilters()` now stashes the UI filter state into
`_activeFilter` instead of applying it eagerly, so subsequent calls
to `setTimeWindow()` keep the UI filters in effect.

### 2. TimeBrush gets a `deckFastPath` shortcut

```js
class TimeBrush {
    constructor(canvas, onChange) {
        // ... existing state
        this.deckFastPath = null;     // (yearFrom, yearTo, cumulative) => void
        this.playMode = "sliding";    // "sliding" | "cumulative"
        this.playSpeed = 1.0;
    }

    // New: called from app.js after bulk data loads
    useDeckFastPath(fn) {
        this.deckFastPath = fn;
    }
}
```

Inside the playback `step()` loop:

```js
const step = () => {
    if (!this.playing) return;

    // ... existing window[0]/[1] math for sliding mode
    // OR cumulative mode: leftEdge stays fixed, rightEdge advances

    // Direct GPU call — no debounce, no form inputs, no applyFilters.
    if (this.deckFastPath) {
        const y0 = new Date(this.window[0]).getUTCFullYear();
        const y1 = new Date(this.window[1]).getUTCFullYear();
        this.deckFastPath(y0, y1, this.playMode === "cumulative");
    } else {
        // Legacy fallback for non-GPU browsers.
        this.onChange(this._isoDate(a), this._isoDate(b));
    }

    this._syncWindow();
    this.playRaf = requestAnimationFrame(step);
};
```

On `STOP`, the brush commits the final window through the
**debounced** `onChange` path so the URL hash and form inputs
catch up to where the user paused.

### 3. Client-computed histogram

`TimeBrush.ensureData()` currently:

```js
const [histResp, annResp] = await Promise.all([
    fetch("/api/timeline?bins=monthly&full_range=1").catch(() => null),
    fetch("/static/data/key_sightings.json").catch(() => null),
]);
```

The monthly timeline fetch gets replaced with an in-place check:

```js
if (window.UFODeck && window.UFODeck.isReady()) {
    this.bins = window.UFODeck.getYearHistogram();
} else {
    // Fall back to /api/timeline (legacy path)
    const histResp = await fetch("/api/timeline?bins=monthly&full_range=1");
    // ...
}
```

`UFODeck.getYearHistogram()` walks `POINTS.year` once, bins per
year, caches the result in `_yearStats.histogram`. Cost: ~3 ms for
396k rows. First call only; every subsequent call is O(1).

### 4. Cumulative mode toggle

A new button in the brush header cycles between `SLIDING` and
`CUMULATIVE`:

```html
<button id="brush-play" class="brush-play-btn">▶ PLAY</button>
<button id="brush-mode" class="brush-mode-btn" data-mode="sliding">SLIDING</button>
<div class="brush-readout">...</div>
<button id="brush-reset" class="brush-reset-btn">RESET</button>
```

- **Sliding** (default, current behavior): the window slides across
  the timeline with a fixed width. Shows "the 5-year window at time
  T".
- **Cumulative**: the window's left edge stays fixed at the
  dataset minimum; the right edge advances. Shows "everything up
  to time T". Good for watching the dataset fill up historically.

The toggle only affects playback. Manual brush drag is always
sliding.

### 5. Playback speed (stretch)

Speed is plumbed through as a `TimeBrush.playSpeed` property
(default `1.0`) that scales `stepSize`. **No UI control in v0.8.1**
— the API exists for a future `×1 / ×2 / ×5` toggle, but the
button adds clutter without a clear user story yet.

## Data flow during playback

```
requestAnimationFrame
  └─▶ TimeBrush.step()
        ├─▶ advance this.window[] by stepSize
        ├─▶ this._syncWindow()              (update .brush-window CSS %)
        └─▶ this.deckFastPath(y0, y1, cum)  ── direct, no debounce, no form I/O
              └─▶ UFODeck.setTimeWindow(y0, y1, {cumulative: cum})
                    ├─▶ mutates _timeState
                    ├─▶ _rebuildVisible()    (1 tight loop over POINTS)
                    │     ├─▶ walks ~396k entries
                    │     ├─▶ writes indices into _visibleScratch
                    │     └─▶ POINTS.visibleIdx = scratch.subarray(0, j)
                    └─▶ refreshActiveLayer()
                          └─▶ leafletLayer.setProps({ layers: [fresh Layer] })
                                └─▶ deck.gl GPU rebuild (~5 ms)
```

Per frame total: **~8–15 ms**. Well under the 16.6 ms 60-fps budget.

## Non-GPU fallback

Legacy browsers without WebGL never see `UFODeck`. TimeBrush falls
through to the current v0.7.6 path:

- `ensureData()` fetches `/api/timeline` as before.
- `togglePlay()` uses the debounced `onChange` path that writes
  form inputs and calls `applyFilters()` → `loadMapMarkers()`.
- Playback is still ~3 fps but at least the map updates at all.

Zero regression for anyone on the legacy path.

## Implementation plan

### Phase 1 — deck.js (day 1, ~1 hour)

1. Add `_timeState`, `_activeFilter`, `_yearStats`, `_visibleScratch`
   module state.
2. Refactor `applyClientFilters(filter)` → stash filter and call
   `_rebuildVisible()`.
3. Add `_rebuildVisible()` that merges `_activeFilter` with
   `_timeState` in one tight loop.
4. Add `setTimeWindow(yearFrom, yearTo, opts)` public API.
5. Add `clearTimeWindow()` public API.
6. Add `getYearHistogram()` + `getYearRange()` with lazy cache.
7. Replace the ad-hoc `new Uint32Array(N)` in the hot loop with a
   reused `_visibleScratch` + `subarray(0, j)` view.

### Phase 2 — TimeBrush rewire (day 1, ~1 hour)

8. In `TimeBrush.constructor`, default `deckFastPath = null` and
   `playMode = "sliding"`.
9. Add `useDeckFastPath(fn)` setter.
10. Modify `ensureData()` to prefer `UFODeck.getYearHistogram()`
    when available; otherwise fetch `/api/timeline`.
11. Modify `togglePlay()` to call `this.deckFastPath(...)` inside
    the `step()` closure instead of `this.onChange(...)` when the
    fast path is set. Only the debounced `onChange` fires on STOP.
12. Add the cumulative mode branch to the playback math (left edge
    stays fixed).

### Phase 3 — App integration (day 1, ~30 min)

13. In `bootDeckGL()` (after `loadBulkPoints()` succeeds), pre-warm
    `UFODeck.getYearHistogram()` and call
    `state.timeBrush.useDeckFastPath((y0, y1, cum) =>
     UFODeck.setTimeWindow(y0, y1, {cumulative: cum}))` *if the
    brush exists yet*. If not, the Observatory mount path does it
    on first visit.
14. `loadObservatory()` similarly wires the brush to the fast
    path after `ensureData()` completes.
15. Wire the new `#brush-mode` button to toggle
    `state.timeBrush.playMode`.

### Phase 4 — HTML + CSS (day 1, ~20 min)

16. Add `<button id="brush-mode">` next to `#brush-play` in
    `index.html`.
17. Add `.brush-mode-btn` CSS — same visual language as
    `.brush-play-btn`, toggles between SLIDING / CUMULATIVE labels.

### Phase 5 — Tests (day 1, ~45 min)

18. Unit tests for `setTimeWindow()`: window shrinks/expands
    correctly, cumulative pins leftEdge to min year.
19. Unit tests for `getYearHistogram()`: correct bin counts, cache
    returns same reference on repeat call.
20. Frontend contract tests: `deck.js` exposes the new API,
    `app.js` references `useDeckFastPath`, `index.html` has the
    mode button.
21. Regression tests for v0.8.0 filter pipeline (must still work
    when time window is active alongside UI filters).

### Phase 6 — Ship (day 1, ~15 min)

22. Update `CHANGELOG.md` with v0.8.1 entry.
23. Commit, tag `v0.8.1`, push.
24. Watch deploy, live verify: histogram renders instantly on
    Observatory mount, PLAY button advances at 60 fps, cumulative
    toggle works, non-GPU path still works.

## What we're NOT doing in v0.8.1

- **Full date precision.** The bulk schema stores year as `uint16`.
  Day/month granularity would double the row size (or need a
  separate `unix_ts: uint32` column) and is not in the brief.
  Year-level is sufficient for a time-lapse effect.
- **Playback speed UI.** The API supports it, but no button.
- **Reverse playback.** Forward only for now.
- **Server-side histogram tests.** The client histogram is pure
  JS, tested against the frontend. The `/api/timeline` endpoint
  stays untouched.
- **Retiring `/api/timeline`.** Still used by the Timeline tab's
  main chart. Only the brush's full-range histogram fetch moves
  to the client.

## Risk register

| Risk                                    | Mitigation                                             |
| --------------------------------------- | ------------------------------------------------------ |
| 60 fps filter loop starves main thread  | Filter walk is ~2 ms / 396k rows. Budget is 16.6 ms. Plenty of headroom. |
| Hex/Heat layer rebuild lags on fast pan | deck.gl rebuild is ~5 ms for ScatterplotLayer; Hex is ~10–20 ms. Playback on Hex will be slower but still visibly smooth. Acceptable. |
| Scratch buffer size drift across filters | `_visibleScratch` sized to `POINTS.count` once on first use; never reallocated since count is fixed. |
| TimeBrush fires before bulk data loads  | `ensureData()` still works against `/api/timeline` when `UFODeck.isReady()` is false. |
| Cumulative toggle confuses users        | Clear SLIDING/CUMULATIVE label; starts in SLIDING so existing muscle memory works. |

## Success metrics

- **Playback frame rate** stays ≥ 50 fps during a full dataset
  sweep in Points mode. Measured via `performance.now()` wrapping
  the `step()` closure.
- **First-paint time** for the brush histogram drops by the
  removed `/api/timeline?full_range=1` latency (~150 ms typical,
  up to 500 ms on a cold gunicorn worker).
- **DB query count** during a playback session stays at zero.
- **Legacy browser** still animates on the debounced path without
  regression.
