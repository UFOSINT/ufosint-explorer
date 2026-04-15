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
    // v0.9.0 — touch-primary feature detect. Adds body.is-touch when
    // the device's primary input is a coarse pointer with no hover
    // capability. More robust than width-based breakpoints for real
    // phones, foldables, and iPads-in-landscape (wide viewport,
    // coarse pointer). Desktop users resizing their browser narrow
    // ALSO get the mobile layout via a parallel @media (max-width:
    // 700px) rule in CSS, so both input classes are covered.
    const _touchMQ = window.matchMedia("(hover: none) and (pointer: coarse)");
    const applyTouchClass = () => {
        document.body.classList.toggle("is-touch", _touchMQ.matches);
    };
    applyTouchClass();
    if (_touchMQ.addEventListener) {
        _touchMQ.addEventListener("change", applyTouchClass);
    } else if (_touchMQ.addListener) {
        // Older Safari/Edge fallback
        _touchMQ.addListener(applyTouchClass);
    }

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
            state.statsData = statsData;  // v0.11.2: stash for tour
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
    // v0.10.0: Apply + Clear buttons removed. All filters are now
    // live-reactive via initFilterBarPolish(). The Reset link is
    // wired there too.
    // Legacy: if the old buttons somehow survive in the DOM (e.g.
    // stale HTML cache), wire them defensively so they don't break.
    document.getElementById("btn-apply-filters")?.addEventListener("click", applyFilters);
    document.getElementById("btn-clear-filters")?.addEventListener("click", clearFilters);

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

    // v0.11.2: Help tour button (always available in header)
    initHelpTourButton();

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

    // v0.11.4: Region (geofence) draw tool — must come after initMap()
    // because it attaches pointer listeners to state.map's container.
    initRegionDrawTool();
    // If the hash carried a region= param, apply it now that state.map
    // exists. applyHashToFilters() stashed the parsed bbox on
    // state.pendingRegionFilter.
    _applyPendingRegionFilter();

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

    // v0.11.2: First-visit cinematic intro + tour. Runs AFTER the map
    // and Observatory are initialized so the tour has real DOM targets
    // to highlight. Data loads behind the overlay; the intro is purely
    // visual. statsPromise is already in-flight from line 103.
    try {
        if (!localStorage.getItem(TOUR_STORAGE_KEY)) {
            const stats = await statsPromise.catch(() => null);
            const total = stats ? stats.total_sightings : 614505;
            startTour(false, total);
        } else {
            skipCinematicIntro();
        }
    } catch (_) {
        // localStorage blocked (private browsing) — skip intro
        skipCinematicIntro();
    }
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
    // v0.11.3: "has data" option lets users filter to rows that
    // have ANY value for this field (index != 0). Appears between
    // the "All ..." placeholder and the individual values.
    const fieldLabel = placeholder.replace(/^All\s*/i, "");
    el.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`;
    // Count how many values exist (skip index-0 null)
    const populated = (values || []).filter(v => !!v);
    if (populated.length > 0) {
        const hasDataOpt = document.createElement("option");
        hasDataOpt.value = "__has_data__";
        hasDataOpt.textContent = `All — has ${fieldLabel.toLowerCase()} defined`;
        el.appendChild(hasDataOpt);
    }
    for (const v of (values || [])) {
        if (!v) continue;  // skip index-0 "unknown" placeholder
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = v;
        el.appendChild(opt);
    }
    if (prev) {
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

    // Compact badge — up to five chips separated by middle dots.
    // The first two (total, mapped) always render without the
    // .stats-chip-optional class — they're the headline numbers.
    // The derived chips (high-quality, with-movement) only render
    // once the v0.8.2/v0.8.3b columns are populated, AND carry the
    // .stats-chip-optional class so v0.9.0 CSS can hide them on
    // narrow viewports. The popover still shows everything.
    const chips = [
        `${total} sightings`,
        `${mapped} mapped`,
    ];
    if (highQStr) {
        chips.push(`<span class="stats-chip-optional">${highQStr} high quality</span>`);
    }
    if (withMovStr) {
        chips.push(`<span class="stats-chip-optional">${withMovStr} with movement</span>`);
    }
    chips.push(`<span class="stats-chip-optional">${dupes} possible duplicates</span>`);
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
    // v0.11: hide the shared TimeBrush on tabs where temporal
    // navigation doesn't apply (Methodology, AI, Connect).
    const hideBrush = (tab === "methodology" || tab === "ai" || tab === "connect");
    if (hideBrush) {
        document.body.setAttribute("data-hide-brush", "");
    } else {
        document.body.removeAttribute("data-hide-brush");
    }
    writeHash();
}

async function applyFilters() {
    // v0.10.0: the Apply button is gone — filters are live-reactive.
    // This function is still the central entry point for all filter
    // changes (called by the auto-apply debounce, rail toggles,
    // brush drag commit, and the Reset link). The btn-apply-filters
    // element no longer exists in the DOM, so the disableButtonWhile-
    // Pending call gracefully no-ops (returns a no-op restore).
    //
    // Clear any tab-local cross-filter when a global filter changes.
    // The cross-filter was computed against the OLD visibleIdx; the
    // new filter state makes it stale. The user can re-apply it
    // after the global filter settles.
    if (state.crossFilter) {
        state.crossFilter = null;
        _renderCrossFilterChips();
    }
    const applyBtn = document.getElementById("btn-apply-filters");
    const restore = disableButtonWhilePending(applyBtn, "Applying…");
    try {
        // v0.9.4-fix: ALWAYS run applyClientFilters first,
        // regardless of which tab is active. This updates
        // POINTS.visibleIdx from the current filter state.
        // Previously this only ran on the Observatory tab,
        // which meant Timeline and Insights were reading stale
        // visibleIdx and showing outdated charts when the user
        // changed filters while on those tabs.
        //
        // applyClientFilters also retallies the brush histogram
        // and refreshes Timeline/Insights cards via the hooks
        // it already has — so calling it here is sufficient for
        // cross-tab filter consistency. The tab-specific blocks
        // below handle map-layer refreshes and legacy fallbacks.
        const gpu = applyClientFilters();

        if (state.activeTab === "map" || state.activeTab === "observatory") {
            // v0.8.0: When the deck.gl layer is ready,
            // applyClientFilters() above already walked the
            // typed arrays and refreshed the layer. Falls
            // through to the legacy server fetch on ancient
            // browsers where deck.gl isn't available.
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
    // v0.11.4: region (geofence) filter. Encoded as
    // `region=rect:south,west;north,east` with 2-decimal precision.
    if (typeof _encodeRegionHash === "function") {
        const rh = _encodeRegionHash();
        if (rh) params.set("region", rh);
    }
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
        // v0.11.4 — region (geofence) filter deep-link restore.
        // Parses `rect:south,west;north,east` and applies to
        // state.regionFilter. _applyPendingRegionFilter below paints
        // the Leaflet rectangle once the map is ready.
        const regionParam = params.get("region");
        if (regionParam && typeof _decodeRegionHash === "function") {
            const r = _decodeRegionHash(regionParam);
            if (r) {
                state.pendingRegionFilter = r;
            }
        } else {
            state.pendingRegionFilter = null;
        }
        // v0.8.6: the legacy `q`, `page`, and `sort` URL params
        // belonged to the removed Search tab. Silently ignored so
        // pre-v0.8.6 deep links don't throw.
    } finally {
        state.hashLoading = false;
    }
}

// v0.11.4 — called after initMap() to restore a region filter from
// the URL hash. The L.rectangle needs state.map to exist; the hash
// parsing runs earlier in DOMContentLoaded.
function _applyPendingRegionFilter() {
    if (!state.pendingRegionFilter || !state.map) return;
    const r = state.pendingRegionFilter;
    state.pendingRegionFilter = null;
    if (typeof applyRegionFilter === "function") {
        applyRegionFilter(r);
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

    // v0.11.3: show/hide points-specific controls (color-by + dot size)
    const pc = document.getElementById("points-controls");
    if (pc) pc.hidden = (mode !== "points");
    // Hide legend when switching away from points
    if (mode !== "points") {
        const legend = document.getElementById("color-legend");
        if (legend) legend.hidden = true;
    }

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
    // v0.10.0-fix: the source dropdown stores numeric source_db_id
    // as its value (e.g. "1" for MUFON), but _rebuildVisible does
    // POINTS.sources.indexOf(f.sourceName) which needs the NAME
    // string ("MUFON"). Read the selected option's TEXT instead
    // of its value. When "All sources" is selected (value=""),
    // sourceName correctly becomes null (no filter).
    const srcEl = document.getElementById("filter-source");
    const srcVal = srcEl?.value;
    const srcName = srcVal ? (srcEl.selectedOptions?.[0]?.text || null) : null;

    const filter = {
        sourceName: srcName,
        shapeName:  document.getElementById("filter-shape")?.value  || null,
        colorName:  document.getElementById("filter-color")?.value  || null,
        emotionName: document.getElementById("filter-emotion")?.value || null,
        // v0.8.7 — multi-select movement category cluster. Array of
        // category names (OR-semantics bit mask in _rebuildVisible).
        movementCats: _readMovementCats(),
        yearFrom:   _parseYearFilter(document.getElementById("filter-date-from")?.value),
        yearTo:     _parseYearFilter(document.getElementById("filter-date-to")?.value),
        // v0.11.4 — region (geofence) from the REGION draw tool.
        // v0.11.5 — now supports rect / polygon / circle via
        // state.regionFilter.type. The bbox is always derived (used
        // as a fast pre-cull in deck.js) and the full shape is
        // passed in `regionShape` for polygon/circle point-in-shape
        // tests. When the TimeBrush toggle is OFF, _regionActive is
        // false which nulls both so the filter is bypassed without
        // losing the drawn geometry.
        bbox: (state.regionFilter && _regionActive && state.regionFilter.bbox)
            ? state.regionFilter.bbox.slice()
            : null,
        regionShape: (state.regionFilter && _regionActive &&
                      state.regionFilter.type !== "rect")
            ? state.regionFilter
            : null,
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

    // v0.11.9 — refresh the Observatory sidebar live analytics.
    // Cheap aggregate over POINTS.visibleIdx; ~3ms for 396k rows.
    if (typeof refreshRailAnalytics === "function") {
        refreshRailAnalytics();
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
        // v0.11.9 — wire the Observatory Data Quality gear popup
        if (typeof initObservatoryDqGear === "function") {
            initObservatoryDqGear();
        }
        state.observatoryMounted = true;
    }

    // v0.11.9 — refresh the sidebar live analytics on every visit
    if (typeof refreshRailAnalytics === "function") {
        refreshRailAnalytics();
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

// v0.11.3 — Wire the color-by dropdown and dot size slider in
// the Points topbar controls. Updates deck.gl layer in real time.
function wirePointsControls() {
    const select = document.getElementById("color-by-select");
    const slider = document.getElementById("dot-size-slider");
    const legend = document.getElementById("color-legend");

    if (select) {
        select.addEventListener("change", () => {
            const mode = select.value;
            if (window.UFODeck && window.UFODeck.setColorByMode) {
                window.UFODeck.setColorByMode(mode);
            }
            _renderColorLegend(legend, mode);
        });
    }
    if (slider) {
        slider.addEventListener("input", () => {
            const px = parseFloat(slider.value);
            if (window.UFODeck && window.UFODeck.setDotSize) {
                window.UFODeck.setDotSize(px);
            }
        });
    }
}

function _renderColorLegend(el, mode) {
    if (!el) return;
    if (mode === "default" || !window.UFODeck || !window.UFODeck.getColorLegend) {
        el.hidden = true;
        return;
    }
    const items = window.UFODeck.getColorLegend();
    if (!items || items.length === 0) {
        el.hidden = true;
        return;
    }

    const title = mode === "source" ? "Source" : mode === "shape" ? "Shape" : "Color";
    let html = `<div class="color-legend-title">${escapeHtml(title)}</div>`;
    for (const item of items) {
        const [r, g, b] = item.color;
        html += `<div class="color-legend-item">
            <span class="color-legend-swatch" style="background:rgb(${r},${g},${b})"></span>
            <span>${escapeHtml(item.label)}${item.count != null ? ` <span style="color:var(--text-faint)">(${item.count.toLocaleString()})</span>` : ""}</span>
        </div>`;
    }
    el.innerHTML = html;
    el.hidden = false;
}

function wireObservatoryModeToggle() {
    document.querySelectorAll(".mode-toggle .mode-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const mode = btn.dataset.mode;
            if (mode) toggleMapMode(mode);
        });
    });

    // v0.11.3 — Points controls: color-by + dot size
    wirePointsControls();

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
            // v0.9.3: value is now absolute (days of timeline per
            // wall-second) instead of a multiplier. Write into the
            // new field; the step loop reads it every frame so
            // changes take effect mid-playback.
            const v = parseFloat(e.target.value);
            if (Number.isFinite(v) && v > 0) {
                state.timeBrush.playStepDaysPerSec = v;
            }
        });
        // v0.9.3: sync the brush's default from whatever the
        // dropdown is currently showing, so future mutations of
        // the HTML <option selected> propagate without code
        // changes.
        if (state.timeBrush) {
            const v = parseFloat(speedSel.value);
            if (Number.isFinite(v) && v > 0) {
                state.timeBrush.playStepDaysPerSec = v;
            }
        }
    }
}

// Populate the left rail from /api/filters (already fetched at boot)
// and from /api/stats by-source counts. Rail checkboxes mirror the
// existing filter dropdowns one-way: toggling a rail checkbox writes
// into #filter-source / #filter-shape and calls applyFilters().
// First version supports single-select (matches the existing <select>
// semantics); multi-select is a future improvement.
function mountObservatoryRail() {
    // v0.11.9: the Sources + Shapes rail lists were removed in favor
    // of the Live Analytics sidebar. Those mounts are skipped but the
    // quality rail + accordion still need to run below, so we no
    // longer early-return here. Each section block bails gracefully
    // if its target list element is missing.
    const srcList = document.getElementById("rail-source-list");
    const shapeList = document.getElementById("rail-shape-list");

    // Source list: read options from #filter-source (already populated
    // at boot from /api/filters).
    const srcSelect = document.getElementById("filter-source");
    if (srcList) srcList.innerHTML = "";
    if (srcList && srcSelect) {
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
    if (shapeList) shapeList.innerHTML = "";
    if (shapeList && shapeSelect) {
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

    // v0.9.0 — wire accordion collapse buttons on the rail. Safe
    // to call multiple times; bound buttons are marked with
    // data-rail-wired="1" so re-mounts don't double-bind.
    hydrateRailCollapsibles();
}

// v0.9.0 — rail accordion hydration. On desktop the buttons are
// decorative (sections always expanded via CSS); on touch / narrow
// viewports the click toggles the body visibility using two
// complementary classes:
//   .is-collapsed — user explicitly collapsed a
//                   default-expanded section (only rail-quality)
//   .is-expanded — user explicitly expanded a default-collapsed
//                  section (everything except rail-quality)
// The CSS :not() rules read both classes to figure out what
// should be visible. This is simpler than tracking a single
// is-user-toggled class because we don't need to know the
// default state at read time.
function hydrateRailCollapsibles() {
    document.querySelectorAll(".rail-collapse-btn").forEach(btn => {
        if (btn.dataset.railWired === "1") return;
        btn.dataset.railWired = "1";
        btn.addEventListener("click", () => {
            const section = btn.closest(".rail-section");
            if (!section) return;
            // Only flip visual state on touch / narrow viewports.
            // On desktop the click is effectively a no-op (sections
            // stay expanded).
            const isTouch = document.body.classList.contains("is-touch")
                || window.matchMedia("(max-width: 700px)").matches;
            if (!isTouch) return;
            if (section.classList.contains("rail-quality")) {
                // Default: expanded. Toggle collapse.
                section.classList.toggle("is-collapsed");
                btn.setAttribute("aria-expanded",
                    section.classList.contains("is-collapsed") ? "false" : "true");
            } else {
                // Default: collapsed. Toggle expand.
                section.classList.toggle("is-expanded");
                btn.setAttribute("aria-expanded",
                    section.classList.contains("is-expanded") ? "true" : "false");
            }
        });
    });
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
            // v0.9.1: renamed from "Hide likely hoaxes". The
            // underlying score is a keyword heuristic, not a
            // probability — describing it as "likely hoax" was
            // false precision.
            label: "Hide narrative red flags",
            sub: `flag score > ${HOAX_THRESHOLD / 100}`,
            coverageKey: "hoax_score",
        },
        {
            key: "hasDescription",
            // v0.9.1: the public DB strips raw narrative text, so
            // "has description" rows don't actually have readable
            // descriptions in this build. The flag records whether
            // a description existed in the ORIGINAL source DB
            // before the privacy strip. Renamed to reflect that.
            label: "Had description (in source)",
            sub: "classifier ran; text not retained",
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
                // v0.9.1 — re-evaluate the bias warning banner on
                // every toggle change.
                updateQualityBiasBanner();
                applyFilters();
            });
        }
    }

    // Expose the thresholds for applyClientFilters() to read when
    // building the filter descriptor.
    state.qualityFilter.qualityThreshold = QUALITY_THRESHOLD;
    state.qualityFilter.hoaxThreshold = HOAX_THRESHOLD;

    // v0.9.1 — mount the bias warning element inside the
    // rail-quality section. Shown when the "High quality only"
    // toggle is active. Text explains that the composite score
    // rewards modern, MUFON-investigated, well-dated reports so
    // any downstream chart is structurally biased.
    if (!host.querySelector(".quality-bias-banner")) {
        const banner = document.createElement("div");
        banner.className = "quality-bias-banner";
        banner.hidden = true;
        banner.innerHTML = `
            <strong>High-quality filter is biased.</strong>
            This subset (~19% of rows) rewards modern, MUFON-investigated,
            well-dated reports. The quality score is a composite of 6
            factors with unvalidated weights — see
            <a href="#/methodology" class="quality-bias-link">Methodology</a>
            before citing any number derived from it.
        `;
        host.parentElement?.appendChild(banner);
    }
    updateQualityBiasBanner();
}

// v0.9.1 — toggle the high-quality bias warning banner based on
// the current filter state. Called from mountQualityRail (initial
// mount), on every rail toggle change, and on any external filter
// update that flips state.qualityFilter.highQuality.
function updateQualityBiasBanner() {
    const banner = document.querySelector(".quality-bias-banner");
    if (!banner) return;
    const active = !!(state.qualityFilter && state.qualityFilter.highQuality);
    banner.hidden = !active;
}

// =========================================================================
// v0.11.1 — Data Quality gear popup for Timeline / Insights tabs
// =========================================================================
//
// Mirrors the Observatory rail's quality toggles into a small floating
// popover anchored to a gear icon in each tab header. State is shared
// with the rail — toggling a checkbox in the gear popup also updates the
// rail checkbox (and vice versa when the rail re-mounts). The popup is
// populated lazily on first open so it costs nothing until clicked.

function _mountDqGearPopup(gearBtnId, popupId, listId) {
    const btn = document.getElementById(gearBtnId);
    const popup = document.getElementById(popupId);
    const list = document.getElementById(listId);
    if (!btn || !popup || !list) return;
    if (btn.dataset.dqWired === "1") return;
    btn.dataset.dqWired = "1";

    function openPopup() {
        _populateDqList(listId);
        popup.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        // Close on outside click / Escape
        setTimeout(() => {
            document.addEventListener("pointerdown", closeOnOutside);
            document.addEventListener("keydown", closeOnEscape);
        }, 0);
    }
    function closePopup() {
        popup.hidden = true;
        btn.setAttribute("aria-expanded", "false");
        document.removeEventListener("pointerdown", closeOnOutside);
        document.removeEventListener("keydown", closeOnEscape);
    }
    function closeOnOutside(e) {
        if (!popup.contains(e.target) && !btn.contains(e.target)) closePopup();
    }
    function closeOnEscape(e) {
        if (e.key === "Escape") closePopup();
    }
    btn.addEventListener("click", () => {
        if (popup.hidden) openPopup(); else closePopup();
    });
}

function _populateDqList(listId) {
    const list = document.getElementById(listId);
    if (!list) return;

    const coverage = (
        window.UFODeck
        && typeof window.UFODeck.getCoverage === "function"
        && window.UFODeck.POINTS
        && window.UFODeck.POINTS.ready
    ) ? window.UFODeck.getCoverage() : {};
    const cov = (key) => (coverage[key] || 0);

    const QUALITY_THRESHOLD = 60;
    const HOAX_THRESHOLD = 50;
    const toggles = [
        { key: "highQuality", label: "High quality only", sub: `score \u2265 ${QUALITY_THRESHOLD}`, coverageKey: "quality_score" },
        { key: "hideHoaxes", label: "Hide narrative red flags", sub: `flag score > ${HOAX_THRESHOLD / 100}`, coverageKey: "hoax_score" },
        { key: "hasDescription", label: "Had description (in source)", sub: "classifier ran; text not retained", coverageKey: "has_description" },
        { key: "hasMedia", label: "Has media", sub: "photo / video reference", coverageKey: "has_media" },
        { key: "hasMovement", label: "Has movement described", sub: "hovering / landing / erratic / \u2026", coverageKey: "has_movement" },
    ];

    list.innerHTML = "";
    for (const t of toggles) {
        const populated = cov(t.coverageKey) > 0;
        const li = document.createElement("li");
        li.className = populated ? "" : "rail-toggle-disabled";
        const id = `dq-gear-${listId}-${t.key}`;
        const disabled = populated ? "" : " disabled";
        const checked = _isDqActive(t.key) ? " checked" : "";
        li.innerHTML = `
            <input type="checkbox" id="${id}" data-qkey="${t.key}"${disabled}${checked}>
            <label for="${id}">
                ${escapeHtml(t.label)}
                <span class="rail-toggle-sub">${escapeHtml(t.sub)}</span>
            </label>
        `;
        list.appendChild(li);
        if (populated) {
            const input = li.querySelector("input");
            input.addEventListener("change", (e) => {
                const key = e.target.dataset.qkey;
                if (!state.qualityFilter) state.qualityFilter = {};
                if (key === "highQuality") {
                    state.qualityFilter.highQuality = e.target.checked;
                } else if (key === "hideHoaxes") {
                    state.qualityFilter.hideHoaxes = e.target.checked;
                } else {
                    state.qualityFilter[key] = e.target.checked ? true : null;
                }
                updateQualityBiasBanner();
                _syncDqGearBadges();
                applyFilters();
            });
        }
    }
}

function _isDqActive(key) {
    if (!state.qualityFilter) return false;
    const v = state.qualityFilter[key];
    return key === "highQuality" || key === "hideHoaxes" ? !!v : v === true;
}

// Update the small badge on each gear icon showing how many DQ
// filters are active.
function _syncDqGearBadges() {
    const count = _countActiveDqFilters();
    for (const btnId of ["timeline-dq-gear", "insights-dq-gear"]) {
        const btn = document.getElementById(btnId);
        if (!btn) continue;
        let badge = btn.querySelector(".dq-gear-badge");
        if (!badge) {
            badge = document.createElement("span");
            badge.className = "dq-gear-badge";
            btn.appendChild(badge);
        }
        badge.textContent = count > 0 ? String(count) : "";
    }
}

function _countActiveDqFilters() {
    if (!state.qualityFilter) return 0;
    let n = 0;
    if (state.qualityFilter.highQuality) n++;
    if (state.qualityFilter.hideHoaxes) n++;
    if (state.qualityFilter.hasDescription === true) n++;
    if (state.qualityFilter.hasMedia === true) n++;
    if (state.qualityFilter.hasMovement === true) n++;
    return n;
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

        // v0.9.0 — zoom state. viewMinT/viewMaxT is the currently
        // VISIBLE time range (what the histogram bars span). Starts
        // at the full dataset range and shrinks on wheel zoom.
        // ORTHOGONAL to this.window[0]/[1] which is the PLAYBACK
        // SELECTION. A user can zoom in to place a narrow window
        // precisely, then zoom back out — the window stays put and
        // Play still runs the same selection.
        //
        // Invariant: minT <= viewMinT < viewMaxT <= maxT
        this.viewMinT = this.minT;
        this.viewMaxT = this.maxT;
        // Minimum view span — 7 days of real time. Prevents zooming
        // in past sub-week precision where the year-binned data
        // would collapse into a solid wall of bars.
        this._minViewSpanMs = 7 * 86400000;

        // v0.9.2 — legacy single-granularity bins field. Still
        // populated via ensureData() for fallback code paths (e.g.
        // the /api/timeline legacy path when deck.gl isn't ready)
        // but _draw() prefers the per-granularity cache below.
        this.bins = null;

        // v0.9.2 — adaptive-granularity cache. Three slots: year,
        // month, day. Each holds the unfiltered + filtered bins for
        // that granularity. _draw() picks the right slot based on
        // current view span; _invalidateBinsCache() clears the
        // filtered variants on every filter change (retally).
        this._binsCache = {
            year:  { full: null, filtered: null },
            month: { full: null, filtered: null },
            day:   { full: null, filtered: null },
        };
        // Non-null when a filter is active (retally was called with
        // a truthy signal). Drives the ghost overlay in _draw.
        this._hasActiveFilter = false;
        // Legacy field kept for backward compat with any external
        // caller that reads this.binsFiltered directly. The actual
        // per-granularity filtered bins live in _binsCache.
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
        // v0.9.3 — playback speed is now absolute: days of
        // timeline advanced per wall-second. Previously this was
        // a multiplier relative to a full-range-sweep baseline,
        // which made narrow-window playback feel way too fast
        // even at the slowest (0.25×) setting. Default 1 year/sec
        // gives a ~126-sec sweep over the full range with the
        // auto-narrow 5-year selection window.
        this.playStepDaysPerSec = 365;
        // Legacy alias kept so any external caller that reads
        // state.timeBrush.playSpeed doesn't crash. Always 1 now.
        this.playSpeed = 1.0;
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
        this._updateResetViewBtn();
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

    // v0.9.2 — pick the histogram granularity for the current view
    // span. Thresholds are in milliseconds of view span:
    //   > 10 years → year bars (126 max across full range)
    //   > 400 days → month bars (~1,500 across full range)
    //   else       → day bars (~46,000 across full range, ~90
    //                visible at 3-month zoom)
    //
    // The thresholds are chosen so the visible bin count at each
    // transition is roughly the same (50-120 bars) so the density
    // stays readable. Day bars kick in when the user zooms below a
    // ~1-year view, which is where sub-year precision becomes
    // meaningful.
    _pickGranularity() {
        const span = this._viewSpan();
        const tenYears = 10 * 365.25 * 86400000;
        const thirteenMonths = 400 * 86400000;
        if (span > tenYears) return "year";
        if (span > thirteenMonths) return "month";
        return "day";
    }

    // v0.9.2 — fetch the unfiltered histogram for the given
    // granularity from deck.js's cache. Stores a reference in
    // _binsCache so subsequent draws at the same granularity
    // avoid any work. deck.js caches the computed result
    // indefinitely, so this is O(1) after the first call per
    // granularity.
    _getFullBins(gran) {
        const slot = this._binsCache[gran];
        if (slot && slot.full) return slot.full;
        if (!(window.UFODeck && typeof window.UFODeck.getHistogram === "function")) {
            return this.bins;  // legacy fallback — year-only
        }
        const bins = window.UFODeck.getHistogram(gran);
        if (slot) slot.full = bins;
        return bins;
    }

    // v0.9.2 — fetch the filtered histogram for the given
    // granularity. Recomputed lazily on first access after a
    // filter change (retally sets slot.filtered = null); cached
    // within a single filter state across pan/zoom changes.
    _getFilteredBins(gran) {
        const slot = this._binsCache[gran];
        if (slot && slot.filtered) return slot.filtered;
        if (!(window.UFODeck && typeof window.UFODeck.getHistogramForGranularityVisible === "function")) {
            return null;
        }
        const bins = window.UFODeck.getHistogramForGranularityVisible(gran);
        if (slot) slot.filtered = bins;
        return bins;
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

        // v0.9.2 — pick the right histogram granularity for the
        // current zoom level, then fetch foreground/ghost bins at
        // that granularity. When zoomed out → year bars (as before);
        // zoomed to a 2-5 year window → month bars; zoomed below a
        // year → day bars. Each { startMs, count } bin's x-position
        // is computed via _viewTimeToPx so the draw loop is
        // uniform across granularities.
        const gran = this._pickGranularity();
        let fgBins = this._hasActiveFilter ? this._getFilteredBins(gran) : this._getFullBins(gran);
        const ghostBins = this._hasActiveFilter ? this._getFullBins(gran) : null;

        // Fallback: if deck.js helpers aren't available yet (cold
        // boot), use the legacy this.bins populated by ensureData.
        if (!fgBins) fgBins = this.bins;

        if (!fgBins || fgBins.length === 0) {
            // Empty state
            ctx.fillStyle = line;
            ctx.fillRect(0, h - 1, w, 1);
            return;
        }

        const viewL = this.viewMinT;
        const viewR = this.viewMaxT;

        // Legacy (year-only) fallback path: bins have .year, not
        // .startMs. Detect and convert on the fly so the draw loop
        // below can assume .startMs.
        const normaliseBins = (bins) => {
            if (!bins || bins.length === 0) return bins;
            if (bins[0].startMs !== undefined) return bins;
            // Legacy { year, count } → add startMs inline
            return bins.map(b => ({
                startMs: Date.UTC(b.year || 0, 0, 1),
                count: b.count,
            }));
        };
        const fg = normaliseBins(fgBins);
        const ghost = normaliseBins(ghostBins);

        // v0.11.4: use INDEPENDENT max values for the gray (ghost/
        // unfiltered) layer and the bright (fg/filtered) layer.
        //
        // The old approach used a single `max = ghost.max` for both
        // layers, which meant a tight filter (1% of rows) made the
        // bright bars vanish — the filtered distribution's SHAPE
        // became invisible because every bright bar was 1% the
        // height of the gray backdrop.
        //
        // The new approach scales each layer to its own in-view
        // peak, so both distributions fill the vertical space.
        // Tradeoff: a tall bright bar and a tall gray bar at the
        // same year no longer represent equal counts — but the
        // filtered shape is now legible, which is what the user
        // actually wants from the overlay.
        let maxFull = 1;
        let maxFiltered = 1;
        for (const b of (ghost || [])) {
            if (b.startMs < viewL || b.startMs > viewR) continue;
            if (b.count > maxFull) maxFull = b.count;
        }
        for (const b of fg) {
            if (b.startMs < viewL || b.startMs > viewR) continue;
            if (b.count > maxFiltered) maxFiltered = b.count;
        }
        // Without an active filter, `ghost` is null and `fg` is the
        // full data — reuse maxFiltered for both to keep legacy
        // behavior (the unfiltered bars normalize to their own peak).
        if (!ghost) maxFull = maxFiltered;

        // Bar width: count visible bins so the bar fills the
        // canvas width without overlap. At year gran ~126 bars;
        // at month gran ~1500 bars; at day gran ~46,000 bars —
        // but only the subset in view is drawn.
        let visibleBars = 0;
        for (const b of fg) {
            if (b.startMs >= viewL && b.startMs <= viewR) visibleBars++;
        }
        const barW = Math.max(1, w / Math.max(1, visibleBars));

        // Shared draw routine for ghost + foreground layers. Each
        // bar's x-coord is computed via _viewTimeToPx so the math
        // respects the current zoom level. v0.11.4: maxVal is
        // per-layer so the bright filtered bars normalize to their
        // own peak instead of shrinking against the unfiltered
        // max. See the comment block above for the tradeoff.
        const drawLayer = (bins, alpha, fill, maxVal) => {
            if (!bins) return;
            ctx.fillStyle = fill;
            ctx.globalAlpha = alpha;
            for (const b of bins) {
                if (b.startMs < viewL || b.startMs > viewR) continue;
                const x = this._viewTimeToPx(b.startMs);
                const hBar = (b.count / maxVal) * (h - 14);
                ctx.fillRect(x, h - hBar - 2, Math.max(0.6, barW - 0.4), hBar);
            }
            ctx.globalAlpha = 1;
        };
        drawLayer(ghost, 0.25, accentDim, maxFull);
        drawLayer(fg, 0.85, accent, maxFiltered);

        // Baseline
        ctx.strokeStyle = line;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(0, h - 0.5);
        ctx.lineTo(w, h - 0.5);
        ctx.stroke();

        // v0.10.0 — redraw the overview mini-map whenever the main
        // brush redraws. The overview's histogram bars don't change
        // with zoom (they're full-range), but the view box position
        // does, and calling _drawOverview here is cheap (~2ms).
        this._drawOverview();

        // v0.11: throttle-refresh Timeline + Insights cards when the
        // brush zoom changes. Uses debounce here (not throttle) because
        // zoom/pan DOES stop eventually — the timer fires after the
        // last wheel tick or drag end. This is fine for zoom; the play
        // loop has its own throttle in the step() function above.
        if (state.activeTab === "timeline" && typeof refreshTimelineCards === "function") {
            clearTimeout(this._timelineRefreshTimer);
            this._timelineRefreshTimer = setTimeout(() => {
                refreshTimelineCards();
            }, 200);
        }
        if (state.activeTab === "insights" && typeof refreshInsightsClientCards === "function") {
            clearTimeout(this._insightsRefreshTimer);
            this._insightsRefreshTimer = setTimeout(() => {
                refreshInsightsClientCards();
            }, 200);
        }
    }

    // v0.8.6 — called by applyClientFilters() after the filter
    // pipeline updates POINTS.visibleIdx.
    //
    // v0.9.2 — with adaptive granularity, `bins` is no longer used
    // directly — _draw() fetches per-granularity filtered bins on
    // demand from deck.js. retally() just invalidates the three
    // filtered slots so the next _draw() recomputes. The `bins`
    // argument is kept for backward compat: pass truthy (anything)
    // to signal "filter is active", pass null to signal "no filter"
    // (restore the unfiltered appearance).
    retally(bins) {
        this._hasActiveFilter = !!bins;
        // Legacy field kept for external readers.
        this.binsFiltered = bins || null;
        // Invalidate all three granularity's filtered slots so
        // _draw recomputes from the current POINTS.visibleIdx.
        for (const gran of ["year", "month", "day"]) {
            if (this._binsCache[gran]) {
                this._binsCache[gran].filtered = null;
            }
        }
        this._draw();
    }

    _drawAnnotations() {
        if (!this.annEl || !this.annotations) return;
        this.annEl.innerHTML = "";
        // v0.9.0 — annotations are positioned as percent across the
        // current VIEW range, not the full dataset. Annotations
        // outside the view are skipped entirely.
        const viewL = this.viewMinT;
        const viewR = this.viewMaxT;
        const vSpan = viewR - viewL;
        if (vSpan <= 0) return;
        for (const a of this.annotations) {
            if (!a.year) continue;
            const tMs = Date.UTC(a.year, 0, 1);
            if (tMs < viewL || tMs > viewR) continue;
            const x = ((tMs - viewL) / vSpan) * 100;
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
        // v0.9.0 — clip the selection rectangle to the current view.
        // If the window extends beyond the view on either side, add
        // extends-left/extends-right classes so CSS can render a
        // visual cue. If the window is entirely outside the view,
        // hide the rectangle (the selection data is preserved, just
        // not visible until the user pans or zooms out).
        const viewL = this.viewMinT;
        const viewR = this.viewMaxT;
        const vSpan = viewR - viewL;
        if (vSpan <= 0) return;
        const winL = this.window[0];
        const winR = this.window[1];

        const clippedL = Math.max(winL, viewL);
        const clippedR = Math.min(winR, viewR);

        if (clippedR <= viewL || clippedL >= viewR) {
            this.windowEl.style.display = "none";
        } else {
            this.windowEl.style.display = "";
            const leftPct = ((clippedL - viewL) / vSpan) * 100;
            const rightPct = ((clippedR - viewL) / vSpan) * 100;
            this.windowEl.style.left = leftPct + "%";
            this.windowEl.style.width = (rightPct - leftPct) + "%";
            this.windowEl.classList.toggle("extends-left", winL < viewL);
            this.windowEl.classList.toggle("extends-right", winR > viewR);
        }

        // Readout uses ACTUAL window bounds, not clipped ones. The
        // user should always see the real selection values even if
        // they've zoomed past them.
        const text = this._formatWindowLabel(new Date(winL), new Date(winR));
        const rangeLabel = document.getElementById("brush-range-label");
        const railLabel = document.getElementById("rail-time-label");
        if (rangeLabel) rangeLabel.textContent = text;
        if (railLabel) railLabel.textContent = text;

        // v0.10.0 — keep the overview boxes in sync with the
        // selection window position. The view box position is
        // updated by _drawOverview() (called from _draw()); the
        // selection box tracks the playback window and needs to
        // update on every syncWindow call too.
        this._syncOverviewBoxes();
    }

    // v0.9.0 — format the selection-window readout based on its
    // span. Wide windows show year-only; medium show year-month;
    // narrow (< ~40 days) show year-month-day. Tells the user
    // visually when their selection has sub-year precision.
    _formatWindowLabel(d0, d1) {
        const spanDays = (d1 - d0) / 86400000;
        const pad = (n) => String(n).padStart(2, "0");
        if (spanDays > 365 * 2) {
            return `${d0.getUTCFullYear()} — ${d1.getUTCFullYear()}`;
        }
        if (spanDays > 40) {
            return `${d0.getUTCFullYear()}-${pad(d0.getUTCMonth() + 1)} — ${d1.getUTCFullYear()}-${pad(d1.getUTCMonth() + 1)}`;
        }
        return `${d0.getUTCFullYear()}-${pad(d0.getUTCMonth() + 1)}-${pad(d0.getUTCDate())} — ${d1.getUTCFullYear()}-${pad(d1.getUTCMonth() + 1)}-${pad(d1.getUTCDate())}`;
    }

    // =================================================================
    // v0.10.0 — Overview mini-map
    // =================================================================
    // A 24px-tall canvas below the main brush that always shows the
    // FULL dataset range (34 AD → 2026) with a bilinear scale:
    //   - 15% of the canvas width for 34 AD → 1899 (1,866 years)
    //   - 85% for 1900 → 2026 (126 years)
    // Pre-1900 records render as thin event ticks (not binned bars)
    // because bins with 0-3 records each are noise. Post-1900 uses
    // year-level bars from the cached histogram.
    //
    // Two overlaid boxes:
    //   - View box (cyan) = main brush's zoom range (viewMinT..viewMaxT)
    //     Draggable to pan, edges draggable to resize the zoom.
    //   - Selection box (gold) = playback window (window[0]..window[1])
    //     Display-only from the overview; dragged on the main brush.

    // Bilinear scale: maps a timestamp (ms) to a percentage across
    // the overview canvas. The break is at 1900.
    _overviewTimeToPct(t) {
        const BREAK_YEAR = 1900;
        const breakT = Date.UTC(BREAK_YEAR, 0, 1);
        const PRE_WIDTH = 15;   // % of canvas for pre-1900
        const POST_WIDTH = 85;  // % for post-1900

        if (t <= breakT) {
            // Pre-1900 segment: linear from minT to breakT → 0% to 15%
            const segStart = this.minT;
            const segEnd = breakT;
            const frac = Math.max(0, Math.min(1, (t - segStart) / (segEnd - segStart)));
            return frac * PRE_WIDTH;
        }
        // Post-1900 segment: linear from breakT to maxT → 15% to 100%
        const segStart = breakT;
        const segEnd = this.maxT;
        const frac = Math.max(0, Math.min(1, (t - segStart) / (segEnd - segStart)));
        return PRE_WIDTH + frac * POST_WIDTH;
    }

    // Inverse: overview percentage → timestamp (ms). For click/drag.
    _overviewPctToTime(pct) {
        const BREAK_YEAR = 1900;
        const breakT = Date.UTC(BREAK_YEAR, 0, 1);
        const PRE_WIDTH = 15;
        const POST_WIDTH = 85;

        if (pct <= PRE_WIDTH) {
            const frac = pct / PRE_WIDTH;
            return this.minT + frac * (breakT - this.minT);
        }
        const frac = (pct - PRE_WIDTH) / POST_WIDTH;
        return breakT + frac * (this.maxT - breakT);
    }

    _drawOverview() {
        const canvas = document.getElementById("overview-canvas");
        if (!canvas) return;
        const r = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.round(r.width * dpr));
        canvas.height = Math.max(1, Math.round(r.height * dpr));
        const w = r.width;
        const h = r.height;
        const ctx = canvas.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";

        // Get the full-range year histogram (unfiltered, cached).
        const bins = this._getFullBins("year");
        if (!bins || bins.length === 0) return;

        // Find max count for scaling bar heights.
        let max = 1;
        for (const b of bins) if (b.count > max) max = b.count;

        // Draw bars/ticks using the bilinear scale.
        const BREAK_YEAR = 1900;
        for (const b of bins) {
            const year = b.year !== undefined ? b.year : new Date(b.startMs).getUTCFullYear();
            if (b.count === 0) continue;
            const t = b.startMs !== undefined ? b.startMs : Date.UTC(year, 0, 1);
            const pct = this._overviewTimeToPct(t);
            const x = (pct / 100) * w;

            if (year < BREAK_YEAR) {
                // Pre-1900: thin event ticks (1px wide, fixed height)
                ctx.fillStyle = accent;
                ctx.globalAlpha = 0.5;
                ctx.fillRect(x, 4, 1, h - 8);
            } else {
                // Post-1900: scaled bars
                const barH = (b.count / max) * (h - 4);
                ctx.fillStyle = accent;
                ctx.globalAlpha = 0.35;
                ctx.fillRect(x, h - barH - 1, Math.max(1, w * 0.85 / 126 - 0.5), barH);
            }
        }
        ctx.globalAlpha = 1;

        // Draw the scale break line at 1900.
        const breakPct = 15;
        const breakX = (breakPct / 100) * w;
        ctx.strokeStyle = "rgba(255,179,0,0.5)";
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(breakX, 0);
        ctx.lineTo(breakX, h);
        ctx.stroke();
        ctx.setLineDash([]);

        // Position the view box and selection box via DOM elements.
        this._syncOverviewBoxes();
    }

    _syncOverviewBoxes() {
        const viewBox = document.getElementById("overview-view-box");
        const selBox = document.getElementById("overview-selection-box");
        if (!viewBox) return;

        // View box = main brush's zoom range.
        const vl = this._overviewTimeToPct(this.viewMinT);
        const vr = this._overviewTimeToPct(this.viewMaxT);
        viewBox.style.left = vl + "%";
        viewBox.style.width = (vr - vl) + "%";

        // Selection box = playback window.
        if (selBox) {
            const sl = this._overviewTimeToPct(this.window[0]);
            const sr = this._overviewTimeToPct(this.window[1]);
            selBox.style.left = sl + "%";
            selBox.style.width = (sr - sl) + "%";
        }
    }

    _bindOverviewEvents() {
        const wrap = document.querySelector(".overview-wrap");
        const viewBox = document.getElementById("overview-view-box");
        if (!wrap || !viewBox) return;

        let dragging = null;

        const getOverviewPct = (e) => {
            const r = wrap.getBoundingClientRect();
            return ((e.clientX - r.left) / r.width) * 100;
        };

        // Click on the overview background = re-center view on that point.
        wrap.addEventListener("pointerdown", (e) => {
            if (e.target === viewBox || e.target.classList.contains("overview-handle")) return;
            // Click on empty overview = re-center the view
            const pct = getOverviewPct(e);
            const clickT = this._overviewPctToTime(pct);
            const halfView = this._viewSpan() / 2;
            let newL = Math.max(this.minT, clickT - halfView);
            let newR = Math.min(this.maxT, newL + this._viewSpan());
            if (newR > this.maxT) { newL = this.maxT - this._viewSpan(); }
            this.viewMinT = Math.max(this.minT, newL);
            this.viewMaxT = Math.min(this.maxT, newR);
            this._draw();
            this._drawAnnotations();
            this._syncWindow();
            this._drawOverview();
            this._updateResetViewBtn();
        });

        // Drag the view box = pan the main brush's zoom.
        viewBox.addEventListener("pointerdown", (e) => {
            if (e.target.classList.contains("overview-handle")) {
                // Edge drag = resize the zoom
                const side = e.target.classList.contains("overview-handle-l") ? "l" : "r";
                dragging = {
                    mode: "resize",
                    side,
                    startX: e.clientX,
                    startViewL: this.viewMinT,
                    startViewR: this.viewMaxT,
                };
            } else {
                // Body drag = pan the zoom
                dragging = {
                    mode: "pan",
                    startX: e.clientX,
                    startViewL: this.viewMinT,
                    startViewR: this.viewMaxT,
                };
            }
            viewBox.classList.add("dragging");
            e.preventDefault();
            e.stopPropagation();
        });

        window.addEventListener("pointermove", (e) => {
            if (!dragging) return;
            const r = wrap.getBoundingClientRect();
            const dx = e.clientX - dragging.startX;
            // Convert px delta → time delta via the overview's bilinear scale.
            // Approximate: use the post-1900 segment for the delta
            // since most drags happen there.
            const pctDelta = (dx / r.width) * 100;
            const startPctL = this._overviewTimeToPct(dragging.startViewL);
            const startPctR = this._overviewTimeToPct(dragging.startViewR);

            if (dragging.mode === "pan") {
                const newPctL = startPctL + pctDelta;
                const newPctR = startPctR + pctDelta;
                let newL = this._overviewPctToTime(Math.max(0, Math.min(100 - (startPctR - startPctL), newPctL)));
                let newR = this._overviewPctToTime(Math.min(100, Math.max(startPctR - startPctL, newPctR)));
                this.viewMinT = Math.max(this.minT, newL);
                this.viewMaxT = Math.min(this.maxT, newR);
            } else if (dragging.mode === "resize") {
                if (dragging.side === "l") {
                    const newPctL = Math.max(0, Math.min(startPctR - 2, startPctL + pctDelta));
                    this.viewMinT = Math.max(this.minT, this._overviewPctToTime(newPctL));
                } else {
                    const newPctR = Math.min(100, Math.max(startPctL + 2, startPctR + pctDelta));
                    this.viewMaxT = Math.min(this.maxT, this._overviewPctToTime(newPctR));
                }
                // Enforce minimum view span
                if (this.viewMaxT - this.viewMinT < this._minViewSpanMs) {
                    if (dragging.side === "l") {
                        this.viewMinT = this.viewMaxT - this._minViewSpanMs;
                    } else {
                        this.viewMaxT = this.viewMinT + this._minViewSpanMs;
                    }
                }
            }
            this._draw();
            this._drawAnnotations();
            this._syncWindow();
            this._drawOverview();
        });

        window.addEventListener("pointerup", () => {
            if (!dragging) return;
            dragging = null;
            viewBox.classList.remove("dragging");
            this._updateResetViewBtn();
        });
    }

    // v0.9.2 — live commit during selection-window drag. Bypasses
    // applyClientFilters → hash-write → form-input-write and goes
    // straight to UFODeck.setTimeWindow with the current window
    // in day-precision mode. This is the SAME code path the Play
    // loop uses at 60fps, so it's known to be cheap enough to
    // run on every pointermove. The final commit (which DOES
    // write form inputs + hash) still happens on pointerup via
    // the existing _onChangeRaw path.
    //
    // Result: the map re-tallies continuously as the user drags,
    // giving live feedback, without churning the URL history or
    // triggering the full applyFilters pipeline mid-drag.
    _liveCommit() {
        if (!(window.UFODeck && typeof window.UFODeck.setTimeWindow === "function")) {
            return;
        }
        if (!window.UFODeck.isReady || !window.UFODeck.isReady()) return;
        // Convert ms → day-index for the GPU filter.
        const msPerDay = 86400000;
        const epoch = Date.UTC(1900, 0, 1);
        const dayFrom = Math.floor((this.window[0] - epoch) / msPerDay);
        const dayTo = Math.floor((this.window[1] - epoch) / msPerDay);
        window.UFODeck.setTimeWindow(dayFrom, dayTo, { dayPrecision: true });
    }

    // v0.9.0 — px coordinate <-> ms in the CURRENT VIEW (not the
    // full dataset range). All drawing and event math routes
    // through these helpers so zoom is honoured uniformly.
    _viewSpan() {
        return this.viewMaxT - this.viewMinT;
    }
    _pxToViewTime(px) {
        return this.viewMinT + (px / this.w) * this._viewSpan();
    }
    _viewTimeToPx(t) {
        return ((t - this.viewMinT) / this._viewSpan()) * this.w;
    }
    // Legacy alias kept so existing callers don't break during the
    // v0.9.0 cutover. Prefer _pxToViewTime in new code.
    _pxToTime(px) {
        return this._pxToViewTime(px);
    }

    _bindEvents() {
        if (!this.windowEl || !this.canvas) return;

        const wrap = this.canvas.parentElement;
        // v0.9.0: dragging modes are now:
        //   "move" — selection window drag (existing)
        //   "l" / "r" — selection handle drag (existing)
        //   "pan" — NEW: view pan (click empty canvas, drag)
        let dragging = null;

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
            // v0.9.0 — click on the canvas itself (or the wrap)
            // initiates view pan. Distinguished from selection-drag
            // by target — the selection window has pointer-events
            // on, so clicking it routes through the .windowEl branch
            // above. Only empty canvas area falls through here.
            if (e.target === this.canvas || e.target === wrap) {
                dragging = {
                    mode: "pan",
                    startX: e.clientX,
                    startViewL: this.viewMinT,
                    startViewR: this.viewMaxT,
                };
                wrap.classList.add("panning");
                e.preventDefault();
                return;
            }
        };

        // v0.8.6: split drag handlers. During drag we only update
        // the visual window position — no filter pipeline, no
        // debounced onChange, no deck.gl setProps. On pointerup we
        // commit once via the RAW (un-debounced) callback so the
        // map updates within one frame of release.
        //
        // v0.9.0: the selection-drag math now uses this._viewSpan()
        // instead of (this.maxT - this.minT) so a 100px drag moves
        // the selection by 100/w*viewSpan ms, not
        // 100/w*fullSpan ms. When zoomed in, this means selection
        // dragging feels proportionally precise.
        const onPointerMove = (e) => {
            if (!dragging) return;
            const wrapRect = wrap.getBoundingClientRect();
            const dx = e.clientX - dragging.startX;

            // v0.9.0 pan mode: translate the view range by the drag
            // distance, clamped to the dataset bounds.
            if (dragging.mode === "pan") {
                const vSpan = dragging.startViewR - dragging.startViewL;
                // Drag-right → view moves left (time slides right
                // under the cursor), same feel as grabbing a map.
                const dtMs = -(dx / wrapRect.width) * vSpan;
                let newL = dragging.startViewL + dtMs;
                let newR = dragging.startViewR + dtMs;
                if (newL < this.minT) {
                    const over = this.minT - newL;
                    newL += over; newR += over;
                }
                if (newR > this.maxT) {
                    const over = newR - this.maxT;
                    newL -= over; newR -= over;
                }
                this.viewMinT = newL;
                this.viewMaxT = newR;
                this._draw();
                this._drawAnnotations();
                this._syncWindow();
                return;
            }

            // Selection drag (move / l / r) — dt now scales with
            // view span, not full span.
            const span = this._viewSpan();
            const dt = (dx / wrapRect.width) * span;

            let newL = dragging.startL;
            let newR = dragging.startR;
            if (dragging.mode === "move") {
                newL = dragging.startL + dt;
                newR = dragging.startR + dt;
                // Clamp within dataset bounds (not view bounds — the
                // user can drag the selection outside the current
                // view, which then gets clipped visually by
                // _syncWindow).
                if (newL < this.minT) { newR += (this.minT - newL); newL = this.minT; }
                if (newR > this.maxT) { newL -= (newR - this.maxT); newR = this.maxT; }
            } else if (dragging.mode === "l") {
                // v0.9.2: min window span dropped from 30 days to
                // 7 days. With day-level histograms now visible
                // when zoomed in, users can meaningfully set a
                // one-week window (7 bars) for narrow-slice
                // playback. Narrower than that and the histogram
                // becomes visually useless.
                newL = Math.max(this.minT, Math.min(newR - 7 * 86400000, dragging.startL + dt));
            } else if (dragging.mode === "r") {
                newR = Math.min(this.maxT, Math.max(newL + 7 * 86400000, dragging.startR + dt));
            }
            this.window = [newL, newR];
            // Visual-only update: window rectangle + year labels.
            this._syncWindow();
            // v0.9.2 — live commit on every move. Pushes the new
            // window straight into the deck.gl filter via
            // UFODeck.setTimeWindow, bypassing the hash/form-input
            // pipeline. The final commit (which DOES write form
            // inputs + hash) still happens on pointerup. Result:
            // live map feedback during drag without history churn.
            this._liveCommit();
        };

        const onPointerUp = () => {
            if (!dragging) return;
            const wasDragging = dragging;
            const wasPan = wasDragging.mode === "pan";
            dragging = null;
            this.windowEl.classList.remove("dragging");
            wrap.classList.remove("panning");
            // v0.9.0 — pan mode doesn't modify the selection, so
            // skip the onChange commit in that case. The
            // _updateResetViewBtn call keeps the reset button in
            // sync.
            if (wasPan) {
                this._updateResetViewBtn();
                return;
            }
            // v0.8.6: commit the final window directly, bypassing
            // the debounce. Also cancel any stale debounced call
            // so we don't re-apply the mid-drag window 300ms later.
            this.onChange.cancel?.();
            const [L, R] = this.window;
            if (this._onChangeRaw) {
                this._onChangeRaw(this._isoDate(L), this._isoDate(R));
            }
        };

        // v0.9.0 — scroll wheel zoom. Classic Google Maps pattern:
        // the time value under the cursor stays stationary while
        // the view shrinks (zoom in) or grows (zoom out) around it.
        const onWheel = (e) => {
            e.preventDefault();
            const wrapRect = wrap.getBoundingClientRect();
            const mouseX = e.clientX - wrapRect.left;
            const cursorMs = this._pxToViewTime(mouseX);
            const factor = e.deltaY < 0 ? 0.8 : 1.25;
            const newL = cursorMs - (cursorMs - this.viewMinT) * factor;
            const newR = cursorMs + (this.viewMaxT - cursorMs) * factor;
            let clampedL = Math.max(this.minT, newL);
            let clampedR = Math.min(this.maxT, newR);
            // Enforce minimum view span. At max zoom-in, re-center
            // on the cursor without further shrinking.
            if (clampedR - clampedL < this._minViewSpanMs) {
                const half = this._minViewSpanMs / 2;
                clampedL = Math.max(this.minT, cursorMs - half);
                clampedR = Math.min(this.maxT, clampedL + this._minViewSpanMs);
            }
            this.viewMinT = clampedL;
            this.viewMaxT = clampedR;
            this._draw();
            this._drawAnnotations();
            this._syncWindow();
            this._updateResetViewBtn();
        };

        wrap.addEventListener("pointerdown", onPointerDown);
        window.addEventListener("pointermove", onPointerMove);
        window.addEventListener("pointerup", onPointerUp);
        wrap.addEventListener("wheel", onWheel, { passive: false });

        // v0.9.0 — double-click on the canvas resets the view to
        // the full range. Classic "zoom out to fit" pattern.
        this.canvas.addEventListener("dblclick", () => this.resetView());

        // v0.10.0 — bind the overview mini-map interactions.
        this._bindOverviewEvents();

        // Resize observer so the histogram redraws on viewport changes.
        const ro = new ResizeObserver(() => {
            this._resize();
            this._draw();
            this._syncWindow();
        });
        ro.observe(this.canvas);

        // Play / reset / reset-view / apply buttons
        const playBtn = document.getElementById("brush-play");
        const resetBtn = document.getElementById("brush-reset");
        const resetViewBtn = document.getElementById("brush-reset-view");
        const applyBtn = document.getElementById("brush-apply");
        if (playBtn) playBtn.addEventListener("click", () => this.togglePlay());
        if (resetBtn) resetBtn.addEventListener("click", () => this.reset());
        if (resetViewBtn) resetViewBtn.addEventListener("click", () => this.resetView());
        // v0.9.2 — explicit Apply button. Force-applies the
        // current window via the full filter pipeline (form
        // inputs, hash, applyFilters). Same commit path as
        // pointerup, but user-initiated.
        if (applyBtn) applyBtn.addEventListener("click", () => this.applyNow());
    }

    // v0.9.2 — force-apply the current selection window to the
    // full filter pipeline. Equivalent to what pointerup does,
    // but callable from the Apply button or programmatically.
    // Cancels any pending debounced onChange so the commit is
    // immediate.
    applyNow() {
        this.onChange.cancel?.();
        if (this._onChangeRaw) {
            this._onChangeRaw(
                this._isoDate(this.window[0]),
                this._isoDate(this.window[1]),
            );
        }
    }

    // v0.9.0 — restore the full-range view (full dataset bounds).
    // Called by the Reset View button, dblclick on the canvas, and
    // reset() (which also clears the selection window).
    resetView() {
        this.viewMinT = this.minT;
        this.viewMaxT = this.maxT;
        this._draw();
        this._drawAnnotations();
        this._syncWindow();
        this._updateResetViewBtn();
    }

    // Show/hide the Reset View button based on whether we're zoomed.
    _updateResetViewBtn() {
        const btn = document.getElementById("brush-reset-view");
        if (!btn) return;
        const fullSpan = this.maxT - this.minT;
        const vSpan = this._viewSpan();
        btn.hidden = (vSpan >= fullSpan * 0.98);
    }

    _isoDate(ms) {
        return new Date(ms).toISOString().substring(0, 10);
    }

    togglePlay() {
        const playBtn = document.getElementById("brush-play");
        const progressEl = document.getElementById("brush-play-progress");
        if (this.playing) {
            this.playing = false;
            cancelAnimationFrame(this.playRaf);
            if (playBtn) {
                playBtn.classList.remove("playing");
                playBtn.textContent = "▶ PLAY";
                playBtn.setAttribute("aria-pressed", "false");
            }
            // v0.11.1 — hide progress bar on stop
            if (progressEl) progressEl.hidden = true;
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
        // v0.11.1 — show progress bar
        if (progressEl) {
            progressEl.hidden = false;
            const fill = progressEl.querySelector(".brush-play-progress-fill");
            if (fill) fill.style.width = "0%";
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

        // v0.9.3 — step size is now ABSOLUTE: days of timeline
        // advanced per wall-second. The dropdown value is in
        // days-per-second, so each frame advances by
        // (daysPerSec / 60) days. Convert to ms via × 86400000.
        //
        // At 1 year/sec with a 5-year auto-narrow window, the
        // full 1900-2026 sweep takes ~121 seconds (~2 minutes).
        // At 1 day/sec the same sweep takes ~46,000 seconds
        // (~12.8 hours) — but the user would never run it at
        // full zoom. The dropdown provides 8 options from
        // 0.5 day/sec to 1 year/sec so users can pick the right
        // pace for their current zoom level.
        //
        // The arrow function preserves `this` lexically, so
        // re-reading this.playStepDaysPerSec every frame "just
        // works" when the user changes the dropdown mid-play.
        const msPerDay = 86400000;
        const epoch = Date.UTC(1900, 0, 1);
        const legacyOnChange = this.onChange;

        const step = () => {
            if (!this.playing) return;

            // Re-read speed every frame so the dropdown takes
            // effect immediately.
            const daysPerSec = this.playStepDaysPerSec || 365;
            const stepMs = (daysPerSec / 60) * msPerDay;

            if (isCumulative) {
                // Advance only the right edge. Wrap back to the
                // initial window size when we hit the end.
                let b = this.window[1] + stepMs;
                if (b > this.maxT) {
                    b = this._cumulativeLeft + winSpan;
                }
                this.window = [this._cumulativeLeft, b];
            } else {
                // Sliding: slide both edges. Loop back when the
                // right edge passes maxT.
                let a = this.window[0] + stepMs;
                let b = a + winSpan;
                if (b > this.maxT) {
                    a = this.minT;
                    b = a + winSpan;
                }
                this.window = [a, b];
            }
            this._syncWindow();

            // v0.11.1 — update playback progress bar. Shows how
            // far through the dataset range the window's right
            // edge has advanced (0% = minT, 100% = maxT).
            if (progressEl && !progressEl.hidden) {
                const fill = progressEl.querySelector(".brush-play-progress-fill");
                if (fill) {
                    const totalSpan = this.maxT - this.minT;
                    const pct = totalSpan > 0
                        ? ((this.window[1] - this.minT) / totalSpan) * 100
                        : 0;
                    fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
                }
            }

            // v0.9.3 — GPU fast path. Uses day-precision so
            // sub-year playback actually filters correctly
            // (the old v0.8.1 path passed year integers, which
            // meant a June-August 1997 window showed all of
            // 1997 regardless of handle position). The
            // _liveCommit helper wraps the same setTimeWindow
            // call the brush drag uses — guaranteed cheap at
            // 60fps.
            if (window.UFODeck && typeof window.UFODeck.setTimeWindow === "function" && window.UFODeck.isReady && window.UFODeck.isReady()) {
                const dayFrom = Math.floor((this.window[0] - epoch) / msPerDay);
                const dayTo = Math.floor((this.window[1] - epoch) / msPerDay);
                window.UFODeck.setTimeWindow(dayFrom, dayTo, {
                    dayPrecision: true,
                    cumulative: isCumulative,
                });
            } else {
                // Legacy fallback: debounced onChange → applyFilters
                // → loadMapMarkers (~3 fps, but still animates).
                legacyOnChange(
                    this._isoDate(this.window[0]),
                    this._isoDate(this.window[1]),
                );
            }

            // v0.11: during playback, THROTTLE-refresh the
            // Timeline or Insights cards so they animate along
            // with the map. Uses a throttle (fire at most once
            // per 250ms) NOT a debounce (fire after updates stop)
            // — debounce never fires during continuous 60fps
            // playback because each frame resets the timer before
            // it triggers. Throttle guarantees ~4 updates/sec.
            //
            // v0.11.1: pass playback=true so the refresh functions
            // skip expensive work that doesn't help animation:
            // - coverage strip computation (walks all 396k rows)
            // - coverage strip DOM updates
            // - cross-filter setup (no cross-filter during play)
            // This cuts per-frame JS from ~20ms to ~8ms, roughly
            // doubling the budget headroom at 4 fps throttle.
            const now = Date.now();
            if (state.activeTab === "timeline" && typeof refreshTimelineCards === "function") {
                if (!this._lastTimelineRefresh || now - this._lastTimelineRefresh > 250) {
                    this._lastTimelineRefresh = now;
                    refreshTimelineCards(true);
                }
            }
            if (state.activeTab === "insights" && typeof refreshInsightsClientCards === "function") {
                if (!this._lastInsightsRefresh || now - this._lastInsightsRefresh > 250) {
                    this._lastInsightsRefresh = now;
                    refreshInsightsClientCards(true);
                }
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
        // v0.9.0 — also reset the zoom view so "Reset" is truly
        // "back to the starting state", not just "selection cleared
        // but still zoomed in".
        this.viewMinT = this.minT;
        this.viewMaxT = this.maxT;
        this._draw();
        this._drawAnnotations();
        this._syncWindow();
        this._updateResetViewBtn();
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
    // v0.11.11: apply Chart.js default colors + update every existing
    // chart so labels/ticks use readable colors on both themes. The
    // Chart.js default is a 50% gray (#666) which fails WCAG on both
    // our dark cyan-on-void and our cream-on-paper backgrounds. We
    // pull --text from the CSS token set so both themes get the
    // right value automatically.
    _applyChartJsThemeColors();
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

// v0.11.11 — sync Chart.js defaults + all existing charts with the
// current theme. Called by setTheme() and at boot (once Chart.js is
// available). Reads --text and --text-muted from the computed styles
// so both Dark and Light themes get the right value without us
// hard-coding per-theme literals.
function _applyChartJsThemeColors() {
    if (typeof Chart === "undefined") return;
    const styles = getComputedStyle(document.body);
    const text = styles.getPropertyValue("--text").trim() || "#e8edf5";
    const muted = styles.getPropertyValue("--text-muted").trim() || "#8a96ad";
    const border = styles.getPropertyValue("--border").trim() || "#2a3346";
    // Chart.js global defaults (apply to all new charts)
    Chart.defaults.color = text;
    Chart.defaults.borderColor = border;
    if (Chart.defaults.scale) {
        Chart.defaults.scale.grid = Chart.defaults.scale.grid || {};
        Chart.defaults.scale.grid.color = border;
        Chart.defaults.scale.ticks = Chart.defaults.scale.ticks || {};
        Chart.defaults.scale.ticks.color = text;
    }
    // Per-scale-type defaults (Chart.js 4.x splits these)
    for (const key of ["category", "linear", "logarithmic", "time", "timeseries"]) {
        const s = Chart.defaults.scales && Chart.defaults.scales[key];
        if (s) {
            s.grid = s.grid || {};
            s.grid.color = border;
            s.ticks = s.ticks || {};
            s.ticks.color = text;
            s.title = s.title || {};
            s.title.color = text;
        }
    }
    if (Chart.defaults.plugins) {
        if (Chart.defaults.plugins.legend) {
            Chart.defaults.plugins.legend.labels = Chart.defaults.plugins.legend.labels || {};
            Chart.defaults.plugins.legend.labels.color = text;
        }
        if (Chart.defaults.plugins.title) {
            Chart.defaults.plugins.title.color = text;
        }
        if (Chart.defaults.plugins.tooltip) {
            Chart.defaults.plugins.tooltip.titleColor = text;
            Chart.defaults.plugins.tooltip.bodyColor = text;
        }
    }
    // Update any chart instances already created (walk state for
    // them). Chart.instances holds the live registry.
    if (Chart.instances) {
        for (const id in Chart.instances) {
            const c = Chart.instances[id];
            if (!c) continue;
            try {
                // Update per-scale ticks + grid on the live chart
                if (c.options && c.options.scales) {
                    for (const axisKey in c.options.scales) {
                        const ax = c.options.scales[axisKey];
                        if (!ax) continue;
                        ax.ticks = Object.assign({}, ax.ticks, { color: text });
                        ax.grid = Object.assign({}, ax.grid, { color: border });
                        if (ax.title) ax.title.color = text;
                    }
                }
                if (c.options && c.options.plugins) {
                    if (c.options.plugins.legend && c.options.plugins.legend.labels) {
                        c.options.plugins.legend.labels.color = text;
                    }
                }
                c.update("none");
            } catch (e) {}
        }
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
    // v0.11.11: make sure Chart.js defaults match the current theme
    // before any charts are instantiated. Idempotent; safe to call
    // on every tab visit.
    if (typeof _applyChartJsThemeColors === "function") {
        _applyChartJsThemeColors();
    }
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

    // v0.10.0 — wire the Day/Month/Year granularity toggle buttons.
    // state.timelineGranularity defaults to "year" and persists
    // across tab switches. Each button click sets the granularity
    // and refreshes the cards so the stacked chart re-renders at
    // the new resolution.
    if (!state._timelineGranWired) {
        state._timelineGranWired = true;
        state.timelineGranularity = state.timelineGranularity || "year";
        document.querySelectorAll(".gran-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const gran = btn.dataset.gran;
                if (!gran) return;
                state.timelineGranularity = gran;
                // Update active state on all buttons
                document.querySelectorAll(".gran-btn").forEach(b => {
                    b.classList.toggle("active", b.dataset.gran === gran);
                });
                // Destroy all three chart instances so they rebuild
                // with the right dataset shape. Year has source-
                // stacking on the main chart; month/day doesn't.
                // Quality and movement also change label format.
                if (state.chart) {
                    state.chart.destroy();
                    state.chart = null;
                }
                if (state.timelineQualityChart) {
                    state.timelineQualityChart.destroy();
                    state.timelineQualityChart = null;
                }
                if (state.timelineMovementChart) {
                    state.timelineMovementChart.destroy();
                    state.timelineMovementChart = null;
                }
                refreshTimelineCards();
            });
        });
    }

    // v0.11.1 — mount the Data Quality gear popup on the Timeline
    // tab header. Lazily wired once; re-populates on each open.
    _mountDqGearPopup("timeline-dq-gear", "timeline-dq-popup", "timeline-dq-list");
    _syncDqGearBadges();

    refreshTimelineCards();
}

// =================================================================
// v0.10.0 — Cross-filtering infrastructure
// =================================================================
// Shared state for both Timeline and Insights tabs. A cross-filter
// is a LOCAL drill-down within the current visible set — it doesn't
// modify the top filter bar or the Observatory map. Clicking a bar
// segment on any chart sets a cross-filter; clicking it again or
// clicking the x chip clears it.
//
// Cross-filter state:
//   state.crossFilter = null | { dim, value, tab }
//
// dim = "year" | "source" | "shape" | "emotion" | "movement" | "quality" | "color"
// value = the clicked value (string for categorical, number for year)
// tab = "timeline" | "insights" (which tab owns this cross-filter)
//
// When a cross-filter is active, the chart renderers further filter
// POINTS.visibleIdx to only rows matching the cross-filter. This
// is done via _applyCrossFilter() which returns a sub-array of
// visibleIdx. The cross-filtered sub-array is passed to each
// renderer's data computation.

// Apply the current cross-filter to POINTS.visibleIdx and return
// the sub-set. Returns visibleIdx unchanged when no cross-filter
// is active. This is the single entry point for cross-filtering
// on both tabs.
function _getCrossFilteredIndices() {
    const P = window.UFODeck?.POINTS;
    if (!P || !P.ready) return null;
    const iter = P.visibleIdx;
    if (!iter) return null;
    const cf = state.crossFilter;
    if (!cf) return iter;  // no cross-filter active

    // Walk visibleIdx and keep only rows matching the cross-filter.
    const out = new Uint32Array(iter.length);
    let j = 0;
    const dim = cf.dim;
    const val = cf.value;

    if (dim === "year") {
        const dd = P.dateDays;
        const targetYear = Number(val);
        for (let k = 0; k < iter.length; k++) {
            const i = iter[k];
            const d = dd[i];
            if (d === 0) continue;
            const year = new Date(Date.UTC(1900, 0, 1) + d * 86400000).getUTCFullYear();
            if (year === targetYear) out[j++] = i;
        }
    } else if (dim === "source") {
        const si = P.sourceIdx;
        const sources = P.sources || [];
        const targetIdx = sources.indexOf(val);
        if (targetIdx < 0) return iter;  // unknown source → no filter
        for (let k = 0; k < iter.length; k++) {
            if (si[iter[k]] === targetIdx) out[j++] = iter[k];
        }
    } else if (dim === "shape") {
        const sh = P.shapeIdx;
        const shapes = P.shapes || [];
        const targetIdx = shapes.indexOf(val);
        if (targetIdx < 0) return iter;
        for (let k = 0; k < iter.length; k++) {
            if (sh[iter[k]] === targetIdx) out[j++] = iter[k];
        }
    } else if (dim === "emotion") {
        const ei = P.emotionIdx;
        const emotions = P.emotions || [];
        const targetIdx = emotions.indexOf(val);
        if (targetIdx < 0) return iter;
        for (let k = 0; k < iter.length; k++) {
            if (ei[iter[k]] === targetIdx) out[j++] = iter[k];
        }
    } else if (dim === "color") {
        const ci = P.colorIdx;
        const colors = P.colors || [];
        const targetIdx = colors.indexOf(val);
        if (targetIdx < 0) return iter;
        for (let k = 0; k < iter.length; k++) {
            if (ci[iter[k]] === targetIdx) out[j++] = iter[k];
        }
    } else if (dim === "movement") {
        const mf = P.movementFlags;
        const movements = P.movements || [];
        const bit = movements.indexOf(val);
        if (bit < 0) return iter;
        const mask = 1 << bit;
        for (let k = 0; k < iter.length; k++) {
            if (mf[iter[k]] & mask) out[j++] = iter[k];
        }
    } else if (dim === "quality") {
        const qs = P.qualityScore;
        const lo = parseInt(String(val), 10);
        if (isNaN(lo)) return iter;
        const hi = lo + 9;
        for (let k = 0; k < iter.length; k++) {
            const q = qs[iter[k]];
            if (q !== 255 && q >= lo && q <= hi) out[j++] = iter[k];
        }
    // v0.11 — new emotion cross-filter dimensions
    } else if (dim === "emotion_group") {
        const grp = P.emotion28Group;
        const idx = P.emotion28Idx;
        const groups = P.emotions28Groups || _SENTI_GROUP_NAMES;
        const targetGrp = groups.indexOf(val);
        if (targetGrp < 0) return iter;
        for (let k = 0; k < iter.length; k++) {
            const i = iter[k];
            if (idx[i] > 0 && grp[i] === targetGrp) out[j++] = i;
        }
    } else if (dim === "emotion_7") {
        const ei = P.emotion7Idx;
        const names = P.emotions7 || [];
        const targetIdx = names.indexOf(val);
        if (targetIdx < 0) return iter;
        for (let k = 0; k < iter.length; k++) {
            if (ei[iter[k]] === targetIdx) out[j++] = iter[k];
        }
    } else if (dim === "emotion_28") {
        const ei = P.emotion28Idx;
        const names = P.emotions28 || [];
        const targetIdx = names.indexOf(val);
        if (targetIdx < 0) return iter;
        for (let k = 0; k < iter.length; k++) {
            if (ei[iter[k]] === targetIdx) out[j++] = iter[k];
        }
    }
    return out.subarray(0, j);
}

// Set or clear a cross-filter and refresh the active tab.
function setCrossFilter(dim, value, tab) {
    // Toggle: clicking the same bar again clears the filter.
    if (state.crossFilter &&
        state.crossFilter.dim === dim &&
        state.crossFilter.value === value) {
        state.crossFilter = null;
    } else {
        state.crossFilter = { dim, value, tab };
    }
    _renderCrossFilterChips();
    if (tab === "timeline") refreshTimelineCards();
    else if (tab === "insights") {
        refreshInsightsClientCards();
    }
}

function clearCrossFilter() {
    if (!state.crossFilter) return;
    const tab = state.crossFilter.tab;
    state.crossFilter = null;
    _renderCrossFilterChips();
    if (tab === "timeline") refreshTimelineCards();
    else if (tab === "insights") refreshInsightsClientCards();
}

// Render the "Filtered by: X x" chip bar at the top of the active
// tab. Appends to #timeline-filter-chips or #insights-filter-chips.
function _renderCrossFilterChips() {
    for (const id of ["timeline-filter-chips", "insights-filter-chips"]) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (!state.crossFilter) {
            el.innerHTML = "";
            el.hidden = true;
            continue;
        }
        const cf = state.crossFilter;
        el.hidden = false;
        el.innerHTML = `
            <span class="cross-filter-chip">
                Filtered by: <strong>${escapeHtml(String(cf.value))}</strong>
                <button class="cross-filter-clear" type="button"
                        title="Clear cross-filter"
                        onclick="clearCrossFilter()">&times;</button>
            </span>
        `;
    }
}
window.clearCrossFilter = clearCrossFilter;

function refreshTimelineCards(playback) {
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) return;

    // v0.10.0: if a cross-filter is active on the Timeline tab,
    // temporarily swap POINTS.visibleIdx to the cross-filtered
    // sub-set so the deck.js aggregate helpers read the right
    // data. Restore after rendering.
    // v0.11.1: skip cross-filter swap during playback — no cross-
    // filter is active while playing, and the swap + restore adds
    // overhead.
    const P = window.UFODeck.POINTS;
    const origIdx = P.visibleIdx;
    const cf = state.crossFilter;
    if (!playback && cf && cf.tab === "timeline") {
        P.visibleIdx = _getCrossFilteredIndices();
    }

    // v0.10.0: Timeline charts now mirror the brush's zoom state.
    // If the brush is zoomed to a narrow range, the charts show
    // only that range at the matching granularity (year/month/day).
    // This uses the same _pickGranularity() + getHistogram(gran)
    // pipeline the brush uses, so the Timeline tab feels like a
    // bigger version of the brush histogram.
    const brush = state.timeBrush;
    let viewMin = null;
    let viewMax = null;
    let gran = "year";
    if (brush) {
        viewMin = brush.viewMinT;
        viewMax = brush.viewMaxT;
        gran = brush._pickGranularity();
    }

    // Main chart: use adaptive-granularity histogram filtered to
    // the brush's view range. getHistogramBySource only exists at
    // year level, so for month/day granularity the main chart
    // falls back to a single-series histogram (no source stacking).
    // The quality and movement charts use the same view-filtered
    // data at year level (their aggregate patterns are meaningful
    // even when the chart x-axis matches the zoomed range).
    const stacked = window.UFODeck.getYearHistogramBySource(true);
    const quality = window.UFODeck.computeMedianByYear(P.qualityScore);
    const movement = window.UFODeck.computeMovementShareByYear();
    const visible = P.visibleIdx ? P.visibleIdx.length : P.count;

    // Restore original visibleIdx so the Observatory map isn't
    // affected by the cross-filter.
    P.visibleIdx = origIdx;

    // v0.10.0: filter all three datasets to the brush's view range.
    // For stacked (year-level), trim years outside the view. For
    // quality and movement, same year-range trim.
    const filterToView = (data, getYear) => {
        if (!viewMin || !viewMax || !data) return data;
        const minYear = new Date(viewMin).getUTCFullYear();
        const maxYear = new Date(viewMax).getUTCFullYear();
        return data.filter(d => {
            const y = getYear(d);
            return y >= minYear && y <= maxYear;
        });
    };

    // Filter stacked histogram
    if (stacked && stacked.years) {
        const minYear = viewMin ? new Date(viewMin).getUTCFullYear() : -Infinity;
        const maxYear = viewMax ? new Date(viewMax).getUTCFullYear() : Infinity;
        // Find start/end indices in the years array
        let start = 0, end = stacked.years.length - 1;
        while (start < stacked.years.length && stacked.years[start] < minYear) start++;
        while (end >= 0 && stacked.years[end] > maxYear) end--;
        if (start <= end) {
            // Create a view-filtered copy
            const filteredYears = stacked.years.slice(start, end + 1);
            const sourceCount = stacked.sources.length;
            const filteredCounts = new Uint32Array(filteredYears.length * sourceCount);
            const filteredTotals = new Uint32Array(filteredYears.length);
            for (let y = 0; y < filteredYears.length; y++) {
                filteredTotals[y] = stacked.totals[start + y];
                for (let s = 0; s < sourceCount; s++) {
                    filteredCounts[y * sourceCount + s] = stacked.counts[(start + y) * sourceCount + s];
                }
            }
            let maxTotal = 0;
            for (let y = 0; y < filteredYears.length; y++) {
                if (filteredTotals[y] > maxTotal) maxTotal = filteredTotals[y];
            }
            stacked.years = filteredYears;
            stacked.counts = filteredCounts;
            stacked.totals = filteredTotals;
            stacked.maxTotal = maxTotal;
        }
    }

    // Filter quality and movement to the view range
    const filteredQuality = filterToView(quality, d => d.year);
    const filteredMovement = movement;
    if (filteredMovement && filteredMovement.years && viewMin && viewMax) {
        const minYear = new Date(viewMin).getUTCFullYear();
        const maxYear = new Date(viewMax).getUTCFullYear();
        let start = 0, end = filteredMovement.years.length - 1;
        while (start < filteredMovement.years.length && filteredMovement.years[start] < minYear) start++;
        while (end >= 0 && filteredMovement.years[end] > maxYear) end--;
        if (start <= end && (start > 0 || end < filteredMovement.years.length - 1)) {
            const M = 10;
            filteredMovement.years = filteredMovement.years.slice(start, end + 1);
            const newCounts = new Uint32Array(filteredMovement.years.length * M);
            const newTotals = new Uint32Array(filteredMovement.years.length);
            for (let y = 0; y < filteredMovement.years.length; y++) {
                newTotals[y] = filteredMovement.totals[start + y];
                for (let b = 0; b < M; b++) {
                    newCounts[y * M + b] = filteredMovement.counts[(start + y) * M + b];
                }
            }
            filteredMovement.counts = newCounts;
            filteredMovement.totals = newTotals;
        }
    }

    // v0.10.0: the main stacked chart supports day/month/year
    // granularity via the toggle buttons. When month or day is
    // selected, we use the adaptive getHistogram(gran) from
    // deck.js (which returns {startMs, count} bins, no source
    // breakdown — source stacking only works at year level).
    // When year is selected, we use the original stacked-by-source
    // path. The selected granularity is stored in
    // state.timelineGranularity.
    const selectedGran = state.timelineGranularity || "year";

    if (selectedGran === "year") {
        renderTimelineMainChart(stacked);
    } else {
        // Month or day: use the adaptive histogram filtered to
        // the view range. Single-series (no source stacking).
        let adaptiveBins = window.UFODeck.getHistogramForGranularityVisible(selectedGran);
        if (!adaptiveBins) {
            adaptiveBins = window.UFODeck.getHistogram(selectedGran);
        }
        if (adaptiveBins && viewMin && viewMax) {
            adaptiveBins = adaptiveBins.filter(b =>
                b.startMs >= viewMin && b.startMs <= viewMax
            );
        }
        renderTimelineMainChartAdaptive(adaptiveBins, selectedGran);
    }

    // v0.10.0: quality + movement cards also use the selected
    // granularity. When month/day is selected, we compute per-
    // period aggregates from the adaptive bins instead of the
    // year-level helpers. This makes all 3 cards zoom together.
    if (selectedGran === "year") {
        renderTimelineQualityChart(filteredQuality);
        renderTimelineMovementChart(filteredMovement);
    } else {
        // Fetch the same adaptive bins used by the main chart.
        let bins = window.UFODeck.getHistogramForGranularityVisible(selectedGran);
        if (!bins) bins = window.UFODeck.getHistogram(selectedGran);
        if (bins && viewMin && viewMax) {
            bins = bins.filter(b => b.startMs >= viewMin && b.startMs <= viewMax);
        }
        // Compute quality medians per bin. We use a simplified
        // approach: for each bin, walk visibleIdx rows whose
        // dateDays falls in [bin.startMs, nextBin.startMs), collect
        // quality scores, and pick the median. This is O(N*B) but
        // at day-level zoom the bin count is small (90 for 3 months)
        // and visibleIdx is pre-filtered, so it's fast enough.
        if (bins && bins.length > 0) {
            const epoch = Date.UTC(1900, 0, 1);
            const msPerDay = 86400000;
            const dd = P.dateDays;
            const qs = P.qualityScore;
            const mf = P.movementFlags;
            const movements = P.movements || [];
            const M = 10;
            const iter = P.visibleIdx;
            const N = iter ? iter.length : P.count;

            // Build bin boundaries in day-index space for fast lookup
            const binDayStarts = bins.map(b =>
                Math.floor((b.startMs - epoch) / msPerDay)
            );
            // Sentinel: one past the last bin
            const lastBinEnd = bins.length > 1
                ? binDayStarts[binDayStarts.length - 1] + (binDayStarts[1] - binDayStarts[0])
                : binDayStarts[0] + (selectedGran === "day" ? 1 : 30);

            // Quality: collect scores per bin, compute median
            const qualBins = bins.map(() => []);
            // Movement: count per category per bin
            const movCounts = new Uint32Array(bins.length * M);
            const movTotals = new Uint32Array(bins.length);

            for (let k = 0; k < N; k++) {
                const i = iter ? iter[k] : k;
                const d = dd[i];
                if (d === 0) continue;
                // Find which bin this row belongs to via linear scan
                // (bins are sorted, small count at zoom level)
                let bi = -1;
                for (let b = binDayStarts.length - 1; b >= 0; b--) {
                    if (d >= binDayStarts[b]) { bi = b; break; }
                }
                if (bi < 0) continue;

                // Quality
                const q = qs[i];
                if (q !== 255) qualBins[bi].push(q);

                // Movement
                const mv = mf[i];
                if (mv !== 0) {
                    movTotals[bi]++;
                    for (let c = 0; c < M; c++) {
                        if (mv & (1 << c)) movCounts[bi * M + c]++;
                    }
                }
            }

            // Build quality output: [{year (or startMs label), median, count}]
            const pad = (n) => String(n).padStart(2, "0");
            const qualityAdaptive = bins.map((b, bi) => {
                const arr = qualBins[bi];
                let median = null;
                if (arr.length > 0) {
                    arr.sort((a, c) => a - c);
                    median = arr[Math.floor(arr.length / 2)];
                }
                const d = new Date(b.startMs);
                const year = d.getUTCFullYear();
                return { year, startMs: b.startMs, median, count: arr.length };
            });

            // Build movement output
            const movYears = bins.map(b => new Date(b.startMs).getUTCFullYear());
            const movAdaptive = {
                years: movYears,
                movements: movements.slice(),
                counts: movCounts,
                totals: movTotals,
                _bins: bins,  // stash for label formatting
                _gran: selectedGran,
            };

            renderTimelineQualityChartAdaptive(qualityAdaptive, selectedGran);
            renderTimelineMovementChartAdaptive(movAdaptive, selectedGran);
        } else {
            renderTimelineQualityChart(filteredQuality);
            renderTimelineMovementChart(filteredMovement);
        }
    }

    const countEl = document.getElementById("timeline-visible-count");
    if (countEl) countEl.textContent = visible.toLocaleString();

    const labelEl = document.getElementById("timeline-range-label");
    if (labelEl) {
        if (viewMin && viewMax) {
            const d0 = new Date(viewMin);
            const d1 = new Date(viewMax);
            const pad = (n) => String(n).padStart(2, "0");
            if (selectedGran === "day") {
                labelEl.textContent = `${d0.getUTCFullYear()}-${pad(d0.getUTCMonth() + 1)}-${pad(d0.getUTCDate())} — ${d1.getUTCFullYear()}-${pad(d1.getUTCMonth() + 1)}-${pad(d1.getUTCDate())}`;
            } else if (selectedGran === "month") {
                labelEl.textContent = `${d0.getUTCFullYear()}-${pad(d0.getUTCMonth() + 1)} — ${d1.getUTCFullYear()}-${pad(d1.getUTCMonth() + 1)}`;
            } else {
                labelEl.textContent = `${d0.getUTCFullYear()} — ${d1.getUTCFullYear()}`;
            }
        } else if (stacked && stacked.years && stacked.years.length) {
            labelEl.textContent = `${stacked.years[0]} — ${stacked.years[stacked.years.length - 1]}`;
        }
    }
}

// v0.10.0 — render the main Timeline chart with adaptive-
// granularity bins (month or day). Single-series (no source
// stacking) because the source breakdown is only computed at
// year level. Uses the same {startMs, count} bin shape as the
// TimeBrush's adaptive draw.
function renderTimelineMainChartAdaptive(bins, gran) {
    const canvas = document.getElementById("timeline-main-chart");
    if (!canvas || !bins || bins.length === 0) return;

    // Trim leading/trailing zero-count bins so the chart doesn't
    // render a wall of empty space around the actual data. At day
    // level with a 4-year date filter, the histogram has ~1,460
    // non-zero bins scattered across ~46,000 total bins — without
    // trimming, 97% of the chart is blank and the visible bars are
    // scrunched into a tiny sliver.
    let start = 0;
    let end = bins.length - 1;
    while (start < bins.length && bins[start].count === 0) start++;
    while (end >= 0 && bins[end].count === 0) end--;
    if (start > end) return;  // all-zero
    bins = bins.slice(start, end + 1);

    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";
    const pad = (n) => String(n).padStart(2, "0");

    // Build labels based on granularity.
    const labels = bins.map(b => {
        const d = new Date(b.startMs);
        if (gran === "day") {
            return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
        }
        if (gran === "month") {
            const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
            return `${monthNames[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
        }
        return String(d.getUTCFullYear());
    });
    const data = bins.map(b => b.count);

    const datasets = [{
        label: "Sightings",
        data,
        backgroundColor: accent + "BB",
        borderColor: accent,
        borderWidth: 1,
    }];

    if (state.chart) {
        state.chart.data.labels = labels;
        state.chart.data.datasets = datasets;
        state.chart.options.scales.x.ticks.maxTicksLimit = gran === "day" ? 20 : 16;
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
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (item) => `${item.parsed.y.toLocaleString()} sightings`,
                    },
                },
            },
            scales: {
                x: { ticks: { maxTicksLimit: gran === "day" ? 20 : 16 } },
                y: { beginAtZero: true },
            },
        },
    });
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
                            return `Total: ${total.toLocaleString()}\nClick to filter`;
                        },
                    },
                },
            },
            scales: {
                x: { stacked: true, ticks: { maxTicksLimit: 16 } },
                y: { stacked: true, beginAtZero: true },
            },
            // v0.10.0 — click a year bar → cross-filter the other
            // two Timeline cards to that year. Click again to clear.
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const idx = elements[0].index;
                const year = labels[idx];
                if (year != null) {
                    setCrossFilter("year", year, "timeline");
                }
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
            },
        },
    });
}

// v0.10.0 — adaptive quality chart (month/day granularity).
// Mirrors renderTimelineQualityChart but uses startMs-keyed bins
// with month/day labels instead of year-only.
function renderTimelineQualityChartAdaptive(data, gran) {
    const canvas = document.getElementById("timeline-quality-chart");
    if (!canvas || !data) return;
    const pad = (n) => String(n).padStart(2, "0");
    const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#00F0FF";
    const accentHover = getComputedStyle(document.body).getPropertyValue("--accent-hover").trim() || accent;

    const labels = [];
    const values = [];
    for (const row of data) {
        if (row.count === 0 || row.median == null) continue;
        const d = new Date(row.startMs);
        if (gran === "day") {
            labels.push(`${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`);
        } else {
            const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
            labels.push(`${monthNames[d.getUTCMonth()]} ${d.getUTCFullYear()}`);
        }
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
                label: `Median quality score (per ${gran})`,
                data: values,
                borderColor: accent,
                backgroundColor: "transparent",
                pointBackgroundColor: accentHover,
                pointRadius: gran === "day" ? 1 : 2,
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
                x: { ticks: { maxTicksLimit: gran === "day" ? 20 : 16 } },
                y: { min: 0, max: 100, title: { display: true, text: `Median QS (per ${gran})` } },
            },
        },
    });
}

// v0.10.0 — adaptive movement chart (month/day granularity).
function renderTimelineMovementChartAdaptive(mv, gran) {
    const canvas = document.getElementById("timeline-movement-chart");
    if (!canvas || !mv || !mv._bins) return;
    const pad = (n) => String(n).padStart(2, "0");
    const bins = mv._bins;
    const M = 10;

    // Trim leading/trailing zero bins
    let start = 0, end = bins.length - 1;
    while (start < bins.length && mv.totals[start] === 0) start++;
    while (end >= 0 && mv.totals[end] === 0) end--;
    if (start > end) { start = 0; end = bins.length - 1; }

    const labels = [];
    for (let y = start; y <= end; y++) {
        const d = new Date(bins[y].startMs);
        if (gran === "day") {
            labels.push(`${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`);
        } else {
            const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
            labels.push(`${monthNames[d.getUTCMonth()]} ${d.getUTCFullYear()}`);
        }
    }

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
                x: { stacked: true, ticks: { maxTicksLimit: gran === "day" ? 20 : 16 } },
                y: { stacked: true, beginAtZero: true },
            },
            elements: { point: { radius: 0 } },
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

    // v0.11.11: sync Chart.js defaults with current theme before
    // building chart instances. Idempotent.
    if (typeof _applyChartJsThemeColors === "function") {
        _applyChartJsThemeColors();
    }

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

    // v0.11.1 — mount the Data Quality gear popup on the Insights
    // tab header. Same pattern as Timeline.
    _mountDqGearPopup("insights-dq-gear", "insights-dq-popup", "insights-dq-list");
    _syncDqGearBadges();

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
function refreshInsightsClientCards(playback) {
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) return;

    // v0.10.0: if a cross-filter is active on the Insights tab,
    // temporarily swap POINTS.visibleIdx to the cross-filtered
    // sub-set so every renderer reads the right data. Restore
    // after rendering so the Observatory map isn't affected.
    // v0.11.1: skip cross-filter swap during playback for speed.
    const P = window.UFODeck.POINTS;
    const origIdx = P.visibleIdx;
    const cf = state.crossFilter;
    if (!playback && cf && cf.tab === "insights") {
        P.visibleIdx = _getCrossFilteredIndices();
    }

    // v0.9.1 — compute coverage for each derived column over the
    // (possibly cross-filtered) visible set.
    // v0.11.1: skip during playback — coverage doesn't change
    // meaningfully frame-to-frame and the full walk of 396k rows
    // costs ~8ms per call.
    if (!playback) {
        state.insightsCoverage = _computeInsightsCoverage(P);
    }

    // v0.10.0: check the cross-filtered N. Science reviewer's
    // guardrail: N < 30 → show a warning instead of rendering.
    if (!playback) {
        const cfN = P.visibleIdx ? P.visibleIdx.length : P.count;
        if (cf && cf.tab === "insights" && cfN < 30) {
            P.visibleIdx = origIdx;
            _renderCrossFilterChips();
            const chipsEl = document.getElementById("insights-filter-chips");
            if (chipsEl) {
                chipsEl.innerHTML += `
                    <span class="cross-filter-warning">
                        Only ${cfN} sightings match — too few to chart reliably
                    </span>
                `;
            }
            return;
        }
    }

    // v0.11 — Section A: Emotion & Sentiment Analysis (5 cards)
    renderSentimentGroup();
    renderEmotion7();
    renderGoEmotions28();
    renderSentimentScores();
    renderEmotionBySourceV11();
    // Section B: Data Quality (2 cards, unchanged)
    renderQualityDistribution();
    renderHoaxCurve();
    // Section C: Movement & Shape (2 cards, unchanged)
    renderMovementTaxonomy();
    renderShapeMovementMatrix();

    // Restore original visibleIdx.
    P.visibleIdx = origIdx;

    // v0.9.1 — mount coverage strips on all 9 client-side cards
    // AFTER all Chart.js instances have rendered.
    // v0.11.1: skip during playback — DOM updates for coverage
    // strips add ~3ms per frame with no visible benefit during
    // animation. Strips re-mount when playback stops and the
    // normal (non-playback) refresh path runs.
    if (!playback) {
        _mountAllCoverageStrips();
    }
}

// v0.9.1 — mount a coverage strip on every client-side insights
// card. Called at the end of refreshInsightsClientCards. Reads
// state.insightsCoverage (populated earlier in the same function).
function _mountAllCoverageStrips() {
    const cov = state.insightsCoverage;
    if (!cov) return;

    // v0.11 — compute coverage for the new emotion columns.
    // emotion28 and emotion7 use their idx arrays; vader and
    // roberta use the score arrays (128 = default neutral for
    // unclassified rows, but we actually check the idx for
    // classified-or-not since scores are always filled).
    // For simplicity: use the emotion28 coverage as the emotion
    // coverage proxy since all 5 columns have identical coverage
    // (502,985 classified rows).
    const emo28Cov = _computeColumnCoverage("emotion28Idx");
    const emo7Cov = _computeColumnCoverage("emotion7Idx");

    // Section A: Emotion cards
    _renderCoverageStrip("sentiment-group-chart", emo28Cov,
        "GoEmotions classifier populated");
    _renderCoverageStrip("emotion-7-chart", emo7Cov,
        "7-class RoBERTa populated");
    _renderCoverageStrip("emotion-28-chart", emo28Cov,
        "GoEmotions classifier populated");
    _renderCoverageStrip("sentiment-scores-chart", emo28Cov,
        "Sentiment scores populated");
    _renderCoverageStrip("emotion-source-chart", emo7Cov,
        "7-class RoBERTa populated");

    // Section B: Quality cards
    _renderCoverageStrip("quality-distribution-chart", cov.quality,
        "Quality score populated");
    _renderCoverageStrip("hoax-curve-chart", cov.hoax,
        "Red flag score populated");

    // Section C: Movement cards
    _renderCoverageStrip("movement-taxonomy-chart", cov.movementFlags,
        "Movement-tagged rows");
    const shapeMov = (cov.shape && cov.movementFlags)
        ? { n: Math.min(cov.shape.n, cov.movementFlags.n),
            pct: Math.min(cov.shape.pct, cov.movementFlags.pct) }
        : null;
    _renderCoverageStrip("shape-movement-chart", shapeMov,
        "Shape + movement both populated");
}

// v0.11 — compute coverage for a specific POINTS typed array.
// Returns { n, pct } where n = count of rows with value > 0
// in the current visibleIdx.
function _computeColumnCoverage(arrayName) {
    const P = window.UFODeck?.POINTS;
    if (!P || !P.ready) return { n: 0, pct: 0 };
    const arr = P[arrayName];
    if (!arr) return { n: 0, pct: 0 };
    const iter = P.visibleIdx;
    const N = iter ? iter.length : P.count;
    let n = 0;
    if (iter) {
        for (let k = 0; k < N; k++) {
            if (arr[iter[k]] > 0) n++;
        }
    } else {
        for (let i = 0; i < N; i++) {
            if (arr[i] > 0) n++;
        }
    }
    return { n, pct: N > 0 ? (n / N) * 100 : 0 };
}

// v0.9.1 — walk POINTS.visibleIdx once and count, for each
// derived column, how many rows have the column populated (or
// the corresponding flag bit set). Returns an object with a
// per-column { n, pct } pair plus the total visible count.
//
// The Insights tab's biggest honesty problem was that emotion /
// color / hoax / movement charts rendered identical layouts
// regardless of underlying coverage — a filter that left only
// 200 emotion-labelled rows drew the same radar as a filter that
// left 150,000. Now every card surfaces its coverage so users can
// tell when they're looking at a sliver.
function _computeInsightsCoverage(P) {
    const iter = P.visibleIdx || null;
    const N = iter ? iter.length : P.count;
    const qs = P.qualityScore;
    const hs = P.hoaxScore;
    const rs = P.richnessScore;
    const shp = P.shapeIdx;
    const ci = P.colorIdx;
    const ei = P.emotionIdx;
    const fl = P.flags;
    const mf = P.movementFlags;
    const dd = P.dateDays;
    const UNK = 255;
    // flag bit constants are defined in deck.js. Duplicated here
    // because _computeInsightsCoverage is the one place app.js
    // reads the flags byte directly.
    const FLAG_HAS_DESC = 0x01;
    const FLAG_HAS_MEDIA = 0x02;
    const FLAG_HAS_MOVEMENT = 0x04;

    let dated = 0;
    let quality = 0;
    let hoax = 0;
    let richness = 0;
    let shape = 0;
    let color = 0;
    let emotion = 0;
    let hasDesc = 0;
    let hasMedia = 0;
    let hasMovement = 0;
    let movementFlags = 0;

    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        if (dd[i] !== 0) dated++;
        if (qs[i] !== UNK) quality++;
        if (hs[i] !== UNK) hoax++;
        if (rs[i] !== UNK) richness++;
        if (shp[i] !== 0) shape++;
        if (ci[i] !== 0) color++;
        if (ei[i] !== 0) emotion++;
        const f = fl[i];
        if (f & FLAG_HAS_DESC) hasDesc++;
        if (f & FLAG_HAS_MEDIA) hasMedia++;
        if (f & FLAG_HAS_MOVEMENT) hasMovement++;
        if (mf[i] !== 0) movementFlags++;
    }

    const pct = (n) => N > 0 ? (n / N) * 100 : 0;
    return {
        total: N,
        dated: { n: dated, pct: pct(dated) },
        quality: { n: quality, pct: pct(quality) },
        hoax: { n: hoax, pct: pct(hoax) },
        richness: { n: richness, pct: pct(richness) },
        shape: { n: shape, pct: pct(shape) },
        color: { n: color, pct: pct(color) },
        emotion: { n: emotion, pct: pct(emotion) },
        hasDescription: { n: hasDesc, pct: pct(hasDesc) },
        hasMedia: { n: hasMedia, pct: pct(hasMedia) },
        hasMovement: { n: hasMovement, pct: pct(hasMovement) },
        movementFlags: { n: movementFlags, pct: pct(movementFlags) },
    };
}

// v0.9.1 — mount (or update) a coverage strip at the bottom of a
// given insight card. The strip is a single line of text + a
// colored pill showing the percentage. Colors: green ≥ 80%,
// yellow 50–80%, orange 30–50%, red < 30%. Cards with <50%
// coverage also get the .is-low-coverage class which greys out
// the whole card (users can still read it but the visual cue
// says "don't over-interpret this").
function _renderCoverageStrip(canvasId, covEntry, label) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !covEntry) return;
    const card = canvas.closest(".insight-card");
    if (!card) return;

    const pct = covEntry.pct;
    const n = covEntry.n;
    const total = state.insightsCoverage?.total || 0;

    let band = "cov-hi";
    if (pct < 30) band = "cov-low";
    else if (pct < 50) band = "cov-midlo";
    else if (pct < 80) band = "cov-mid";

    card.classList.toggle("is-low-coverage", pct < 50);
    card.classList.toggle("is-critical-coverage", pct < 30);

    let strip = card.querySelector(".insight-coverage-strip");
    if (!strip) {
        strip = document.createElement("div");
        strip.className = "insight-coverage-strip";
        card.appendChild(strip);
    }
    strip.innerHTML = `
        <span class="cov-label">${escapeHtml(label)}</span>
        <span class="cov-pill ${band}">${pct.toFixed(1)}%</span>
        <span class="cov-n">n = ${n.toLocaleString()} / ${total.toLocaleString()}</span>
    `;
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
            // v0.10.0 — click a quality bucket → cross-filter.
            // Science reviewer rated this the SAFEST filter source
            // (100% coverage, no bias introduced).
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const idx = elements[0].index;
                const bucketLabel = labels[idx];
                if (bucketLabel) {
                    setCrossFilter("quality", bucketLabel, "insights");
                }
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
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
            // v0.10.0 — click a movement bar → cross-filter
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const idx = elements[0].index;
                const movName = labels[idx];
                if (movName) {
                    setCrossFilter("movement", movName.toLowerCase(), "insights");
                }
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
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
                        title: (items) => `Red flag score ${items[0].label}-${(+items[0].label) + 4}`,
                        label: (item) => `${item.parsed.y.toLocaleString()} sightings`,
                    },
                },
            },
            scales: {
                x: { title: { display: true, text: "Narrative red flag score (heuristic, not a probability)" } },
                y: { beginAtZero: true },
            },
        },
    });
}

// =========================================================================
// v0.11 — Emotion & Sentiment cards (transformer-based)
// =========================================================================
// Replaces the v0.8.8 legacy 8-class keyword emotion cards with 5
// new cards built from the transformer classifier outputs:
//   1. Sentiment Group donut (emotion28Group — 4 slices)
//   2. 7-class RoBERTa emotion bar (emotion7Idx — 7 bars)
//   3. GoEmotions 28-class detail bar (emotion28Idx — 27/28 bars,
//      "± neutral" toggle)
//   4. Sentiment score dual histogram (vaderCompound + robertaSentiment)
//   5. Emotion by source (emotion7Idx × sourceIdx, stacked 100%)
//
// All 5 walk POINTS.visibleIdx once per render (~5ms for 396k rows)
// and feed Chart.js directly. Cross-filterable: clicking a bar/slice
// sets state.crossFilter via setCrossFilter().

// Sentiment group colors (shared across multiple cards)
const _SENTI_GROUP_COLORS = ["#6b7280", "#22c55e", "#ef4444", "#f59e0b"];
const _SENTI_GROUP_NAMES = ["neutral", "positive", "negative", "ambiguous"];

// 7-class emotion colors
const _EMO7_COLORS = {
    anger: "#ef4444", disgust: "#a855f7", fear: "#f97316",
    joy: "#22c55e", neutral: "#6b7280", sadness: "#3b82f6",
    surprise: "#eab308",
};

// ---- Card 1: Sentiment Group donut ----
function renderSentimentGroup() {
    const canvas = document.getElementById("sentiment-group-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const grp = P.emotion28Group;
    const idx = P.emotion28Idx;
    const counts = [0, 0, 0, 0];  // neutral, positive, negative, ambiguous
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        if (idx[i] === 0) continue;  // unclassified
        counts[grp[i]]++;
    }
    const total = counts.reduce((a, b) => a + b, 0);
    const labels = _SENTI_GROUP_NAMES.map((n, i) => {
        const pct = total > 0 ? ((counts[i] / total) * 100).toFixed(1) : "0";
        return `${n.charAt(0).toUpperCase() + n.slice(1)} (${pct}%)`;
    });

    if (state.insightsCharts.sentimentGroup) {
        const c = state.insightsCharts.sentimentGroup;
        c.data.datasets[0].data = counts;
        c.data.labels = labels;
        c.update("none");
        return;
    }
    state.insightsCharts.sentimentGroup = new Chart(canvas.getContext("2d"), {
        type: "doughnut",
        data: {
            labels,
            datasets: [{
                data: counts,
                backgroundColor: _SENTI_GROUP_COLORS,
                borderColor: "#1f2937",
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { position: "right", labels: { boxWidth: 12 } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const v = ctx.parsed;
                            const pct = total > 0 ? ((v / total) * 100).toFixed(1) : "0";
                            return `${v.toLocaleString()} sightings (${pct}%)`;
                        },
                    },
                },
            },
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const idx = elements[0].index;
                setCrossFilter("emotion_group", _SENTI_GROUP_NAMES[idx], "insights");
            },
        },
    });
}

// ---- Card 2: 7-class RoBERTa emotion bar ----
function renderEmotion7() {
    const canvas = document.getElementById("emotion-7-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const ei = P.emotion7Idx;
    const names = P.emotions7 || [];
    const counts = new Uint32Array(names.length);
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        if (ei[i] > 0) counts[ei[i]]++;
    }
    // Sort descending by count, skip index 0 (null)
    const rows = [];
    for (let i = 1; i < names.length; i++) {
        rows.push({ name: names[i], count: counts[i] });
    }
    rows.sort((a, b) => b.count - a.count);
    const labels = rows.map(r => r.name.charAt(0).toUpperCase() + r.name.slice(1));
    const data = rows.map(r => r.count);
    const colors = rows.map(r => _EMO7_COLORS[r.name] || "#6b7280");

    if (state.insightsCharts.emotion7) {
        const c = state.insightsCharts.emotion7;
        c.data.labels = labels;
        c.data.datasets[0].data = data;
        c.data.datasets[0].backgroundColor = colors;
        c.update("none");
        return;
    }
    state.insightsCharts.emotion7 = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "Sightings",
                data,
                backgroundColor: colors,
                borderWidth: 0,
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
            scales: { x: { beginAtZero: true } },
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const label = labels[elements[0].index];
                setCrossFilter("emotion_7", label.toLowerCase(), "insights");
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
            },
        },
    });
}

// ---- Card 3: GoEmotions 28-class detail bar ----
function renderGoEmotions28() {
    const canvas = document.getElementById("emotion-28-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const ei = P.emotion28Idx;
    const grp = P.emotion28Group;
    const names = P.emotions28 || [];
    const counts = new Uint32Array(names.length);
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        if (ei[i] > 0) counts[ei[i]]++;
    }
    // Build rows, optionally hiding neutral
    const showNeutral = state.showNeutral28 || false;
    const rows = [];
    for (let i = 1; i < names.length; i++) {
        if (!showNeutral && names[i] === "neutral") continue;
        rows.push({
            name: names[i],
            count: counts[i],
            groupIdx: grp ? undefined : 0,  // will be looked up below
        });
    }
    rows.sort((a, b) => b.count - a.count);

    const labels = rows.map(r => r.name);
    const data = rows.map(r => r.count);
    // Color by sentiment group
    const groupLookup = {};
    if (P.emotions28) {
        // Build a name → group mapping by walking a sample
        for (let i = 1; i < names.length; i++) {
            // Find one row with this emotion to get its group
            for (let k = 0; k < Math.min(N, 50000); k++) {
                const ri = iter ? iter[k] : k;
                if (ei[ri] === i) {
                    groupLookup[names[i]] = grp[ri];
                    break;
                }
            }
        }
    }
    const colors = rows.map(r => _SENTI_GROUP_COLORS[groupLookup[r.name] || 0]);

    if (state.insightsCharts.goEmotions28) {
        const c = state.insightsCharts.goEmotions28;
        c.data.labels = labels;
        c.data.datasets[0].data = data;
        c.data.datasets[0].backgroundColor = colors;
        c.update("none");
        return;
    }
    state.insightsCharts.goEmotions28 = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "Sightings",
                data,
                backgroundColor: colors,
                borderWidth: 0,
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
            scales: { x: { beginAtZero: true } },
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const label = labels[elements[0].index];
                setCrossFilter("emotion_28", label, "insights");
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
            },
        },
    });
}

// Wire the "± neutral" toggle button
document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("emotion-28-toggle");
    if (btn) {
        btn.addEventListener("click", () => {
            state.showNeutral28 = !state.showNeutral28;
            btn.textContent = state.showNeutral28 ? "- neutral" : "+ neutral";
            btn.setAttribute("aria-pressed", String(state.showNeutral28));
            if (state.insightsCharts.goEmotions28) {
                state.insightsCharts.goEmotions28.destroy();
                state.insightsCharts.goEmotions28 = null;
            }
            if (typeof refreshInsightsClientCards === "function") {
                refreshInsightsClientCards();
            }
        });
    }
});

// ---- Card 4: Sentiment score dual histogram ----
function renderSentimentScores() {
    const canvas = document.getElementById("sentiment-scores-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const vader = P.vaderCompound;
    const roberta = P.robertaSentiment;
    const eidx = P.emotion28Idx;
    const BINS = 20;
    const vBins = new Uint32Array(BINS);
    const rBins = new Uint32Array(BINS);
    const N = iter ? iter.length : P.count;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        if (eidx[i] === 0) continue;  // unclassified
        // Scale 0-255 → -1..+1 → bin 0..BINS-1
        const vScore = (vader[i] / 255) * 2 - 1;
        const rScore = (roberta[i] / 255) * 2 - 1;
        const vBin = Math.min(BINS - 1, Math.max(0, Math.floor((vScore + 1) / 2 * BINS)));
        const rBin = Math.min(BINS - 1, Math.max(0, Math.floor((rScore + 1) / 2 * BINS)));
        vBins[vBin]++;
        rBins[rBin]++;
    }
    const labels = [];
    for (let i = 0; i < BINS; i++) {
        const lo = (-1 + i * (2 / BINS)).toFixed(1);
        labels.push(lo);
    }

    if (state.insightsCharts.sentimentScores) {
        const c = state.insightsCharts.sentimentScores;
        c.data.datasets[0].data = Array.from(vBins);
        c.data.datasets[1].data = Array.from(rBins);
        c.update("none");
        return;
    }
    state.insightsCharts.sentimentScores = new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
            labels,
            datasets: [
                {
                    label: "VADER compound",
                    data: Array.from(vBins),
                    backgroundColor: "rgba(78, 121, 167, 0.55)",
                    borderColor: "#4e79a7",
                    borderWidth: 1,
                    barPercentage: 1.0,
                    categoryPercentage: 1.0,
                },
                {
                    label: "RoBERTa sentiment",
                    data: Array.from(rBins),
                    backgroundColor: "rgba(242, 142, 43, 0.55)",
                    borderColor: "#f28e2b",
                    borderWidth: 1,
                    barPercentage: 1.0,
                    categoryPercentage: 1.0,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: {
                legend: { position: "top", labels: { boxWidth: 12 } },
                tooltip: {
                    callbacks: {
                        title: (items) => {
                            const lo = parseFloat(items[0].label);
                            const hi = (lo + 2 / BINS).toFixed(1);
                            return `Score ${lo} to ${hi}`;
                        },
                        label: (item) => `${item.dataset.label}: ${item.parsed.y.toLocaleString()}`,
                    },
                },
            },
            scales: {
                x: { title: { display: true, text: "Score (-1 negative ... +1 positive)" } },
                y: { beginAtZero: true },
            },
        },
    });
}

// ---- Card 5: Emotion by Source (7-class × source, stacked 100%) ----
function renderEmotionBySourceV11() {
    const canvas = document.getElementById("emotion-source-chart");
    if (!canvas) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx || null;
    const si = P.sourceIdx;
    const ei = P.emotion7Idx;
    const sources = P.sources || [];
    const emotions = P.emotions7 || [];
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
    const srcIdxes = [];
    for (let s = 1; s < nSrc; s++) {
        if (srcTotals[s] > 0) srcIdxes.push(s);
    }
    const labels = srcIdxes.map(s => sources[s] || "Unknown");
    const datasets = [];
    for (let e = 1; e < nEmo; e++) {
        const emoName = emotions[e] || "";
        const color = _EMO7_COLORS[emoName] || "#6b7280";
        const data = srcIdxes.map(s => {
            const tot = srcTotals[s];
            return tot > 0 ? (grid[s * nEmo + e] / tot) * 100 : 0;
        });
        datasets.push({
            label: emoName.charAt(0).toUpperCase() + emoName.slice(1),
            data,
            backgroundColor: color + "CC",
            borderColor: color,
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
    state.insightsCharts.source = new Chart(canvas.getContext("2d"), {
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
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const srcName = labels[elements[0].index];
                if (srcName) setCrossFilter("source", srcName, "insights");
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
            },
        },
    });
}

// v0.11: the old v0.8.8 emotion renderers (renderEmotionRadar,
// renderEmotionOverTime, renderEmotionBySource, renderEmotionByShape,
// _collectEmotionCounts, _emotionColor) were deleted. They used the
// legacy dominant_emotion 8-class keyword classifier with 37.8%
// coverage. The 5 new renderers above use the transformer outputs
// with 81.9% coverage and richer models.

// OLD CODE DELETED — see the comment block above for the mapping.
// Everything between here and the Detail Modal section is the old
// v0.8.8 code that was replaced by the v0.11 renderers above.
// KEEP NOTHING — the old functions were here. All deleted in v0.11.
// Jump straight to the Detail Modal section below.
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
                // v0.9.1: renamed from "Hoax likelihood" to
                // "Narrative red flags" because the underlying
                // column is a keyword-match heuristic, not a
                // calibrated probability. The value stays
                // 0.0-1.0 so existing clients don't break, but
                // the display label and tooltip explain what it
                // actually measures.
                const val = Number(r.hoax_likelihood);
                const pct = Math.max(0, Math.min(100, Math.round(val * 100)));
                html += `<div class="detail-row">
                    <span class="detail-label" title="Keyword-based heuristic flagging narrative patterns (hoax-language, shape/date collisions, source priors). Not a calibrated probability — see Methodology.">Narrative red flags:</span>
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

    // v0.11.2 — Credits modal
    const creditsBtn = document.getElementById("credits-menu-item");
    if (creditsBtn) {
        creditsBtn.addEventListener("click", () => {
            closeMenu();
            openCreditsModal();
        });
    }
}

function openCreditsModal() {
    const overlay = document.getElementById("credits-overlay");
    if (!overlay) return;
    overlay.hidden = false;
    // Trigger reflow so the opacity transition plays
    void overlay.offsetHeight;
    overlay.classList.add("is-open");

    const closeBtn = document.getElementById("credits-close");
    const close = () => {
        overlay.classList.remove("is-open");
        setTimeout(() => { overlay.hidden = true; }, 200);
        document.removeEventListener("keydown", escHandler);
    };
    const escHandler = (e) => { if (e.key === "Escape") close(); };
    if (closeBtn) closeBtn.onclick = close;
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", escHandler);
}


// =========================================================================
// Sprint 3: Filter bar polish — auto-apply, is-dirty, mobile collapse,
// active filter counts. v0.8.7 dropped the advanced drawer (all dead
// filters) and narrowed the auto-apply + dirty lists to the 6
// surviving fields.
// =========================================================================

// v0.10.0: ALL filters are now live-reactive. The Apply button was
// removed because applyClientFilters() runs in ~5ms — batching
// multiple filter changes into one commit is unnecessary and the
// explicit Apply step confuses users who expect instant feedback
// (the UX reviewer's #2 issue). Selects fire on change (250ms
// debounce); date inputs fire on input (500ms debounce to allow
// typing a full year without thrashing). The "Reset" link replaces
// both Apply and Clear.
const AUTO_APPLY_SELECT_IDS = [
    "filter-shape",
    "filter-source",
    "filter-color",
    "filter-emotion",
];

const AUTO_APPLY_DATE_IDS = [
    "filter-date-from",
    "filter-date-to",
];

let _autoApplyTimer = null;
let _dateApplyTimer = null;

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

    // v0.10.0: date inputs also auto-apply with a longer debounce
    // (500ms) so the user has time to type a full 4-digit year or
    // a YYYY-MM-DD date without thrashing applyClientFilters on
    // every keystroke. On blur, commit immediately (the user is
    // done typing).
    AUTO_APPLY_DATE_IDS.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener("input", () => {
            clearTimeout(_dateApplyTimer);
            _dateApplyTimer = setTimeout(() => {
                if (typeof applyFilters === "function") applyFilters();
            }, 500);
        });
        el.addEventListener("blur", () => {
            clearTimeout(_dateApplyTimer);
            if (typeof applyFilters === "function") applyFilters();
        });
    });

    // v0.10.0: "Reset" link replaces the old Apply + Clear buttons.
    const resetLink = document.getElementById("btn-reset-filters");
    if (resetLink) {
        resetLink.addEventListener("click", (e) => {
            e.preventDefault();
            clearFilters();
        });
    }

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
        // v0.9.4: on mobile the hamburger now collapses EVERYTHING
        // above the map: filter bar, movement row, and the
        // Observatory accordion rail. We toggle a body-level class
        // instead of just #filters-bar.is-collapsed so CSS can
        // target all three panels from a single selector.
        // Default: collapsed on narrow / touch screens so the map
        // gets maximum vertical space on first load.
        const isMobileWidth = window.innerWidth <= 720;
        const isTouchDevice = document.body.classList.contains("is-touch");
        if (isMobileWidth || isTouchDevice) {
            document.body.classList.add("mobile-filters-hidden");
            bar.classList.add("is-collapsed");
            mobileBtn.setAttribute("aria-expanded", "false");
        }
        mobileBtn.addEventListener("click", () => {
            const willHide = !document.body.classList.contains("mobile-filters-hidden");
            document.body.classList.toggle("mobile-filters-hidden", willHide);
            bar.classList.toggle("is-collapsed", willHide);
            mobileBtn.setAttribute("aria-expanded", String(!willHide));
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


// =========================================================================
// v0.11.2: Cinematic intro + guided tooltip tour
// =========================================================================
//
// First-visit experience: a terminal-style "scanning databases" counter
// that ticks up to the real sighting count, followed by a 5-step tooltip
// tour highlighting the map, rail, TimeBrush, tabs, and stats badge.
// Returning visitors skip the intro; a help button in the header replays
// the tour (without the cinematic) anytime.

const TOUR_STORAGE_KEY = "ufosint-intro-seen";

const TOUR_STEPS = [
    {
        target: ".observatory-canvas-wrap",
        body: "This is the <strong>Observatory</strong>. {mapped} mapped sightings from 5 databases, rendered on a GPU-accelerated map. Click any point to see its full report.",
        position: "left",
    },
    {
        target: ".observatory-rail",
        body: "<strong>Data Quality filters</strong> live here. Toggle high-quality only, narrative red flags, media, movement categories, and more. Filters update the map in real time.",
        position: "right",
    },
    {
        target: "#region-draw-btn",
        body: "<strong>Region tool</strong>. Click to pick a shape (Rectangle / Polygon / Ellipse), then draw on the map to filter to a geographic area. The filter applies across Observatory, Timeline, and Insights — and you can toggle it on/off from the TimeBrush bar without losing the shape.",
        position: "bottom",
    },
    {
        target: ".observatory-time-brush",
        body: "The <strong>TimeBrush</strong>. Drag handles to select a time window. Scroll to zoom into a decade or month. Hit Play to animate through history.",
        position: "top",
    },
    {
        target: ".tabs",
        body: "Switch between <strong>Observatory</strong> (map), <strong>Timeline</strong> (charts over time), <strong>Insights</strong> (emotion + quality analysis), and <strong>Methodology</strong>.",
        position: "bottom",
    },
    {
        target: "#stats-badge",
        body: "Click the <strong>stats badge</strong> for a detailed breakdown of all 5 source databases, date ranges, and coverage metrics.",
        position: "bottom",
    },
];

let _tourActive = false;
let _tourStep = 0;
let _tourMapped = "396,158";   // Replaced at runtime from stats

// ---- Cinematic intro ----

function runCinematicIntro(total) {
    return new Promise((resolve) => {
        const overlay = document.getElementById("intro-overlay");
        const counterEl = document.getElementById("intro-counter-value");
        const statusEl = document.getElementById("intro-status");
        if (!overlay || !counterEl) {
            skipCinematicIntro();
            resolve();
            return;
        }

        // Reduced motion: skip animation, show final number briefly
        const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (prefersReduced) {
            counterEl.textContent = total.toLocaleString();
            if (statusEl) statusEl.textContent = "READY";
            setTimeout(() => {
                overlay.classList.add("intro-done");
                resolve();
            }, 800);
            return;
        }

        const duration = 3000;  // 3 seconds counter
        const start = performance.now();
        const statusMessages = [
            "CONNECTING TO 5 SOURCES",
            "CROSS-REFERENCING RECORDS",
            "GEOCODING COORDINATES",
            "BUILDING GPU LAYER",
            "READY",
        ];
        let statusIdx = 0;
        const statusInterval = setInterval(() => {
            statusIdx++;
            if (statusIdx < statusMessages.length && statusEl) {
                statusEl.textContent = statusMessages[statusIdx];
            }
            if (statusIdx >= statusMessages.length - 1) {
                clearInterval(statusInterval);
            }
        }, 700);

        function tick(now) {
            const elapsed = now - start;
            const progress = Math.min(1, elapsed / duration);
            // Ease-out cubic: fast start, slow finish
            const eased = 1 - Math.pow(1 - progress, 3);
            const value = Math.floor(eased * total);
            counterEl.textContent = value.toLocaleString();

            if (progress < 1) {
                requestAnimationFrame(tick);
            } else {
                counterEl.textContent = total.toLocaleString();
                // Hold on "READY" for 500ms then dissolve
                setTimeout(() => {
                    overlay.classList.add("intro-dissolving");
                    // After CSS transition (600ms), mark done
                    setTimeout(() => {
                        overlay.classList.add("intro-done");
                        clearInterval(statusInterval);
                        resolve();
                    }, 650);
                }, 500);
            }
        }
        requestAnimationFrame(tick);
    });
}

function skipCinematicIntro() {
    const overlay = document.getElementById("intro-overlay");
    if (overlay) overlay.classList.add("intro-done");
}

// ---- Tour state machine ----

function startTour(skipIntro, total) {
    // Ensure we're on the Observatory tab so all targets exist
    if (state.activeTab !== "observatory") {
        switchTab("observatory");
    }

    // Resolve {mapped} placeholder
    if (state.statsData && state.statsData.mapped_sightings) {
        _tourMapped = state.statsData.mapped_sightings.toLocaleString();
    }

    if (!skipIntro) {
        runCinematicIntro(total || 614505).then(() => {
            _beginTourSteps();
        });
    } else {
        skipCinematicIntro();
        _beginTourSteps();
    }
}

function _beginTourSteps() {
    _tourActive = true;
    _tourStep = 0;
    const backdrop = document.getElementById("tour-backdrop");
    const tooltip = document.getElementById("tour-tooltip");
    const skipBtn = document.getElementById("tour-skip");
    const nextBtn = document.getElementById("tour-next");
    if (backdrop) backdrop.hidden = false;
    if (tooltip) tooltip.hidden = false;
    if (skipBtn) skipBtn.addEventListener("click", _endTour);
    if (nextBtn) nextBtn.addEventListener("click", _advanceTour);
    document.addEventListener("keydown", _tourEscapeHandler);
    window.addEventListener("resize", _tourResizeHandler);
    _showTourStep(0);
}

function _showTourStep(index) {
    if (index >= TOUR_STEPS.length) { _endTour(); return; }

    const step = TOUR_STEPS[index];
    const target = document.querySelector(step.target);
    const backdrop = document.getElementById("tour-backdrop");
    const tooltip = document.getElementById("tour-tooltip");
    const bodyEl = document.getElementById("tour-tooltip-body");
    const progressEl = document.getElementById("tour-progress");
    const arrow = document.getElementById("tour-arrow");
    if (!tooltip || !bodyEl) return;

    // Resolve placeholders in body text
    let bodyText = step.body.replace("{mapped}", _tourMapped);
    bodyEl.innerHTML = bodyText;
    if (progressEl) progressEl.textContent = `${index + 1} / ${TOUR_STEPS.length}`;

    // Last step: change Next to "Done"
    const nextBtn = document.getElementById("tour-next");
    if (nextBtn) {
        nextBtn.textContent = index === TOUR_STEPS.length - 1 ? "Done" : "Next";
    }

    if (!target) {
        // Target not in DOM yet — skip this step
        tooltip.style.opacity = "0";
        setTimeout(() => _advanceTour(), 100);
        return;
    }

    // Scroll target into view
    target.scrollIntoView({ behavior: "smooth", block: "nearest" });

    // Position after a brief delay for scroll to settle
    setTimeout(() => _positionTooltip(target, step.position, tooltip, backdrop, arrow), 150);
}

function _positionTooltip(target, position, tooltip, backdrop, arrow) {
    const rect = target.getBoundingClientRect();
    const pad = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Cut a hole in the backdrop using clip-path
    if (backdrop) {
        const x1 = Math.max(0, rect.left - pad);
        const y1 = Math.max(0, rect.top - pad);
        const x2 = Math.min(vw, rect.right + pad);
        const y2 = Math.min(vh, rect.bottom + pad);
        // Outer rect (full viewport) + inner rect (hole) — polygon with counter-wound inner
        backdrop.style.clipPath = `polygon(
            0 0, ${vw}px 0, ${vw}px ${vh}px, 0 ${vh}px, 0 0,
            ${x1}px ${y1}px, ${x1}px ${y2}px, ${x2}px ${y2}px, ${x2}px ${y1}px, ${x1}px ${y1}px
        )`;
    }

    // Position tooltip
    tooltip.setAttribute("data-position", position);
    tooltip.style.opacity = "1";

    // On narrow screens, use fixed bottom sheet layout.
    // v0.11.2: clear inline top so the CSS `bottom: 12px` rule
    // takes effect — the old code set top: (rect.bottom + 16)
    // which pushed the tooltip off the viewport on phones.
    const isMobile = window.innerWidth <= 700;
    if (isMobile) {
        tooltip.style.top = "";
        tooltip.style.left = "";
        tooltip.style.right = "";
        return;
    }

    const ttWidth = 340;
    const ttHeight = tooltip.offsetHeight || 160;
    let top, left;

    switch (position) {
        case "bottom":
            top = rect.bottom + 12;
            left = rect.left + rect.width / 2 - ttWidth / 2;
            break;
        case "top":
            top = rect.top - ttHeight - 12;
            left = rect.left + rect.width / 2 - ttWidth / 2;
            break;
        case "left":
            top = rect.top + rect.height / 2 - ttHeight / 2;
            left = rect.left - ttWidth - 16;
            break;
        case "right":
            top = rect.top + rect.height / 2 - ttHeight / 2;
            left = rect.right + 16;
            break;
        default:
            top = rect.bottom + 12;
            left = rect.left;
    }

    // Clamp to viewport
    left = Math.max(8, Math.min(left, vw - ttWidth - 8));
    top = Math.max(8, Math.min(top, vh - ttHeight - 8));

    tooltip.style.top = top + "px";
    tooltip.style.left = left + "px";
}

function _advanceTour() {
    _tourStep++;
    if (_tourStep >= TOUR_STEPS.length) {
        _endTour();
    } else {
        _showTourStep(_tourStep);
    }
}

function _endTour() {
    _tourActive = false;
    const backdrop = document.getElementById("tour-backdrop");
    const tooltip = document.getElementById("tour-tooltip");
    if (backdrop) { backdrop.hidden = true; backdrop.style.clipPath = ""; }
    if (tooltip) tooltip.hidden = true;
    document.removeEventListener("keydown", _tourEscapeHandler);
    window.removeEventListener("resize", _tourResizeHandler);
    // Remove click listeners to avoid duplicates on replay
    const skipBtn = document.getElementById("tour-skip");
    const nextBtn = document.getElementById("tour-next");
    if (skipBtn) skipBtn.removeEventListener("click", _endTour);
    if (nextBtn) nextBtn.removeEventListener("click", _advanceTour);
    try { localStorage.setItem(TOUR_STORAGE_KEY, "1"); } catch (_) {}
}

function _tourEscapeHandler(e) {
    if (e.key === "Escape" && _tourActive) _endTour();
}

function _tourResizeHandler() {
    if (_tourActive) _showTourStep(_tourStep);
}

// ---- Help button ----

function initHelpTourButton() {
    const btn = document.getElementById("help-tour-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
        startTour(true);
    });
}


// =========================================================================
// v0.11.4: Region (geofence) draw tool
// =========================================================================
//
// Click the REGION button in the topbar to enter draw mode. Click-drag on
// the map to define a bounding box. Release to apply as a spatial filter
// across Observatory / Timeline / Insights. The drawn rectangle persists
// on the map as a Leaflet L.rectangle until cleared.
//
// State lives on state.regionFilter:
//   null                      — no region filter active
//   { south, north, west, east } — bounding box in WGS84 degrees
//
// The rectangle bbox flows through applyClientFilters() -> UFODeck's
// _rebuildVisible hot loop which already has lat/lng bbox culling built in.
// No server round-trip.

// Internal state for the drawing interaction (pre-apply).
let _regionDrawing = false;
let _regionMode = null;        // "rect" | "polygon" | "circle"
let _regionDragStart = null;   // {x, y} in container pixels (rect + circle)
let _regionDragCurrent = null; // {x, y}
let _regionPolyPoints = [];    // array of [x, y] pixel points (polygon)
let _regionPolyCursor = null;  // {x, y} — polygon cursor follower
let _regionAppliedLayer = null; // L.rectangle/L.polygon/L.circle on the map
// v0.11.4: when a region is DRAWN but the toggle is disabled, we keep
// the geometry on state.regionFilter but flag it inactive so the filter
// pipeline ignores it. Lets users A/B compare "with region" vs "without"
// without redrawing.
let _regionActive = true;

function initRegionDrawTool() {
    const btn = document.getElementById("region-draw-btn");
    const cancelBtn = document.getElementById("region-cancel-btn");
    const clearBtn = document.getElementById("region-clear-btn");
    const toggleBtn = document.getElementById("brush-region-toggle");
    const modeMenu = document.getElementById("region-mode-menu");
    if (!btn) return;

    // REGION button toggles the shape picker menu (or cancels if
    // already drawing)
    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (_regionDrawing) {
            _exitRegionDrawMode();
            return;
        }
        if (modeMenu) {
            const isOpen = !modeMenu.hidden;
            modeMenu.hidden = isOpen;
            btn.setAttribute("aria-expanded", String(!isOpen));
        }
    });

    // Pick a shape mode from the menu
    if (modeMenu) {
        modeMenu.querySelectorAll("[data-region-mode]").forEach(item => {
            item.addEventListener("click", () => {
                const mode = item.dataset.regionMode;
                modeMenu.hidden = true;
                btn.setAttribute("aria-expanded", "false");
                _enterRegionDrawMode(mode);
            });
        });
        // Close menu on outside click
        document.addEventListener("click", (e) => {
            if (modeMenu.hidden) return;
            if (btn.contains(e.target) || modeMenu.contains(e.target)) return;
            modeMenu.hidden = true;
            btn.setAttribute("aria-expanded", "false");
        });
    }

    if (cancelBtn) {
        cancelBtn.addEventListener("click", () => _exitRegionDrawMode());
    }
    if (clearBtn) {
        clearBtn.addEventListener("click", () => clearRegionFilter());
    }
    if (toggleBtn) {
        toggleBtn.addEventListener("click", () => toggleRegionFilter());
    }

    // Escape cancels draw mode or closes the menu.
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (_regionDrawing) {
            _exitRegionDrawMode();
        } else if (modeMenu && !modeMenu.hidden) {
            modeMenu.hidden = true;
            btn.setAttribute("aria-expanded", "false");
        }
    });
}

// v0.11.4 — toggle the region filter on/off without clearing the
// drawn geometry. Button on the TimeBrush bar (visible across all
// tabs) so users can A/B compare data with/without the spatial cut.
function toggleRegionFilter() {
    if (!state.regionFilter) return;
    _regionActive = !_regionActive;
    _syncRegionToggleUi();
    // Show or hide the Leaflet rectangle overlay
    if (state.map) {
        if (_regionActive) {
            if (_regionAppliedLayer && !state.map.hasLayer(_regionAppliedLayer)) {
                _regionAppliedLayer.addTo(state.map);
            }
        } else {
            if (_regionAppliedLayer && state.map.hasLayer(_regionAppliedLayer)) {
                state.map.removeLayer(_regionAppliedLayer);
            }
        }
    }
    applyFilters();
}

// v0.11.4 — reflect _regionActive on the toggle button UI + the
// region chip. Called on toggle + when the filter is applied/cleared.
function _syncRegionToggleUi() {
    const toggleBtn = document.getElementById("brush-region-toggle");
    const txtEl = toggleBtn?.querySelector(".brush-region-toggle-text");
    if (toggleBtn) {
        const hasRegion = !!state.regionFilter;
        toggleBtn.hidden = !hasRegion;
        toggleBtn.setAttribute("aria-pressed", String(_regionActive));
        if (txtEl) txtEl.textContent = _regionActive ? "REGION ON" : "REGION OFF";
    }
    const chip = document.getElementById("region-chip");
    if (chip) {
        chip.classList.toggle("is-disabled", !_regionActive);
    }
}

function _enterRegionDrawMode(mode) {
    if (!state.map) return;
    mode = mode || "rect";
    _regionDrawing = true;
    _regionMode = mode;
    _regionPolyPoints = [];
    _regionPolyCursor = null;
    _regionDragStart = null;
    _regionDragCurrent = null;

    const btn = document.getElementById("region-draw-btn");
    const banner = document.getElementById("region-banner");
    const wrap = document.querySelector(".observatory-canvas-wrap");
    if (btn) btn.classList.add("active");
    if (btn) btn.setAttribute("aria-pressed", "true");
    if (banner) {
        banner.hidden = false;
        const text = banner.querySelector(".region-banner-text");
        if (text) {
            text.textContent = (
                mode === "polygon" ? "POLYGON MODE — Click to place vertices, drag dots to reposition. Double-click or click the first vertex to close." :
                mode === "ellipse" ? "ELLIPSE MODE — Click and drag corner-to-corner to define the bounding box." :
                "RECTANGLE MODE — Click and drag on the map to draw a bounding box."
            );
        }
    }
    if (wrap) wrap.classList.add("region-drawing");

    // Disable Leaflet interactions that conflict with drawing
    state.map.dragging.disable();
    state.map.boxZoom.disable();
    state.map.doubleClickZoom.disable();

    // v0.11.9: SVG overlay fully removed. Rectangle uses the DOM
    // DIV overlay, ellipse + polygon use Leaflet-native layers.

    const mapEl = state.map.getContainer();
    if (mode === "polygon") {
        // v0.11.5 fix: use pointerdown/up (not click) because
        // deck.gl's click handler on the canvas eats the event.
        // pointerdown lands first + we can do our own click-vs-drag
        // threshold like the rectangle.
        mapEl.addEventListener("pointerdown", _regionPolyPointerDown, true);
        mapEl.addEventListener("pointermove", _regionPolyPointerMove, true);
        mapEl.addEventListener("pointerup", _regionPolyPointerUp, true);
        mapEl.addEventListener("dblclick", _regionPolyDblclick, true);
    } else {
        // rect + ellipse use the same pointer-drag pattern
        mapEl.addEventListener("pointerdown", _regionPointerDown, true);
        mapEl.addEventListener("pointermove", _regionPointerMove, true);
        mapEl.addEventListener("pointerup", _regionPointerUp, true);
    }
}

function _exitRegionDrawMode() {
    _regionDrawing = false;
    const mode = _regionMode;
    _regionMode = null;
    _regionDragStart = null;
    _regionDragCurrent = null;
    _regionPolyPoints = [];
    _regionPolyCursor = null;

    const btn = document.getElementById("region-draw-btn");
    const banner = document.getElementById("region-banner");
    const dragRect = document.getElementById("region-drag-rect");
    const wrap = document.querySelector(".observatory-canvas-wrap");
    if (btn) btn.classList.remove("active");
    if (btn) btn.setAttribute("aria-pressed", "false");
    if (banner) banner.hidden = true;
    if (dragRect) dragRect.hidden = true;
    // Tear down Leaflet-native preview layers
    _clearPolyVertexMarkers();
    _clearEllipsePreview();
    if (wrap) wrap.classList.remove("region-drawing");

    if (state.map) {
        state.map.dragging.enable();
        state.map.boxZoom.enable();
        state.map.doubleClickZoom.enable();

        const mapEl = state.map.getContainer();
        if (mode === "polygon") {
            mapEl.removeEventListener("pointerdown", _regionPolyPointerDown, true);
            mapEl.removeEventListener("pointermove", _regionPolyPointerMove, true);
            mapEl.removeEventListener("pointerup", _regionPolyPointerUp, true);
            mapEl.removeEventListener("dblclick", _regionPolyDblclick, true);
        } else {
            mapEl.removeEventListener("pointerdown", _regionPointerDown, true);
            mapEl.removeEventListener("pointermove", _regionPointerMove, true);
            mapEl.removeEventListener("pointerup", _regionPointerUp, true);
        }
    }
}

// v0.11.9: _resizeRegionSvg was removed along with the SVG overlay —
// preview rendering moved to Leaflet-native layers. Pointer event
// coordinates still use state.map.getContainer().getBoundingClientRect()
// below, which is where the map canvas lives.

function _regionPointerDown(e) {
    if (!_regionDrawing || !state.map) return;
    if (e.button !== 0) return;  // left-click only
    e.preventDefault();
    e.stopPropagation();
    const rect = state.map.getContainer().getBoundingClientRect();
    _regionDragStart = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    _regionDragCurrent = { ..._regionDragStart };
    if (_regionMode === "ellipse") {
        _updateDragEllipseVisual();
    } else {
        _updateDragRectVisual();
    }
}

function _regionPointerMove(e) {
    if (!_regionDrawing || !_regionDragStart) return;
    e.preventDefault();
    const rect = state.map.getContainer().getBoundingClientRect();
    _regionDragCurrent = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    if (_regionMode === "ellipse") {
        _updateDragEllipseVisual();
    } else {
        _updateDragRectVisual();
    }
}

function _regionPointerUp(e) {
    if (!_regionDrawing || !_regionDragStart || !state.map) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = state.map.getContainer().getBoundingClientRect();
    const endX = e.clientX - rect.left;
    const endY = e.clientY - rect.top;
    const dx = endX - _regionDragStart.x;
    const dy = endY - _regionDragStart.y;

    // Minimum 10px drag so a twitch doesn't commit a zero shape.
    if (dx * dx + dy * dy < 100) {
        _exitRegionDrawMode();
        return;
    }

    const map = state.map;
    // Both rect and ellipse use corner-to-corner. The only
    // difference is shape.type — which the deck.js filter + chip
    // renderer inspect to pick the right math.
    const p1 = map.containerPointToLatLng(
        L.point(Math.min(_regionDragStart.x, endX), Math.min(_regionDragStart.y, endY))
    );
    const p2 = map.containerPointToLatLng(
        L.point(Math.max(_regionDragStart.x, endX), Math.max(_regionDragStart.y, endY))
    );
    const shape = {
        type: _regionMode === "ellipse" ? "ellipse" : "rect",
        north: Math.max(p1.lat, p2.lat),
        south: Math.min(p1.lat, p2.lat),
        west:  Math.min(p1.lng, p2.lng),
        east:  Math.max(p1.lng, p2.lng),
    };
    _exitRegionDrawMode();
    applyRegionFilter(shape);
}

function _updateDragRectVisual() {
    const dragRect = document.getElementById("region-drag-rect");
    if (!dragRect || !_regionDragStart || !_regionDragCurrent) return;
    const x1 = Math.min(_regionDragStart.x, _regionDragCurrent.x);
    const y1 = Math.min(_regionDragStart.y, _regionDragCurrent.y);
    const x2 = Math.max(_regionDragStart.x, _regionDragCurrent.x);
    const y2 = Math.max(_regionDragStart.y, _regionDragCurrent.y);
    dragRect.hidden = false;
    dragRect.style.left = x1 + "px";
    dragRect.style.top = y1 + "px";
    dragRect.style.width = (x2 - x1) + "px";
    dragRect.style.height = (y2 - y1) + "px";
}

// v0.11.8: ellipse preview uses a Leaflet-native L.polygon (64-vertex
// ellipse approximation). The SVG overlay approach had z-index /
// coordinate-space issues that made it invisible in some browsers.
// Leaflet-native renders in its own pane stack correctly.
let _ellipsePreviewLayer = null;

function _updateDragEllipseVisual() {
    if (!state.map || !_regionDragStart || !_regionDragCurrent) return;
    const map = state.map;
    const p1 = map.containerPointToLatLng(L.point(_regionDragStart.x, _regionDragStart.y));
    const p2 = map.containerPointToLatLng(L.point(_regionDragCurrent.x, _regionDragCurrent.y));
    const shape = {
        type: "ellipse",
        north: Math.max(p1.lat, p2.lat),
        south: Math.min(p1.lat, p2.lat),
        west:  Math.min(p1.lng, p2.lng),
        east:  Math.max(p1.lng, p2.lng),
    };
    const latLngs = _ellipseToPolygonPoints(shape, 64);
    if (_ellipsePreviewLayer) {
        _ellipsePreviewLayer.setLatLngs(latLngs);
    } else {
        _ellipsePreviewLayer = L.polygon(latLngs, {
            className: "region-preview-shape",
            dashArray: "6, 4",
            interactive: false,
            renderer: _getRegionRenderer(),
        }).addTo(map);
    }
}

function _clearEllipsePreview() {
    if (_ellipsePreviewLayer && state.map) {
        state.map.removeLayer(_ellipsePreviewLayer);
    }
    _ellipsePreviewLayer = null;
}

// ---- Polygon drawing ----
//
// v0.11.6: uses pointerdown/up pattern (not click) because deck.gl's
// click handler eats the event. Also supports dragging placed
// vertices to reposition them — pointerdown that lands within 14px
// of an existing vertex starts a drag instead of placing a new one.

let _polyDownAt = null;           // {x, y} of pointerdown
let _polyDraggingVertex = -1;     // index of vertex being repositioned, -1 if placing

function _regionPolyPointerDown(e) {
    if (!_regionDrawing || _regionMode !== "polygon" || !state.map) return;
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = state.map.getContainer().getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    _polyDownAt = { x, y };

    // Hit-test existing vertices (14px radius)
    _polyDraggingVertex = -1;
    for (let i = 0; i < _regionPolyPoints.length; i++) {
        const [vx, vy] = _regionPolyPoints[i];
        const dx = x - vx, dy = y - vy;
        if (dx * dx + dy * dy <= 196) {  // 14px
            _polyDraggingVertex = i;
            break;
        }
    }
}

function _regionPolyPointerMove(e) {
    if (!_regionDrawing || _regionMode !== "polygon") return;
    const rect = state.map.getContainer().getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    if (_polyDraggingVertex >= 0) {
        // Live vertex drag
        _regionPolyPoints[_polyDraggingVertex] = [x, y];
    } else {
        // Cursor preview for next vertex
        _regionPolyCursor = { x, y };
    }
    _updatePolyVisual();
}

function _regionPolyPointerUp(e) {
    if (!_regionDrawing || _regionMode !== "polygon") return;
    e.preventDefault();
    e.stopPropagation();
    const rect = state.map.getContainer().getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    // Vertex drag finalized — just update visual, no placement.
    if (_polyDraggingVertex >= 0) {
        _polyDraggingVertex = -1;
        _polyDownAt = null;
        _updatePolyVisual();
        return;
    }

    if (!_polyDownAt) return;
    const dx = x - _polyDownAt.x;
    const dy = y - _polyDownAt.y;
    _polyDownAt = null;

    // Treat as click only if movement is small (<5px); otherwise
    // the user was probably trying to pan and we missed it above.
    if (dx * dx + dy * dy > 25) return;

    // Close on click-near-first-vertex (within 14px) if we have 3+
    if (_regionPolyPoints.length >= 3) {
        const [fx, fy] = _regionPolyPoints[0];
        const fdx = x - fx, fdy = y - fy;
        if (fdx * fdx + fdy * fdy < 196) {  // 14px
            _closePolygon();
            return;
        }
    }

    // Place a new vertex
    _regionPolyPoints.push([x, y]);
    _updatePolyVisual();
}

function _regionPolyDblclick(e) {
    if (!_regionDrawing || _regionMode !== "polygon") return;
    e.preventDefault();
    e.stopPropagation();
    if (_regionPolyPoints.length >= 3) {
        _closePolygon();
    }
}

// v0.11.7: polygon vertex markers rendered via Leaflet's native SVG
// renderer (L.circleMarker). This sidesteps z-index issues where
// our own SVG overlay sat below the Leaflet map stack.
// v0.11.8: extended the same Leaflet-native approach to the in-
// progress line (L.polyline) and fill (L.polygon) previews.
let _polyVertexMarkers = [];    // L.circleMarker[]
let _polyLinePreview = null;    // L.polyline (dashed, follows cursor)
let _polyFillPreview = null;    // L.polygon (shown when 3+ vertices)

// v0.11.10: the Leaflet map is initialised with preferCanvas:true,
// which means L.polygon / L.circleMarker / L.polyline use a canvas
// renderer by default. That canvas sits above the deck.gl canvas in
// DOM order and intercepts pointer events regardless of
// interactive:false on individual shapes — so clicks on map points
// stopped working after drawing a region.
//
// Force an SVG renderer for all region-related layers. Paths with
// interactive:false get `pointer-events: none` in SVG, which lets
// clicks pass through to the deck.gl canvas below.
let _regionSvgRenderer = null;
function _getRegionRenderer() {
    if (!_regionSvgRenderer) {
        _regionSvgRenderer = L.svg({ padding: 0.1 });
    }
    return _regionSvgRenderer;
}

function _updatePolyVisual() {
    if (!state.map) return;
    const map = state.map;
    const pts = _regionPolyPoints;

    // Convert placed pixel points to lat/lng (Leaflet layers need ll)
    const vertexLLs = pts.map(([x, y]) =>
        map.containerPointToLatLng(L.point(x, y))
    );

    // --- Translucent fill (3+ vertices) ---
    if (pts.length >= 3) {
        if (_polyFillPreview) {
            _polyFillPreview.setLatLngs(vertexLLs);
        } else {
            _polyFillPreview = L.polygon(vertexLLs, {
                className: "region-preview-shape",
                dashArray: null,
                interactive: false,
                renderer: _getRegionRenderer(),
            }).addTo(map);
        }
    } else if (_polyFillPreview) {
        map.removeLayer(_polyFillPreview);
        _polyFillPreview = null;
    }

    // --- Dashed rubber-band line (vertices + cursor preview) ---
    const lineLLs = vertexLLs.slice();
    if (_regionPolyCursor && _polyDraggingVertex < 0) {
        const cursorLL = map.containerPointToLatLng(
            L.point(_regionPolyCursor.x, _regionPolyCursor.y)
        );
        lineLLs.push(cursorLL);
    }
    if (lineLLs.length >= 2) {
        if (_polyLinePreview) {
            _polyLinePreview.setLatLngs(lineLLs);
        } else {
            _polyLinePreview = L.polyline(lineLLs, {
                className: "region-preview-line",
                dashArray: "6, 4",
                interactive: false,
                renderer: _getRegionRenderer(),
            }).addTo(map);
        }
    } else if (_polyLinePreview) {
        map.removeLayer(_polyLinePreview);
        _polyLinePreview = null;
    }

    // --- Vertex markers (Leaflet-native circle markers) ---
    while (_polyVertexMarkers.length > pts.length) {
        const m = _polyVertexMarkers.pop();
        if (m) map.removeLayer(m);
    }
    for (let i = 0; i < pts.length; i++) {
        const ll = vertexLLs[i];
        const isFirst = i === 0 && pts.length >= 3;
        const isDragging = i === _polyDraggingVertex;
        const cls = [
            "region-vertex",
            isFirst ? "region-vertex-first" : "",
            isDragging ? "region-vertex-dragging" : "",
        ].filter(Boolean).join(" ");
        if (_polyVertexMarkers[i]) {
            _polyVertexMarkers[i].setLatLng(ll);
            _polyVertexMarkers[i].setRadius(isFirst ? 9 : (isDragging ? 10 : 7));
            const el = _polyVertexMarkers[i]._path;
            if (el) el.setAttribute("class", "leaflet-interactive " + cls);
        } else {
            const marker = L.circleMarker(ll, {
                radius: isFirst ? 9 : 7,
                className: cls,
                interactive: false,
                bubblingMouseEvents: false,
                renderer: _getRegionRenderer(),
            }).addTo(map);
            _polyVertexMarkers.push(marker);
        }
    }
}

function _clearPolyVertexMarkers() {
    if (!state.map) {
        _polyVertexMarkers = [];
        _polyLinePreview = null;
        _polyFillPreview = null;
        return;
    }
    const map = state.map;
    for (const m of _polyVertexMarkers) {
        if (m) map.removeLayer(m);
    }
    _polyVertexMarkers = [];
    if (_polyLinePreview) { map.removeLayer(_polyLinePreview); _polyLinePreview = null; }
    if (_polyFillPreview) { map.removeLayer(_polyFillPreview); _polyFillPreview = null; }
}

function _closePolygon() {
    if (!state.map || _regionPolyPoints.length < 3) return;
    const latlngs = _regionPolyPoints.map(([x, y]) =>
        state.map.containerPointToLatLng(L.point(x, y))
    );
    const shape = {
        type: "polygon",
        points: latlngs.map(ll => [ll.lat, ll.lng]),
    };
    _exitRegionDrawMode();
    applyRegionFilter(shape);
}

function applyRegionFilter(shape) {
    if (!shape || !state.map) return;
    // v0.11.5: shape can be any of:
    //   { type: "rect", south, north, west, east }
    //   { type: "polygon", points: [[lat,lng], ...] }
    //   { type: "circle", centerLat, centerLng, radiusKm }
    // Default to "rect" for backward compat if caller omitted type.
    if (!shape.type) shape.type = "rect";

    // Derive a bounding box for fast pre-culling in deck.js regardless
    // of shape type. Polygon uses min/max of vertices; circle uses
    // a degree-approximation from lat/lng + radius.
    shape.bbox = _computeShapeBbox(shape);

    // Replace any existing region
    if (_regionAppliedLayer) {
        state.map.removeLayer(_regionAppliedLayer);
        _regionAppliedLayer = null;
    }
    state.regionFilter = shape;
    _regionActive = true;  // new drawing always starts active

    // Paint the persistent Leaflet overlay matching the shape type.
    // Leaflet has no built-in "ellipse in lat/lng" layer, so for
    // ellipse we approximate with a 64-vertex polygon that matches
    // the point-in-ellipse test the filter uses.
    const _regionLayerOpts = {
        className: "region-applied",
        interactive: false,
        renderer: _getRegionRenderer(),
    };
    if (shape.type === "ellipse") {
        const pts = _ellipseToPolygonPoints(shape, 64);
        _regionAppliedLayer = L.polygon(pts, _regionLayerOpts).addTo(state.map);
    } else if (shape.type === "polygon") {
        _regionAppliedLayer = L.polygon(shape.points, _regionLayerOpts).addTo(state.map);
    } else {
        _regionAppliedLayer = L.rectangle(
            [[shape.south, shape.west], [shape.north, shape.east]],
            _regionLayerOpts
        ).addTo(state.map);
    }
    _renderRegionChip();
    _syncRegionToggleUi();
    applyFilters();
    writeHash();
}

// Compute a [south, north, west, east] bbox for any shape type.
// Used for fast pre-cull in deck.js's hot loop.
function _computeShapeBbox(shape) {
    if (shape.type === "rect" || shape.type === "ellipse") {
        return [shape.south, shape.north, shape.west, shape.east];
    }
    if (shape.type === "polygon") {
        let s = 90, n = -90, w = 180, e = -180;
        for (const [lat, lng] of shape.points) {
            if (lat < s) s = lat;
            if (lat > n) n = lat;
            if (lng < w) w = lng;
            if (lng > e) e = lng;
        }
        return [s, n, w, e];
    }
    return null;
}

// Convert an ellipse shape to a polygon approximation (for the
// persistent Leaflet overlay). N vertices traces the ellipse.
function _ellipseToPolygonPoints(shape, n) {
    const cLat = (shape.north + shape.south) / 2;
    const cLng = (shape.east + shape.west) / 2;
    const rLat = (shape.north - shape.south) / 2;
    const rLng = (shape.east - shape.west) / 2;
    const pts = [];
    for (let i = 0; i < n; i++) {
        const t = (i / n) * 2 * Math.PI;
        pts.push([cLat + rLat * Math.sin(t), cLng + rLng * Math.cos(t)]);
    }
    return pts;
}

function clearRegionFilter() {
    state.regionFilter = null;
    _regionActive = true;  // reset for next time
    if (_regionAppliedLayer && state.map) {
        state.map.removeLayer(_regionAppliedLayer);
    }
    _regionAppliedLayer = null;
    _renderRegionChip();
    _syncRegionToggleUi();
    applyFilters();
    writeHash();
}
// Expose for CSS onclick fallbacks + hash restore.
window.clearRegionFilter = clearRegionFilter;

function _renderRegionChip() {
    const chip = document.getElementById("region-chip");
    const bounds = document.getElementById("region-chip-bounds");
    const icon = chip?.querySelector(".region-chip-icon");
    if (!chip || !bounds) return;
    if (!state.regionFilter) {
        chip.hidden = true;
        return;
    }
    const r = state.regionFilter;
    const fmt = (v, posLabel, negLabel) => {
        const abs = Math.abs(v).toFixed(1);
        return `${abs}°${v >= 0 ? posLabel : negLabel}`;
    };
    let label = "";
    if (r.type === "ellipse") {
        const cLat = (r.north + r.south) / 2;
        const cLng = (r.east + r.west) / 2;
        const center = `${fmt(cLat, "N", "S")}, ${fmt(cLng, "E", "W")}`;
        label = `○ ${center} ellipse`;
        if (icon) icon.textContent = "○";
    } else if (r.type === "polygon") {
        label = `⬠ ${r.points.length}-vertex polygon`;
        if (icon) icon.textContent = "⬠";
    } else {
        // rect
        const [s, n, w, e] = r.bbox;
        const sw = `${fmt(s, "N", "S")}, ${fmt(w, "E", "W")}`;
        const ne = `${fmt(n, "N", "S")}, ${fmt(e, "E", "W")}`;
        label = `${sw} → ${ne}`;
        if (icon) icon.textContent = "▭";
    }
    bounds.textContent = label;
    chip.hidden = false;
}

// URL hash encoding formats (2-decimal precision throughout):
//   rect:south,west;north,east
//   ellipse:south,west;north,east   (same corners as rect, different math)
//   poly:lat1,lng1;lat2,lng2;...;latN,lngN
function _encodeRegionHash() {
    const r = state.regionFilter;
    if (!r) return null;
    const f = (n) => Number(n).toFixed(2);
    if (r.type === "polygon") {
        return "poly:" + r.points.map(([lat, lng]) => `${f(lat)},${f(lng)}`).join(";");
    }
    // rect + ellipse share the same two-corner format
    const prefix = r.type === "ellipse" ? "ellipse" : "rect";
    const [s, n, w, e] = r.bbox;
    return `${prefix}:${f(s)},${f(w)};${f(n)},${f(e)}`;
}
function _decodeRegionHash(val) {
    if (!val || typeof val !== "string") return null;
    const colonIdx = val.indexOf(":");
    if (colonIdx < 0) return null;
    const kind = val.slice(0, colonIdx);
    const rest = val.slice(colonIdx + 1);

    if (kind === "rect" || kind === "ellipse") {
        const parts = rest.split(";");
        if (parts.length !== 2) return null;
        const [sw, ne] = parts.map(p => p.split(",").map(Number));
        if (sw.length !== 2 || ne.length !== 2) return null;
        if (sw.some(isNaN) || ne.some(isNaN)) return null;
        return {
            type: kind,
            south: sw[0], west: sw[1],
            north: ne[0], east: ne[1],
        };
    }
    if (kind === "poly") {
        const pts = rest.split(";").map(p => p.split(",").map(Number));
        if (pts.length < 3) return null;
        if (pts.some(p => p.length !== 2 || p.some(isNaN))) return null;
        return { type: "polygon", points: pts };
    }
    return null;
}



// =========================================================================
// v0.11.9: Observatory sidebar Live Analytics
// =========================================================================
//
// Replaces the old dead Sources/Shapes/Data Quality rail sections with
// a live dashboard that updates every time filters change. All render
// functions aggregate over POINTS.visibleIdx — zero server round-trips.
// Updates from applyClientFilters() -> refreshRailAnalytics().

const _RAIL_SOURCE_COLORS = {
    1: "#4e79a7",  // UFOCAT blue
    2: "#f28e2b",  // NUFORC orange
    3: "#e15759",  // MUFON red
    4: "#76b7b2",  // UPDB teal
    5: "#59a14f",  // UFO-search green
};
const _RAIL_SOURCE_KEYS = {
    "UFOCAT": "ufocat",
    "NUFORC": "nuforc",
    "MUFON": "mufon",
    "UPDB": "updb",
    "UFO-search": "ufo-search",
};

function refreshRailAnalytics() {
    if (!window.UFODeck || !window.UFODeck.POINTS || !window.UFODeck.POINTS.ready) return;
    const P = window.UFODeck.POINTS;
    const iter = P.visibleIdx;
    const N = iter ? iter.length : P.count;

    // 1) Visible count + %
    const countEl = document.getElementById("rail-visible-count");
    const totalEl = document.getElementById("rail-total-count");
    const pctEl = document.getElementById("rail-visible-pct");
    if (countEl) countEl.textContent = N.toLocaleString();
    if (totalEl) totalEl.textContent = P.count.toLocaleString();
    if (pctEl) {
        const pct = P.count > 0 ? (N / P.count) * 100 : 0;
        pctEl.textContent = `· ${pct.toFixed(1)}%`;
    }

    if (N === 0) {
        _clearRailChart("rail-shapes-chart");
        _clearRailChart("rail-sources-chart");
        const stacked = document.getElementById("rail-sources-stacked");
        if (stacked) stacked.innerHTML = "";
        _clearRailHistogram();
        return;
    }

    // 2) Aggregate shape, source, quality in a single pass
    const shapeCounts = new Uint32Array(256);
    const sourceCounts = new Uint32Array(16);
    const qualBuckets = new Uint32Array(10);  // 0-10, 10-20, ..., 90-100
    let qualCount = 0;
    const qs = P.qualityScore;
    const si = P.shapeIdx;
    const src = P.sourceIdx;
    const UNK = 255;
    for (let k = 0; k < N; k++) {
        const i = iter ? iter[k] : k;
        shapeCounts[si[i]]++;
        sourceCounts[src[i]]++;
        const q = qs[i];
        if (q !== UNK) {
            let b = Math.min(9, Math.floor(q / 10));
            qualBuckets[b]++;
            qualCount++;
        }
    }

    // 3) Render Top Shapes (skip index 0 = unknown)
    const shapeItems = [];
    const shapes = P.shapes || [];
    for (let i = 1; i < shapes.length && i < 256; i++) {
        if (shapeCounts[i] > 0 && shapes[i]) {
            shapeItems.push({ label: shapes[i], count: shapeCounts[i] });
        }
    }
    shapeItems.sort((a, b) => b.count - a.count);
    _renderRailChart("rail-shapes-chart", shapeItems.slice(0, 8));

    // 4) Render Sources (stacked bar + list)
    const sourceItems = [];
    const sources = P.sources || [];
    for (let i = 1; i < sources.length && i < 16; i++) {
        if (sourceCounts[i] > 0 && sources[i]) {
            sourceItems.push({
                label: sources[i],
                count: sourceCounts[i],
                key: _RAIL_SOURCE_KEYS[sources[i]] || `src-${i}`,
                color: _RAIL_SOURCE_COLORS[i] || "#888",
            });
        }
    }
    sourceItems.sort((a, b) => b.count - a.count);
    _renderRailStackedBar("rail-sources-stacked", sourceItems);
    _renderRailChart("rail-sources-chart", sourceItems);

    // 5) Render Quality histogram (10 buckets)
    _renderRailHistogram(qualBuckets, qualCount);
}

function _renderRailChart(elementId, items) {
    const el = document.getElementById(elementId);
    if (!el) return;
    if (!items || items.length === 0) {
        el.innerHTML = `<li class="rail-mini-chart-empty" style="grid-template-columns:1fr;text-align:center;color:var(--text-faint);padding:4px">no data</li>`;
        return;
    }
    const max = items[0].count;
    el.innerHTML = items.map(it => {
        const pct = max > 0 ? (it.count / max) * 100 : 0;
        const dataSrc = it.key ? ` data-src="${escapeHtml(it.key)}"` : "";
        return `<li>
            <span class="rail-mini-chart-label" title="${escapeHtml(it.label)}">${escapeHtml(it.label)}</span>
            <span class="rail-mini-chart-bar"><span class="rail-mini-chart-fill"${dataSrc} style="width:${pct.toFixed(1)}%"></span></span>
            <span class="rail-mini-chart-count">${it.count.toLocaleString()}</span>
        </li>`;
    }).join("");
}

function _clearRailChart(elementId) {
    const el = document.getElementById(elementId);
    if (el) el.innerHTML = "";
}

function _renderRailStackedBar(elementId, items) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const total = items.reduce((a, b) => a + b.count, 0);
    if (total === 0) { el.innerHTML = ""; return; }
    el.innerHTML = items.map(it => {
        const pct = (it.count / total) * 100;
        return `<span class="rail-stacked-bar-seg"
                      style="flex:${pct.toFixed(2)};background:${it.color}"
                      title="${escapeHtml(it.label)}: ${it.count.toLocaleString()}"></span>`;
    }).join("");
}

function _renderRailHistogram(buckets, total) {
    const el = document.getElementById("rail-quality-histogram");
    if (!el) return;
    if (total === 0) { el.innerHTML = `<div style="color:var(--text-faint);font-size:10px;text-align:center;width:100%">no quality data</div>`; return; }
    // Find max for scaling
    let max = 1;
    for (let i = 0; i < buckets.length; i++) {
        if (buckets[i] > max) max = buckets[i];
    }
    const bars = [];
    for (let i = 0; i < buckets.length; i++) {
        const h = (buckets[i] / max) * 100;
        const lo = i * 10, hi = lo + 10;
        let cls = "rail-histogram-bar";
        if (i < 3) cls += " is-low";
        else if (i < 6) cls += " is-mid";
        else cls += " is-high";
        bars.push(`<div class="${cls}" style="height:${h.toFixed(1)}%"
                        title="${lo}-${hi}: ${buckets[i].toLocaleString()} sightings"></div>`);
    }
    el.innerHTML = bars.join("");
}
function _clearRailHistogram() {
    const el = document.getElementById("rail-quality-histogram");
    if (el) el.innerHTML = "";
}

// v0.11.9 — wire up the Observatory DQ gear popup. Reuses the same
// _mountDqGearPopup pattern Timeline + Insights already use.
function initObservatoryDqGear() {
    if (typeof _mountDqGearPopup === "function") {
        _mountDqGearPopup("observatory-dq-gear", "observatory-dq-popup", "observatory-dq-list");
    }
}
