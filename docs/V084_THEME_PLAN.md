# v0.8.4 — Signal / Declass theme overhaul

## TL;DR

The SIGNAL / DECLASS theme infrastructure already exists from v0.7
(CSS token swap, localStorage persistence, pre-paint script, radio
group inside the settings menu). v0.8.4 finishes the job by:

1. Promoting the theme toggle from the settings menu to a **visible
   top-nav pill** next to the main tabs.
2. Swapping the **base map tiles** between Carto Dark Matter (signal)
   and Carto Voyager (declass) so the whole map reads correctly in
   both modes.
3. **Theming the deck.gl layers** — ScatterplotLayer dot color,
   HexagonLayer color ramp, HeatmapLayer palette — via a
   `window.UFODeck.setTheme(name)` helper that re-applies the active
   layer's props without a reload.
4. **Auditing the v0.8.x CSS** (Quality rail, Data Quality bars,
   Derived Metadata pills, PLAY speed selector, brush mode button,
   popup-btn, popup-desc-badge) to confirm every new element
   inherits the token swap correctly.
5. Wiring `setTheme()` in app.js to call both the map-tile swap and
   `UFODeck.setTheme()` so the toggle is truly instant.

No DB work, no backend changes. Pure front-end presentation pass.

## What already works (from v0.7)

- `body.theme-signal` / `body.theme-declass` — token swap via CSS
  variables in `static/style.css` lines 123–164. `--bg`, `--accent`,
  `--text`, `--border`, etc. are all defined per-theme.
- `initThemeToggle()` + `setTheme(theme)` in `static/app.js` —
  registers click handlers on every `.theme-opt` button, flips the
  body class, persists to `localStorage["ufosint-theme"]`, and
  redraws the TimeBrush histogram + annotations so they pick up the
  new accent color.
- **Pre-paint script** in `static/index.html` lines 52–69 — reads
  `localStorage` before the stylesheet loads so DECLASS users don't
  see a flash of SIGNAL palette on hard-refresh.
- **TimeBrush `_draw()` reads live CSS tokens** via
  `getComputedStyle(document.body).getPropertyValue("--accent")` so
  the histogram bars auto-adapt.

**What v0.7 got right**: the whole token-swap plumbing. Any CSS rule
that uses `var(--accent)` / `var(--text)` / `var(--bg)` / etc. just
works on both themes for free.

## What doesn't work yet

### 1. Theme toggle is buried in the settings menu

The radio group lives inside `#settings-menu`, which is hidden by
default and needs a click on the gear icon to reveal. New users
never find it. The operator explicitly asked for the toggle to live
"at the top of the site (next to Observatory / Timeline / Search)".

**Fix**: move (or duplicate) the toggle into a new `.theme-pill` slot
in the top nav, between the main tabs and the gear icon. Keep the
settings-menu copy too — it's a fallback for discovery and the
existing `initThemeToggle()` already binds to every `.theme-opt`
so no JS change is needed for the duplication.

### 2. Base map tiles are hardcoded OSM

Two Leaflet tile layers exist in the codebase, both hard-coded to
standard OpenStreetMap:

- `static/app.js:933` — the main Observatory map
- `static/app.js:3561` — the detail modal mini-map

OSM standard tiles are a light beige/grey that looks *acceptable*
on the dark SIGNAL theme but is the wrong aesthetic for either
mode (dark tiles would read better on SIGNAL; pure light tiles on
DECLASS).

**Fix**: switch to Carto's basemap set, which ships dark + light
variants of the same cartographic style:

- **SIGNAL**: `https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png`
  ("Dark Matter" — dark background, white roads)
- **DECLASS**: `https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png`
  ("Voyager" — warm cream paper, soft accents, matches the DECLASS
  cream/burgundy palette)

Both styles:
- Free for public / non-commercial use
- CORS-enabled, work in a `<img>` from any origin
- Retina-aware (the `{r}` suffix resolves to `@2x` on hi-DPI)
- No API key required
- Share the CartoDB attribution, so one attribution string works
  for both

Keep a single `L.TileLayer` instance on `state.tileLayer` and swap
its URL template via `state.tileLayer.setUrl(newUrl)` on theme
change. No need to remove/re-add layers; `setUrl` triggers a clean
reload of the visible tile grid.

### 3. deck.gl layer colors are hardcoded

Three colors live in `static/deck.js`:

- **ScatterplotLayer** line 664: `getFillColor: [0, 240, 255, 180]`
  (cyan) — invisible on a light map.
- **HexagonLayer** lines 713–716: 5-stop `colorRange` (cold plasma
  blue → cyan → amber → hot orange → red) — the warm end works on
  both themes but the cold plasma end is invisible on cream paper.
- **HeatmapLayer** line 739 (no explicit colorRange, uses deck.gl
  default) — default palette is cold blue → hot red which works
  acceptably on both but not perfectly.

**Fix**: define two palette dicts at the top of `deck.js`:

```js
const THEME_PALETTES = {
    signal: {
        scatter:  [0, 240, 255, 180],   // cyan, SIGNAL accent
        hexRange: [
            [0, 59, 92],     // cold plasma
            [0, 140, 180],
            [0, 240, 255],   // hot plasma
            [255, 179, 0],   // amber
            [255, 78, 0],    // hot
        ],
        heatRange: [  // Defaults to deck.gl's
            [255, 255, 178],
            [254, 204, 92],
            [253, 141, 60],
            [240, 59, 32],
            [189, 0, 38],
        ],
    },
    declass: {
        scatter:  [15, 23, 42, 200],    // near-black, DECLASS text color
        hexRange: [
            [233, 219, 180],  // pale cream
            [200, 150, 100],  // tan
            [150, 80, 60],    // rust
            [120, 20, 30],    // burgundy
            [80, 0, 15],      // deep wine
        ],
        heatRange: [
            [240, 230, 200],
            [220, 180, 120],
            [180, 100, 60],
            [130, 30, 30],
            [80, 0, 15],
        ],
    },
};
```

Add a new `UFODeck.setTheme(name)` public helper that:
1. Stashes the theme name in a module-level variable.
2. Looks up the active palette.
3. Calls `refreshActiveLayer()` so the new layer instance picks up
   the new colors. Layer factories read `THEME_PALETTES[_theme]`
   instead of the hardcoded values.

### 4. Live wire-up in `setTheme()` (app.js)

The existing `setTheme()` (app.js line 2445) only redraws the
TimeBrush canvas. v0.8.4 extends it to also:

```js
function setTheme(theme) {
    // ... existing class swap + localStorage + TimeBrush redraw ...

    // v0.8.4: swap the base map tile layer
    if (state.tileLayer) {
        state.tileLayer.setUrl(TILE_URLS[theme]);
    }

    // v0.8.4: swap the deck.gl layer colors
    if (window.UFODeck && typeof window.UFODeck.setTheme === "function") {
        window.UFODeck.setTheme(theme);
    }
}
```

That's it — 8 lines of new code plus the two helper modules.

### 5. CSS audit for v0.8.x elements

Quick grep for every `.class` added since v0.8.0:

| Element                          | Added in  | Uses tokens?               | Audit |
| -------------------------------- | --------- | -------------------------- | ----- |
| `.popup-btn`                     | v0.7.6    | `--accent`, `--bg`         | ✓     |
| `.popup-desc-badge.has-desc`     | v0.7.6    | `--accent`                 | ✓     |
| `.popup-desc-badge.no-desc`      | v0.7.6    | `--text-muted`             | ✓     |
| `.brush-mode-btn`                | v0.8.1    | `--accent`, `--bg`         | ✓     |
| `.brush-speed-select`            | v0.8.2c2  | `--accent`, `--bg`         | ✓     |
| `.rail-quality`                  | v0.8.2    | inherits rail tokens       | ✓     |
| `.rail-toggle-list`              | v0.8.2    | inherits rail tokens       | ✓     |
| `.rail-toggle-disabled`          | v0.8.2    | opacity only               | ✓     |
| `.quality-bar`                   | v0.8.3    | `--accent`, `--bg-panel`   | ✓     |
| `.quality-bar-fill`              | v0.8.3    | `--accent`                 | ✓     |
| `.quality-bar-hoax`              | v0.8.3    | `--danger`                 | ✓     |
| `.result-derived`                | v0.8.3    | `--text-muted`             | ✓     |
| `.result-derived .quality-inline`| v0.8.3    | `--accent`                 | ✓     |
| `.result-derived .hoax-inline`   | v0.8.3    | `--danger`                 | ✓     |
| `.meta-pill.has-desc`            | v0.8.3    | `--accent`                 | ✓     |
| `.meta-pill.has-media`           | v0.8.3    | `--accent`                 | ✓     |

**Result: every v0.8.x element already reads from theme tokens.**
No CSS audit fixes needed. The token swap covers everything.

(This is a happy outcome — v0.7's token-swap system is paying off
across every new component I've added without realising it.)

## Implementation sequence

1. **Write this plan doc** ← you are here
2. **Top-nav theme pill**:
   - Add a new `.theme-pill` radio group between the tabs and the
     gear icon in `static/index.html`.
   - Duplicate the existing `.theme-opt` buttons — `initThemeToggle()`
     already binds to every `.theme-opt` so no JS change.
   - Add `.theme-pill` CSS rules so it fits the nav height + reads
     as a compact SIGNAL/DECLASS radio button pair.
3. **Tile layer swap**:
   - Add a `TILE_URLS = { signal: ..., declass: ... }` constant at
     the top of `static/app.js`.
   - Modify `initMap()` to pick the URL based on `state.theme` and
     stash the layer on `state.tileLayer`.
   - Modify `setTheme()` to call `state.tileLayer.setUrl(...)` on
     change.
   - Also update the detail-modal mini-map's tile URL so that
     matches the theme.
4. **deck.gl palette + setTheme**:
   - Add `THEME_PALETTES` constant + module-level `_theme` var in
     `static/deck.js`.
   - Refactor `makeScatterplotLayer` / `makeHexagonLayer` /
     `makeHeatmapLayer` to read from the active palette.
   - Add `setTheme(name)` public method that updates `_theme` and
     calls `refreshActiveLayer()`.
5. **Wire setTheme**:
   - In `static/app.js setTheme()`, call `state.tileLayer.setUrl()`
     and `window.UFODeck.setTheme()`.
6. **Tests**:
   - `tests/test_v084_theme.py` with ~15 assertions:
     - `.theme-pill` exists in index.html
     - `TILE_URLS` constant exists in app.js
     - `THEME_PALETTES` constant exists in deck.js
     - `UFODeck.setTheme` is exported
     - CSS tokens for both themes still exist
     - CartoDB dark/light tile URLs are valid strings
     - `setTheme()` calls `tileLayer.setUrl` and `UFODeck.setTheme`
7. **CHANGELOG + commit + tag v0.8.4 + ship**.
8. **Browser verification** — user hard-refreshes, toggles between
   SIGNAL and DECLASS, confirms:
   - Map tiles actually swap
   - Cyan dots become dark dots in declass mode
   - Hex bins recolor per palette
   - All UI chrome (rail, brush, modal, result cards) reads correctly
   - No FOUC on fresh load in declass

## Non-goals

- **No new color palette tweaking** beyond the minimum needed for the
  deck.gl layers. The existing token tables are the source of truth.
- **No font change**. DECLASS already switches to Courier Prime in
  `body.theme-declass` (style.css line 164).
- **No server-side theme**. The theme is pure client state; the
  server has no knowledge of which theme a given request prefers.
- **No dark-mode media query autoswitch**. Users pick the theme
  explicitly; we don't try to match `prefers-color-scheme`. The
  toggle persists across sessions, which is more predictable than
  auto-switching.
- **No new theme beyond SIGNAL / DECLASS**. The whole system
  supports a 3rd theme just by adding another `body.theme-foo {}`
  block + a `THEME_PALETTES.foo` dict, but that's out of scope.

## Risks

| Risk | Mitigation |
|---|---|
| Carto tile URL changes / outage | Falls back to OSM standard on 404; Leaflet handles missing tiles gracefully with a blank grid; unlikely — Carto has been stable for years |
| deck.gl layer colors don't actually update when setProps is called | `refreshActiveLayer()` already constructs a fresh layer instance each call, so the new props land; v0.8.1's PLAY loop proves this works at 60 fps |
| Declass hex palette hard to read in some viewports | Picked warm cream → rust → burgundy → wine to mirror the existing DECLASS CSS accents (`#B8001F` burgundy); operator can fine-tune in a follow-up |
| Top-nav pill breaks layout on narrow viewports | Responsive breakpoint at 900px hides the pill text in favor of just a SIGNAL / DECLASS swatch; tabs can wrap or collapse |
| Users can't find the toggle on desktop | The pill is now visible without clicking anything — that's the whole point of the move |
