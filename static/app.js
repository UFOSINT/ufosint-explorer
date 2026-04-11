/**
 * UFO Explorer — Main application logic
 * Map (Leaflet + markercluster), Timeline (Chart.js), Search
 */

// =========================================================================
// State
// =========================================================================
const state = {
    activeTab: "map",
    filters: {},
    map: null,
    markerLayer: null,
    heatLayer: null,
    mapMode: "points",    // "points" (clustered markers) | "heatmap" | "hexbin" — v0.7 renamed "clusters" → "points"
    chart: null,
    timelineYear: null,  // null = yearly view, "2005" = monthly drill-down
    // v0.8.6: searchPage/searchTotal/searchSort/dupesPage/dupesTotal
    // were removed along with the Search + Duplicates panels.
    insightsCharts: {},  // { radar, timeline, source, shape }
    // Set to true while we're loading state from the URL hash on
    // navigation, so the hash-update side effect is suppressed.
    hashLoading: false,
};

// Filter ID -> human label, used by the active-filter chip strip in the
// search panel and as the source-of-truth for serializing filters to the
// URL hash.
// v0.8.7: trimmed to the 6 filters the Observatory bulk buffer
// actually supports. Country / State / Hynek / Vallee / Collection /
// Coords were removed because they have no byte slot in the 32-byte
// bulk row and applyClientFilters couldn't filter by them client-side.
// See docs/V087_FILTER_CLEANUP.md for the full audit.
const FILTER_FIELDS = [
    { id: "filter-date-from", key: "date_from", label: "From" },
    { id: "filter-date-to",   key: "date_to",   label: "To" },
    { id: "filter-shape",     key: "shape",     label: "Shape" },
    { id: "filter-source",    key: "source",    label: "Source" },
    { id: "filter-color",     key: "color",     label: "Color" },
    { id: "filter-emotion",   key: "emotion",   label: "Emotion" },
];

// Source colors for chart and badges
const SOURCE_COLORS = {
    "UFOCAT":    { bg: "#4e79a7", border: "#3a5d82" },
    "NUFORC":    { bg: "#f28e2b", border: "#c97520" },
    "MUFON":     { bg: "#e15759", border: "#b84445" },
    "UPDB":      { bg: "#76b7b2", border: "#5d9490" },
    "UFO-search": { bg: "#59a14f", border: "#478240" },
};

function sourceColor(name) {
    return (SOURCE_COLORS[name] || { bg: "#999", border: "#777" });
}

// Emotion colors for sentiment charts
const EMOTION_COLORS = {
    joy:          { bg: "rgba(255, 206, 86, 0.7)",  border: "#ffce56" },
    fear:         { bg: "rgba(153, 102, 255, 0.7)", border: "#9966ff" },
    anger:        { bg: "rgba(255, 99, 132, 0.7)",  border: "#ff6384" },
    sadness:      { bg: "rgba(54, 162, 235, 0.7)",  border: "#36a2eb" },
    surprise:     { bg: "rgba(255, 159, 64, 0.7)",  border: "#ff9f40" },
    disgust:      { bg: "rgba(75, 192, 192, 0.7)",  border: "#4bc0c0" },
    trust:        { bg: "rgba(63, 185, 80, 0.7)",   border: "#3fb950" },
    anticipation: { bg: "rgba(88, 166, 255, 0.7)",  border: "#58a6ff" },
};
const EMOTION_NAMES = ["joy", "fear", "anger", "sadness", "surprise", "disgust", "trust", "anticipation"];

// =========================================================================
// Init
// =========================================================================
document.addEventListener("DOMContentLoaded", async () => {
    // Start the stats-badge boot sequence immediately so the user sees
    // something moving while /api/filters + /api/stats fetch in parallel.
    const _stopBadgeBoot = startStatsBadgeBoot();

    // Progressive boot: kick off both fetches in parallel and apply
    // each one the instant it returns, rather than waiting for both
    // via Promise.all. /api/filters usually wins (it's tiny + cached
    // server-side) — populating the dropdowns first lets the user
    // start typing into search/filter fields before /api/stats has
    // even resolved.
    const filtersPromise = fetchJSON("/api/filters");
    const statsPromise = fetchJSON("/api/stats");

    filtersPromise
        .then(populateFilterDropdowns)
        .catch(err => console.error("filters fetch failed:", err));

    statsPromise
        .then(statsData => {
            _stopBadgeBoot();
            showStats(statsData);
            initStatsBadge();
        })
        .catch(err => {
            _stopBadgeBoot();
            console.error("stats fetch failed:", err);
        });

    // Wait for both before doing anything that needs them. Most of the
    // setup below only depends on the filters being populated, but a
    // handful of routes (timeline / search) read filter values out of
    // the dropdowns so we serialize the rest of init behind that.
    await filtersPromise.catch(() => {});

    // Setup tabs. Filter to nav tabs with a data-tab AND skip the gear
    // icon — it shares the .tab class for consistent styling but has
    // its own click handler in initSettingsMenu() that opens the
    // dropdown instead of switching panels. Without the filter, clicking
    // the gear would call switchTab(undefined) and blank out the
    // entire panel area.
    document.querySelectorAll(".tab[data-tab]").forEach(btn => {
        if (btn.id === "settings-btn") return;
        btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // v0.8.5 — site title acts as a "home" link back to the
    // Observatory landing tab. The `href="#/observatory"` on the
    // anchor triggers the hashchange listener automatically, but we
    // also intercept the click so switchTab runs even when the hash
    // is already `#/observatory` (otherwise clicking from the
    // Observatory tab would be a no-op and feel broken).
    const siteTitle = document.getElementById("site-title");
    if (siteTitle) {
        siteTitle.addEventListener("click", (e) => {
            e.preventDefault();
            switchTab("observatory");
        });
    }

    // Filter buttons
    document.getElementById("btn-apply-filters").addEventListener("click", applyFilters);
    document.getElementById("btn-clear-filters").addEventListener("click", clearFilters);

    // v0.8.7: #coords-filter was deleted (had no byte slot in the
    // bulk buffer so the filter pipeline couldn't use it). The old
    // change handler that bound to it here is gone with the element.

    // v0.8.6: Search panel, Duplicates panel, and Timeline drill-down
    // "back" button were all removed. Search semantics moved to the
    // Observatory rail filters; the v0.8.3b export ships zero
    // duplicate candidates; the new Timeline dashboard doesn't drill
    // into monthly sub-views so no back button is needed.

    // Modal
    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-overlay").addEventListener("click", e => {
        if (e.target === e.currentTarget) closeModal();
    });

    // Map mode toggle (Clusters / Heatmap)
    document.querySelectorAll(".map-mode-btn").forEach(btn => {
        btn.addEventListener("click", () => toggleMapMode(btn.dataset.mode));
    });

    // Settings dropdown menu (wraps the AI tools)
    initSettingsMenu();

    // Sprint 3: filter bar polish — auto-apply selects, is-dirty on
    // text inputs, "More filters" drawer, mobile filter bar toggle.
    initFilterBarPolish();

    // Map place search (Nominatim) + browser geolocation
    initMapPlaceSearch();

    // v0.8.6: initSearchActions() removed with the Search panel.
    // /api/export.csv and /api/export.json still work via direct URL
    // but are no longer wired to any UI button.

    // BYOK AI chat
    loadAISettings();
    applySettingsToUI();
    aiInitListeners();

    // v0.7: Theme toggle (SIGNAL / DECLASS)
    initThemeToggle();

    // v0.7.2: default date range is 1900 → current year. The UFOSINT
    // DB technically has records back to year 34 AD, but 1900+ is the
    // meaningful modern era and users expect a sane starting range
    // across Map, Timeline, and Search. Applied BEFORE the hash-
    // restore below so any #/...?date_from=1950 deep link still wins.
    // The Clear button resets both fields to empty, so users who
    // want to explore pre-1900 data can opt out at any time.
    applyDefaultDateRange();

    // Init map
    initMap();

    // v0.7: Observatory is the default tab. switchTab() validates the
    // name against VALID_TABS, aliases map → observatory, and falls
    // back to observatory for any garbage (including a polluted
    // #/undefined hash from older buggy sessions).
    switchTab("observatory");

    // ----- URL hash routing: load any deep-linked tab + filters -----
    const initial = readHash();
    if (initial) {
        applyHashToFilters(initial.params);
        if (initial.tab && VALID_TABS.has(initial.tab) &&
                initial.tab !== "map" && initial.tab !== "observatory") {
            switchTab(initial.tab);
        }
        // v0.8.6: #/search and #/duplicates deep links no longer
        // resolve; switchTab() falls back to observatory for any tab
        // not in VALID_TABS. #/map still aliases through switchTab.
    }

    // Back/forward navigation
    window.addEventListener("hashchange", () => {
        const h = readHash();
        if (!h) return;
        applyHashToFilters(h.params);
        if (h.tab && h.tab !== state.activeTab) {
            switchTab(h.tab);
        } else {
            // Same tab — re-run with new filters
            applyFilters();
        }
    });
});

// =========================================================================
// Hackery loading terminal
// -------------------------------------------------------------------------
// Renders a monospace terminal "card" that cycles through a set of
// intelligence-agency-flavored status messages while something loads.
// Uses a typewriter CSS animation for each message + a blinking cursor
// + a drifting scanline (both CSS). The JS just swaps text every ~900ms
// and tears the whole thing down when unmountLoadingTerminal() is called.
//
// Respects prefers-reduced-motion by cycling text without restarting
// the typewriter animation on each message.
// =========================================================================

const TERMINAL_MESSAGE_BANKS = {
    // Generic fallback used when a caller doesn't pass a bank name.
    generic: [
        "ESTABLISHING SECURE CHANNEL",
        "AUTHENTICATING CREDENTIALS",
        "DECRYPTING PAYLOAD",
        "STREAMING RECORDS",
        "APPLYING FILTERS",
        "RENDERING RESULTS",
    ],
    search: [
        "TOKENIZING QUERY",
        "SCANNING 614,505 SIGHTING RECORDS",
        "CROSS-REFERENCING DESCRIPTIONS",
        "APPLYING SHAPE + DATE FILTERS",
        "RANKING BY RELEVANCE",
        "DECLASSIFYING RESULTS",
    ],
    map: [
        "ACQUIRING TILE SATELLITE FEED",
        "GEOCODING 105,836 LOCATIONS",
        "CLUSTERING COORDINATES",
        "PROJECTING WORLD MERCATOR",
        "PLOTTING SIGHTINGS",
    ],
    timeline: [
        "AGGREGATING BY YEAR",
        "BUILDING STACK FROM 5 SOURCES",
        "COMPUTING MONTHLY BREAKDOWN",
        "RENDERING CHART",
    ],
    duplicates: [
        "LOADING 126,730 DUPLICATE PAIRS",
        "COMPUTING CROSS-SOURCE SIMILARITY",
        "RANKING BY CONFIDENCE",
        "SURFACING TOP CANDIDATES",
    ],
    insights: [
        "ACCESSING SENTIMENT ANALYSIS",
        "AGGREGATING EMOTION VECTORS",
        "COMPUTING TRENDS",
        "RENDERING DASHBOARD",
    ],
    boot: [
        "BOOT SEQUENCE INITIATED",
        "CONTACTING CORTEX NODE",
        "DECRYPTING FILTER CACHE",
        "LOADING 614,505 SIGHTINGS",
        "READY",
    ],
};

const _activeTerminals = new WeakMap();

/**
 * Render a hackery loading terminal into `el`. Returns a handle with a
 * `.stop()` method that cancels the cycle and clears the DOM.
 *
 * @param {HTMLElement} el  — target container; its innerHTML is replaced
 * @param {string} bank     — key into TERMINAL_MESSAGE_BANKS
 * @param {object} opts     — { header?, compact?, interval? }
 */
function mountLoadingTerminal(el, bank = "generic", opts = {}) {
    if (!el) return { stop() {} };

    // Clean up a previous terminal in the same container.
    const prev = _activeTerminals.get(el);
    if (prev) prev.stop();

    const messages = (TERMINAL_MESSAGE_BANKS[bank] || TERMINAL_MESSAGE_BANKS.generic).slice();
    const header = opts.header || `CORTEX / ${bank.toUpperCase()}.LOG`;
    const compact = !!opts.compact;
    const interval = opts.interval || 900;

    el.innerHTML = `
        <div class="loading-terminal${compact ? " compact" : ""}" role="status" aria-live="polite">
            ${compact ? "" : `<div class="term-header">${escapeHtml(header)}</div>`}
            <div class="term-line">
                <span class="term-prompt">&gt;</span>
                <span class="term-msg" aria-live="polite"></span>
                <span class="term-cursor" aria-hidden="true"></span>
            </div>
            ${compact ? "" : `<div class="term-progress"></div>`}
        </div>
    `;

    const msgEl = el.querySelector(".term-msg");
    if (!msgEl) return { stop() {} };

    // Respect reduced motion — don't restart the typewriter animation
    // on each tick, just swap text.
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

    let i = 0;
    function tick() {
        const msg = messages[i % messages.length];
        if (reduced) {
            msgEl.textContent = msg;
        } else {
            // Re-trigger the typewriter animation by cloning the node.
            // Restarting via animation: none → reflow → animation works
            // but is uglier than a clone.
            msgEl.textContent = msg;
            msgEl.style.animation = "none";
            // force reflow
            void msgEl.offsetWidth;
            msgEl.style.animation = "";
        }
        i += 1;
    }
    tick();
    const timer = setInterval(tick, interval);

    const handle = {
        stop() {
            clearInterval(timer);
            _activeTerminals.delete(el);
        },
    };
    _activeTerminals.set(el, handle);
    return handle;
}

/**
 * Cancel any active terminal inside `el` without necessarily wiping the
 * DOM (the caller usually replaces innerHTML anyway). Safe to call even
 * if no terminal is mounted.
 */
function unmountLoadingTerminal(el) {
    if (!el) return;
    const handle = _activeTerminals.get(el);
    if (handle) handle.stop();
}

// =========================================================================
// Progressive loading — keep previous content visible while new data loads
// -------------------------------------------------------------------------
// Pattern:
//   1. showProgressiveLoading(container, bank, opts) — adds the
//      .is-loading-progressive class (which dims existing children via
//      CSS) and mounts a centered terminal overlay on top.
//   2. await fetchJSON(...)
//   3. hideProgressiveLoading(container) — removes the class, leaves the
//      overlay node in place but stops its message cycle.
//   4. Replace the dimmed children with new ones, optionally adding the
//      `.is-new` class with a `--i` index for stagger fade-in.
//
// Reuses mountLoadingTerminal under the hood, so the visual language is
// identical to the v0.5 loading system — just placed over existing
// content instead of replacing it.
// =========================================================================

function showProgressiveLoading(container, bank = "generic", opts = {}) {
    if (!container) return;
    container.classList.add("is-progressive", "is-loading-progressive");

    // Reuse an existing overlay if there is one (same container loaded
    // twice in a row), otherwise create one. We always render the
    // terminal in compact form here — the overlay style assumes it.
    let overlay = container.querySelector(":scope > .progressive-overlay");
    if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = "progressive-overlay";
        container.appendChild(overlay);
    }
    mountLoadingTerminal(overlay, bank, { compact: true, ...opts });
}

function hideProgressiveLoading(container) {
    if (!container) return;
    container.classList.remove("is-loading-progressive");
    const overlay = container.querySelector(":scope > .progressive-overlay");
    if (overlay) {
        unmountLoadingTerminal(overlay);
        // Defer removal so the CSS opacity fade has time to finish.
        // 240ms covers --t-base (180ms) plus a small margin.
        setTimeout(() => {
            // Bail if a new load started before the fade finished.
            if (!container.classList.contains("is-loading-progressive")) {
                overlay.remove();
            }
        }, 240);
    }
}

/**
 * Stagger-tag a freshly-rendered NodeList so each element fades in
 * sequentially via the CSS .is-new keyframe + --i index.
 */
function staggerNewChildren(parent, selector = ".result-card") {
    if (!parent) return;
    const newOnes = parent.querySelectorAll(selector);
    newOnes.forEach((el, i) => {
        el.classList.add("is-new");
        el.style.setProperty("--i", String(i));
    });
}

// =========================================================================
// Helpers
// =========================================================================
async function fetchJSON(url, options = {}) {
    // Optional second argument forwards an AbortSignal so callers can
    // cancel in-flight requests when the user pans the map again, types
    // a new search, etc. Stale responses get an AbortError that the
    // caller can suppress instead of overwriting fresh data.
    const resp = await fetch(url, options);
    if (!resp.ok) {
        let detail = "";
        try { detail = await resp.text(); } catch (_) {}
        throw new Error(`HTTP ${resp.status}: ${detail.substring(0, 200)}`);
    }
    return resp.json();
}

// Returns true if the error was a deliberate abort (so callers can
// silently ignore it instead of rendering an error state).
function isAbortError(err) {
    return err && (err.name === "AbortError" || err.code === 20);
}

function getFilterParams() {
    // v0.8.7: serializes the 6 Observatory filter fields plus the
    // multi-select movement cluster. Country / State / Hynek / Vallee /
    // Collection / Coords are gone (no buffer byte slot) and the old
    // Search panel's `q` param is gone with the panel.
    const p = new URLSearchParams();
    const df = document.getElementById("filter-date-from")?.value || "";
    const dt = document.getElementById("filter-date-to")?.value || "";
    const shape = document.getElementById("filter-shape")?.value || "";
    const source = document.getElementById("filter-source")?.value || "";
    const color = document.getElementById("filter-color")?.value || "";
    const emotion = document.getElementById("filter-emotion")?.value || "";

    if (df) p.set("date_from", df);
    if (dt) p.set("date_to", dt);
    if (shape) p.set("shape", shape);
    if (source) p.set("source", source);
    if (color) p.set("color", color);
    if (emotion) p.set("emotion", emotion);

    // Movement cluster is multi-select; serialize as comma-separated.
    // _readMovementCats returns [] when nothing is checked, which
    // means the `movement` param is omitted (no filter).
    const movs = _readMovementCats();
    if (movs.length) p.set("movement", movs.join(","));

    return p;
}

// v0.8.7 — collect the checked categories from the movement cluster.
// Returns an array of category names that match POINTS.movements
// entries verbatim, or [] when nothing is selected. Safe to call
// before the cluster is mounted (returns [] if the host element
// doesn't exist yet).
function _readMovementCats() {
    const boxes = document.querySelectorAll(".movement-cluster input[type='checkbox']:checked");
    if (!boxes.length) return [];
    const out = [];
    for (const b of boxes) {
        if (b.value) out.push(b.value);
    }
    return out;
}

// v0.8.7: populateFilterDropdowns is now responsible only for the
// source dropdown (which needs source_database IDs from the server)
// and the __sourceMap lookup table. Shape / Color / Emotion / Movement
// are populated from POINTS metadata after bootDeckGL completes via
// populateFilterDropdownsFromDeck() below, so they use the canonical
// standardized lists the bulk buffer was built with. Country / State /
// Hynek / Vallee / Collection were deleted in v0.8.7 (no buffer byte
// slot).
function populateFilterDropdowns(data) {
    const sourceSelect = document.getElementById("filter-source");
    if (sourceSelect) {
        // Lookup table used by chart legend callbacks to map a
        // source name back to its numeric id.
        window.__sourceMap = {};
        (data.sources || []).forEach(s => {
            window.__sourceMap[s.name] = s;
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = s.name;
            sourceSelect.appendChild(opt);
        });
    }
    // Shape / color / emotion / movement arrive later via
    // populateFilterDropdownsFromDeck(), called from bootDeckGL()
    // and _wireTimeBrushToDeck() once POINTS.ready flips.
}

// v0.8.7 — populate shape / color / emotion dropdowns and the
// movement checkbox cluster from POINTS metadata. Called after
// bootDeckGL() completes so POINTS.shapes / .colors / .emotions /
// .movements are the canonical standardized lists the bulk buffer
// was built with (vs. /api/filters returning raw per-source shape
// strings). Idempotent — each helper clears its target's innerHTML
// before re-populating, so double-runs are safe.
function populateFilterDropdownsFromDeck() {
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) {
        return;
    }
    const P = window.UFODeck.POINTS;
    _populateLookupDropdown("filter-shape",   P.shapes,   "All shapes");
    _populateLookupDropdown("filter-color",   P.colors,   "All colors");
    _populateLookupDropdown("filter-emotion", P.emotions, "All emotions");
    _mountMovementCluster(P.movements);
}

// Blank a <select>, write a placeholder option, then append one
// <option> per non-null entry in `values`. Preserves any existing
// selection across re-population (applied if still present in the
// new list).
function _populateLookupDropdown(id, values, placeholder) {
    const el = document.getElementById(id);
    if (!el) return;
    const prev = el.value;
    el.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`;
    for (const v of (values || [])) {
        if (!v) continue;  // skip index-0 "unknown" placeholder
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = v;
        el.appendChild(opt);
    }
    if (prev) {
        // Only restore the previous value if it still exists as
        // an option — avoids the dropdown silently swapping to
        // blank on a re-populate.
        const hit = Array.from(el.options).some(o => o.value === prev);
        if (hit) el.value = prev;
    }
}

// Render the 10-pill movement cluster. Each pill is a checkbox
// labelled with the category name; the checkbox value matches
// POINTS.movements verbatim so _readMovementCats returns strings
// that _rebuildVisible's indexOf lookup recognises. Change events
// trigger applyFilters() for immediate re-tally.
function _mountMovementCluster(movements) {
    const host = document.getElementById("filter-movement-cluster");
    if (!host || !Array.isArray(movements)) return;
    // Preserve any existing checked state so re-mount doesn't wipe
    // the user's selection. Merge with any hash-restore intent from
    // state.pendingMovementFilter (set by applyHashToFilters when
    // the cluster wasn't mounted yet at deep-link restore time).
    const prevChecked = new Set(
        Array.from(host.querySelectorAll("input[type='checkbox']:checked"))
            .map(b => b.value),
    );
    const pending = state.pendingMovementFilter;
    const initial = pending instanceof Set ? pending : prevChecked;
    if (pending) state.pendingMovementFilter = null;  // consume
    host.innerHTML = "";
    for (const name of movements) {
        if (!name) continue;  // skip the index-0 sentinel if any
        const label = document.createElement("label");
        label.className = "movement-chip-label";
        const safe = escapeHtml(name);
        label.innerHTML = `<input type="checkbox" value="${safe}"><span>${safe}</span>`;
        host.appendChild(label);
        const input = label.querySelector("input");
        if (initial.has(name)) input.checked = true;
        input.addEventListener("change", () => applyFilters());
    }
}

/**
 * Cycle the stats-badge through hackery boot messages until showStats()
 * replaces the innerHTML. Returns a stop function the caller invokes
 * once real stats land.
 */
function startStatsBadgeBoot() {
    const badge = document.getElementById("stats-badge");
    if (!badge) return () => {};
    const msgEl = badge.querySelector(".stats-boot-msg");
    if (!msgEl) return () => {};

    const messages = TERMINAL_MESSAGE_BANKS.boot;
    let i = 0;
    // Tick once immediately (skipping the pre-filled initial message on
    // the first tick since it's already in the DOM from index.html), then
    // every 600ms. Fast enough that the user perceives activity.
    const timer = setInterval(() => {
        i += 1;
        msgEl.textContent = messages[i % messages.length];
    }, 600);

    return function stop() {
        clearInterval(timer);
    };
}

function showStats(data) {
    const badge = document.getElementById("stats-badge");
    const popover = document.getElementById("stats-popover");
    const total = data.total_sightings.toLocaleString();
    // v0.8.7.2 — prefer mapped_sightings (the sighting-level count
    // via `sighting JOIN location`) so the badge shows how many
    // markers are actually on the map. Fall back to
    // geocoded_locations (the distinct-place count) when talking to
    // a pre-v0.8.7.2 server — which is what the frontend ran against
    // for months without anyone noticing the number was ~4x too low.
    // The popover still shows the geocoded_locations number in the
    // detail section because "distinct places" is a legitimate stat,
    // just differently named.
    const mappedCount = (typeof data.mapped_sightings === "number")
        ? data.mapped_sightings
        : data.geocoded_locations;
    const mapped = mappedCount.toLocaleString();
    const geo = data.geocoded_locations.toLocaleString();
    const geoOrig = (data.geocoded_original || 0).toLocaleString();
    const geoGN = (data.geocoded_geonames || 0).toLocaleString();
    const dupes = data.duplicate_candidates.toLocaleString();

    // v0.8.5 — new derived counts from the v0.8.3b data layer.
    // The server returns null on pre-v0.8.2/v0.8.3 schemas; we hide
    // the corresponding row in both the badge and the popover so
    // the UI never shows "undefined sightings".
    //
    // Also hide when the count is 0: in practice that means the
    // column exists but isn't populated yet (e.g. the operator ran
    // add_v083_derived_columns.sql but hasn't re-migrated from
    // ufo_public.db yet). Showing "0 with movement" is misleading —
    // better to hide the chip entirely until the data lands.
    const highQ = data.high_quality;
    const withMovement = data.with_movement;
    const highQStr = (typeof highQ === "number" && highQ > 0)
        ? highQ.toLocaleString() : null;
    const withMovStr = (typeof withMovement === "number" && withMovement > 0)
        ? withMovement.toLocaleString() : null;

    // Compact badge — up to five chips separated by middle dots. The
    // first three (total, mapped, duplicates) always render; the new
    // "high quality" and "with movement" chips only render once the
    // v0.8.2/v0.8.3b derived columns are populated. Everything over
    // ~900px shows all visible chips inline; on narrower viewports
    // the CSS truncates.
    const chips = [
        `${total} sightings`,
        `${mapped} mapped`,
    ];
    if (highQStr) chips.push(`${highQStr} high quality`);
    if (withMovStr) chips.push(`${withMovStr} with movement`);
    chips.push(`${dupes} possible duplicates`);
    const sep = ` <span class="stats-sep">·</span> `;
    badge.innerHTML = chips.join(sep);

    if (popover) {
        const sources = (data.by_source || []).map(s =>
            `<tr><td>${escapeHtml(s.name)}</td><td>${s.count.toLocaleString()}</td></tr>`
        ).join("");
        // v0.8.5 — insert the two new rows between geocoded and
        // duplicates so the detail popover mirrors the badge order.
        const derivedRows = [];
        if (highQStr) {
            derivedRows.push(
                `<div class="stats-popover-row"><span>High quality (score ≥ 60)</span><strong>${highQStr}</strong></div>`
            );
        }
        if (withMovStr) {
            derivedRows.push(
                `<div class="stats-popover-row"><span>With movement described</span><strong>${withMovStr}</strong></div>`
            );
        }
        popover.innerHTML = `
            <div class="stats-popover-section">
                <div class="stats-popover-row"><span>Total sightings</span><strong>${total}</strong></div>
                <div class="stats-popover-row"><span>Sightings on map</span><strong>${mapped}</strong></div>
                <div class="stats-popover-row"><span>Distinct geocoded places</span><strong>${geo}</strong></div>
                <div class="stats-popover-row stats-popover-sub"><span>· from source data</span>${geoOrig}</div>
                <div class="stats-popover-row stats-popover-sub"><span>· from GeoNames lookup</span>${geoGN}</div>
                ${derivedRows.join("")}
                <div class="stats-popover-row"><span>Duplicate candidate pairs</span><strong>${dupes}</strong></div>
            </div>
            ${sources ? `<div class="stats-popover-section">
                <div class="stats-popover-title">By source</div>
                <table class="stats-popover-table">${sources}</table>
            </div>` : ""}
            <div class="stats-popover-foot">All counts come from <a href="#/methodology" onclick="switchTab('methodology'); document.getElementById('stats-popover').hidden=true; document.getElementById('stats-badge').setAttribute('aria-expanded','false'); return false;">the deduplication pipeline</a>.</div>
        `;
    }
}

function initStatsBadge() {
    const badge = document.getElementById("stats-badge");
    const popover = document.getElementById("stats-popover");
    if (!badge || !popover) return;

    function open() {
        popover.hidden = false;
        badge.setAttribute("aria-expanded", "true");
        setTimeout(() => document.addEventListener("click", outside), 0);
        document.addEventListener("keydown", escape);
    }
    function close() {
        popover.hidden = true;
        badge.setAttribute("aria-expanded", "false");
        document.removeEventListener("click", outside);
        document.removeEventListener("keydown", escape);
    }
    function outside(e) {
        if (!popover.contains(e.target) && e.target !== badge) close();
    }
    function escape(e) { if (e.key === "Escape") close(); }

    badge.addEventListener("click", (e) => {
        e.stopPropagation();
        if (popover.hidden) open();
        else close();
    });
}

function sourceBadge(name) {
    const c = sourceColor(name);
    return `<span class="source-badge" style="background:${c.bg}">${name}</span>`;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function formatLocation(city, st, country) {
    const parts = [city, st, country].filter(x => x);
    return parts.join(", ");
}

// =========================================================================
// Tabs
// =========================================================================
// Whitelist of real tab names. Used by switchTab as a guard so a
// stray call or a polluted URL hash (e.g. "#/undefined" from an
// earlier buggy click handler) can't blank out the whole panel area.
const VALID_TABS = new Set([
    "observatory", "map", "timeline",
    "insights", "methodology", "ai", "connect",
]);

function switchTab(tab) {
    // Guard against stray calls: missing tab, literal "undefined"
    // from a polluted URL hash, or any name not in our whitelist.
    // Falls back to the Observatory so the user never sees a blank
    // panel area.
    if (!tab || !VALID_TABS.has(tab)) {
        tab = "observatory";
    }

    // v0.7 alias: legacy Map deep links (#/map?shape=triangle) still
    // resolve to the Observatory dashboard since Map was merged into it.
    // v0.7.1: Timeline is its own tab again (users wanted the full
    // Chart.js drill-down back) so it does NOT alias to Observatory.
    if (tab === "map") {
        state.legacyView = tab;
        tab = "observatory";
    }

    state.activeTab = tab;

    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    const tabBtn = document.querySelector(`.tab[data-tab="${tab}"]:not([hidden])`);
    if (tabBtn) tabBtn.classList.add("active");

    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    const panel = document.getElementById(`panel-${tab}`);
    if (panel) panel.classList.add("active");

    if (tab === "observatory") {
        // Observatory owns the #map node + the time brush. On first
        // activation we populate the left rail and bind the brush;
        // subsequent activations just invalidateSize on the Leaflet map
        // so it reflows after a panel switch.
        loadObservatory();
    } else if (tab === "timeline") {
        // v0.8.6: 3-card client-side dashboard driven by the bulk
        // buffer. Subsequent filter changes call refreshTimelineCards()
        // directly from applyClientFilters().
        loadTimeline();
    } else if (tab === "insights") {
        loadInsights();
    } else if (tab === "ai") {
        // Focus the input on tab open
        setTimeout(() => document.getElementById("ai-input")?.focus(), 100);
    }
    // Hide filters bar on the AI / Connect / Methodology tabs (no global filters)
    const fb = document.getElementById("filters-bar");
    if (fb) fb.style.display = (tab === "methodology" || tab === "ai" || tab === "connect") ? "none" : "flex";
    writeHash();
}

async function applyFilters() {
    const applyBtn = document.getElementById("btn-apply-filters");
    // Clear the "unsaved changes" pulse — filters are now committed.
    applyBtn?.classList.remove("is-dirty");
    const restore = disableButtonWhilePending(applyBtn, "Applying…");
    try {
        if (state.activeTab === "map" || state.activeTab === "observatory") {
            // v0.8.0: Try the GPU filter path first. When the deck.gl
            // layer is ready, applyClientFilters() walks the typed
            // arrays in ~1 ms and we're done. Falls through to the
            // legacy server fetch on ancient browsers.
            //
            // v0.8.2-cleanup-2: ALSO suppress the fallback during the
            // deck.gl boot window. Without this guard, an early
            // applyFilters() (e.g. from clearFilters() at boot, or
            // from a hash-driven filter restore) would fire the
            // legacy markerCluster path, which then renders its
            // numbered bubbles on top of the deck.gl layer once
            // bootDeckGL() finishes.
            const gpu = applyClientFilters();
            const deckPending = (state.deckBoot === "pending");
            if (!gpu && !deckPending) {
                if (state.mapMode === "heatmap") await loadHeatmap();
                else if (state.mapMode === "hexbin") await loadHexBins();
                else await loadMapMarkers();
            }
        }
        else if (state.activeTab === "timeline") await loadTimeline();
        else if (state.activeTab === "insights") await loadInsights();
    } finally {
        restore();
        writeHash();
    }
}

function clearFilters() {
    // v0.8.7 — drive the clear from FILTER_FIELDS so adding a new
    // filter automatically wires it into the Clear button without
    // touching this function. Movement cluster is separate because
    // it's not a single input.
    FILTER_FIELDS.forEach(({ id }) => {
        const el = document.getElementById(id);
        if (el) el.value = "";
    });
    // Uncheck every movement category
    document.querySelectorAll(".movement-cluster input[type='checkbox']")
        .forEach(b => { b.checked = false; });
    // Also reset the Quality rail toggles
    if (state.qualityFilter) {
        state.qualityFilter.highQuality = false;
        state.qualityFilter.hideHoaxes = false;
        state.qualityFilter.hasDescription = null;
        state.qualityFilter.hasMedia = null;
        state.qualityFilter.hasMovement = null;
    }
    document.querySelectorAll("#rail-quality-list input[type='checkbox']")
        .forEach(b => { b.checked = false; });
    applyFilters();
}

// =========================================================================
// URL hash routing  (#/<tab>?key=value&key=value)
// =========================================================================
function writeHash() {
    if (state.hashLoading) return;
    const params = new URLSearchParams();
    FILTER_FIELDS.forEach(({ id, key }) => {
        const v = document.getElementById(id);
        if (v && v.value) {
            params.set(key, v.value);
        }
    });
    // v0.8.7: movement cluster is multi-select; serialize as a
    // comma-separated `movement` param when any are checked.
    const movs = _readMovementCats();
    if (movs.length) params.set("movement", movs.join(","));
    // v0.8.6: no per-tab search state to serialise. The Observatory
    // owns all filter state via FILTER_FIELDS above, and there's no
    // free-text `q` or pagination to persist anymore.
    const qs = params.toString();
    const newHash = `#/${state.activeTab}${qs ? "?" + qs : ""}`;
    if (newHash !== window.location.hash) {
        history.replaceState(null, "", newHash);
    }
}

function readHash() {
    const m = window.location.hash.match(/^#\/([^?]+)(?:\?(.*))?$/);
    if (!m) return null;
    const tab = m[1];
    const params = new URLSearchParams(m[2] || "");
    return { tab, params };
}

function applyHashToFilters(params) {
    state.hashLoading = true;
    try {
        FILTER_FIELDS.forEach(({ id, key }) => {
            const el = document.getElementById(id);
            if (!el) return;
            const v = params.get(key);
            if (v != null) el.value = v;
        });
        // v0.8.7 — movement cluster deep-link restore. The cluster
        // might not be mounted yet (it waits for POINTS.ready), so
        // stash the wanted-set on state for _mountMovementCluster
        // to apply when it runs. When the cluster IS already
        // mounted (e.g. tab switch, hash update), apply directly.
        const movParam = params.get("movement");
        if (movParam) {
            const wanted = new Set(movParam.split(",").filter(Boolean));
            state.pendingMovementFilter = wanted;
            const boxes = document.querySelectorAll(
                ".movement-cluster input[type='checkbox']",
            );
            if (boxes.length) {
                boxes.forEach(b => { b.checked = wanted.has(b.value); });
            }
        } else {
            state.pendingMovementFilter = null;
        }
        // v0.8.6: the legacy `q`, `page`, and `sort` URL params
        // belonged to the removed Search tab. Silently ignored so
        // pre-v0.8.6 deep links don't throw.
    } finally {
        state.hashLoading = false;
    }
}

/**
 * v0.8.6 — navigateToSearch() replacement.
 *
 * Instead of jumping to a dedicated Search tab, apply the given
 * filter updates to the Observatory rail and flip to the Observatory
 * (which owns the bulk-buffer client-side filter pipeline).
 *
 * Callers keep working: `navigateToSearch({date_from: "1973-10-15"})`
 * now sets the Observatory's date filter and switches to the
 * Observatory tab. The map re-tallies immediately via
 * applyClientFilters().
 */
function navigateToSearch(filterUpdates, clearFirst = false) {
    state.hashLoading = true;
    try {
        if (clearFirst) {
            FILTER_FIELDS.forEach(({ id, key }) => {
                if (key === "coords") return;
                const el = document.getElementById(id);
                if (el) el.value = "";
            });
        }
        Object.entries(filterUpdates || {}).forEach(([key, value]) => {
            const field = FILTER_FIELDS.find(f => f.key === key);
            if (field) {
                const el = document.getElementById(field.id);
                if (el) el.value = value == null ? "" : String(value);
            }
            // v0.8.6: the legacy `q` (free-text search) param is
            // no longer supported — Observatory filters are faceted,
            // not full-text. Silently ignored so old deep links from
            // Timeline bar clicks don't throw.
        });
    } finally {
        state.hashLoading = false;
    }
    switchTab("observatory");
    applyFilters();
}

/**
 * Last day of the month for date_to= bounds. month is 1-12.
 */
function lastDayOfMonth(year, month) {
    return new Date(parseInt(year, 10), parseInt(month, 10), 0).getDate();
}

// =========================================================================
// Map
// =========================================================================

// v0.8.4 — Per-theme base map tile sources. Carto ships dark and
// light variants of the same cartographic style, so switching between
// the two gives a visually coherent light/dark map without changing
// the data geometry. Both are free for public use, CORS-enabled,
// retina-aware (the {r} → @2x suffix kicks in on hi-DPI), and don't
// need an API key. Carto attribution covers both.
//
//   signal  — "Dark Matter": dark slate background, white roads.
//             Matches the cyan-on-void palette.
//   declass — "Voyager": warm cream paper, soft desaturated accents.
//             Matches the DECLASS ink-on-paper palette.
//
// setTheme() below calls state.tileLayer.setUrl() with the right
// template when the user toggles — no layer remove/re-add needed.
const TILE_URLS = {
    signal:  "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    declass: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
};
const TILE_ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
    '&copy; <a href="https://carto.com/attributions">CARTO</a>';


function _currentTheme() {
    // Read from the body class the pre-paint script set, with a
    // localStorage fallback for environments where the class hasn't
    // been applied yet (initMap runs before setTheme in some paths).
    if (document.body.classList.contains("theme-declass")) return "declass";
    if (document.body.classList.contains("theme-signal")) return "signal";
    try {
        const stored = localStorage.getItem("ufosint-theme");
        if (stored === "declass" || stored === "signal") return stored;
    } catch (_) { /* localStorage blocked */ }
    return "signal";
}


function initMap() {
    state.map = L.map("map", {
        center: [39, -98],
        zoom: 4,
        preferCanvas: true,
    });

    // v0.8.4 — stash the tile layer on state so setTheme() can
    // call state.tileLayer.setUrl() to swap the tile source without
    // removing and re-adding the layer.
    state.tileLayer = L.tileLayer(TILE_URLS[_currentTheme()], {
        attribution: TILE_ATTRIBUTION,
        maxZoom: 19,
        detectRetina: true,
    }).addTo(state.map);

    // v0.8.0: Try to boot the deck.gl GPU path. If WebGL or deck.gl
    // itself is unavailable we fall through to the legacy Leaflet
    // marker-cluster path below, which is the same code that v0.7
    // shipped. The user gets the right experience for their browser
    // with zero regression for anyone on ancient hardware.
    //
    // state.deckBoot is a tri-state lifecycle flag read by
    // scheduleMapReload() and the initial-load branch below:
    //   "none"    — legacy path will run; GPU never attempted
    //   "pending" — bootDeckGL() is in flight; legacy fetches are
    //               suppressed so we don't hammer the DB during the
    //               ~1.5 s bulk-download window
    //   "ready"   — GPU layer mounted; state.useDeckGL is true
    //   "failed"  — GPU boot failed; legacy path takes over
    state.useDeckGL = false;
    state.deckBoot = "none";
    if (window.UFODeck && window.UFODeck.hasWebGL()) {
        state.deckBoot = "pending";
        bootDeckGL().then(() => {
            state.deckBoot = "ready";
        }).catch((err) => {
            console.warn("[v0.8] deck.gl boot failed, using legacy path:", err);
            state.useDeckGL = false;
            state.deckBoot = "failed";
            // Kick off the legacy path NOW that the boot has failed —
            // otherwise the user is staring at a blank map.
            if (state.activeTab === "observatory" || state.activeTab === "map") {
                loadMapMarkers();
            }
        });
    } else {
        console.info("[v0.8] WebGL unavailable or deck.js missing, legacy path");
    }

    state.markerLayer = L.markerClusterGroup({
        chunkedLoading: true,
        maxClusterRadius: 50,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        disableClusteringAtZoom: 14,
    });
    state.map.addLayer(state.markerLayer);

    // Heatmap layer (initially not added to map)
    state.heatLayer = L.heatLayer([], {
        radius: 18,
        blur: 22,
        maxZoom: 10,
        max: 1.0,
        gradient: {
            0.0: "#0d1b5e",
            0.2: "#1565c0",
            0.4: "#00acc1",
            0.6: "#4caf50",
            0.8: "#ffeb3b",
            1.0: "#ff1744",
        },
    });

    // Hex bin layer (v0.7) — initially not added to the map. Populated
    // by loadHexBins() from /api/hexbin which returns pre-computed H3
    // cell boundaries + counts. Stays a plain LayerGroup because each
    // cell is a small L.polygon and we clear + re-add on every load.
    state.hexLayer = L.layerGroup();

    // HUD readout wiring: mousemove over the map updates the top-right
    // LAT/LON readout so users get a live crosshair feel. Throttled by
    // the DOM update cost of setting textContent — no rAF needed.
    const hudLat = document.getElementById("hud-lat");
    const hudLon = document.getElementById("hud-lon");
    if (hudLat && hudLon) {
        state.map.on("mousemove", (e) => {
            hudLat.textContent = e.latlng.lat.toFixed(3) + "°";
            hudLon.textContent = e.latlng.lng.toFixed(3) + "°";
        });
        state.map.on("mouseout", () => {
            hudLat.textContent = "—";
            hudLon.textContent = "—";
        });
    }

    // Load data on move end, debounced + abortable. A casual pan fires
    // multiple moveend events in rapid succession; without this each one
    // triggered a full /api/map request and stale responses could arrive
    // out of order, overwriting fresh data with old markers.
    //
    // v0.8.0: When state.useDeckGL is true, the deck.gl LeafletLayer
    // renders every point on the GPU and pan/zoom is a zero-work
    // operation — no network, no SQL, no marker tree rebuild. The
    // schedule-reload path below only runs for the legacy fallback.
    let reloadSuppressed = false;
    let mapReloadTimer = null;
    let mapReloadAbort = null;

    function scheduleMapReload() {
        if (state.useDeckGL) return;  // deck.gl handles this for free
        // v0.8.0-cleanup: Also bail during the boot window. If deck.gl
        // is currently booting (state.deckBoot === "pending"), a pan
        // fired in the first ~1.5 s would otherwise trigger a legacy
        // /api/map request for a DB that's being hammered by the bulk
        // fetch. If deck.gl failed to boot (state.deckBoot === "failed"),
        // we DO need the legacy path — otherwise users get a blank map
        // forever.
        if (state.deckBoot === "pending") return;
        if (reloadSuppressed) return;
        clearTimeout(mapReloadTimer);
        mapReloadTimer = setTimeout(() => {
            // Cancel any in-flight request from a previous pan
            if (mapReloadAbort) mapReloadAbort.abort();
            mapReloadAbort = new AbortController();
            const signal = mapReloadAbort.signal;
            if (state.mapMode === "heatmap") loadHeatmap(signal);
            else if (state.mapMode === "hexbin") loadHexBins();
            else loadMapMarkers(signal);
        }, 200);
    }

    state.map.on("moveend", scheduleMapReload);
    state.map.on("popupopen", () => { reloadSuppressed = true; });
    state.map.on("popupclose", () => {
        reloadSuppressed = false;
        scheduleMapReload();
    });

    // v0.8.0-cleanup: Only fire the legacy initial-load path when the
    // deck.gl boot is definitely NOT in flight. Previously this
    // unconditionally fired loadMapMarkers() at init to "give the user
    // something to look at while the 4MB bulk data downloads", but
    // that meant every cold-start with a warm deck.gl boot hit Azure
    // front-door with an expensive /api/map zoom=4 whole-world query,
    // which routinely 502'd against a cold DB. The 502 didn't matter
    // functionally (deck.gl rendered anyway) but it looked like a bug
    // in DevTools and wasted gunicorn cycles.
    //
    // If the GPU boot is in progress (state.deckBoot === "pending"),
    // wait for the ScatterplotLayer to render instead. If the boot
    // already failed or was never attempted, fire the legacy path
    // immediately.
    if (state.deckBoot !== "pending") {
        loadMapMarkers();
    }
}

// v0.8.0 — Bulk-load the packed geocoded dataset and mount the deck.gl
// LeafletLayer on top of state.map. On success, the legacy marker /
// heat / hex layers are hidden and state.useDeckGL flips to true so
// scheduleMapReload() becomes a no-op. On any failure we leave the
// legacy path running. Safe to call multiple times.
async function bootDeckGL() {
    if (!window.UFODeck) throw new Error("deck.js not loaded");
    const UFODeck = window.UFODeck;

    // Wait for the deck.gl global to show up (loaded via <script defer>).
    await UFODeck.waitForDeck(40);

    // Fetch + deserialise in parallel with the legacy initial load.
    await UFODeck.loadBulkPoints();

    // v0.8.4 — seed deck.gl with the active theme BEFORE mounting the
    // layer, so the initial ScatterplotLayer instantiates with the
    // right palette instead of the signal default. Without this, a
    // user loading the page in DECLASS would briefly see cyan dots
    // before the setTheme() call later in this function re-renders
    // with the burgundy palette.
    if (typeof UFODeck.setTheme === "function") {
        UFODeck.setTheme(_currentTheme());
    }

    // Mount a LeafletLayer on the existing map. From now on, pan/zoom
    // is GPU-rendered with no network activity until the user clicks.
    UFODeck.mountDeckLayer(state.map, state.mapMode || "points");

    // v0.8.2-cleanup-2: REMOVE the legacy layers from the map instance
    // (not just clear their data). v0.8.0 only called clearLayers()
    // here, so the legacy markerLayer was still attached to the map —
    // anything that later re-fired loadMapMarkers() (loadObservatory()
    // on tab switch, the coords-filter change handler, certain
    // toggleMapMode() paths) would silently re-populate the legacy
    // layer and the numbered cluster bubbles would appear OVER the
    // deck.gl ScatterplotLayer dots. Removing the layer entirely
    // means nothing can render on top of deck.gl.
    if (state.markerLayer) {
        state.markerLayer.clearLayers();
        if (state.map.hasLayer(state.markerLayer)) {
            state.map.removeLayer(state.markerLayer);
        }
    }
    if (state.heatLayer) {
        if (state.heatLayer.setLatLngs) state.heatLayer.setLatLngs([]);
        if (state.map.hasLayer(state.heatLayer)) {
            state.map.removeLayer(state.heatLayer);
        }
    }
    if (state.hexLayer) {
        state.hexLayer.clearLayers();
        if (state.map.hasLayer(state.hexLayer)) {
            state.map.removeLayer(state.hexLayer);
        }
    }

    state.useDeckGL = true;

    // Apply the current filter state immediately so the initial paint
    // respects any #hash filters already in place.
    if (typeof applyClientFilters === "function") applyClientFilters();

    // v0.8.1 — if the TimeBrush was created before the bulk data
    // finished loading, wire its fast path + swap its histogram to
    // the client-computed one now.
    _wireTimeBrushToDeck();

    // v0.8.7 — populate the shape/color/emotion dropdowns and the
    // movement cluster from POINTS metadata, now that the bulk
    // buffer is ready. These dropdowns are intentionally empty in
    // the server-side /api/filters response because POINTS has the
    // canonical standardized lists (shape_source = "standardized").
    // Safe to call before the Observatory DOM exists — each helper
    // no-ops if its target element is missing, and re-runs when
    // the user first opens Observatory.
    if (typeof populateFilterDropdownsFromDeck === "function") {
        populateFilterDropdownsFromDeck();
    }

    // v0.8.7 — re-run mountQualityRail now that POINTS.ready is true.
    // The first call from loadObservatory() (if it ran before
    // bootDeckGL finished) rendered every toggle as disabled because
    // getCoverage() returned {}. Re-mounting picks up the real
    // coverage numbers and wires the change handlers. mountQualityRail
    // is idempotent (blanks innerHTML on every call).
    if (typeof mountQualityRail === "function") {
        mountQualityRail();
    }

    console.info("[v0.8] deck.gl layer mounted — GPU path active");
}

// v0.8.1 — Plug the TimeBrush into the GPU filter pipeline. Safe to
// call multiple times: the second call is a no-op if the fast path
// is already set. Called from bootDeckGL() and from loadObservatory()
// depending on which side of the race finishes first.
function _wireTimeBrushToDeck() {
    if (!state.timeBrush) return;
    if (!(state.useDeckGL && window.UFODeck && window.UFODeck.isReady())) return;
    if (state.timeBrush.deckFastPath) return;  // already wired

    state.timeBrush.useDeckFastPath((yearFrom, yearTo, cumulative) => {
        window.UFODeck.setTimeWindow(yearFrom, yearTo, { cumulative });
    });

    // Re-run ensureData() to swap the (potentially already-fetched)
    // server histogram for the client-computed one. If ensureData
    // has never run, the brush's first paint will use the client
    // path automatically.
    if (state.timeBrush.bins) {
        state.timeBrush.bins = window.UFODeck.getYearHistogram();
        state.timeBrush._draw?.();
    }
}

// Build a marker popup with cross-tab pivots. The popup has up to 3
// links: View Details (modal), View all in this city (Search filtered),
// and Drill into this month (Timeline monthly view). The latter two
// are only added when the marker has the requisite metadata.
function buildMarkerPopupHTML(m) {
    const loc = formatLocation(m.city, m.state, m.country);
    // Cross-tab pivot links — secondary actions, rendered as plain links.
    const links = [];
    if (m.city) {
        const cityArg = JSON.stringify(m.city).replace(/"/g, '&quot;');
        const stateArg = m.state ? JSON.stringify(m.state).replace(/"/g, '&quot;') : "''";
        const countryArg = m.country ? JSON.stringify(m.country).replace(/"/g, '&quot;') : "''";
        links.push(`<a href="#" class="popup-link" onclick="viewAllInCity(${cityArg}, ${stateArg}, ${countryArg}); return false;">View all in ${escapeHtml(m.city)} →</a>`);
    }
    if (m.date && m.date.length >= 7) {
        const ym = m.date.substring(0, 7);  // "YYYY-MM"
        links.push(`<a href="#" class="popup-link" onclick="drillToMonth('${ym}'); return false;">See ${escapeHtml(ym)} on the timeline →</a>`);
    }
    // v0.7.6: Most sightings ship with no description text. Show a small
    // badge so users can tell at a glance which markers have a written
    // narrative attached and which are coordinates-only.
    const descBadge = m.has_desc
        ? `<span class="popup-desc-badge has-desc" title="Sighting has a written description">[ DESC ]</span>`
        : `<span class="popup-desc-badge no-desc" title="No written description on record">[ NO DESC ]</span>`;
    return `
        <div class="popup">
            <div class="popup-date">${escapeHtml(m.date || "Unknown date")}</div>
            <div class="popup-loc">${escapeHtml(loc) || "Unknown location"}</div>
            <div class="popup-tags">${sourceBadge(m.source)} ${m.shape ? `<span class="shape-tag">${escapeHtml(m.shape)}</span>` : ""}</div>
            <div class="popup-desc-row">${descBadge}</div>
            <button type="button" class="popup-btn" onclick="openDetail(${m.id}); return false;">View Details →</button>
            ${links.length ? `<div class="popup-links">${links.join("")}</div>` : ""}
        </div>
    `;
}

// Map marker popup link handlers — exposed globally so the inline
// onclick attributes in the popup HTML can call them.
function viewAllInCity(city, state, country) {
    // City filter is not in the global filter bar (we'd need a free-text
    // city input). For now we use the search free-text "q" against the
    // structured location columns via state + country plus a city
    // qualifier in the search box. This is a deliberate compromise: it
    // lets users get most of the way to the result set without
    // requiring a city autocomplete UI.
    const updates = { q: city };
    if (state)   updates.state = state;
    if (country) updates.country = country;
    navigateToSearch(updates, true);
}
window.viewAllInCity = viewAllInCity;

function drillToMonth(yearMonth) {
    // v0.8.6: the Timeline tab no longer has a monthly drill-down
    // mode — it's a 3-card dashboard on the bulk buffer. Instead,
    // set the Observatory date filter to that month and flip to the
    // Observatory tab so the user sees the geographic distribution
    // for the month they clicked.
    const [year, month] = yearMonth.split("-");
    if (!year || !month) return;
    const lastDay = lastDayOfMonth(year, month);
    const fromField = document.getElementById("filter-date-from");
    const toField   = document.getElementById("filter-date-to");
    if (fromField) fromField.value = `${year}-${month}-01`;
    if (toField)   toField.value   = `${year}-${month}-${String(lastDay).padStart(2, "0")}`;
    switchTab("observatory");
    applyFilters();
}
window.drillToMonth = drillToMonth;

async function loadMapMarkers(signal) {
    const status = document.getElementById("map-status");
    const mapEl = document.getElementById("map");
    status.innerHTML = '<span class="loading-pulse">PLOTTING SIGHTINGS</span>';
    // Progressive: keep old markers visible but dimmed via the
    // is-loading-progressive class. The radar HUD overlays them.
    // Old markers do NOT get cleared until the new ones are ready —
    // see the clearLayers() call moved into the success branch below.
    mapEl?.classList.add("is-loading", "is-loading-progressive");
    ensureMapScanframe("PLOTTING / GRID LIVE");

    const bounds = state.map.getBounds();
    const params = getFilterParams();
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west", bounds.getWest().toFixed(4));
    params.set("east", bounds.getEast().toFixed(4));
    // Zoom drives the backend's sampling strategy: hash sample at low
    // zoom (even spread across the dataset), grid sample at high zoom
    // (even visual coverage across the viewport).
    params.set("zoom", state.map.getZoom());

    try {
        const data = await fetchJSON(`/api/map?${params}`, { signal });
        // Old markers stayed visible while we waited; clear + re-add
        // happens in the same tick so the user sees a swap, not a flash
        // of empty.
        state.markerLayer.clearLayers();

        const markers = data.markers.map(m => {
            const marker = L.circleMarker([m.lat, m.lng], {
                radius: 5,
                fillColor: sourceColor(m.source).bg,
                color: sourceColor(m.source).border,
                weight: 1,
                fillOpacity: 0.7,
            });

            marker.bindPopup(buildMarkerPopupHTML(m));

            return marker;
        });

        state.markerLayer.addLayers(markers);
        updateMapStatus(data.count, data.total_in_view, "markers");
    } catch (err) {
        if (isAbortError(err)) return;  // user moved the map again, fresh request will replace this
        document.getElementById("map-status").textContent = "Couldn't load sightings — check your connection or pan again to retry.";
        document.getElementById("btn-load-all").style.display = "none";
        console.error(err);
    } finally {
        mapEl?.classList.remove("is-loading", "is-loading-progressive");
        clearMapScanframe();
    }
}

// -------------------------------------------------------------------------
// Map HUD scan frame — corner brackets + label overlay shown while the
// map is loading. Created once, reused across every subsequent load.
// -------------------------------------------------------------------------
function ensureMapScanframe(label = "SCANNING") {
    const mapEl = document.getElementById("map");
    if (!mapEl) return;
    let frame = mapEl.querySelector(".map-scanframe");
    if (!frame) {
        frame = document.createElement("div");
        frame.className = "map-scanframe";
        frame.innerHTML = `
            <div class="mscf-tl"></div>
            <div class="mscf-tr"></div>
            <div class="mscf-bl"></div>
            <div class="mscf-br"></div>
            <div class="map-scanframe-label"></div>
        `;
        mapEl.appendChild(frame);
    }
    const labelEl = frame.querySelector(".map-scanframe-label");
    if (labelEl) labelEl.textContent = label;
}
function clearMapScanframe() {
    // We leave the node in place and let the CSS opacity transition on
    // #map.is-loading → :not(.is-loading) fade it out. Removing it would
    // kill the fade.
}

// =========================================================================
// Map Status & Load All
// =========================================================================
function updateMapStatus(loaded, total, unit) {
    const status = document.getElementById("map-status");
    const loadAllBtn = document.getElementById("btn-load-all");

    if (loaded < total) {
        // Truncated — show "X of Y" and Load All button
        status.textContent = `${loaded.toLocaleString()} of ${total.toLocaleString()} ${unit}`;
        loadAllBtn.textContent = `Load All ${total.toLocaleString()}`;
        loadAllBtn.style.display = "block";
    } else {
        // All data loaded
        status.textContent = `${total.toLocaleString()} ${unit}`;
        loadAllBtn.style.display = "none";
    }
}

async function loadAll() {
    const btn = document.getElementById("btn-load-all");
    const status = document.getElementById("map-status");
    // When the button is in the "confirming" state, its label has been
    // swapped to "Tap again…" — parse the count from the dataset we
    // stashed, not the label.
    const totalText = (btn.dataset.total || btn.textContent.replace(/[^0-9]/g, ""));
    const total = parseInt(totalText) || 100000;

    // Inline confirmation for very large cluster loads — replaces the
    // jarring native confirm() dialog. First click swaps the button into
    // a "Tap again to load NN,NNN" state for 3 seconds; second click
    // proceeds. No second click = revert to original label.
    if (total > 30000 && state.mapMode === "points" && !btn.classList.contains("confirming")) {
        btn.dataset.total = String(total);
        btn.dataset.originalText = btn.textContent;
        btn.classList.add("confirming");
        btn.textContent = `Tap again to load ${total.toLocaleString()}`;
        btn.dataset.confirmTimer = String(setTimeout(() => {
            btn.classList.remove("confirming");
            btn.textContent = btn.dataset.originalText || `Load All ${total.toLocaleString()}`;
            delete btn.dataset.confirmTimer;
        }, 3000));
        return;
    }

    // User confirmed (or the load is small enough to skip confirmation)
    if (btn.dataset.confirmTimer) {
        clearTimeout(parseInt(btn.dataset.confirmTimer));
        delete btn.dataset.confirmTimer;
    }
    btn.classList.remove("confirming");
    btn.style.display = "none";
    status.textContent = `Loading all ${total.toLocaleString()}...`;

    const bounds = state.map.getBounds();
    const params = getFilterParams();
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west", bounds.getWest().toFixed(4));
    params.set("east", bounds.getEast().toFixed(4));
    params.set("zoom", state.map.getZoom());
    params.set("limit", total);

    try {
        if (state.mapMode === "heatmap") {
            const data = await fetchJSON(`/api/heatmap?${params}`);
            state.heatLayer.setLatLngs(data.points);
            status.textContent = `${data.count.toLocaleString()} of ${data.total_in_view.toLocaleString()} points (full)`;
        } else {
            const data = await fetchJSON(`/api/map?${params}`);
            state.markerLayer.clearLayers();

            const markers = data.markers.map(m => {
                const marker = L.circleMarker([m.lat, m.lng], {
                    radius: 5,
                    fillColor: sourceColor(m.source).bg,
                    color: sourceColor(m.source).border,
                    weight: 1,
                    fillOpacity: 0.7,
                });
                marker.bindPopup(buildMarkerPopupHTML(m));
                return marker;
            });
            state.markerLayer.addLayers(markers);
            status.textContent = `${data.count.toLocaleString()} of ${data.total_in_view.toLocaleString()} markers (full)`;
        }
    } catch (err) {
        status.textContent = "Error loading data";
        console.error(err);
    }
}

// =========================================================================
// Map Mode Toggle (Points / Heatmap / HexBin)
// -------------------------------------------------------------------------
// Three modes: "points" = markercluster layer, "heatmap" = leaflet.heat,
// "hexbin" = L.layerGroup of L.polygon H3 cells from /api/hexbin.
// The v0.7 Observatory mode toggle calls this with .mode-btn instead of
// .map-mode-btn, so we update both selectors for backward compat.
// =========================================================================
function toggleMapMode(mode) {
    if (mode === state.mapMode) return;
    const prev = state.mapMode;
    state.mapMode = mode;

    // Update toggle button styles (both old .map-mode-btn AND new .mode-btn)
    document.querySelectorAll(".map-mode-btn, .mode-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === mode);
    });

    // v0.8.0: On the GPU path, swapping modes is a single setProps()
    // call on the deck.gl LeafletLayer. No layer add/remove, no SQL.
    if (state.useDeckGL && window.UFODeck && window.UFODeck.isReady()) {
        window.UFODeck.setDeckMode(mode);
        return;
    }

    // v0.8.2-cleanup-2: Bail on the legacy add-and-fetch sequence
    // entirely if deck.gl is in the boot window. The user will see
    // the cyan dots in a moment; we don't need to flash the legacy
    // marker cluster bubbles in the meantime.
    if (state.deckBoot === "pending") return;

    // Legacy fallback — remove whichever Leaflet layer was active, add
    // the new one, kick off the matching server query.
    if (prev === "heatmap")  state.map.removeLayer(state.heatLayer);
    else if (prev === "hexbin") state.map.removeLayer(state.hexLayer);
    else                      state.map.removeLayer(state.markerLayer);

    if (mode === "heatmap") {
        state.map.addLayer(state.heatLayer);
        loadHeatmap();
    } else if (mode === "hexbin") {
        state.map.addLayer(state.hexLayer);
        loadHexBins();
    } else {
        // "points" — the legacy clusters mode
        state.map.addLayer(state.markerLayer);
        loadMapMarkers();
    }
}

// v0.8.0 — Client-side filter pipeline. Reads the current filter form
// values, builds a filter descriptor, calls UFODeck.applyClientFilters
// to rebuild the visible index, and refreshes the active deck.gl layer.
// Returns true if the GPU path handled the filter, false if the caller
// should fall through to the legacy per-endpoint reloads.
//
// v0.8.2 extends the filter object with the quality rail state:
// qualityMin / hoaxMax / hasDescription / hasMedia / colorName /
// emotionName. These ride on top of the v0.8.0 source/shape/year/
// bbox filters and compose inside UFODeck._rebuildVisible.
function applyClientFilters() {
    if (!(state.useDeckGL && window.UFODeck && window.UFODeck.isReady())) {
        return false;
    }
    const q = state.qualityFilter || {};
    const filter = {
        sourceName: document.getElementById("filter-source")?.value || null,
        shapeName:  document.getElementById("filter-shape")?.value  || null,
        colorName:  document.getElementById("filter-color")?.value  || null,
        emotionName: document.getElementById("filter-emotion")?.value || null,
        // v0.8.7 — multi-select movement category cluster. Array of
        // category names (OR-semantics bit mask in _rebuildVisible).
        movementCats: _readMovementCats(),
        yearFrom:   _parseYearFilter(document.getElementById("filter-date-from")?.value),
        yearTo:     _parseYearFilter(document.getElementById("filter-date-to")?.value),
        bbox: null,  // deck.gl clips by viewport automatically, skip the CPU cull
        qualityMin:  q.highQuality ? (q.qualityThreshold || 60) : null,
        hoaxMax:     q.hideHoaxes   ? (q.hoaxThreshold || 50)    : null,
        hasDescription: q.hasDescription,
        hasMedia:       q.hasMedia,
        // v0.8.5 — v0.8.3b movement classification (boolean: any bit)
        hasMovement:    q.hasMovement,
    };
    window.UFODeck.applyClientFilters(filter);
    window.UFODeck.refreshActiveLayer();

    // v0.8.6 — retally the brush histogram against the new visible
    // set. Uses the filtered variant of getYearHistogram so the user
    // sees the current filter's shape over time without a server trip.
    // When the filter is "nothing active" we clear the overlay so
    // the cached unfiltered histogram draws alone.
    if (state.timeBrush && typeof window.UFODeck.getYearHistogramForVisible === "function") {
        const activeFilterCount = _countActiveFilters(filter);
        if (activeFilterCount > 0) {
            state.timeBrush.retally(window.UFODeck.getYearHistogramForVisible());
        } else {
            state.timeBrush.retally(null);
        }
    }

    // v0.8.6 — if the user is on the Timeline or Insights tab, refresh
    // the cards so they reflect the current filter state. This is
    // cheap (all client-side) so no debounce needed.
    if (state.activeTab === "timeline" && typeof refreshTimelineCards === "function") {
        refreshTimelineCards();
    }
    if (state.activeTab === "insights" && typeof refreshInsightsClientCards === "function") {
        refreshInsightsClientCards();
    }

    return true;
}

// v0.8.6 — true if any field of the filter descriptor would reduce
// the visible set. Used to decide whether to overlay the brush
// histogram with the filtered-vs-ghost pair or draw the unfiltered
// bins alone.
function _countActiveFilters(f) {
    if (!f) return 0;
    let n = 0;
    if (f.sourceName) n++;
    if (f.shapeName) n++;
    if (f.colorName) n++;
    if (f.emotionName) n++;
    // v0.8.7 — movement cluster counts as one active filter
    // regardless of how many categories are checked.
    if (Array.isArray(f.movementCats) && f.movementCats.length) n++;
    if (f.yearFrom != null) n++;
    if (f.yearTo != null) n++;
    if (f.qualityMin != null) n++;
    if (f.hoaxMax != null) n++;
    if (f.hasDescription !== undefined && f.hasDescription !== null) n++;
    if (f.hasMedia !== undefined && f.hasMedia !== null) n++;
    if (f.hasMovement !== undefined && f.hasMovement !== null) n++;
    return n;
}

// Pull a year integer out of whatever the user typed into a date-range
// input. Accepts "2005", "2005-06-14", "" — anything non-parseable
// returns null so applyClientFilters treats it as "no filter".
function _parseYearFilter(raw) {
    if (!raw) return null;
    const m = String(raw).match(/^(\d{1,4})/);
    if (!m) return null;
    const y = parseInt(m[1], 10);
    return Number.isFinite(y) ? y : null;
}
// Legacy alias — the old "clusters" name maps to the new "points" mode.
function _modeAlias(mode) { return mode === "clusters" ? "points" : mode; }

// =========================================================================
// Observatory (v0.7) — unified dashboard for map + timeline
// -------------------------------------------------------------------------
// Entry point called from switchTab("observatory"). Idempotent: first
// call mounts the rail, binds the mode toggle, and creates the TimeBrush.
// Subsequent calls just invalidateSize() on the Leaflet map so it
// reflows after a panel switch.
// =========================================================================

function loadObservatory() {
    // Leaflet needs an explicit invalidateSize when its container was
    // hidden via display:none (which .panel:not(.active) does). Delay
    // one tick so the browser has applied the class change.
    setTimeout(() => {
        if (state.map) state.map.invalidateSize();
    }, 50);

    if (!state.observatoryMounted) {
        mountObservatoryRail();
        wireObservatoryModeToggle();
        state.observatoryMounted = true;
    }

    // Initialize the time brush lazily on first Observatory visit.
    if (!state.timeBrush) {
        const canvas = document.getElementById("brush-canvas");
        if (canvas) {
            state.timeBrush = new TimeBrush(canvas, onBrushWindowChange);
            state.timeBrush.ensureData();
            // v0.8.1 — wire the GPU fast path. If the bulk data
            // hasn't finished loading yet, bootDeckGL() calls this
            // again from its success branch, so we cover both
            // race orderings.
            _wireTimeBrushToDeck();
        }
    }

    // v0.8.2-cleanup-2: Only fire the legacy server-fetch path when
    // deck.gl is NOT handling rendering. With the GPU path active
    // (or in the middle of booting), the deck.gl ScatterplotLayer
    // already shows everything from the in-memory bulk dataset and
    // the legacy markerCluster layer would just stack numbered
    // bubbles on top — exactly the bug the user caught after
    // v0.8.2 shipped.
    if (state.useDeckGL || state.deckBoot === "pending") {
        // GPU path is in charge. Just make sure the current filter
        // state is reflected in the visible index — applyClientFilters
        // is a no-op when bulk data hasn't loaded yet, so this is safe
        // to call regardless of boot stage.
        if (typeof applyClientFilters === "function") applyClientFilters();
    } else {
        if (state.mapMode === "heatmap") loadHeatmap();
        else if (state.mapMode === "hexbin") loadHexBins();
        else loadMapMarkers();
    }
}

function wireObservatoryModeToggle() {
    document.querySelectorAll(".mode-toggle .mode-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const mode = btn.dataset.mode;
            if (mode) toggleMapMode(mode);
        });
    });

    // v0.8.1 — Sliding / Cumulative toggle for the PLAY button.
    // Clicking cycles between the two modes and updates the button
    // label. The brush picks up the new mode on the next play click.
    const modeBtn = document.getElementById("brush-mode");
    if (modeBtn) {
        modeBtn.addEventListener("click", () => {
            if (!state.timeBrush) return;
            const next = state.timeBrush.playMode === "sliding"
                ? "cumulative" : "sliding";
            state.timeBrush.setPlayMode(next);
        });
    }

    // v0.8.2-cleanup-2 — Playback speed selector. The TimeBrush
    // multiplies its per-frame stepSize by playSpeed inside togglePlay,
    // so changing this number live takes effect on the very next
    // playback (and during an in-progress playback if applied while
    // playing — though changing speed mid-play is jittery, so we
    // recommend pausing first).
    const speedSel = document.getElementById("brush-speed");
    if (speedSel) {
        speedSel.addEventListener("change", (e) => {
            if (!state.timeBrush) return;
            const v = parseFloat(e.target.value);
            if (Number.isFinite(v) && v > 0) {
                state.timeBrush.playSpeed = v;
            }
        });
    }
}

// Populate the left rail from /api/filters (already fetched at boot)
// and from /api/stats by-source counts. Rail checkboxes mirror the
// existing filter dropdowns one-way: toggling a rail checkbox writes
// into #filter-source / #filter-shape and calls applyFilters().
// First version supports single-select (matches the existing <select>
// semantics); multi-select is a future improvement.
function mountObservatoryRail() {
    const srcList = document.getElementById("rail-source-list");
    const shapeList = document.getElementById("rail-shape-list");
    if (!srcList || !shapeList) return;

    // Source list: read options from #filter-source (already populated
    // at boot from /api/filters).
    const srcSelect = document.getElementById("filter-source");
    srcList.innerHTML = "";
    if (srcSelect) {
        for (const opt of srcSelect.options) {
            if (!opt.value) continue;  // skip "All"
            const li = document.createElement("li");
            li.innerHTML = `
                <input type="checkbox" id="rail-src-${opt.value}"
                       data-src-id="${opt.value}" checked>
                <label for="rail-src-${opt.value}">${escapeHtml(opt.text)}</label>
            `;
            srcList.appendChild(li);
            li.querySelector("input").addEventListener("change", (e) => {
                // Single-source select semantics: ticking one checks it,
                // unticking reverts to "all". Real multi-select would
                // need backend support for IN lists.
                if (e.target.checked) {
                    srcSelect.value = opt.value;
                    // Uncheck all other rail source checkboxes to mirror
                    // the single-value state of the underlying <select>.
                    srcList.querySelectorAll("input[data-src-id]").forEach(c => {
                        if (c !== e.target) c.checked = false;
                    });
                } else {
                    srcSelect.value = "";
                }
                applyFilters();
            });
        }
    }

    // Shape list: same story for shapes.
    const shapeSelect = document.getElementById("filter-shape");
    shapeList.innerHTML = "";
    if (shapeSelect) {
        for (const opt of shapeSelect.options) {
            if (!opt.value) continue;
            const li = document.createElement("li");
            li.innerHTML = `
                <input type="checkbox" id="rail-shape-${opt.value}"
                       data-shape="${opt.value}" checked>
                <label for="rail-shape-${opt.value}">${escapeHtml(opt.text)}</label>
            `;
            shapeList.appendChild(li);
            li.querySelector("input").addEventListener("change", (e) => {
                if (e.target.checked) {
                    shapeSelect.value = opt.value;
                    shapeList.querySelectorAll("input[data-shape]").forEach(c => {
                        if (c !== e.target) c.checked = false;
                    });
                } else {
                    shapeSelect.value = "";
                }
                applyFilters();
            });
        }
    }

    // v0.8.2 — mount the Data Quality rail. Safe to call multiple
    // times; idempotent by element id.
    mountQualityRail();
}

// v0.8.2 — Data Quality rail with "High quality only", "Hide likely
// hoaxes", "Has description", "Has media" toggles. Each toggle writes
// into state.qualityFilter and triggers applyFilters() which routes
// through applyClientFilters() → UFODeck.applyClientFilters().
//
// Unpopulated toggles (the derived column exists in the schema but no
// rows have data yet, or the column doesn't exist at all) render
// disabled with a tooltip explaining why.
function mountQualityRail() {
    const host = document.getElementById("rail-quality-list");
    if (!host) return;

    // v0.8.7: the old `dataset.mounted` idempotent guard has been
    // removed. The first call from loadObservatory() runs BEFORE
    // bootDeckGL() completes, which means getCoverage() returns {}
    // and every toggle gets populated=false → cursor:not-allowed +
    // permanently disabled event handlers. Removing the guard lets
    // _wireTimeBrushToDeck() re-run mountQualityRail after the bulk
    // buffer lands, picking up the real coverage numbers and wiring
    // the change handlers. `host.innerHTML = ""` below blanks the
    // list on every call, so re-mounting is safe.

    state.qualityFilter = state.qualityFilter || {
        highQuality: false,
        hideHoaxes: false,
        hasDescription: null,
        hasMedia: null,
        hasMovement: null,  // v0.8.5 — v0.8.3b movement classification
    };

    // v0.8.7: gate on POINTS.ready too, not just the presence of
    // the function. On a WebGL-disabled browser UFODeck might exist
    // but never flip to ready; we want the rail to render as
    // disabled in that case instead of silently mounting with
    // coverage={}.
    const coverage = (
        window.UFODeck
        && typeof window.UFODeck.getCoverage === "function"
        && window.UFODeck.POINTS
        && window.UFODeck.POINTS.ready
    ) ? window.UFODeck.getCoverage() : {};
    const cov = (key) => (coverage[key] || 0);

    // Threshold semantics locked by the brief:
    //   "High quality only"   → quality_score >= 60
    //   "Hide likely hoaxes"  → hoax_score <= 50  (the server packs
    //                           hoax_likelihood × 100 into a uint8, so
    //                           50/100 == 0.5 hoax_likelihood from the
    //                           ufo-dedup REAL column)
    const QUALITY_THRESHOLD = 60;
    const HOAX_THRESHOLD = 50;

    const toggles = [
        {
            key: "highQuality",
            label: "High quality only",
            sub: `score ≥ ${QUALITY_THRESHOLD}`,
            coverageKey: "quality_score",
        },
        {
            key: "hideHoaxes",
            label: "Hide likely hoaxes",
            sub: `score > ${HOAX_THRESHOLD / 100}`,
            coverageKey: "hoax_score",
        },
        {
            key: "hasDescription",
            label: "Has description",
            sub: "narrative text",
            coverageKey: "has_description",
        },
        {
            key: "hasMedia",
            label: "Has media",
            sub: "photo / video reference",
            coverageKey: "has_media",
        },
        // v0.8.5 — v0.8.3b movement classification. Filters to rows
        // whose narrative mentioned any of the 10 movement categories
        // (hovering, linear, erratic, accelerating, rotating,
        // ascending, descending, vanished, followed, landed).
        {
            key: "hasMovement",
            label: "Has movement described",
            sub: "hovering / landing / erratic / …",
            coverageKey: "has_movement",
        },
    ];

    host.innerHTML = "";
    for (const t of toggles) {
        const populated = cov(t.coverageKey) > 0;
        const li = document.createElement("li");
        li.className = populated ? "" : "rail-toggle-disabled";
        const id = `rail-q-${t.key}`;
        const disabled = populated ? "" : " disabled";
        const tooltip = populated
            ? ""
            : ` title="No rows have ${t.coverageKey} populated yet. Run the ufo-dedup analysis pipeline and re-migrate to enable this filter."`;
        li.innerHTML = `
            <input type="checkbox" id="${id}" data-qkey="${t.key}"${disabled}${tooltip}>
            <label for="${id}"${tooltip}>
                ${escapeHtml(t.label)}
                <span class="rail-toggle-sub">${escapeHtml(t.sub)}</span>
            </label>
        `;
        host.appendChild(li);
        const input = li.querySelector("input");
        if (populated) {
            input.addEventListener("change", (e) => {
                const key = e.target.dataset.qkey;
                if (key === "highQuality") {
                    state.qualityFilter.highQuality = e.target.checked;
                } else if (key === "hideHoaxes") {
                    state.qualityFilter.hideHoaxes = e.target.checked;
                } else if (key === "hasDescription") {
                    state.qualityFilter.hasDescription = e.target.checked ? true : null;
                } else if (key === "hasMedia") {
                    state.qualityFilter.hasMedia = e.target.checked ? true : null;
                } else if (key === "hasMovement") {
                    // v0.8.5 — bit 2 of the flags byte
                    state.qualityFilter.hasMovement = e.target.checked ? true : null;
                }
                applyFilters();
            });
        }
    }

    // Expose the thresholds for applyClientFilters() to read when
    // building the filter descriptor.
    state.qualityFilter.qualityThreshold = QUALITY_THRESHOLD;
    state.qualityFilter.hoaxThreshold = HOAX_THRESHOLD;
}

// =========================================================================
// Hex bin rendering (v0.7)
// =========================================================================
//
// loadHexBins() fetches pre-computed H3 cells from /api/hexbin and draws
// each as a Leaflet polygon. The color ramp mirrors the mockup's cold→hot
// gradient (void → plasma → amber → hot). Cells with counts near the
// dataset max get warm colors; cells near the min stay cold.
//
// Graceful degradation: if the backend returns 503 (MV not populated
// yet) we disable the HexBin toggle and fall back to Points mode so
// the user never sees an empty canvas.

const HEX_RAMP = [
    [0.00, [0, 59, 92]],     // cold plasma
    [0.25, [0, 140, 180]],
    [0.50, [0, 240, 255]],   // hot plasma
    [0.75, [255, 179, 0]],   // amber
    [1.00, [255, 78, 0]],    // hot
];

function _sampleRamp(t) {
    t = Math.max(0, Math.min(1, t));
    for (let i = 1; i < HEX_RAMP.length; i++) {
        if (t <= HEX_RAMP[i][0]) {
            const [t0, c0] = HEX_RAMP[i - 1];
            const [t1, c1] = HEX_RAMP[i];
            const k = (t - t0) / (t1 - t0);
            return [
                Math.round(c0[0] + (c1[0] - c0[0]) * k),
                Math.round(c0[1] + (c1[1] - c0[1]) * k),
                Math.round(c0[2] + (c1[2] - c0[2]) * k),
            ];
        }
    }
    return HEX_RAMP[HEX_RAMP.length - 1][1];
}
function _rgb(c) { return `rgb(${c[0]}, ${c[1]}, ${c[2]})`; }

// Build a pointy-top hexagon polygon (list of [lat, lng] vertices)
// centered on (cLat, cLng) for a honeycomb tessellation. `sizeDeg`
// is the horizontal center-to-center spacing between hexes in the
// same row (matches `hex_h` in the backend bucketing); the circum-
// radius (center-to-vertex) is sizeDeg / sqrt(3). Returns 6
// vertices — Leaflet closes the loop.
//
// v0.7.7: Earlier passes drew hexes inscribed in a square grid
// (r = sizeDeg/2) which looked crisp but left diagonal gaps — a
// honeycomb needs offset rows with odd rows shifted by sizeDeg/2,
// and the circumradius must be sizeDeg/sqrt(3) so the flat-to-flat
// width equals the horizontal spacing. The backend now computes
// that offset-row bucketing so adjacent cells share edges.
//
// No Mercator latitude correction: at high latitudes (e.g. > 60°N)
// the hexes will read as slightly squashed horizontally because
// one degree of longitude is narrower than one degree of latitude
// on screen, but that's an honest visual trade for preserving the
// tessellation across the whole viewport.
function _hexPolygonAround(cLat, cLng, sizeDeg) {
    const R = sizeDeg / Math.sqrt(3);   // circumradius in degrees
    const pts = [];
    for (let i = 0; i < 6; i++) {
        // Pointy-top: vertices at 30°, 90°, 150°, 210°, 270°, 330°
        const angle = (Math.PI / 3) * i + Math.PI / 6;
        pts.push([
            cLat + R * Math.sin(angle),
            cLng + R * Math.cos(angle),
        ]);
    }
    return pts;
}

async function loadHexBins() {
    const status = document.getElementById("map-status");
    const hudStatus = document.getElementById("hud-status");
    if (!state.map) return;

    // v0.7.3: the endpoint now computes bins on the fly in SQL with
    // the same add_common_filters() helper used by /api/map, so
    // country + date filters work out of the box. No more fallback
    // to Heatmap, no more 503 state, no more pre-compute setup.

    const bounds = state.map.getBounds();
    const zoom = state.map.getZoom();
    const params = getFilterParams();
    params.set("zoom", zoom);
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west",  bounds.getWest().toFixed(4));
    params.set("east",  bounds.getEast().toFixed(4));

    if (status) status.textContent = "BUILDING HEX GRID";
    if (hudStatus) hudStatus.textContent = "BUILDING HEX GRID";

    try {
        const data = await fetchJSON(`/api/hexbin?${params}`);
        state.hexLayer.clearLayers();

        const cells = data.cells || [];
        const size = Number(data.size) || 2.0;

        if (cells.length === 0) {
            if (status) status.textContent = "0 hex cells in view";
            if (hudStatus) hudStatus.textContent = "NO DATA";
            document.getElementById("rail-visible-count").textContent = "0";
            return;
        }

        // Log scale on the color ramp so one huge cell doesn't wash
        // out the rest. A single-cell bucket still gets a warm tint.
        let max = 0;
        for (const c of cells) if (c.cnt > max) max = c.cnt;
        const logMax = Math.log(max + 1);

        let total = 0;
        for (const c of cells) {
            total += c.cnt;
            const t = Math.log(c.cnt + 1) / logMax;
            const color = _rgb(_sampleRamp(t));
            const ring = _hexPolygonAround(c.lat, c.lng, size);
            const polygon = L.polygon(ring, {
                color: color,
                weight: 1,
                opacity: 0.85,
                fillColor: color,
                fillOpacity: 0.5,
            });
            polygon.bindTooltip(
                `<div class="hex-tooltip">${c.cnt.toLocaleString()} sightings</div>`,
                { sticky: true },
            );
            polygon.addTo(state.hexLayer);
        }

        document.getElementById("rail-visible-count").textContent = total.toLocaleString();
        if (status) status.textContent = `${cells.length.toLocaleString()} hex cells (${total.toLocaleString()} sightings)`;
        if (hudStatus) hudStatus.textContent = "READY";
    } catch (err) {
        console.error("loadHexBins error:", err);
        if (status) status.textContent = "Couldn't load hex bins — " + (err.message || err);
        if (hudStatus) hudStatus.textContent = "ERROR";
    }
}

// =========================================================================
// Small toast helper — used by the hex-bin country-filter fall-back
// and future warnings. Appends a transient pill to the top-right corner
// that auto-dismisses after 3 s.
// =========================================================================
function showToast(msg, ms = 3000) {
    let host = document.getElementById("toast-host");
    if (!host) {
        host = document.createElement("div");
        host.id = "toast-host";
        host.style.cssText = "position:fixed;top:70px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none";
        document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast";
    el.style.cssText = "background:var(--bg-panel);border:1px solid var(--accent);color:var(--text);font-family:var(--font-mono);font-size:11px;padding:8px 14px;border-radius:4px;box-shadow:0 4px 12px rgba(0,0,0,0.4);opacity:0;transform:translateY(-6px);transition:opacity 180ms,transform 180ms;pointer-events:auto;max-width:320px";
    el.textContent = msg;
    host.appendChild(el);
    requestAnimationFrame(() => {
        el.style.opacity = "1";
        el.style.transform = "none";
    });
    setTimeout(() => {
        el.style.opacity = "0";
        el.style.transform = "translateY(-6px)";
        setTimeout(() => el.remove(), 200);
    }, ms);
}

// =========================================================================
// TimeBrush — draggable time window + histogram + key sighting annotations
// -------------------------------------------------------------------------
// Lives at the bottom of the Observatory panel. Fetches the full-range
// monthly histogram once from /api/timeline?bins=monthly&full_range=1
// and the key sightings list from static/data/key_sightings.json.
//
// Dragging the window or its handles updates state.filters.date_from/to
// (debounced 300 ms) and triggers a map reload via applyFilters().
// Play button auto-scrubs the window forward.
// =========================================================================

// v0.7.2: brush starts at 1900 instead of 1947 so the full modern
// sighting era is visible (the database has records back to the
// mid-1800s but 1900+ is where the curve becomes meaningful, and
// 1947 arbitrarily cut off the pre-Roswell context). BRUSH_MAX_YEAR
// uses the current year + 1 so in-progress year bars are visible.
const BRUSH_MIN_YEAR = 1900;
const BRUSH_MAX_YEAR = new Date().getFullYear() + 1;

class TimeBrush {
    constructor(canvas, onChange) {
        this.canvas = canvas;
        this.ctx = canvas.getContext("2d");
        // v0.8.6: keep a RAW reference alongside the debounced one so
        // pointerup / togglePlay can commit without waiting out the
        // 300ms window. The debounced `onChange` is still used by
        // programmatic callers (hash parsing, setWindow) where
        // coalescing rapid calls is the right behaviour.
        this._onChangeRaw = onChange;
        this.onChange = debounce(onChange, 300);
        this.minT = Date.UTC(BRUSH_MIN_YEAR, 0, 1);
        this.maxT = Date.UTC(BRUSH_MAX_YEAR, 0, 1);
        // Default window covers the entire range so nothing's filtered
        // until the user drags.
        this.window = [this.minT, this.maxT];
        this.bins = null;
        // v0.8.6: filtered overlay bins set by retally() after
        // applyClientFilters. When non-null, _draw() prefers this
        // over `bins` so the brush histogram always shows the
        // currently visible set.
        this.binsFiltered = null;
        this.annotations = null;
        this.playing = false;
        this.playRaf = null;
        this.windowEl = document.getElementById("brush-window");
        this.annEl = document.getElementById("brush-annotations");

        // v0.8.1 — GPU fast path for the PLAY loop. When set by
        // app.js after bootDeckGL() succeeds, togglePlay()'s per-
        // frame step calls this function directly instead of going
        // through the debounced onChange → applyFilters → form-input
        // pipeline. Smooth 60 fps playback instead of 3 fps.
        this.deckFastPath = null;
        this.playMode = "sliding";    // "sliding" | "cumulative"
        this.playSpeed = 1.0;         // reserved for a future speed toggle
        this._cumulativeLeft = null;  // memoised leftEdge during cumulative play

        this._bindEvents();
    }

    // v0.8.1 — call once from app.js when UFODeck.isReady() flips
    // true. The argument is (yearFrom, yearTo, cumulative) => void.
    useDeckFastPath(fn) {
        this.deckFastPath = fn;
    }

    // v0.8.1 — toggle cumulative vs sliding playback. Called from
    // the new #brush-mode button. Does nothing if the brush is
    // already in the requested mode.
    setPlayMode(mode) {
        if (mode !== "sliding" && mode !== "cumulative") return;
        if (mode === this.playMode) return;
        this.playMode = mode;
        this._cumulativeLeft = null;  // reset memoised leftEdge
        const btn = document.getElementById("brush-mode");
        if (btn) {
            btn.dataset.mode = mode;
            btn.textContent = mode === "cumulative" ? "CUMULATIVE" : "SLIDING";
            btn.setAttribute("aria-pressed", mode === "cumulative" ? "true" : "false");
        }
    }

    async ensureData() {
        // v0.8.1 — if the bulk dataset is already loaded, compute the
        // histogram client-side instead of hitting /api/timeline.
        // Saves ~150 ms on Observatory mount and removes one of the
        // last per-session server queries.
        const haveDeck =
            typeof window.UFODeck !== "undefined" &&
            window.UFODeck.POINTS &&
            window.UFODeck.POINTS.ready &&
            typeof window.UFODeck.getYearHistogram === "function";

        if (haveDeck) {
            // Client path — walk POINTS.year once in JS, cache forever.
            this.bins = window.UFODeck.getYearHistogram();
            // Annotations still come from a static JSON file.
            const annResp = await fetch("/static/data/key_sightings.json").catch(() => null);
            if (annResp && annResp.ok) {
                this.annotations = await annResp.json();
            }
        } else {
            // Legacy path — same as v0.8.0.
            const [histResp, annResp] = await Promise.all([
                fetch("/api/timeline?bins=monthly&full_range=1").catch(() => null),
                fetch("/static/data/key_sightings.json").catch(() => null),
            ]);
            if (histResp && histResp.ok) {
                const data = await histResp.json();
                this.bins = this._collapseTimelineToMonthly(data);
            }
            if (annResp && annResp.ok) {
                this.annotations = await annResp.json();
            }
        }

        this._resize();
        this._draw();
        this._drawAnnotations();
        this._syncWindow();
    }

    // /api/timeline's default shape groups by year across sources. For
    // the brush we flatten it to total-per-year and treat that as the
    // histogram. When the backend supports monthly binning we'll swap
    // this out. Returns [{year, count}].
    _collapseTimelineToMonthly(data) {
        const rows = data?.data || {};
        const out = [];
        for (const year of Object.keys(rows).sort()) {
            const sourceMap = rows[year] || {};
            let total = 0;
            for (const k of Object.keys(sourceMap)) total += Number(sourceMap[k] || 0);
            out.push({ year: parseInt(year, 10), count: total });
        }
        return out;
    }

    _resize() {
        const r = this.canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        this.canvas.width = Math.max(1, Math.round(r.width * dpr));
        this.canvas.height = Math.max(1, Math.round(r.height * dpr));
        this.w = r.width;
        this.h = r.height;
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _draw() {
        const ctx = this.ctx;
        const w = this.w, h = this.h;
        if (!w || !h) return;
        ctx.clearRect(0, 0, w, h);

        // Read theme tokens so the histogram inherits SIGNAL or DECLASS colors.
        const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";
        const accentDim = getComputedStyle(document.body).getPropertyValue("--fg-dim").trim() || "#8A94AD";
        const line = getComputedStyle(document.body).getPropertyValue("--border").trim() || "#13203A";

        // v0.8.6: always draw the unfiltered bins as a dim "ghost"
        // background so the user sees the full dataset shape even
        // when filters are active. The filtered bins draw on top in
        // the full accent color. When nothing is filtered, only the
        // foreground draws (ghost is skipped because binsFiltered is
        // null).
        const fgBins = this.binsFiltered || this.bins;
        const ghostBins = this.binsFiltered ? this.bins : null;

        if (!fgBins || fgBins.length === 0) {
            // Empty state
            ctx.fillStyle = line;
            ctx.fillRect(0, h - 1, w, 1);
            return;
        }

        const yearSpan = BRUSH_MAX_YEAR - BRUSH_MIN_YEAR;
        // Scale against the unfiltered max so filtered bars shrink
        // visibly rather than renormalising to fill the pane.
        let max = 1;
        const scaleBins = this.bins || fgBins;
        for (const b of scaleBins) if (b.count > max) max = b.count;

        const barW = Math.max(1, w / yearSpan);

        // Ghost (unfiltered) layer
        if (ghostBins) {
            ctx.fillStyle = accentDim;
            ctx.globalAlpha = 0.25;
            for (const b of ghostBins) {
                if (b.year < BRUSH_MIN_YEAR || b.year > BRUSH_MAX_YEAR) continue;
                const x = ((b.year - BRUSH_MIN_YEAR) / yearSpan) * w;
                const hBar = (b.count / max) * (h - 14);
                ctx.fillRect(x, h - hBar - 2, Math.max(0.6, barW - 0.4), hBar);
            }
        }

        // Foreground (filtered or full) layer
        ctx.fillStyle = accent;
        ctx.globalAlpha = 0.85;
        for (const b of fgBins) {
            if (b.year < BRUSH_MIN_YEAR || b.year > BRUSH_MAX_YEAR) continue;
            const x = ((b.year - BRUSH_MIN_YEAR) / yearSpan) * w;
            const hBar = (b.count / max) * (h - 14);
            ctx.fillRect(x, h - hBar - 2, Math.max(0.6, barW - 0.4), hBar);
        }
        ctx.globalAlpha = 1;

        // Baseline
        ctx.strokeStyle = line;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(0, h - 0.5);
        ctx.lineTo(w, h - 0.5);
        ctx.stroke();
    }

    // v0.8.6 — called by applyClientFilters() after the filter
    // pipeline updates POINTS.visibleIdx. Pass null to clear the
    // filtered overlay and restore the cached unfiltered bins.
    retally(bins) {
        this.binsFiltered = bins;
        this._draw();
    }

    _drawAnnotations() {
        if (!this.annEl || !this.annotations) return;
        this.annEl.innerHTML = "";
        const yearSpan = BRUSH_MAX_YEAR - BRUSH_MIN_YEAR;
        for (const a of this.annotations) {
            if (!a.year) continue;
            const x = ((a.year - BRUSH_MIN_YEAR) / yearSpan) * 100; // percent
            const line = document.createElement("div");
            line.className = "brush-ann";
            line.style.left = x + "%";
            line.title = `${a.label} (${a.year}) — ${a.tag || ""}`;
            this.annEl.appendChild(line);
            const label = document.createElement("div");
            label.className = "brush-ann-label";
            label.style.left = x + "%";
            label.textContent = a.label;
            this.annEl.appendChild(label);
        }
    }

    _syncWindow() {
        if (!this.windowEl || !this.w) return;
        const span = this.maxT - this.minT;
        const leftPct = ((this.window[0] - this.minT) / span) * 100;
        const rightPct = ((this.window[1] - this.minT) / span) * 100;
        this.windowEl.style.left = leftPct + "%";
        this.windowEl.style.width = (rightPct - leftPct) + "%";
        // Update readout
        const y0 = new Date(this.window[0]).getUTCFullYear();
        const y1 = new Date(this.window[1]).getUTCFullYear();
        const rangeLabel = document.getElementById("brush-range-label");
        const railLabel = document.getElementById("rail-time-label");
        const text = `${y0} — ${y1}`;
        if (rangeLabel) rangeLabel.textContent = text;
        if (railLabel) railLabel.textContent = text;
    }

    _pxToTime(px) {
        const span = this.maxT - this.minT;
        return this.minT + (px / this.w) * span;
    }

    _bindEvents() {
        if (!this.windowEl || !this.canvas) return;

        const wrap = this.canvas.parentElement;
        let dragging = null;  // { mode: "move" | "l" | "r", startX, startL, startR }

        const onPointerDown = (e) => {
            if (e.target.classList.contains("brush-handle")) {
                const handle = e.target.dataset.handle;
                dragging = {
                    mode: handle,
                    startX: e.clientX,
                    startL: this.window[0],
                    startR: this.window[1],
                };
                this.windowEl.classList.add("dragging");
                e.preventDefault();
                return;
            }
            if (e.target === this.windowEl) {
                dragging = {
                    mode: "move",
                    startX: e.clientX,
                    startL: this.window[0],
                    startR: this.window[1],
                };
                this.windowEl.classList.add("dragging");
                e.preventDefault();
                return;
            }
        };

        // v0.8.6: split drag handlers. During drag we only update
        // the visual window position — no filter pipeline, no debounced
        // onChange, no deck.gl setProps. On pointerup we commit once
        // via the RAW (un-debounced) callback so the map updates
        // within one frame of release.
        //
        // Prior behaviour queued a debounced onChange on EVERY
        // pointermove, which meant a 300ms dead zone at the start
        // of each drag plus cascading setProps calls if the user
        // held the pointer down and swept quickly.
        const onPointerMove = (e) => {
            if (!dragging) return;
            const wrapRect = wrap.getBoundingClientRect();
            const dx = e.clientX - dragging.startX;
            const span = this.maxT - this.minT;
            const dt = (dx / wrapRect.width) * span;

            let newL = dragging.startL;
            let newR = dragging.startR;
            if (dragging.mode === "move") {
                newL = dragging.startL + dt;
                newR = dragging.startR + dt;
                // Clamp within bounds
                if (newL < this.minT) { newR += (this.minT - newL); newL = this.minT; }
                if (newR > this.maxT) { newL -= (newR - this.maxT); newR = this.maxT; }
            } else if (dragging.mode === "l") {
                newL = Math.max(this.minT, Math.min(newR - 30 * 86400000, dragging.startL + dt));
            } else if (dragging.mode === "r") {
                newR = Math.min(this.maxT, Math.max(newL + 30 * 86400000, dragging.startR + dt));
            }
            this.window = [newL, newR];
            // Visual-only update: window rectangle + year labels.
            // No filter commit yet.
            this._syncWindow();
        };

        const onPointerUp = () => {
            if (!dragging) return;
            const wasDragging = dragging;
            dragging = null;
            this.windowEl.classList.remove("dragging");
            // v0.8.6: commit the final window directly, bypassing the
            // debounce. Also cancel any stale debounced call so we don't
            // re-apply the mid-drag window 300 ms later.
            this.onChange.cancel?.();
            const [L, R] = this.window;
            if (this._onChangeRaw && wasDragging) {
                this._onChangeRaw(this._isoDate(L), this._isoDate(R));
            }
        };

        wrap.addEventListener("pointerdown", onPointerDown);
        window.addEventListener("pointermove", onPointerMove);
        window.addEventListener("pointerup", onPointerUp);

        // Resize observer so the histogram redraws on viewport changes.
        const ro = new ResizeObserver(() => {
            this._resize();
            this._draw();
            this._syncWindow();
        });
        ro.observe(this.canvas);

        // Play / reset buttons
        const playBtn = document.getElementById("brush-play");
        const resetBtn = document.getElementById("brush-reset");
        if (playBtn) playBtn.addEventListener("click", () => this.togglePlay());
        if (resetBtn) resetBtn.addEventListener("click", () => this.reset());
    }

    _isoDate(ms) {
        return new Date(ms).toISOString().substring(0, 10);
    }

    togglePlay() {
        const playBtn = document.getElementById("brush-play");
        if (this.playing) {
            this.playing = false;
            cancelAnimationFrame(this.playRaf);
            if (playBtn) {
                playBtn.classList.remove("playing");
                playBtn.textContent = "▶ PLAY";
                playBtn.setAttribute("aria-pressed", "false");
            }
            // Commit the final window to the URL hash + form inputs
            // via the debounced onChange path. Flush immediately so
            // there's no 300 ms delay between STOP and the state
            // settling.
            this.onChange(
                this._isoDate(this.window[0]),
                this._isoDate(this.window[1]),
            );
            this.onChange.flush?.();
            return;
        }
        this.playing = true;
        if (playBtn) {
            playBtn.classList.add("playing");
            playBtn.textContent = "■ STOP";
            playBtn.setAttribute("aria-pressed", "true");
        }
        const span = this.maxT - this.minT;
        const isCumulative = (this.playMode === "cumulative");

        // v0.7.6: If the user hits PLAY without first narrowing the
        // window, the window IS the full range and the sliding loop
        // has nowhere to slide to. Auto-narrow to a 5-year window
        // starting from the dataset min so the playback sweeps
        // forward visibly. Cumulative mode does the same but the
        // leftEdge stays pinned at minT for the rest of the run.
        let winSpan = this.window[1] - this.window[0];
        if (winSpan >= span * 0.98) {
            const fiveYears = 5 * 365.25 * 86400000;
            winSpan = Math.min(fiveYears, span * 0.2);
            this.window = [this.minT, this.minT + winSpan];
            this._syncWindow();
        }
        if (isCumulative) {
            // Cumulative: memoise the left edge and let only the
            // right edge advance. The visible "window" on the brush
            // grows from left to right — the user sees the dataset
            // fill up over time.
            this._cumulativeLeft = this.minT;
            this.window = [this.minT, Math.max(this.window[1], this.minT + winSpan)];
            this._syncWindow();
        }

        // v0.8.1 — base step size = 0.4% of total span per frame ≈
        // 8 months at the default 1900-2026 range. Multiplied per
        // frame by this.playSpeed, which the v0.8.2-cleanup-2 speed
        // dropdown updates live so the user can speed up or slow
        // down playback mid-sweep without restarting. The arrow
        // function below preserves `this` lexically, so re-reading
        // this.playSpeed every frame "just works" — no captured
        // snapshot needed.
        const baseStep = span * 0.004;
        const fastPath = this.deckFastPath;  // snapshot for the hot loop
        const legacyOnChange = this.onChange;

        const step = () => {
            if (!this.playing) return;

            // Re-read playSpeed every frame so the speed dropdown
            // takes effect immediately.
            const stepSize = baseStep * (this.playSpeed || 1.0);

            if (isCumulative) {
                // Advance only the right edge. Wrap back to the
                // initial window size when we hit the end.
                let b = this.window[1] + stepSize;
                if (b > this.maxT) {
                    b = this._cumulativeLeft + winSpan;
                }
                this.window = [this._cumulativeLeft, b];
            } else {
                // Sliding: slide both edges. Loop back when the
                // right edge passes maxT.
                let a = this.window[0] + stepSize;
                let b = a + winSpan;
                if (b > this.maxT) {
                    a = this.minT;
                    b = a + winSpan;
                }
                this.window = [a, b];
            }
            this._syncWindow();

            // v0.8.1 — GPU fast path. Call the deck.js setTimeWindow
            // helper directly, bypassing the debounced onChange +
            // applyFilters() pipeline. This is what makes 60 fps
            // playback possible.
            if (fastPath) {
                const y0 = new Date(this.window[0]).getUTCFullYear();
                const y1 = new Date(this.window[1]).getUTCFullYear();
                fastPath(y0, y1, isCumulative);
            } else {
                // Legacy fallback: debounced onChange → applyFilters
                // → loadMapMarkers (~3 fps, but still animates).
                legacyOnChange(
                    this._isoDate(this.window[0]),
                    this._isoDate(this.window[1]),
                );
            }

            this.playRaf = requestAnimationFrame(step);
        };
        step();
    }

    reset() {
        // Stop playback if running.
        if (this.playing) this.togglePlay();
        this.window = [this.minT, this.maxT];
        this._cumulativeLeft = null;
        this._syncWindow();
        // v0.8.1 — clear any active time window on the GPU path so
        // points from every year come back immediately, not after
        // the 300 ms debounce on onChange.
        if (window.UFODeck && typeof window.UFODeck.clearTimeWindow === "function") {
            window.UFODeck.clearTimeWindow();
        }
        this.onChange(this._isoDate(this.minT), this._isoDate(this.maxT));
    }
}

// Called by TimeBrush on every (debounced) window change. Writes the
// new date range into the global filter inputs and re-fires the active
// view's data load.
function onBrushWindowChange(fromISO, toISO) {
    const fromEl = document.getElementById("filter-date-from");
    const toEl   = document.getElementById("filter-date-to");
    if (!fromEl || !toEl) return;
    fromEl.value = fromISO;
    toEl.value = toISO;
    // Update the window count on the brush header from /api/stats count,
    // or just fall back to "—" — the full count requires a fresh query.
    // For now re-run the map via applyFilters().
    applyFilters();
}

// Small debounce helper used by TimeBrush.
function debounce(fn, ms) {
    // v0.8.6: flush() now actually fires any pending call synchronously
    // and cancels the scheduled timer, so callers that need an
    // immediate commit (brush pointerup, play STOP) get a real
    // round-trip without waiting out the debounce window. Previous
    // versions stored a no-op .flush which silently swallowed the
    // final commit.
    let t = null;
    let pendingArgs = null;
    const wrapped = (...args) => {
        pendingArgs = args;
        clearTimeout(t);
        t = setTimeout(() => {
            t = null;
            const a = pendingArgs;
            pendingArgs = null;
            fn(...a);
        }, ms);
    };
    wrapped.flush = () => {
        if (t === null) return;
        clearTimeout(t);
        t = null;
        const a = pendingArgs;
        pendingArgs = null;
        if (a) fn(...a);
    };
    wrapped.cancel = () => {
        clearTimeout(t);
        t = null;
        pendingArgs = null;
    };
    return wrapped;
}

// =========================================================================
// Theme toggle (v0.7) — SIGNAL (cyan on void) / DECLASS (burgundy on paper)
// =========================================================================

// v0.7.2: Seeds the global date-range filter with 1900 → current year
// the first time the page loads, so every view (Observatory map,
// Timeline chart, Search results) opens on the modern sighting era
// instead of the full 34 AD → 2026 span. Only fills fields that are
// actually empty — a hash like #/search?date_from=1973-10-15 still
// wins because applyHashToFilters() runs after this.
function applyDefaultDateRange() {
    const fromEl = document.getElementById("filter-date-from");
    const toEl = document.getElementById("filter-date-to");
    if (fromEl && !fromEl.value) fromEl.value = "1900";
    if (toEl && !toEl.value) toEl.value = String(new Date().getFullYear());
}

function initThemeToggle() {
    const opts = document.querySelectorAll(".theme-opt");
    if (opts.length === 0) return;
    opts.forEach(btn => {
        btn.addEventListener("click", () => {
            const theme = btn.dataset.theme;
            if (theme !== "signal" && theme !== "declass") return;
            setTheme(theme);
        });
    });
}

function setTheme(theme) {
    document.body.classList.remove("theme-signal", "theme-declass");
    document.body.classList.add("theme-" + theme);
    document.querySelectorAll(".theme-opt").forEach(b => {
        const active = b.dataset.theme === theme;
        b.classList.toggle("active", active);
        b.setAttribute("aria-checked", String(active));
    });
    try { localStorage.setItem("ufosint-theme", theme); } catch (e) {}
    // Re-draw the time brush so histogram picks up the new accent color.
    if (state.timeBrush) {
        state.timeBrush._draw();
        state.timeBrush._drawAnnotations();
    }
    // v0.8.4 — live tile swap. setUrl() triggers Leaflet to re-fetch
    // the visible tile grid against the new URL template with no layer
    // add/remove. Pan/zoom position stays exactly where it was.
    if (state.tileLayer && TILE_URLS[theme]) {
        state.tileLayer.setUrl(TILE_URLS[theme]);
    }
    // v0.8.4 — live deck.gl recolor. UFODeck.setTheme updates its
    // internal palette pointer and calls refreshActiveLayer() so the
    // Scatterplot/Hexagon/Heatmap layer picks up the new colors.
    if (window.UFODeck && typeof window.UFODeck.setTheme === "function") {
        window.UFODeck.setTheme(theme);
    }
}

async function loadHeatmap(signal) {
    const status = document.getElementById("map-status");
    const mapEl = document.getElementById("map");
    status.innerHTML = '<span class="loading-pulse">COMPUTING HEATMAP</span>';
    mapEl?.classList.add("is-loading", "is-loading-progressive");
    ensureMapScanframe("HEATMAP / THERMAL");

    const bounds = state.map.getBounds();
    const params = getFilterParams();
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west", bounds.getWest().toFixed(4));
    params.set("east", bounds.getEast().toFixed(4));

    try {
        const data = await fetchJSON(`/api/heatmap?${params}`, { signal });
        state.heatLayer.setLatLngs(data.points);
        updateMapStatus(data.count, data.total_in_view, "points");
    } catch (err) {
        if (isAbortError(err)) return;
        document.getElementById("map-status").textContent = "Couldn't build heatmap — check your connection or pan again to retry.";
        document.getElementById("btn-load-all").style.display = "none";
        console.error(err);
    } finally {
        mapEl?.classList.remove("is-loading", "is-loading-progressive");
        clearMapScanframe();
    }
}

// =========================================================================
// Timeline
// =========================================================================
// =========================================================================
// Timeline tab (v0.8.6) — 3-card client-side dashboard
// =========================================================================
//
// The Timeline tab used to hit /api/timeline on every load and render a
// single stacked bar chart. v0.8.6 replaces it with three cards that
// all compute from the in-memory bulk buffer:
//
//   1. Stacked-by-source year histogram (same data as the brush,
//      bigger canvas, full legend)
//   2. Median quality_score per year (line chart, 0-100 y-axis)
//   3. Movement category share per year (stacked area, 10 series)
//
// Every card respects state.qualityFilter + filter rail. No server
// round-trips; filter changes recompute in a few ms.

async function loadTimeline() {
    // First visit: bootstrap the chart instances. Subsequent visits and
    // filter changes go through refreshTimelineCards() instead.
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) {
        // Bulk buffer not ready yet (cold cache). Show an empty state
        // and retry once POINTS.ready flips. Most users land on the
        // Observatory first so by the time they click Timeline the
        // buffer is already loaded.
        const labelEl = document.getElementById("timeline-range-label");
        if (labelEl) labelEl.textContent = "loading…";
        const countEl = document.getElementById("timeline-visible-count");
        if (countEl) countEl.textContent = "0";
        // Schedule a retry when the bulk buffer lands.
        if (!state._timelinePending) {
            state._timelinePending = true;
            const iv = setInterval(() => {
                if (window.UFODeck && window.UFODeck.POINTS && window.UFODeck.POINTS.ready) {
                    clearInterval(iv);
                    state._timelinePending = false;
                    if (state.activeTab === "timeline") loadTimeline();
                }
            }, 200);
        }
        return;
    }
    refreshTimelineCards();
}

function refreshTimelineCards() {
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) return;

    const stacked = window.UFODeck.getYearHistogramBySource(true);
    const quality = window.UFODeck.computeMedianByYear(window.UFODeck.POINTS.qualityScore);
    const movement = window.UFODeck.computeMovementShareByYear();
    const visible = window.UFODeck.countVisible();

    renderTimelineMainChart(stacked);
    renderTimelineQualityChart(quality);
    renderTimelineMovementChart(movement);

    const countEl = document.getElementById("timeline-visible-count");
    if (countEl) countEl.textContent = visible.toLocaleString();

    const labelEl = document.getElementById("timeline-range-label");
    if (labelEl && stacked.years.length) {
        labelEl.textContent = `${stacked.years[0]} — ${stacked.years[stacked.years.length - 1]}`;
    }
}

function renderTimelineMainChart(stacked) {
    const canvas = document.getElementById("timeline-main-chart");
    if (!canvas) return;
    // Trim leading/trailing empty years so the chart doesn't have a
    // hundred blank bars before the first sighting.
    let start = 0, end = stacked.years.length - 1;
    while (start < stacked.years.length && stacked.totals[start] === 0) start++;
    while (end >= 0 && stacked.totals[end] === 0) end--;
    if (start > end) { start = 0; end = stacked.years.length - 1; }

    const labels = stacked.years.slice(start, end + 1);
    const sourceCount = stacked.sources.length;
    const datasets = [];
    for (let s = 1; s < sourceCount; s++) {  // skip index 0 ("unknown")
        const name = stacked.sources[s] || "Unknown";
        const c = sourceColor(name);
        const data = new Array(labels.length);
        for (let y = 0; y < labels.length; y++) {
            data[y] = stacked.counts[(start + y) * sourceCount + s];
        }
        datasets.push({
            label: name,
            data,
            backgroundColor: c.bg,
            borderColor: c.border,
            borderWidth: 1,
        });
    }

    if (state.chart) {
        state.chart.data.labels = labels;
        state.chart.data.datasets = datasets;
        state.chart.update("none");
        return;
    }
    const ctx = canvas.getContext("2d");
    state.chart = new Chart(ctx, {
        type: "bar",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            animation: { duration: 300, easing: "easeOutQuart" },
            plugins: {
                legend: { position: "top" },
                tooltip: {
                    callbacks: {
                        footer: (items) => {
                            const total = items.reduce((s, i) => s + i.parsed.y, 0);
                            return `Total: ${total.toLocaleString()}`;
                        },
                    },
                },
            },
            scales: {
                x: { stacked: true, ticks: { maxTicksLimit: 16 } },
                y: { stacked: true, beginAtZero: true },
            },
        },
    });
}

function renderTimelineQualityChart(data) {
    const canvas = document.getElementById("timeline-quality-chart");
    if (!canvas) return;
    // Drop years with no data so the line doesn't sag to zero between
    // historical gaps.
    const labels = [];
    const values = [];
    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";
    const accentHover = getComputedStyle(document.body).getPropertyValue("--accent-hover").trim() || accent;
    for (const row of data) {
        if (row.count === 0 || row.median == null) continue;
        labels.push(row.year);
        values.push(row.median);
    }

    if (state.timelineQualityChart) {
        state.timelineQualityChart.data.labels = labels;
        state.timelineQualityChart.data.datasets[0].data = values;
        state.timelineQualityChart.data.datasets[0].borderColor = accent;
        state.timelineQualityChart.data.datasets[0].pointBackgroundColor = accentHover;
        state.timelineQualityChart.update("none");
        return;
    }
    const ctx = canvas.getContext("2d");
    state.timelineQualityChart = new Chart(ctx, {
        type: "line",
        data: {
            labels,
            datasets: [{
                label: "Median quality score",
                data: values,
                borderColor: accent,
                backgroundColor: "transparent",
                pointBackgroundColor: accentHover,
                pointRadius: 2,
                tension: 0.2,
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { maxTicksLimit: 16 } },
                y: { min: 0, max: 100, title: { display: true, text: "Median QS (0-100)" } },
            },
        },
    });
}

function renderTimelineMovementChart(mv) {
    const canvas = document.getElementById("timeline-movement-chart");
    if (!canvas || !mv) return;
    // Trim leading/trailing zero years
    let start = 0, end = mv.years.length - 1;
    while (start < mv.years.length && mv.totals[start] === 0) start++;
    while (end >= 0 && mv.totals[end] === 0) end--;
    if (start > end) { start = 0; end = mv.years.length - 1; }

    const labels = mv.years.slice(start, end + 1);
    const M = 10;
    // Palette for 10 movement categories — deterministic so series
    // colours stay stable across filter changes.
    const palette = [
        "#00F0FF", "#FFB300", "#FF4E00", "#B8001F", "#7CF9FF",
        "#8A94AD", "#6ea8ff", "#E6EAF2", "#C97B00", "#9C8B60",
    ];
    const datasets = [];
    for (let b = 0; b < M; b++) {
        const data = new Array(labels.length);
        for (let y = 0; y < labels.length; y++) {
            data[y] = mv.counts[(start + y) * M + b];
        }
        datasets.push({
            label: mv.movements[b] || `cat ${b}`,
            data,
            backgroundColor: palette[b] + "AA",
            borderColor: palette[b],
            borderWidth: 1,
            fill: true,
        });
    }

    if (state.timelineMovementChart) {
        state.timelineMovementChart.data.labels = labels;
        state.timelineMovementChart.data.datasets = datasets;
        state.timelineMovementChart.update("none");
        return;
    }
    const ctx = canvas.getContext("2d");
    state.timelineMovementChart = new Chart(ctx, {
        type: "line",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            animation: { duration: 300 },
            plugins: { legend: { position: "top", labels: { boxWidth: 10 } } },
            scales: {
                x: { stacked: true, ticks: { maxTicksLimit: 16 } },
                y: { stacked: true, beginAtZero: true },
            },
            elements: { point: { radius: 0 } },
        },
    });
}

// =========================================================================
// Shared utility — kept from the old Search/Duplicates block because
// applyFilters() and the AI panel + map place search still use it.
// =========================================================================

/**
 * Disable a button while a request is in flight, replacing its label
 * with a "loading" message. Returns a function that restores the original
 * label and enables the button — call it from a finally block.
 */
function disableButtonWhilePending(btn, loadingLabel) {
    if (!btn) return () => {};
    const originalHTML = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.innerHTML = `<span class="loading-pulse">${loadingLabel}</span>`;
    return function restore() {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.innerHTML = originalHTML;
    };
}

// v0.8.6: Search panel and Duplicates panel functions were deleted.
// `doSearch`, `executeSearch`, `renderActiveFilterChips`, `removeFilter`,
// `renderPager`, `goToPage`, `scoreColor`, `scoreLabel`, and
// `loadDuplicates` are gone along with the DOM elements they
// mutated. Client-side filtering on the Observatory bulk buffer
// replaces server-side faceted search; the v0.8.3b export ships
// zero duplicate_candidate rows so the Duplicates panel had nothing
// to show.

// =========================================================================
// Detail Modal
// =========================================================================
// =========================================================================
// Insights (Sentiment & Emotion)
// =========================================================================
async function loadInsights() {
    const statusEl = document.getElementById("insights-status");

    // v0.8.8 — all 8 insight cards are now client-side. The
    // /api/sentiment/* endpoints were returning empty data after the
    // v0.8.5 reload (sentiment_analysis table was truncated and the
    // public ufo_public.db doesn't ship sentiment rows because it
    // strips the raw narrative text they were computed from). We
    // rewrote the 4 emotion cards to read from POINTS.emotionIdx,
    // which IS populated for 149,607 rows and ships in the bulk
    // buffer. No server trips, everything recomputes on filter
    // change in a few milliseconds.
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) {
        // Bulk buffer still loading. Show a brief message and
        // schedule a retry when POINTS.ready flips.
        if (statusEl) statusEl.textContent = "loading bulk data…";
        if (!state._insightsPending) {
            state._insightsPending = true;
            const iv = setInterval(() => {
                if (window.UFODeck && window.UFODeck.POINTS && window.UFODeck.POINTS.ready) {
                    clearInterval(iv);
                    state._insightsPending = false;
                    if (state.activeTab === "insights") loadInsights();
                }
            }, 200);
        }
        return;
    }

    refreshInsightsClientCards();

    // Status line: surface the emotion coverage number so users
    // know the denominator the cards are computed against.
    const P = window.UFODeck.POINTS;
    const visible = P.visibleIdx ? P.visibleIdx.length : P.count;
    let emotionCovered = 0;
    const ei = P.emotionIdx;
    if (P.visibleIdx) {
        for (let k = 0; k < P.visibleIdx.length; k++) {
            if (ei[P.visibleIdx[k]] > 0) emotionCovered++;
        }
    } else {
        for (let i = 0; i < P.count; i++) {
            if (ei[i] > 0) emotionCovered++;
        }
    }
    if (statusEl) {
        statusEl.textContent =
            `${emotionCovered.toLocaleString()} sightings with emotion classification · ${visible.toLocaleString()} in view`;
    }
}

// v0.8.6 — refresh all 8 client-side cards. Called by loadInsights
// on first mount and by applyClientFilters on every filter change
// so the cards stay in sync with POINTS.visibleIdx.
//
// v0.8.8: the 4 emotion cards (radar, over-time, by-source, by-shape)
// were rewritten to read from POINTS.emotionIdx instead of the dead
// /api/sentiment/* endpoints. All 8 cards are now purely client-side.
function refreshInsightsClientCards() {
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) return;
    // v0.8.8 emotion cards (client-side)
    renderEmotionRadar();
    renderEmotionOverTime();
    renderEmotionBySource();
    renderEmotionByShape();
    // v0.8.6 derived cards
    renderQualityDistribution();
    renderMovementTaxonomy();
    renderShapeMovementMatrix();
    renderHoaxCurve();
}

// -------------------------------------------------------------------------
// v0.8.6 — client-side Insight cards. Each walks POINTS.visibleIdx
// and feeds Chart.js directly. Cards recompute on every filter change
// in single-digit milliseconds.
// -------------------------------------------------------------------------

function renderQualityDistribution() {
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const qs = P.qualityScore;
    const UNK = 255;
    const buckets = new Uint32Array(10);  // 0-9, 10-19, ..., 90-100
    if (iter) {
        for (let k = 0; k < iter.length; k++) {
            const v = qs[iter[k]];
            if (v === UNK) continue;
            buckets[Math.min(9, (v / 10) | 0)]++;
        }
    } else {
        for (let i = 0; i < P.count; i++) {
            const v = qs[i];
            if (v === UNK) continue;
            buckets[Math.min(9, (v / 10) | 0)]++;
        }
    }

    const labels = ["0-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80-89", "90-100"];
    const dimColor = getComputedStyle(document.body).getPropertyValue("--fg-dim").trim() || "#8A94AD";
    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";
    // Highlight the 60+ "high quality" threshold buckets
    const backgroundColors = labels.map((_, i) => (i >= 6 ? accent + "CC" : dimColor + "55"));
    const borderColors = labels.map((_, i) => (i >= 6 ? accent : dimColor));

    const canvas = document.getElementById("quality-distribution-chart");
    if (!canvas) return;
    if (state.insightsCharts.qualityDist) {
        const c = state.insightsCharts.qualityDist;
        c.data.datasets[0].data = Array.from(buckets);
        c.data.datasets[0].backgroundColor = backgroundColors;
        c.data.datasets[0].borderColor = borderColors;
        c.update("none");
        return;
    }
    state.insightsCharts.qualityDist = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "Sightings",
                data: Array.from(buckets),
                backgroundColor: backgroundColors,
                borderColor: borderColors,
                borderWidth: 1,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: (items) => `Quality Score ${items[0].label}`,
                        label: (item) => `${item.parsed.y.toLocaleString()} sightings`,
                    },
                },
            },
            scales: {
                x: { title: { display: true, text: "Quality Score bucket" } },
                y: { beginAtZero: true },
            },
        },
    });
}

function renderMovementTaxonomy() {
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const mf = P.movementFlags;
    const M = 10;
    const counts = new Uint32Array(M);
    if (iter) {
        for (let k = 0; k < iter.length; k++) {
            const v = mf[iter[k]];
            if (v === 0) continue;
            for (let b = 0; b < M; b++) if (v & (1 << b)) counts[b]++;
        }
    } else {
        for (let i = 0; i < P.count; i++) {
            const v = mf[i];
            if (v === 0) continue;
            for (let b = 0; b < M; b++) if (v & (1 << b)) counts[b]++;
        }
    }

    // Sort bars by count descending for readability.
    const names = P.movements || [];
    const rows = [];
    for (let b = 0; b < M; b++) {
        rows.push({ name: names[b] || `cat ${b}`, count: counts[b] });
    }
    rows.sort((a, b) => b.count - a.count);
    const labels = rows.map(r => r.name.charAt(0).toUpperCase() + r.name.slice(1));
    const data = rows.map(r => r.count);
    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";

    const canvas = document.getElementById("movement-taxonomy-chart");
    if (!canvas) return;
    if (state.insightsCharts.movementTax) {
        const c = state.insightsCharts.movementTax;
        c.data.labels = labels;
        c.data.datasets[0].data = data;
        c.update("none");
        return;
    }
    state.insightsCharts.movementTax = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "Sightings with movement",
                data,
                backgroundColor: accent + "BB",
                borderColor: accent,
                borderWidth: 1,
            }],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (item) => `${item.parsed.x.toLocaleString()} sightings`,
                    },
                },
            },
            scales: {
                x: { beginAtZero: true },
            },
        },
    });
}

function renderShapeMovementMatrix() {
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const sh = P.shapeIdx;
    const mf = P.movementFlags;
    const S = (P.shapes || [null]).length;
    const M = 10;
    const matrix = new Uint32Array(S * M);
    const shapeTotals = new Uint32Array(S);
    const walk = (i) => {
        const s = sh[i];
        if (s === 0) return;
        const v = mf[i];
        if (v === 0) return;
        shapeTotals[s]++;
        const base = s * M;
        for (let b = 0; b < M; b++) if (v & (1 << b)) matrix[base + b]++;
    };
    if (iter) {
        for (let k = 0; k < iter.length; k++) walk(iter[k]);
    } else {
        for (let i = 0; i < P.count; i++) walk(i);
    }

    // Top-10 shapes by count of movement-tagged sightings.
    const ranked = [];
    for (let s = 1; s < S; s++) {
        if (shapeTotals[s] > 0) ranked.push({ idx: s, total: shapeTotals[s] });
    }
    ranked.sort((a, b) => b.total - a.total);
    const top = ranked.slice(0, 10);

    const shapeNames = P.shapes || [];
    const movementNames = P.movements || [];
    const labels = top.map(r => shapeNames[r.idx] || `shape ${r.idx}`);

    const palette = [
        "#00F0FF", "#FFB300", "#FF4E00", "#B8001F", "#7CF9FF",
        "#8A94AD", "#6ea8ff", "#E6EAF2", "#C97B00", "#9C8B60",
    ];
    const datasets = [];
    for (let b = 0; b < M; b++) {
        datasets.push({
            label: (movementNames[b] || `cat ${b}`).charAt(0).toUpperCase() +
                   (movementNames[b] || "").slice(1),
            data: top.map(r => matrix[r.idx * M + b]),
            backgroundColor: palette[b],
            borderWidth: 0,
        });
    }

    const canvas = document.getElementById("shape-movement-chart");
    if (!canvas) return;
    if (state.insightsCharts.shapeMovement) {
        const c = state.insightsCharts.shapeMovement;
        c.data.labels = labels;
        c.data.datasets = datasets;
        c.update("none");
        return;
    }
    state.insightsCharts.shapeMovement = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: { labels, datasets },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { position: "bottom", labels: { boxWidth: 10 } },
                tooltip: {
                    callbacks: {
                        footer: (items) => {
                            const t = items.reduce((s, i) => s + i.parsed.x, 0);
                            return `Total: ${t.toLocaleString()}`;
                        },
                    },
                },
            },
            scales: {
                x: { stacked: true, beginAtZero: true },
                y: { stacked: true },
            },
        },
    });
}

function renderHoaxCurve() {
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const hs = P.hoaxScore;
    const UNK = 255;
    // 20 buckets of 5 score-points each: 0-4, 5-9, ..., 95-100
    const buckets = new Uint32Array(20);
    if (iter) {
        for (let k = 0; k < iter.length; k++) {
            const v = hs[iter[k]];
            if (v === UNK) continue;
            buckets[Math.min(19, (v / 5) | 0)]++;
        }
    } else {
        for (let i = 0; i < P.count; i++) {
            const v = hs[i];
            if (v === UNK) continue;
            buckets[Math.min(19, (v / 5) | 0)]++;
        }
    }

    const labels = [];
    for (let i = 0; i < 20; i++) labels.push(`${i * 5}`);
    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";
    const hot = getComputedStyle(document.body).getPropertyValue("--accent-hot").trim() || "#FF4E00";
    // Gradient from accent (likely genuine) to hot (likely hoax)
    const pointColors = labels.map((_, i) => (i >= 16 ? hot : accent));

    const canvas = document.getElementById("hoax-curve-chart");
    if (!canvas) return;
    if (state.insightsCharts.hoaxCurve) {
        const c = state.insightsCharts.hoaxCurve;
        c.data.datasets[0].data = Array.from(buckets);
        c.data.datasets[0].pointBackgroundColor = pointColors;
        c.update("none");
        return;
    }
    state.insightsCharts.hoaxCurve = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
            labels,
            datasets: [{
                label: "Sightings",
                data: Array.from(buckets),
                borderColor: accent,
                backgroundColor: accent + "22",
                pointBackgroundColor: pointColors,
                pointRadius: 3,
                fill: true,
                tension: 0.25,
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: (items) => `Hoax score ${items[0].label}-${(+items[0].label) + 4}`,
                        label: (item) => `${item.parsed.y.toLocaleString()} sightings`,
                    },
                },
            },
            scales: {
                x: { title: { display: true, text: "Hoax likelihood (0 = genuine, 100 = hoax)" } },
                y: { beginAtZero: true },
            },
        },
    });
}

// =========================================================================
// v0.8.8 — Emotion cards (client-side, from POINTS.emotionIdx)
// =========================================================================
// The /api/sentiment/* endpoints return empty data after the v0.8.5
// reload (sentiment_analysis table truncated, ufo_public.db doesn't
// ship sentiment rows). dominant_emotion IS populated in the bulk
// buffer at offset 22 (149,607 of 396,158 rows), so we compute the
// 4 cards client-side with the same walk-POINTS.visibleIdx pattern
// the v0.8.6 derived cards use.
//
// Note: these renderers now take NO arguments — they read directly
// from POINTS inside the function body. That's consistent with the
// v0.8.6 renderQualityDistribution / renderMovementTaxonomy / etc.

// Helper: collect counts of each emotion across visibleIdx.
// Returns { names: [...], counts: Uint32Array, total: int }
function _collectEmotionCounts(P) {
    const iter = P.visibleIdx || null;
    const ei = P.emotionIdx;
    const names = P.emotions || [];
    const counts = new Uint32Array(names.length);
    let total = 0;
    if (iter) {
        for (let k = 0; k < iter.length; k++) {
            const idx = ei[iter[k]];
            if (idx > 0) { counts[idx]++; total++; }
        }
    } else {
        for (let i = 0; i < P.count; i++) {
            const idx = ei[i];
            if (idx > 0) { counts[idx]++; total++; }
        }
    }
    return { names, counts, total };
}

// Look up the EMOTION_COLORS entry for a name, falling back to a
// neutral pair if the name isn't in the palette (POINTS.emotions is
// from the server and could in principle include a name EMOTION_COLORS
// doesn't know about).
function _emotionColor(name) {
    return EMOTION_COLORS[name] || { bg: "rgba(139,148,158,0.6)", border: "#8b949e" };
}

function renderEmotionRadar() {
    const canvas = document.getElementById("emotion-radar-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const { names, counts, total } = _collectEmotionCounts(P);

    // Build a stable label+data pair skipping the index-0 null slot.
    const labels = [];
    const values = [];
    const borderColors = [];
    for (let i = 1; i < names.length; i++) {
        labels.push(names[i].charAt(0).toUpperCase() + names[i].slice(1));
        values.push(counts[i]);
        borderColors.push(_emotionColor(names[i]).border);
    }
    // Normalise to the [0, 1] range so the radar doesn't scale to a
    // single dominant emotion. Guard against divide-by-zero when the
    // current filter set has no emotion-classified rows.
    const maxCount = values.reduce((m, v) => Math.max(m, v), 1);
    const normalized = values.map(v => v / maxCount);

    if (state.insightsCharts.radar) {
        const c = state.insightsCharts.radar;
        c.data.labels = labels;
        c.data.datasets[0].data = normalized;
        c.data.datasets[0].pointBackgroundColor = borderColors;
        c._rawValues = values;  // stash for tooltip callback
        c._total = total;
        c.update("none");
        return;
    }

    const ctx = canvas.getContext("2d");
    state.insightsCharts.radar = new Chart(ctx, {
        type: "radar",
        data: {
            labels,
            datasets: [{
                label: "Emotion Distribution",
                data: normalized,
                backgroundColor: "rgba(0, 240, 255, 0.18)",
                borderColor: "rgba(0, 240, 255, 0.85)",
                borderWidth: 2,
                pointBackgroundColor: borderColors,
                pointBorderColor: "#fff",
                pointRadius: 5,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            scales: {
                r: {
                    beginAtZero: true,
                    grid: { color: "rgba(139, 148, 158, 0.3)" },
                    angleLines: { color: "rgba(139, 148, 158, 0.3)" },
                    pointLabels: { font: { size: 12 } },
                    ticks: { display: false },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const c = state.insightsCharts.radar;
                            const raw = c._rawValues ? c._rawValues[ctx.dataIndex] : 0;
                            const tot = c._total || 1;
                            const pct = ((raw / tot) * 100).toFixed(1);
                            return `${raw.toLocaleString()} sightings (${pct}%)`;
                        }
                    }
                }
            },
        },
    });
    // Stash after creation for the first tooltip invocation.
    state.insightsCharts.radar._rawValues = values;
    state.insightsCharts.radar._total = total;
}

// Stacked-area chart: emotion counts per year. Walks POINTS.dateDays
// + emotionIdx once, bins by year, emits 8 series (one per emotion).
function renderEmotionOverTime() {
    const canvas = document.getElementById("sentiment-timeline-chart");
    if (!canvas) return;
    if (!window.UFODeck.computeMedianByYear) return;  // old deck.js

    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const dd = P.dateDays;
    const ei = P.emotionIdx;
    const names = P.emotions || [];
    const nEmo = names.length;

    // Build yearStarts via the same binary-search pattern deck.js
    // getYearHistogram uses. We inline a small version here because
    // the cross-product (year × emotion) isn't shaped like any of
    // the existing helpers.
    const yearRange = window.UFODeck.getYearRange();
    if (!yearRange || yearRange.min == null) return;
    const yMin = yearRange.min, yMax = yearRange.max;
    const span = yMax - yMin + 1;
    const yearStarts = new Uint32Array(span + 1);
    for (let y = 0; y <= span; y++) {
        yearStarts[y] = Math.floor(
            (Date.UTC(yMin + y, 0, 1) - Date.UTC(1900, 0, 1)) / 86400000,
        );
    }
    const dayToBin = (d) => {
        let lo = 0, hi = span;
        while (lo < hi) {
            const mid = (lo + hi + 1) >>> 1;
            if (yearStarts[mid] <= d) lo = mid;
            else hi = mid - 1;
        }
        return lo;
    };

    const grid = new Uint32Array(span * nEmo);  // row-major: year * nEmo + emo
    const totals = new Uint32Array(span);
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        const e = ei[i];
        if (e === 0) continue;
        const d = dd[i];
        if (d === 0) continue;
        const bin = dayToBin(d);
        if (bin < 0 || bin >= span) continue;
        grid[bin * nEmo + e]++;
        totals[bin]++;
    }

    // Trim leading/trailing zero years so the chart doesn't span the
    // full 1900-2026 range when most of it is empty.
    let start = 0, end = span - 1;
    while (start < span && totals[start] === 0) start++;
    while (end >= 0 && totals[end] === 0) end--;
    if (start > end) { start = 0; end = span - 1; }

    const labels = [];
    for (let y = start; y <= end; y++) labels.push(yMin + y);

    const datasets = [];
    for (let e = 1; e < nEmo; e++) {
        const color = _emotionColor(names[e]);
        const data = new Array(labels.length);
        for (let y = 0; y < labels.length; y++) {
            data[y] = grid[(start + y) * nEmo + e];
        }
        datasets.push({
            label: names[e].charAt(0).toUpperCase() + names[e].slice(1),
            data,
            backgroundColor: color.bg,
            borderColor: color.border,
            borderWidth: 1,
            fill: true,
            pointRadius: 0,
            tension: 0.2,
        });
    }

    if (state.insightsCharts.timeline) {
        const c = state.insightsCharts.timeline;
        c.data.labels = labels;
        c.data.datasets = datasets;
        c.update("none");
        return;
    }

    const ctx = canvas.getContext("2d");
    state.insightsCharts.timeline = new Chart(ctx, {
        type: "line",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            animation: { duration: 300 },
            plugins: {
                legend: { position: "top", labels: { boxWidth: 10 } },
                tooltip: {
                    callbacks: {
                        footer: (items) => {
                            const total = items.reduce((s, i) => s + i.parsed.y, 0);
                            return `Total: ${total.toLocaleString()}`;
                        },
                    },
                },
            },
            scales: {
                x: { stacked: true, ticks: { maxTicksLimit: 16 } },
                y: { stacked: true, beginAtZero: true },
            },
        },
    });
}

// Stacked bar chart: per source (x-axis), show the share of each
// emotion (stacked, sum-to-100%). Reads POINTS.sourceIdx + emotionIdx.
function renderEmotionBySource() {
    const canvas = document.getElementById("emotion-source-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const si = P.sourceIdx;
    const ei = P.emotionIdx;
    const sources = P.sources || [];
    const emotions = P.emotions || [];
    const nSrc = sources.length;
    const nEmo = emotions.length;

    const grid = new Uint32Array(nSrc * nEmo);
    const srcTotals = new Uint32Array(nSrc);
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        const s = si[i];
        if (s === 0) continue;
        const e = ei[i];
        if (e === 0) continue;
        grid[s * nEmo + e]++;
        srcTotals[s]++;
    }

    // Collect non-empty sources, skipping index 0.
    const srcIdxes = [];
    for (let s = 1; s < nSrc; s++) {
        if (srcTotals[s] > 0) srcIdxes.push(s);
    }
    const labels = srcIdxes.map(s => sources[s] || "Unknown");

    const datasets = [];
    for (let e = 1; e < nEmo; e++) {
        const color = _emotionColor(emotions[e]);
        const data = srcIdxes.map(s => {
            const tot = srcTotals[s];
            return tot > 0 ? (grid[s * nEmo + e] / tot) * 100 : 0;
        });
        datasets.push({
            label: emotions[e].charAt(0).toUpperCase() + emotions[e].slice(1),
            data,
            backgroundColor: color.bg,
            borderColor: color.border,
            borderWidth: 1,
        });
    }

    if (state.insightsCharts.source) {
        const c = state.insightsCharts.source;
        c.data.labels = labels;
        c.data.datasets = datasets;
        c.update("none");
        return;
    }

    const ctx = canvas.getContext("2d");
    state.insightsCharts.source = new Chart(ctx, {
        type: "bar",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { position: "top", labels: { boxWidth: 10 } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}%`,
                    },
                },
            },
            scales: {
                x: { stacked: true },
                y: {
                    stacked: true,
                    beginAtZero: true,
                    max: 100,
                    ticks: { callback: (v) => v + "%" },
                },
            },
        },
    });
}

// Horizontal stacked bar chart: top-10 shapes by emotion-classified
// count, each bar split into 8 emotion segments. Reads
// POINTS.shapeIdx + emotionIdx.
function renderEmotionByShape() {
    const canvas = document.getElementById("emotion-shape-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const sh = P.shapeIdx;
    const ei = P.emotionIdx;
    const shapes = P.shapes || [];
    const emotions = P.emotions || [];
    const nShp = shapes.length;
    const nEmo = emotions.length;

    const grid = new Uint32Array(nShp * nEmo);
    const shpTotals = new Uint32Array(nShp);
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        const s = sh[i];
        if (s === 0) continue;
        const e = ei[i];
        if (e === 0) continue;
        grid[s * nEmo + e]++;
        shpTotals[s]++;
    }

    // Top 10 shapes by emotion-classified count.
    const ranked = [];
    for (let s = 1; s < nShp; s++) {
        if (shpTotals[s] > 0) ranked.push({ idx: s, total: shpTotals[s] });
    }
    ranked.sort((a, b) => b.total - a.total);
    const top = ranked.slice(0, 10);
    const labels = top.map(r => shapes[r.idx] || `shape ${r.idx}`);

    const datasets = [];
    for (let e = 1; e < nEmo; e++) {
        const color = _emotionColor(emotions[e]);
        const data = top.map(r => {
            const tot = r.total;
            return tot > 0 ? (grid[r.idx * nEmo + e] / tot) * 100 : 0;
        });
        datasets.push({
            label: emotions[e].charAt(0).toUpperCase() + emotions[e].slice(1),
            data,
            backgroundColor: color.bg,
            borderColor: color.border,
            borderWidth: 1,
        });
    }

    if (state.insightsCharts.shape) {
        const c = state.insightsCharts.shape;
        c.data.labels = labels;
        c.data.datasets = datasets;
        c.update("none");
        return;
    }

    const ctx = canvas.getContext("2d");
    state.insightsCharts.shape = new Chart(ctx, {
        type: "bar",
        data: { labels, datasets },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.x.toFixed(1)}%`,
                    },
                },
            },
            scales: {
                x: {
                    stacked: true,
                    beginAtZero: true,
                    max: 100,
                    ticks: { callback: (v) => v + "%" },
                },
                y: { stacked: true },
            },
        },
    });
}

// =========================================================================
// Detail Modal
// =========================================================================
// Track the element that opened the modal so focus can return there on close.
let _modalReturnFocus = null;

function _modalEscapeHandler(e) {
    if (e.key === "Escape") closeModal();
}

async function openDetail(id) {
    const overlay = document.getElementById("modal-overlay");
    const body = document.getElementById("modal-body");
    const title = document.getElementById("modal-title");

    // Remember where focus came from so we can restore it on close
    _modalReturnFocus = document.activeElement;

    // Reset any leftover inline display from earlier code paths, then
    // use the class-based transition. CSS handles the opacity fade on
    // .modal-overlay and the scale on .modal.
    overlay.style.display = "";
    overlay.classList.add("is-open");
    title.textContent = `Sighting #${id}`;

    // Skeleton loading state — layout-stable blocks shaped roughly like
    // the real detail sections, so the modal doesn't jump when content
    // arrives. Uses the same shimmer animation as the search skeletons.
    body.innerHTML = `
        <div class="detail-grid">
            <div class="detail-section">
                <div class="detail-skeleton skeleton-title"></div>
                <div class="detail-skeleton skeleton-line"></div>
                <div class="detail-skeleton skeleton-line-sm"></div>
                <div class="detail-skeleton skeleton-line"></div>
            </div>
            <div class="detail-section">
                <div class="detail-skeleton skeleton-title"></div>
                <div class="detail-skeleton skeleton-line"></div>
                <div class="detail-skeleton skeleton-line-sm"></div>
                <div class="detail-skeleton skeleton-line"></div>
            </div>
            <div class="detail-section" style="grid-column:1/-1">
                <div class="detail-skeleton skeleton-title"></div>
                <div class="detail-skeleton skeleton-block"></div>
            </div>
        </div>
    `;

    // Move focus inside the modal (close button is the safest landing spot)
    document.getElementById("modal-close")?.focus();

    // Wire up Escape-to-close
    document.addEventListener("keydown", _modalEscapeHandler);

    try {
        const r = await fetchJSON(`/api/sighting/${id}`);

        title.textContent = `Sighting #${id}`;

        let html = '<div class="detail-grid">';

        // Source & provenance
        html += `<div class="detail-section">
            <h3>Source</h3>
            <div class="detail-row">${sourceBadge(r.source_name)}</div>`;
        if (r.collection_name) html += `<div class="detail-row"><span class="detail-label">Collection:</span> <span class="collection-tag">${escapeHtml(r.collection_name)}</span></div>`;
        if (r.origin_name) html += `<div class="detail-row"><span class="detail-label">Origin:</span> ${escapeHtml(r.origin_name)}</div>`;
        if (r.source_record_id) html += `<div class="detail-row"><span class="detail-label">Record ID:</span> ${escapeHtml(r.source_record_id)}</div>`;
        html += `</div>`;

        // Date & Time
        html += `<div class="detail-section"><h3>Date / Time</h3>`;
        if (r.date_event) html += `<div class="detail-row"><span class="detail-label">Date:</span> ${escapeHtml(r.date_event)}</div>`;
        if (r.date_event_raw) html += `<div class="detail-row"><span class="detail-label">Raw date:</span> ${escapeHtml(r.date_event_raw)}</div>`;
        if (r.date_end) html += `<div class="detail-row"><span class="detail-label">End date:</span> ${escapeHtml(r.date_end)}</div>`;
        if (r.time_raw) html += `<div class="detail-row"><span class="detail-label">Time:</span> ${escapeHtml(r.time_raw)}</div>`;
        if (r.date_reported) html += `<div class="detail-row"><span class="detail-label">Reported:</span> ${escapeHtml(r.date_reported)}</div>`;
        html += `</div>`;

        // Location
        html += `<div class="detail-section"><h3>Location</h3>`;
        const loc = formatLocation(r.city, r.state, r.country);
        if (loc) html += `<div class="detail-row">${escapeHtml(loc)}</div>`;
        if (r.loc_raw && r.loc_raw !== loc) html += `<div class="detail-row"><span class="detail-label">Raw:</span> ${escapeHtml(r.loc_raw)}</div>`;
        if (r.latitude && r.longitude) {
            html += `<div class="detail-row"><span class="detail-label">Coords:</span> ${r.latitude.toFixed(4)}, ${r.longitude.toFixed(4)}</div>`;
            html += `<div id="detail-minimap" class="detail-minimap"></div>`;
        }
        html += `</div>`;

        // Observation
        html += `<div class="detail-section"><h3>Observation</h3>`;
        if (r.shape) html += `<div class="detail-row"><span class="detail-label">Shape:</span> <span class="shape-tag">${escapeHtml(r.shape)}</span></div>`;
        if (r.color) html += `<div class="detail-row"><span class="detail-label">Color:</span> ${escapeHtml(r.color)}</div>`;
        if (r.size_estimated) html += `<div class="detail-row"><span class="detail-label">Size:</span> ${escapeHtml(r.size_estimated)}</div>`;
        if (r.duration) html += `<div class="detail-row"><span class="detail-label">Duration:</span> ${escapeHtml(r.duration)}</div>`;
        if (r.num_witnesses) html += `<div class="detail-row"><span class="detail-label">Witnesses:</span> ${r.num_witnesses}</div>`;
        if (r.num_objects) html += `<div class="detail-row"><span class="detail-label">Objects:</span> ${r.num_objects}</div>`;
        if (r.sound) html += `<div class="detail-row"><span class="detail-label">Sound:</span> ${escapeHtml(r.sound)}</div>`;
        if (r.direction) html += `<div class="detail-row"><span class="detail-label">Direction:</span> ${escapeHtml(r.direction)}</div>`;
        if (r.elevation_angle) html += `<div class="detail-row"><span class="detail-label">Elevation:</span> ${escapeHtml(r.elevation_angle)}</div>`;
        html += `</div>`;

        // Classification
        if (r.hynek || r.vallee || r.event_type || r.svp_rating) {
            html += `<div class="detail-section"><h3>Classification</h3>`;
            if (r.hynek) html += `<div class="detail-row"><span class="detail-label">Hynek:</span> ${escapeHtml(r.hynek)}</div>`;
            if (r.vallee) html += `<div class="detail-row"><span class="detail-label">Vallee:</span> ${escapeHtml(r.vallee)}</div>`;
            if (r.event_type) html += `<div class="detail-row"><span class="detail-label">Event type:</span> ${escapeHtml(r.event_type)}</div>`;
            if (r.svp_rating) html += `<div class="detail-row"><span class="detail-label">SVP:</span> ${escapeHtml(r.svp_rating)}</div>`;
            html += `</div>`;
        }

        // Sentiment
        if (r.sentiment) {
            const s = r.sentiment;
            html += `<div class="detail-section"><h3>Sentiment Analysis</h3>`;
            const compoundColor = s.vader_compound >= 0 ? "var(--green)" : "var(--red)";
            html += `<div class="detail-row"><span class="detail-label">VADER Compound:</span> <span style="color:${compoundColor};font-weight:600">${s.vader_compound.toFixed(3)}</span></div>`;
            html += `<div class="detail-row"><span class="detail-label">Positive:</span> ${s.vader_positive.toFixed(3)} &nbsp; <span class="detail-label">Negative:</span> ${s.vader_negative.toFixed(3)} &nbsp; <span class="detail-label">Neutral:</span> ${s.vader_neutral.toFixed(3)}</div>`;
            const emos = ["joy","fear","anger","sadness","surprise","disgust","trust","anticipation"];
            const emoValues = emos.map(e => s["emo_" + e] || 0);
            const maxEmo = Math.max(...emoValues, 1);
            html += `<div style="margin-top:6px">`;
            emos.forEach((e, i) => {
                const width = Math.round(emoValues[i] / maxEmo * 100);
                const c = EMOTION_COLORS[e];
                html += `<div style="display:flex;align-items:center;gap:6px;font-size:12px;margin:2px 0">
                    <span style="width:80px;text-align:right;color:var(--text-muted)">${e}</span>
                    <span style="width:${width}px;height:10px;background:${c.border};border-radius:2px;display:inline-block"></span>
                    <span style="color:var(--text-muted)">${emoValues[i]}</span>
                </div>`;
            });
            html += `</div>`;
            html += `<div style="font-size:11px;color:var(--text-muted);margin-top:4px">Analyzed from ${s.text_source} (${s.text_length.toLocaleString()} chars)</div>`;
            html += `</div>`;
        }

        // v0.8.3 — Data Quality section.
        //
        // Three horizontal bars for the derived scores from the
        // ufo-dedup analyze.py pipeline. Replaces the old
        // "Description" paragraph section that rendered raw
        // narrative text from r.description / r.summary — those
        // columns are dropped from the public schema by
        // scripts/strip_raw_for_public.py, so the response doesn't
        // carry them anymore.
        const hasAnyScore = (
            r.quality_score != null ||
            r.richness_score != null ||
            r.hoax_likelihood != null
        );
        if (hasAnyScore) {
            html += `<div class="detail-section"><h3>Data Quality</h3>`;
            if (r.quality_score != null) {
                const pct = Math.max(0, Math.min(100, r.quality_score));
                html += `<div class="detail-row">
                    <span class="detail-label">Quality:</span>
                    <div class="quality-bar"><div class="quality-bar-fill" style="width:${pct}%"></div></div>
                    <span class="quality-bar-value">${pct} / 100</span>
                </div>`;
            }
            if (r.richness_score != null) {
                const pct = Math.max(0, Math.min(100, r.richness_score));
                html += `<div class="detail-row">
                    <span class="detail-label">Richness:</span>
                    <div class="quality-bar"><div class="quality-bar-fill" style="width:${pct}%"></div></div>
                    <span class="quality-bar-value">${pct} / 100</span>
                </div>`;
            }
            if (r.hoax_likelihood != null) {
                // hoax_likelihood is REAL 0.0-1.0. Render as a
                // burgundy "danger" bar — higher = more likely hoax.
                const val = Number(r.hoax_likelihood);
                const pct = Math.max(0, Math.min(100, Math.round(val * 100)));
                html += `<div class="detail-row">
                    <span class="detail-label">Hoax likelihood:</span>
                    <div class="quality-bar quality-bar-hoax"><div class="quality-bar-fill" style="width:${pct}%"></div></div>
                    <span class="quality-bar-value">${val.toFixed(2)}</span>
                </div>`;
            }
            html += `</div>`;
        }

        // v0.8.3 — Derived Metadata section. Shows the ufo-dedup
        // pipeline's canonical analysis values alongside raw columns
        // in Observation above. When standardized_shape / primary_color
        // / dominant_emotion are null the pipeline didn't find a
        // confident match — hide the row entirely rather than show
        // "None".
        //
        // v0.8.5 — adds a "Movement" row that renders one chip per
        // category the pipeline detected. Categories come from the
        // r.movement_categories JSON array which api_sighting parses
        // server-side from the TEXT column.
        const hasMovementList = Array.isArray(r.movement_categories) && r.movement_categories.length > 0;
        const hasDerived = (
            r.standardized_shape || r.primary_color || r.dominant_emotion ||
            r.has_description != null || r.has_media != null ||
            r.has_movement_mentioned != null || hasMovementList
        );
        if (hasDerived) {
            html += `<div class="detail-section"><h3>Derived Metadata</h3>`;
            if (r.standardized_shape)
                html += `<div class="detail-row"><span class="detail-label">Shape (canon):</span> <span class="shape-tag">${escapeHtml(r.standardized_shape)}</span></div>`;
            if (r.primary_color)
                html += `<div class="detail-row"><span class="detail-label">Color:</span> ${escapeHtml(r.primary_color)}</div>`;
            if (r.dominant_emotion)
                html += `<div class="detail-row"><span class="detail-label">Emotion:</span> ${escapeHtml(r.dominant_emotion)}</div>`;
            if (r.has_description != null) {
                const tag = r.has_description
                    ? `<span class="popup-desc-badge has-desc">[ DESC ]</span>`
                    : `<span class="popup-desc-badge no-desc">[ NO DESC ]</span>`;
                html += `<div class="detail-row"><span class="detail-label">Description:</span> ${tag}</div>`;
            }
            if (r.has_media != null) {
                const tag = r.has_media
                    ? `<span class="popup-desc-badge has-desc">[ MEDIA ]</span>`
                    : `<span class="popup-desc-badge no-desc">[ NO MEDIA ]</span>`;
                html += `<div class="detail-row"><span class="detail-label">Media:</span> ${tag}</div>`;
            }
            // v0.8.5 — Movement row. Chips show every category the
            // ufo-dedup pipeline detected in the narrative. Empty
            // array → render nothing (the boolean flag below still
            // carries meaning). Populated array → one chip per cat.
            if (hasMovementList) {
                const chips = r.movement_categories
                    .map(c => `<span class="movement-chip">${escapeHtml(c)}</span>`)
                    .join(" ");
                html += `<div class="detail-row"><span class="detail-label">Movement:</span> ${chips}</div>`;
            } else if (r.has_movement_mentioned != null) {
                const tag = r.has_movement_mentioned
                    ? `<span class="popup-desc-badge has-desc">[ MOVEMENT ]</span>`
                    : `<span class="popup-desc-badge no-desc">[ STATIC ]</span>`;
                html += `<div class="detail-row"><span class="detail-label">Movement:</span> ${tag}</div>`;
            }
            html += `</div>`;
        }

        // Resolution / Explanation — short free text from the source
        // record. v0.8.3 keeps this field because it's structured
        // enough ("Chinese lantern", "Venus at low horizon") and
        // genuinely useful context for explained sightings. Flagged
        // for science-team cleanup in docs/V083_BACKLOG.md under
        // "Science-team cleanup of free-text fields".
        if (r.explanation) {
            html += `<div class="detail-section detail-full-width"><h3>Explanation</h3>
                <div class="detail-row">${escapeHtml(r.explanation)}</div></div>`;
        }

        // Duplicates
        if (r.duplicates && r.duplicates.length > 0) {
            html += `<div class="detail-section detail-full-width"><h3>Possible Duplicates (${r.duplicates.length})</h3>`;
            r.duplicates.forEach(d => {
                const dloc = formatLocation(d.city, d.state, "");
                html += `<div class="dupe-row" onclick="openDetail(${d.id})">
                    ${sourceBadge(d.source)}
                    <span>${d.date || "?"}</span>
                    <span>${escapeHtml(dloc)}</span>
                    <span class="dupe-score">${d.score ? (d.score * 100).toFixed(0) + "%" : "?"}</span>
                    <span class="dupe-method">${d.method || ""}</span>
                </div>`;
            });
            html += `</div>`;
        }

        // v0.8.3 — no Raw JSON toggle. The `raw_json` column is one
        // of the 4 that scripts/strip_raw_for_public.py drops, and
        // /api/sighting/:id never returns it anymore. The section
        // was useful for debugging the original ETL but those days
        // are behind us now.

        html += "</div>";
        body.innerHTML = html;

        // Render mini-map if coords exist
        if (r.latitude && r.longitude) {
            setTimeout(() => {
                const miniEl = document.getElementById("detail-minimap");
                if (miniEl) {
                    const miniMap = L.map(miniEl, {
                        center: [r.latitude, r.longitude],
                        zoom: 10,
                        zoomControl: false,
                        attributionControl: false,
                        dragging: false,
                        scrollWheelZoom: false,
                    });
                    // v0.8.4 — detail mini-map matches the active theme
                    // so the marker circle has consistent contrast
                    // whether the user is on SIGNAL or DECLASS.
                    L.tileLayer(TILE_URLS[_currentTheme()], {
                        attribution: TILE_ATTRIBUTION,
                        maxZoom: 19,
                        detectRetina: true,
                    }).addTo(miniMap);
                    L.circleMarker([r.latitude, r.longitude], {
                        radius: 8,
                        fillColor: "#e15759",
                        color: "#b84445",
                        weight: 2,
                        fillOpacity: 0.8,
                    }).addTo(miniMap);
                }
            }, 100);
        }

    } catch (err) {
        body.innerHTML = `<p class="error">Error loading sighting details.</p>
            <p class="error" style="font-size:12px;margin-top:8px;">${escapeHtml(err.message || String(err))}</p>`;
        console.error("openDetail error:", err);
    }
}

// =========================================================================
// BYOK ("Bring Your Own Key") AI chat
// =========================================================================
// The chat happens entirely in the user's browser. They paste their own
// LLM provider API key, which is stored in localStorage and sent
// directly to the provider — never to ufosint-explorer servers.
//
// When the LLM requests a tool call (e.g. "search_sightings"), the
// browser hits our /api/tool/<name> endpoint to execute it server-side
// (we hold the PG credentials), then feeds the result back to the LLM
// and continues the conversation.
//
// This means: zero inference cost to us, zero rate limits to manage,
// and users can use any model they prefer (Claude, GPT-4, Llama, etc).
// =========================================================================

const AI = {
    settings: null,           // {provider, apiKey, model}
    tools: null,              // OpenAI-format tool definitions, fetched once
    history: [],              // chat history for the LLM (system + user + assistant)
    activeStream: null,       // AbortController for in-flight requests
    busy: false,
};

const PROVIDER_DEFAULTS = {
    openrouter: {
        url: "https://openrouter.ai/api/v1/chat/completions",
        defaultModel: "openrouter/free",
        keyHeader: "Authorization",
        keyPrefix: "Bearer ",
    },
    openai: {
        url: "https://api.openai.com/v1/chat/completions",
        defaultModel: "gpt-4o-mini",
        keyHeader: "Authorization",
        keyPrefix: "Bearer ",
    },
    anthropic: {
        // Anthropic's chat API isn't OpenAI-compatible directly — but
        // it supports tools natively. We use the messages endpoint with
        // a slightly different request shape (handled in callLLM).
        url: "https://api.anthropic.com/v1/messages",
        defaultModel: "claude-haiku-4-5",
        keyHeader: "x-api-key",
        keyPrefix: "",
        extraHeaders: {
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
        },
    },
};

const SYSTEM_PROMPT = `You are an assistant for the UFOSINT unified UFO sightings database.
You help users explore 614,505 deduplicated sighting records from 5 sources
(NUFORC, MUFON, UFOCAT, UPDB, UFO-search) covering dates from antiquity to 2026.

You have access to tools that query the database read-only:
- search_sightings: free-text + filter search
- get_sighting: full detail for one record
- get_stats: top-level database statistics
- get_timeline: counts by year (or by month for a specific year)
- find_duplicates_for: cross-source duplicate candidates for a sighting
- count_by: top-N rankings (shapes, states, sources, etc)

Use these tools liberally — they're cheap and the user is expecting them.
Prefer concrete data over speculation. When you return a list of sightings,
mention specific examples (id + date + location + shape) so the user can
click through and see the full record. Keep answers concise and factual;
this is a research tool, not a creative writing assistant.

Date params accept either a year (1973) or an ISO date (1973-10-15).
Source names are exactly: NUFORC, MUFON, UFOCAT, UPDB, UFO-search.`;


function loadAISettings() {
    try {
        const raw = localStorage.getItem("ufosint.ai.settings");
        if (raw) AI.settings = JSON.parse(raw);
    } catch (_) { AI.settings = null; }
}

function saveAISettings(s) {
    AI.settings = s;
    localStorage.setItem("ufosint.ai.settings", JSON.stringify(s));
}

function clearAISettings() {
    AI.settings = null;
    localStorage.removeItem("ufosint.ai.settings");
}

function aiSettingsUI() {
    const provider = document.getElementById("ai-provider").value;
    const apiKey = document.getElementById("ai-api-key").value.trim();
    const model = document.getElementById("ai-model").value.trim();
    return { provider, apiKey, model };
}

function applySettingsToUI() {
    if (!AI.settings) return;
    const p = document.getElementById("ai-provider");
    const k = document.getElementById("ai-api-key");
    const m = document.getElementById("ai-model");
    if (p) p.value = AI.settings.provider || "openrouter";
    if (k) k.value = AI.settings.apiKey || "";
    if (m) m.value = AI.settings.model || "";
}

function initSettingsMenu() {
    const btn = document.getElementById("settings-btn");
    const menu = document.getElementById("settings-menu");
    if (!btn || !menu) return;

    function openMenu() {
        menu.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        btn.classList.add("active");
        // Close on outside click
        setTimeout(() => document.addEventListener("click", closeOnOutside), 0);
        // Close on Escape
        document.addEventListener("keydown", closeOnEscape);
    }
    function closeMenu() {
        menu.hidden = true;
        btn.setAttribute("aria-expanded", "false");
        btn.classList.remove("active");
        document.removeEventListener("click", closeOnOutside);
        document.removeEventListener("keydown", closeOnEscape);
    }
    function closeOnOutside(e) {
        if (!menu.contains(e.target) && e.target !== btn) closeMenu();
    }
    function closeOnEscape(e) { if (e.key === "Escape") closeMenu(); }

    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (menu.hidden) openMenu();
        else closeMenu();
    });

    // Each menu item switches to its data-tab and closes the menu
    menu.querySelectorAll("[data-tab]").forEach(item => {
        item.addEventListener("click", () => {
            const tab = item.dataset.tab;
            closeMenu();
            switchTab(tab);
        });
    });
}


// =========================================================================
// Sprint 3: Filter bar polish — auto-apply, is-dirty, mobile collapse,
// active filter counts. v0.8.7 dropped the advanced drawer (all dead
// filters) and narrowed the auto-apply + dirty lists to the 6
// surviving fields.
// =========================================================================

// Selects that should auto-apply with a debounce. Text inputs (the
// date range) keep the explicit Apply button because you don't want
// fire-on-keystroke there. v0.8.7: trimmed to the 4 surviving
// <select> filters.
const AUTO_APPLY_SELECT_IDS = [
    "filter-shape",
    "filter-source",
    "filter-color",
    "filter-emotion",
];

// Text inputs that mark the Apply button "dirty" on input. User must
// still click Apply to commit these since typing a date char-by-char
// would thrash queries.
const DIRTY_INPUT_IDS = [
    "filter-date-from",
    "filter-date-to",
];

let _autoApplyTimer = null;

function initFilterBarPolish() {
    // ----- Auto-apply on select change (250ms debounce) -----
    AUTO_APPLY_SELECT_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener("change", () => {
            clearTimeout(_autoApplyTimer);
            _autoApplyTimer = setTimeout(() => {
                if (typeof applyFilters === "function") applyFilters();
            }, 250);
        });
    });

    // ----- is-dirty on text inputs -----
    const applyBtn = document.getElementById("btn-apply-filters");
    DIRTY_INPUT_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el || !applyBtn) return;
        el.addEventListener("input", () => applyBtn.classList.add("is-dirty"));
    });
    applyBtn?.addEventListener("click", () => applyBtn.classList.remove("is-dirty"));

    // v0.8.7: the "More filters" drawer + badge was removed along
    // with the filter-collection / filter-hynek / filter-vallee
    // dropdowns it used to hold. The remaining 6 filters all fit
    // in the main filter bar.

    // ----- Mobile filter bar toggle + active-count badge -----
    const mobileBtn = document.getElementById("btn-mobile-filters");
    const bar = document.getElementById("filters-bar");
    const mobileCount = document.getElementById("mobile-filter-count");

    function updateMobileCount() {
        if (!mobileCount) return;
        // v0.8.7: FILTER_FIELDS was trimmed to the 6 surviving
        // filters, so we can just count values without any
        // per-field exclusion logic. Movement cluster counts as
        // one if any category is checked.
        let n = FILTER_FIELDS.reduce((count, f) => {
            const el = document.getElementById(f.id);
            return count + (el && el.value ? 1 : 0);
        }, 0);
        if (_readMovementCats().length) n++;
        if (n > 0) {
            mobileCount.textContent = String(n);
            mobileCount.hidden = false;
        } else {
            mobileCount.hidden = true;
        }
    }

    if (mobileBtn && bar) {
        // Start collapsed on narrow screens; initFilterBarPolish runs
        // after DOMContentLoaded so we can just check the window width.
        if (window.innerWidth <= 720) {
            bar.classList.add("is-collapsed");
            mobileBtn.setAttribute("aria-expanded", "false");
        }
        mobileBtn.addEventListener("click", () => {
            const isCollapsed = bar.classList.toggle("is-collapsed");
            mobileBtn.setAttribute("aria-expanded", String(!isCollapsed));
        });
        // Every filter change updates the mobile count
        FILTER_FIELDS.forEach(f => {
            const el = document.getElementById(f.id);
            el?.addEventListener("change", updateMobileCount);
            el?.addEventListener("input", updateMobileCount);
        });
        // Movement cluster change events also update the count.
        // The cluster is populated asynchronously from deck.js meta,
        // so we delegate via the host element.
        const movHost = document.getElementById("filter-movement-cluster");
        movHost?.addEventListener("change", updateMobileCount);
        updateMobileCount();
    }
}


// =========================================================================
// Map place search (Nominatim) + browser geolocation
// =========================================================================
// Lets users type "Phoenix, AZ" or click "Near me" to jump the map.
// Nominatim is the OpenStreetMap geocoder — free, no API key required,
// CORS-enabled. Their usage policy asks for a custom User-Agent and a
// max of one request per second. The browser sets its own UA so we
// can't override it; we add HTTP-Referer-style identification via the
// `email` query param when running anywhere we control. We throttle to
// one in-flight request at a time and add a 400ms typing debounce.

let _placeSearchTimer = null;
let _placeSearchAbort = null;

async function geocodePlace(query) {
    if (_placeSearchAbort) _placeSearchAbort.abort();
    _placeSearchAbort = new AbortController();
    const url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + encodeURIComponent(query);
    const resp = await fetch(url, {
        signal: _placeSearchAbort.signal,
        headers: { "Accept": "application/json" },
    });
    if (!resp.ok) throw new Error("Place search failed (" + resp.status + ")");
    const results = await resp.json();
    if (!results.length) throw new Error("No matches for that place.");
    const top = results[0];
    return {
        lat: parseFloat(top.lat),
        lng: parseFloat(top.lon),
        display: top.display_name,
        bbox: top.boundingbox,  // [south, north, west, east] as strings
    };
}

function initMapPlaceSearch() {
    const input = document.getElementById("place-input");
    const goBtn = document.getElementById("btn-place-search");
    const locBtn = document.getElementById("btn-locate-me");
    if (!input || !goBtn) return;

    async function runPlaceSearch() {
        const q = input.value.trim();
        if (!q) return;
        const restore = disableButtonWhilePending(goBtn, "…");
        try {
            const place = await geocodePlace(q);
            // Prefer a viewport fit if Nominatim gave us a bounding box,
            // otherwise zoom to a reasonable level (city ~ zoom 11).
            if (place.bbox && place.bbox.length === 4) {
                const [s, n, w, e] = place.bbox.map(parseFloat);
                state.map.fitBounds([[s, w], [n, e]], { maxZoom: 12 });
            } else {
                state.map.setView([place.lat, place.lng], 11);
            }
        } catch (err) {
            if (!isAbortError(err)) {
                document.getElementById("map-status").textContent = "Couldn't find that place — try a city + country.";
                console.warn("place search:", err);
            }
        } finally {
            restore();
        }
    }

    goBtn.addEventListener("click", runPlaceSearch);
    input.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); runPlaceSearch(); }
    });

    if (locBtn) {
        locBtn.addEventListener("click", () => {
            if (!navigator.geolocation) {
                document.getElementById("map-status").textContent = "Geolocation isn't available in this browser.";
                return;
            }
            const restore = disableButtonWhilePending(locBtn, "Locating…");
            navigator.geolocation.getCurrentPosition(
                pos => {
                    state.map.setView([pos.coords.latitude, pos.coords.longitude], 10);
                    restore();
                },
                err => {
                    document.getElementById("map-status").textContent = "Couldn't get your location: " + err.message;
                    restore();
                },
                { timeout: 8000, enableHighAccuracy: false }
            );
        });
    }
}


// =========================================================================
// Search panel: export buttons + copy-link button
// =========================================================================

// v0.8.6: initSearchActions() was deleted along with the Search
// panel's CSV/JSON export buttons and the copy-link button. The
// /api/export.csv and /api/export.json routes still work via
// direct URL for anyone who wants scripted downloads, but nothing
// in the UI binds to them anymore. Add an Observatory rail button
// in a future release if users ask for filtered exports.


function aiInitListeners() {
    const settingsBtn = document.getElementById("ai-settings-btn");
    const settingsPane = document.getElementById("ai-settings-pane");
    const saveBtn = document.getElementById("ai-save-key");
    const clearBtn = document.getElementById("ai-clear-key");
    const sendBtn = document.getElementById("ai-send");
    const input = document.getElementById("ai-input");
    if (settingsBtn) settingsBtn.addEventListener("click", () => {
        settingsPane.style.display = settingsPane.style.display === "none" ? "block" : "none";
    });
    if (saveBtn) saveBtn.addEventListener("click", () => {
        const s = aiSettingsUI();
        if (!s.apiKey) {
            alert("Please paste an API key first.");
            return;
        }
        saveAISettings(s);
        settingsPane.style.display = "none";
        renderSystemMsg("API key saved (browser-only). You can now ask questions.");
    });
    if (clearBtn) clearBtn.addEventListener("click", () => {
        clearAISettings();
        applySettingsToUI();
        document.getElementById("ai-api-key").value = "";
        renderSystemMsg("API key cleared.");
    });
    if (sendBtn) sendBtn.addEventListener("click", () => askAI(input.value.trim()));
    if (input) input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askAI(input.value.trim()); }
    });

    // Floating widget
    const fab = document.getElementById("chat-fab");
    const popover = document.getElementById("chat-popover");
    const popClose = document.getElementById("chat-popover-close");
    const popExpand = document.getElementById("chat-popover-expand");
    const popInput = document.getElementById("chat-popover-input");
    const popSend = document.getElementById("chat-popover-send");
    if (fab) fab.addEventListener("click", () => {
        popover.style.display = popover.style.display === "none" ? "flex" : "none";
        if (popover.style.display === "flex") popInput?.focus();
    });
    if (popClose) popClose.addEventListener("click", () => { popover.style.display = "none"; });
    if (popExpand) popExpand.addEventListener("click", () => {
        popover.style.display = "none";
        switchTab("ai");
    });
    if (popSend) popSend.addEventListener("click", () => {
        const v = popInput.value.trim();
        if (!v) return;
        // Bounce the question over to the main panel
        switchTab("ai");
        popover.style.display = "none";
        document.getElementById("ai-input").value = v;
        popInput.value = "";
        askAI(v);
    });
    if (popInput) popInput.addEventListener("keydown", e => {
        if (e.key === "Enter") popSend.click();
    });
}


async function loadAITools() {
    if (AI.tools) return AI.tools;
    const data = await fetchJSON("/api/tools-catalog");
    AI.tools = data.tools;
    return AI.tools;
}


/**
 * Main chat entry point. Called from the input box, the floating widget,
 * and the empty-state suggestion buttons.
 */
async function askAI(text) {
    text = (text || "").trim();
    if (!text) return;
    if (AI.busy) return;            // already in flight
    if (!AI.settings || !AI.settings.apiKey) {
        renderSystemMsg("No API key set. Click Settings above and paste a key from your provider.");
        document.getElementById("ai-settings-pane").style.display = "block";
        return;
    }

    document.getElementById("ai-input").value = "";
    renderUserMsg(text);
    AI.history.push({ role: "user", content: text });

    AI.busy = true;
    try {
        await loadAITools();
        await runChatLoop();
    } catch (err) {
        console.error("askAI error:", err);
        renderErrorMsg(err.message || String(err));
    } finally {
        AI.busy = false;
    }
}

window.askAI = askAI;


/**
 * The chat loop:
 *  - call the LLM with the conversation + tool definitions
 *  - if it returns text, display and stop
 *  - if it returns tool calls, execute each via /api/tool/<name>,
 *    feed the results back, loop
 *  - hard cap on iterations to prevent runaway costs
 */
const MAX_TOOL_ITERATIONS = 8;

async function runChatLoop() {
    showThinking();
    for (let iter = 0; iter < MAX_TOOL_ITERATIONS; iter++) {
        const reply = await callLLM(AI.history, AI.tools);
        // reply: { content: string|null, tool_calls: [{id, name, arguments}] }

        if (reply.content) {
            // Add assistant message to history first (with any tool_calls so the
            // next iteration knows about them) — then render once we know
            // there are no more tool calls.
        }

        // Append assistant message to history
        const histMsg = { role: "assistant", content: reply.content || "" };
        if (reply.tool_calls && reply.tool_calls.length) {
            histMsg.tool_calls = reply.tool_calls.map(tc => ({
                id: tc.id,
                type: "function",
                function: { name: tc.name, arguments: JSON.stringify(tc.arguments || {}) },
            }));
        }
        AI.history.push(histMsg);

        // If there's text, render it
        if (reply.content) {
            hideThinking();
            renderAssistantMsg(reply.content);
        }

        // No tool calls -> we're done
        if (!reply.tool_calls || reply.tool_calls.length === 0) {
            return;
        }

        // Execute each tool call against our backend, render an inline
        // summary card, and append the result to history as a tool message
        for (const tc of reply.tool_calls) {
            renderToolCallStart(tc.name, tc.arguments);
            let result;
            try {
                const resp = await fetch(`/api/tool/${encodeURIComponent(tc.name)}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(tc.arguments || {}),
                });
                result = await resp.json();
            } catch (err) {
                result = { error: String(err) };
            }
            // Stash the most recent search args so the "view all on map"
            // affordance on inline result cards has something to link to.
            if (tc.name === "search_sightings") {
                _lastSearchArgs = tc.arguments || {};
            }
            renderToolCallResult(tc.name, result);
            AI.history.push({
                role: "tool",
                tool_call_id: tc.id,
                content: JSON.stringify(result),
            });
        }
        showThinking();
    }
    hideThinking();
    renderSystemMsg("(stopped after " + MAX_TOOL_ITERATIONS + " tool iterations)");
}


/**
 * Call the user's chosen LLM provider. Returns
 *   { content: string|null, tool_calls: [{id, name, arguments}] }
 *
 * Handles OpenAI-compatible providers (OpenRouter, OpenAI) and
 * Anthropic's slightly different message shape.
 */
async function callLLM(history, tools) {
    const cfg = PROVIDER_DEFAULTS[AI.settings.provider] || PROVIDER_DEFAULTS.openrouter;
    const model = AI.settings.model || cfg.defaultModel;

    if (AI.settings.provider === "anthropic") {
        // Anthropic Messages API: system goes in a top-level field, tools
        // use a slightly different schema (input_schema instead of parameters).
        const anthropicTools = tools.map(t => ({
            name: t.function.name,
            description: t.function.description,
            input_schema: t.function.parameters,
        }));
        const messages = history
            .filter(m => m.role !== "system")
            .map(m => {
                if (m.role === "assistant" && m.tool_calls) {
                    const blocks = [];
                    if (m.content) blocks.push({ type: "text", text: m.content });
                    for (const tc of m.tool_calls) {
                        blocks.push({
                            type: "tool_use",
                            id: tc.id,
                            name: tc.function.name,
                            input: JSON.parse(tc.function.arguments || "{}"),
                        });
                    }
                    return { role: "assistant", content: blocks };
                }
                if (m.role === "tool") {
                    return {
                        role: "user",
                        content: [{ type: "tool_result", tool_use_id: m.tool_call_id, content: m.content }],
                    };
                }
                return { role: m.role, content: m.content };
            });
        const body = {
            model,
            system: SYSTEM_PROMPT,
            messages,
            tools: anthropicTools,
            max_tokens: 2048,
        };
        const resp = await fetch(cfg.url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                [cfg.keyHeader]: cfg.keyPrefix + AI.settings.apiKey,
                ...(cfg.extraHeaders || {}),
            },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const errText = await resp.text();
            throw new Error(`Anthropic ${resp.status}: ${errText.substring(0, 300)}`);
        }
        const data = await resp.json();
        const out = { content: null, tool_calls: [] };
        for (const block of (data.content || [])) {
            if (block.type === "text") out.content = (out.content || "") + block.text;
            if (block.type === "tool_use") {
                out.tool_calls.push({ id: block.id, name: block.name, arguments: block.input || {} });
            }
        }
        return out;
    }

    // OpenAI-compatible (OpenRouter / OpenAI)
    const messages = [{ role: "system", content: SYSTEM_PROMPT }, ...history];
    const body = {
        model,
        messages,
        tools,
        tool_choice: "auto",
    };
    const resp = await fetch(cfg.url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            [cfg.keyHeader]: cfg.keyPrefix + AI.settings.apiKey,
            "HTTP-Referer": window.location.origin,
            "X-Title": "UFOSINT Explorer",
        },
        body: JSON.stringify(body),
    });
    if (!resp.ok) {
        const errText = await resp.text();
        throw new Error(`${AI.settings.provider} ${resp.status}: ${errText.substring(0, 300)}`);
    }
    const data = await resp.json();
    const msg = data.choices?.[0]?.message;
    if (!msg) throw new Error("No message in LLM response: " + JSON.stringify(data).substring(0, 300));
    const out = { content: msg.content || null, tool_calls: [] };
    for (const tc of (msg.tool_calls || [])) {
        let args = {};
        try { args = JSON.parse(tc.function.arguments || "{}"); }
        catch (_) { args = {}; }
        out.tool_calls.push({ id: tc.id, name: tc.function.name, arguments: args });
    }
    return out;
}


// ----- Rendering -----

function aiMessagesEl() { return document.getElementById("ai-messages"); }

function clearEmptyState() {
    const el = aiMessagesEl();
    const empty = el.querySelector(".ai-empty");
    if (empty) empty.remove();
}

function appendBubble(cls, html) {
    clearEmptyState();
    const el = aiMessagesEl();
    const div = document.createElement("div");
    div.className = "ai-msg " + cls;
    div.innerHTML = html;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
    return div;
}

function renderUserMsg(text) {
    appendBubble("user", `<div class="ai-msg-body">${escapeHtml(text)}</div>`);
}
function renderAssistantMsg(text) {
    appendBubble("assistant", `<div class="ai-msg-body">${formatMarkdownLite(text)}</div>`);
}
function renderSystemMsg(text) {
    appendBubble("system", `<div class="ai-msg-body">${escapeHtml(text)}</div>`);
}
function renderErrorMsg(text) {
    appendBubble("error", `<div class="ai-msg-body"><strong>Error:</strong> ${escapeHtml(text)}</div>`);
}

let _thinkingEl = null;
function showThinking() {
    if (_thinkingEl) return;
    _thinkingEl = appendBubble("thinking", '<div class="ai-msg-body"><span class="loading-pulse">ANALYZING QUERY</span><span class="term-cursor" aria-hidden="true"></span></div>');
}
function hideThinking() {
    if (_thinkingEl) { _thinkingEl.remove(); _thinkingEl = null; }
}

function renderToolCallStart(name, args) {
    const argsStr = Object.keys(args || {}).length
        ? Object.entries(args).map(([k, v]) => `${k}=${typeof v === "string" ? '"' + v + '"' : v}`).join(", ")
        : "";
    appendBubble("tool", `<div class="ai-msg-body"><span class="ai-tool-name">${escapeHtml(name)}</span>(<span class="ai-tool-args">${escapeHtml(argsStr)}</span>)</div>`);
}

function renderToolCallResult(name, result) {
    if (!result) return;
    if (result.error && Object.keys(result).length === 1) {
        appendBubble("tool error", `<div class="ai-msg-body"><strong>${escapeHtml(name)} error:</strong> ${escapeHtml(result.error)}</div>`);
        return;
    }
    // Pretty-render specific tool results
    if (name === "search_sightings" && Array.isArray(result.results)) {
        const cards = result.results.slice(0, 8).map(r => {
            const loc = formatLocation(r.city, r.state, r.country);
            return `<a class="ai-result-card" href="#" onclick="openDetail(${r.id}); return false;">
                <div class="ai-result-head">
                    <span class="ai-result-date">${escapeHtml(r.date_event || "—")}</span>
                    ${sourceBadge(r.source)}
                    ${r.shape ? `<span class="shape-tag">${escapeHtml(r.shape)}</span>` : ""}
                </div>
                <div class="ai-result-loc">${escapeHtml(loc) || "Unknown location"}</div>
                ${r.description ? `<div class="ai-result-desc">${escapeHtml(r.description.substring(0, 200))}${r.description.length > 200 ? "…" : ""}</div>` : ""}
            </a>`;
        }).join("");
        const more = result.total > result.results.length ? `<div class="ai-result-more">+${(result.total - result.results.length).toLocaleString()} more — <a href="#" onclick="navigateToSearchFromAI(${JSON.stringify(getLastSearchArgs()).replace(/"/g, '&quot;')}); return false;">view all on map →</a></div>` : "";
        appendBubble("tool-result", `<div class="ai-msg-body"><div class="ai-result-summary">${result.total.toLocaleString()} matching sightings (showing ${Math.min(8, result.results.length)})</div>${cards}${more}</div>`);
        return;
    }
    if (name === "count_by" && Array.isArray(result.rows)) {
        const max = Math.max(1, ...result.rows.map(r => r.count));
        const bars = result.rows.slice(0, 12).map(r =>
            `<div class="ai-bar-row">
                <div class="ai-bar-label">${escapeHtml(String(r.value))}</div>
                <div class="ai-bar-track"><div class="ai-bar-fill" style="width:${(r.count / max * 100).toFixed(1)}%"></div></div>
                <div class="ai-bar-count">${r.count.toLocaleString()}</div>
            </div>`).join("");
        appendBubble("tool-result", `<div class="ai-msg-body"><div class="ai-result-summary">Top ${result.rows.length} by ${escapeHtml(result.field)}</div>${bars}</div>`);
        return;
    }
    if (name === "get_stats") {
        const r = result;
        appendBubble("tool-result", `<div class="ai-msg-body"><div class="ai-result-summary">${r.total_sightings.toLocaleString()} total sightings · ${r.geocoded_locations.toLocaleString()} geocoded · ${r.duplicate_pairs.toLocaleString()} duplicate pairs · ${r.date_range.min} to ${r.date_range.max}</div></div>`);
        return;
    }
    // Default: small JSON dump
    const pretty = JSON.stringify(result, null, 2);
    const truncated = pretty.length > 800 ? pretty.substring(0, 800) + "\n..." : pretty;
    appendBubble("tool-result", `<div class="ai-msg-body"><pre class="ai-tool-json">${escapeHtml(truncated)}</pre></div>`);
}

// Last search args, captured for the AI "view all on map" link
// (v0.8.6: link now routes to Observatory via the rewritten
// navigateToSearch helper)
let _lastSearchArgs = {};
function getLastSearchArgs() { return _lastSearchArgs; }

function navigateToSearchFromAI(args) {
    if (typeof args === "string") {
        try { args = JSON.parse(args); } catch (_) { args = {}; }
    }
    navigateToSearch(args || {}, true);
}
window.navigateToSearchFromAI = navigateToSearchFromAI;

// Tiny markdown -> HTML for assistant messages: **bold**, *italic*,
// `code`, single newlines as <br>, double newlines as paragraphs.
function formatMarkdownLite(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/\*([^*\s][^*]*)\*/g, '<em>$1</em>');
    // Paragraphs
    out = out.split(/\n{2,}/).map(p => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
    return out;
}


// Make functions globally available for inline onclick handlers in
// dynamically-injected markup (popup links, filter chips, pager buttons,
// empty-state CTAs, etc.).
window.openDetail   = openDetail;
window.clearFilters = clearFilters;
// v0.8.6: removeFilter, goToPage, doSearch window bindings removed
// along with the Search panel's pager and chip-remove handlers.

function closeModal() {
    const overlay = document.getElementById("modal-overlay");
    // Class-based hide; CSS handles the fade-out and the delayed
    // visibility change so the transition plays cleanly.
    overlay.classList.remove("is-open");
    // Clear the body after the transition to avoid flashing the old
    // content if the same modal is reopened quickly.
    setTimeout(() => {
        if (!overlay.classList.contains("is-open")) {
            document.getElementById("modal-body").innerHTML = "";
        }
    }, 200);
    document.removeEventListener("keydown", _modalEscapeHandler);
    // Return focus to whatever element opened the modal so keyboard users
    // don't get dropped at the top of the page.
    if (_modalReturnFocus && typeof _modalReturnFocus.focus === "function") {
        _modalReturnFocus.focus();
    }
    _modalReturnFocus = null;
}
