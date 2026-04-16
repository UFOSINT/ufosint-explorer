# v0.13 — UX polish backlog

**Status:** not started. Candidate items captured from a parallel
UX review (3 reviewers, desktop/mobile/first-visit) against
`https://ufosint.com` on 2026-04-16. Version target tentative —
may ship as v0.12.x patches + one minor cut depending on scope.

This doc is a **backlog**, not a spec. Items are independent. Pick
per-sprint, land on feature branches, verify on staging.

## How to read this

- **Effort:** S (<30 min), M (<2 h), L (multi-hour / multi-session).
- **Tier:** 1 = user-task-blocking or visibly broken · 2 = high-value
  polish · 3 = structural improvements that change information
  architecture.
- **Refs:** file paths are relative to `ufosint-explorer/`. Line
  numbers were current at review time — verify before editing.

## Tier 1 — user-task-blocking or visibly broken

### 1. Header stats badge: collapse to single chip (S)

The badge currently joins 5 chips with middle-dots:
`614,505 sightings · 396,158 mapped · 118,320 high quality ·
249,217 with movement · 0 possible duplicates`. At viewport widths
below ~1400 px it wraps to 2 lines, forcing the header from
53 px to 90 px. At iPad portrait (768 px) and below-900 desktop
widths it wraps to 3 lines (108 px header).

**Fix:** render only `${total} sightings` in the badge. The popover
(`#stats-popover`) already shows every chip in detail — no data is
lost. Gate all other chip pushes behind a `// legacy` comment or
remove them.

- `static/app.js:693` `showStats()` — reduce the `chips` array to
  one element. Popover block (lines 753–786) stays untouched.
- `static/style.css:2950–3078` — `.stats-chip-optional` class
  becomes dead; leave for now or remove in a follow-up.
- Bonus (trivially related): the "0 possible duplicates" push at
  `app.js:749` always fires regardless of count, unlike the other
  optional chips. Becomes moot once the collapse ships.

### 2. Filter drawer collapses map to 0 px on mobile (S)

On phones, opening the Filters drawer flows the filter bar into
the layout, pushing the map container to `height: 0`. The
TimeBrush ends up 40 px from the viewport bottom with no map
visible above it. User has to close filters to see anything.

**Fix:** make the filters drawer an overlay/sheet (`position:
fixed` or `absolute` with a backdrop), not a flow-push. The map
keeps its vertical space.

- `static/style.css` — filter drawer block (search
  `#filters-bar` or `.filters-expanded` around the mobile media
  query). Change layout from flow to overlay under the
  `@media (max-width: 720px)` rule.

### 3. TimeBrush handles ungrabbable on touch (S)

`.brush-handle` is 18×22 px; `.overview-handle` is 6×22 px. Far
below the 44×44 px touch-target minimum. The mobile `body.is-touch`
override bumps only the main handle to 14 px — still too small —
and the overview handle has no touch override at all.

**Fix:** add an invisible 44×44 `::before` hit-overlay on both
handles under `body.is-touch`. Keep the visual size unchanged.

- `static/style.css:3831` — `.brush-handle` base
- `static/style.css:3847` — `body.is-touch .brush-handle`
  (currently 14 px — extend to 44 via pseudo)
- Add a new `body.is-touch .overview-handle::before` rule in the
  same block

### 4. Intro "READY" ghost bleeds through after dissolve (S)

Three DOM nodes (`.hud-status`, `.panel-status`, `.intro-status`)
render "READY" during the intro animation. After the dissolve
completes, `.intro-status` is still visible at lower-left of the
map in every post-landing screenshot.

**Fix:** after the dissolve animation ends, detach or
`display: none` the intro overlay — not just fade its parent.

- `static/app.js` — intro controller (search for
  `intro-content` or `intro-status`).
- `static/style.css` — `.intro-status` rule; may need a
  `pointer-events: none` + `visibility: hidden` after the
  `ended` class applies.

### 5. "Find a place" search pans but doesn't filter (M–L)

Users who try the most obvious task — "scope to Arizona" —
silently fail. The place search zooms the map but doesn't add a
filter; Filters badge still shows only `shape + date`. To
actually constrain results to Arizona, the user must open the
Region tool, pick Polygon, and hand-trace the state border.

**Two candidate fixes, not mutually exclusive:**

- (a) After a place search, show a "Filter to this region" toggle
  that materialises as a bbox/state filter chip.
- (b) Add a persistent **State / Country** filter select to the
  Filters bar. The MCP `count_by` tool already exposes `state`
  and `country`, so the data exists — this is pure UI wiring.

Effort estimate is L if we do both, M if just (a).

- Place-search handler in `static/app.js` (search for
  `nominatim` or `geocode`).
- Filter bar markup at `static/index.html:201`.

## Tier 2 — high-value polish

### 6. Raise stats/theme hide breakpoint 720 → 900 (S)

`style.css:2278` hides `.stats-badge` and `.theme-pill` below
720 px. iPad portrait (768) falls in the 720–900 gap, so:

- Header balloons to 237 px (badge wraps to 5 lines)
- Theme pill clips labels to "D" / "L" instead of "DARK" / "LIGHT"

**Fix:** move the `max-width` from 720 to 900 on both rules.
Becomes moot for the badge once #1 ships, but the theme-pill fix
still matters.

### 7. Tour tooltips cover their own targets (M)

Step 4 (TimeBrush) and step 6 (stats badge) place the tooltip
directly on top of the element being explained. No backdrop
cutout, no arrow — the `.tour-tooltip-arrow` element has a 0×0
rect. Users can't locate the feature even after reading the
tooltip.

Secondary: README claims 5 steps, actual is 6 — inconsistency.

**Fix:**
- Popper-style flip-placement: when target is near viewport
  bottom, render tooltip above instead of below.
- Always `scrollIntoView` the target before showing the tooltip.
- SVG mask cutout around the anchor rect on the `.tour-backdrop`.
- Render a visible arrow pointing at the anchor.
- Fix the step-count mismatch in README.

### 8. Global `min-height: 44px` on touch (S)

36 buttons audited under 44×44 across tabs (32 h), gear/help
(32×32), zoom +/- (30×30), Region popover close (28×28), legend
chips (16 h), Day/Month/Year granularity (32 h), PLAY (58×28).

**Fix:** one rule under `body.is-touch`:

```css
body.is-touch .btn,
body.is-touch .tab,
body.is-touch button { min-height: 44px; }
```

Visually enlarge only on touch devices. Desktop unaffected.

### 9. Inline definitions for "score ≥ 60" / "flag score > 0.5" (S)

Data Quality rail uses two different scales (0–100 for quality,
0–1 for red-flags) with no inline definition. Nothing links to
the Methodology tab from the rail.

**Fix:** small (i) icon on each toggle with a 2-line popover:
"Quality score combines N signals, see Methodology →". Link the
arrow to the relevant Methodology anchor.

- `static/index.html` — Data Quality rail block (search for
  `data-quality` or `high-quality-toggle`).
- `static/style.css` — new `.filter-info-pop` class.

### 10. `observatory-topbar` overflows horizontally on mobile (M)

Phone: 303 cw vs 885 sw. Tablet: 466 cw vs 1020 sw. POINTS /
HEATMAP / HEX + legend + REGION / COLOR / SIZE + LAT/LON all
stuffed into a single absolute-positioned row.

**Fix:** on mobile, wrap the topbar to 2 rows (mode + legend on
row 1, region/style on row 2) or collapse the less-used half
behind a toggle.

- `static/style.css:4063` — `.observatory-topbar` uses
  `position:absolute; left:52px; right:16px`. Add mobile media
  query with `flex-wrap: wrap` and relaxed positioning.

## Tier 3 — structural / information architecture

### 11. Single source of truth for time range (M)

Date Range filter (1990–1999) and TimeBrush label
("1900 — 2027 · IN RANGE") are independent. Two sources of truth
for "what time window is active." User has no idea which wins or
how they compose.

**Fix:** bind the Date Range filter inputs to the TimeBrush
window range. Setting filter = 1990–1999 moves the brush window;
dragging the brush window updates the filter inputs. Backed by
the existing `state.filters.dateFrom` / `dateTo`.

### 12. Add State / Country filter (L)

Data exists, no UI. Unlocks the state-scoping use case without
drawing polygons. See also item #5 — these two compose.

- `static/index.html:201` — add filter-group blocks.
- `static/app.js` — wire into the bulk-buffer filter mask; dropdowns
  populate from `/api/filters`.
- Backend: `/api/filters` may need to return `state` / `country`
  enum lists — check `app.py` `init_filters()`.

### 13. Movement row collapse (M)

The 44 px always-on "MOVEMENT" pill band (10 checkboxes:
Hovering / Linear / Erratic / etc.) carries the same visual
weight as Date Range / Shape / Source but is a niche filter.

**Fix:** collapse into a single `Movement ▾` select / chip-popover
matching the Shape / Source pattern. Removes 44 px of header band
by default; power users click to expand.

- `static/index.html` — movement-row block.
- `static/style.css:441` — `.filter-movement-row`.

### 14. Port Observatory rail into mobile filter drawer (M)

`#observatory-rail` is `display: none` at viewport < 900 px. Live
Analytics counter, Top Shapes, By Source, quality histogram — all
inaccessible on phones and tablets. README claim of "Accordion
rail" is half-true.

**Fix:** inside the expanded mobile filter drawer, render the rail
sections as collapsible accordions beneath the filter inputs. May
need a mobile-only DOM clone or a portal, depending on the existing
markup structure.

### 15. Tab bar scrolls horizontally with no affordance (S)

At 375 px viewport, `.tabs` is 193 cw / 479 sw. "Methodology" sits
at x≈471 — 100 px off-screen — with no scrollbar, no gradient
edge, and no hint there's more content.

**Fix:** add a right-edge gradient fade under
`body.is-touch .tabs` to signal scrollability. Or: swap the tab
nav for a bottom-anchored mobile tab bar with all 4 tabs visible.

- `static/style.css:2301` — `.tabs` overflow rule.

## Execution notes

- Tier 1 #1, #2, #3, #4 + Tier 2 #6, #8, #9 were the
  "recommended bundle" — all S-effort, low-risk, independent.
  Collectively ≈ 90 min of focused work.
- Tier 1 #5 (search-doesn't-filter) pairs naturally with Tier 3
  #12 (State/Country filter) — consider shipping together.
- Tier 2 #7 (tour flip-placement) deserves its own focused commit
  and a manual verification pass of all 6 tour steps across 3
  viewports.

## Provenance

Captured from three parallel `Explore`/`general-purpose` agent
reviews on 2026-04-16, shortly after the v0.12.0 release. Each
agent visited the live site via Claude-in-Chrome MCP and reported
independently. Cross-references to agent IDs are not preserved —
the authoritative record is this doc plus git history.
