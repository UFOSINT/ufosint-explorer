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
    mapMode: "clusters",  // "clusters" or "heatmap"
    chart: null,
    timelineYear: null,  // null = yearly view, "2005" = monthly drill-down
    searchPage: 0,
    searchTotal: 0,
    searchSort: "date_desc",   // date_desc | date_asc
    dupesPage: 0,
    dupesTotal: 0,
    insightsCharts: {},  // { radar, timeline, source, shape }
    // Set to true while we're loading state from the URL hash on
    // navigation, so the hash-update side effect is suppressed.
    hashLoading: false,
};

// Filter ID -> human label, used by the active-filter chip strip in the
// search panel and as the source-of-truth for serializing filters to the
// URL hash.
const FILTER_FIELDS = [
    { id: "filter-date-from", key: "date_from", label: "From" },
    { id: "filter-date-to",   key: "date_to",   label: "To" },
    { id: "filter-shape",     key: "shape",     label: "Shape" },
    { id: "filter-collection",key: "collection",label: "Collection" },
    { id: "filter-source",    key: "source",    label: "Source" },
    { id: "filter-country",   key: "country",   label: "Country" },
    { id: "filter-state",     key: "state",     label: "State" },
    { id: "filter-hynek",     key: "hynek",     label: "Hynek" },
    { id: "filter-vallee",    key: "vallee",    label: "Vallee" },
    { id: "coords-filter",    key: "coords",    label: "Coords" },
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
    // Load filters and stats in parallel
    const [filtersData, statsData] = await Promise.all([
        fetchJSON("/api/filters"),
        fetchJSON("/api/stats"),
    ]);

    populateFilterDropdowns(filtersData);
    showStats(statsData);
    initStatsBadge();

    // Setup tabs
    document.querySelectorAll(".tab").forEach(btn => {
        btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // Filter buttons
    document.getElementById("btn-apply-filters").addEventListener("click", applyFilters);
    document.getElementById("btn-clear-filters").addEventListener("click", clearFilters);

    // Coords source filter (auto-refresh map on change)
    document.getElementById("coords-filter").addEventListener("change", () => {
        if (state.activeTab === "map") {
            if (state.mapMode === "heatmap") loadHeatmap();
            else loadMapMarkers();
        }
    });

    // Search
    document.getElementById("btn-search").addEventListener("click", doSearch);
    document.getElementById("search-input").addEventListener("keydown", e => {
        if (e.key === "Enter") doSearch();
    });
    const sortEl = document.getElementById("search-sort");
    if (sortEl) {
        sortEl.addEventListener("change", () => {
            state.searchSort = sortEl.value;
            executeSearch();
        });
    }

    // Modal
    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-overlay").addEventListener("click", e => {
        if (e.target === e.currentTarget) closeModal();
    });

    // Timeline back button
    document.getElementById("timeline-back").addEventListener("click", () => {
        state.timelineYear = null;
        loadTimeline();
    });

    // Duplicates
    document.getElementById("btn-dupes-apply").addEventListener("click", () => {
        state.dupesPage = 0;
        document.getElementById("dupes-results").innerHTML = "";
        loadDuplicates();
    });
    document.getElementById("btn-dupes-more").addEventListener("click", () => {
        state.dupesPage++;
        loadDuplicates(true);
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

    // Search panel: export buttons + copy-link button
    initSearchActions();

    // BYOK AI chat
    loadAISettings();
    applySettingsToUI();
    aiInitListeners();

    // Init map
    initMap();

    // ----- URL hash routing: load any deep-linked tab + filters -----
    const initial = readHash();
    if (initial) {
        applyHashToFilters(initial.params);
        if (initial.tab && initial.tab !== "map") {
            switchTab(initial.tab);
            if (initial.tab === "search") {
                doSearch();
            }
        } else if (initial.tab === "map") {
            // applyHashToFilters above already populated the filter inputs;
            // re-fire the map load with them
            if (state.mapMode === "heatmap") loadHeatmap();
            else loadMapMarkers();
        }
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
    const p = new URLSearchParams();
    const df = document.getElementById("filter-date-from").value;
    const dt = document.getElementById("filter-date-to").value;
    const shape = document.getElementById("filter-shape").value;
    const collection = document.getElementById("filter-collection").value;
    const source = document.getElementById("filter-source").value;
    const country = document.getElementById("filter-country")?.value;
    const stateVal = document.getElementById("filter-state")?.value;
    const hynek = document.getElementById("filter-hynek").value;
    const vallee = document.getElementById("filter-vallee").value;
    const coords = document.getElementById("coords-filter").value;

    if (df) p.set("date_from", df);
    if (dt) p.set("date_to", dt);
    if (shape) p.set("shape", shape);
    if (collection) p.set("collection", collection);
    if (source) p.set("source", source);
    if (country) p.set("country", country);
    if (stateVal) p.set("state", stateVal);
    if (hynek) p.set("hynek", hynek);
    if (vallee) p.set("vallee", vallee);
    if (coords && coords !== "all") p.set("coords", coords);

    return p;
}

function populateFilterDropdowns(data) {
    const shapeSelect = document.getElementById("filter-shape");
    (data.shapes || []).forEach(s => {
        const opt = document.createElement("option");
        opt.value = s;
        opt.textContent = s;
        shapeSelect.appendChild(opt);
    });

    const collectionSelect = document.getElementById("filter-collection");
    (data.collections || []).forEach(c => {
        const opt = document.createElement("option");
        opt.value = c.id;
        opt.textContent = c.display_name || c.name;
        collectionSelect.appendChild(opt);
    });

    const sourceSelect = document.getElementById("filter-source");
    // Lookup table used by the timeline click handler to map a source
    // name (from the chart legend) back to its numeric id.
    window.__sourceMap = {};
    (data.sources || []).forEach(s => {
        window.__sourceMap[s.name] = s;
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = s.name;
        sourceSelect.appendChild(opt);
    });

    const countrySelect = document.getElementById("filter-country");
    if (countrySelect) {
        (data.countries || []).forEach(c => {
            const opt = document.createElement("option");
            opt.value = c.value;
            opt.textContent = `${c.value} (${c.count.toLocaleString()})`;
            countrySelect.appendChild(opt);
        });
    }

    const stateSelect = document.getElementById("filter-state");
    if (stateSelect) {
        (data.states || []).forEach(st => {
            const opt = document.createElement("option");
            opt.value = st.value;
            opt.textContent = `${st.value} (${st.count.toLocaleString()})`;
            stateSelect.appendChild(opt);
        });
    }

    const hynekSelect = document.getElementById("filter-hynek");
    (data.hynek || []).forEach(h => {
        const opt = document.createElement("option");
        opt.value = h;
        opt.textContent = h;
        hynekSelect.appendChild(opt);
    });

    const valleeSelect = document.getElementById("filter-vallee");
    (data.vallee || []).forEach(v => {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = v;
        valleeSelect.appendChild(opt);
    });

    // Duplicates filters
    const dupesMethodSelect = document.getElementById("dupes-method");
    (data.match_methods || []).forEach(m => {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        dupesMethodSelect.appendChild(opt);
    });

    const dupesSourceSelect = document.getElementById("dupes-source");
    (data.sources || []).forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = s.name;
        dupesSourceSelect.appendChild(opt);
    });
}

function showStats(data) {
    const badge = document.getElementById("stats-badge");
    const popover = document.getElementById("stats-popover");
    const total = data.total_sightings.toLocaleString();
    const geo = data.geocoded_locations.toLocaleString();
    const geoOrig = (data.geocoded_original || 0).toLocaleString();
    const geoGN = (data.geocoded_geonames || 0).toLocaleString();
    const dupes = data.duplicate_candidates.toLocaleString();

    // Compact badge — three numbers separated by middle dots, no
    // parenthetical, no implementation jargon. The full breakdown
    // (original vs GeoNames, source counts, date range) lives in the
    // popover that opens on click.
    badge.innerHTML = `${total} sightings <span class="stats-sep">·</span> ${geo} mapped <span class="stats-sep">·</span> ${dupes} possible duplicates`;

    if (popover) {
        const sources = (data.by_source || []).map(s =>
            `<tr><td>${escapeHtml(s.name)}</td><td>${s.count.toLocaleString()}</td></tr>`
        ).join("");
        popover.innerHTML = `
            <div class="stats-popover-section">
                <div class="stats-popover-row"><span>Total sightings</span><strong>${total}</strong></div>
                <div class="stats-popover-row"><span>Geocoded locations</span><strong>${geo}</strong></div>
                <div class="stats-popover-row stats-popover-sub"><span>· from source data</span>${geoOrig}</div>
                <div class="stats-popover-row stats-popover-sub"><span>· from GeoNames lookup</span>${geoGN}</div>
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
function switchTab(tab) {
    state.activeTab = tab;

    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelector(`.tab[data-tab="${tab}"]`).classList.add("active");

    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    document.getElementById(`panel-${tab}`).classList.add("active");

    if (tab === "map") {
        // Leaflet needs an invalidateSize when shown
        setTimeout(() => {
            if (state.map) state.map.invalidateSize();
            if (state.mapMode === "heatmap") loadHeatmap();
            else loadMapMarkers();
        }, 100);
    } else if (tab === "timeline") {
        loadTimeline();
    } else if (tab === "insights") {
        loadInsights();
    } else if (tab === "duplicates") {
        // Load on first visit
        if (document.getElementById("dupes-results").children.length === 0) {
            loadDuplicates();
        }
    } else if (tab === "search") {
        renderActiveFilterChips();
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
        if (state.activeTab === "map") {
            if (state.mapMode === "heatmap") await loadHeatmap();
            else await loadMapMarkers();
        }
        else if (state.activeTab === "timeline") await loadTimeline();
        else if (state.activeTab === "search") await doSearch();
        else if (state.activeTab === "insights") await loadInsights();
    } finally {
        restore();
        writeHash();
    }
}

function clearFilters() {
    document.getElementById("filter-date-from").value = "";
    document.getElementById("filter-date-to").value = "";
    document.getElementById("filter-shape").value = "";
    document.getElementById("filter-collection").value = "";
    document.getElementById("filter-source").value = "";
    document.getElementById("filter-country").value = "";
    document.getElementById("filter-state").value = "";
    document.getElementById("filter-hynek").value = "";
    document.getElementById("filter-vallee").value = "";
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
        if (v && v.value && !(key === "coords" && v.value === "all")) {
            params.set(key, v.value);
        }
    });
    if (state.activeTab === "search") {
        const q = document.getElementById("search-input")?.value?.trim();
        if (q) params.set("q", q);
        if (state.searchPage)  params.set("page", state.searchPage);
        if (state.searchSort && state.searchSort !== "date_desc") params.set("sort", state.searchSort);
    }
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
        if (params.get("q") !== null) {
            const inp = document.getElementById("search-input");
            if (inp) inp.value = params.get("q");
        }
        if (params.get("page") !== null) state.searchPage = parseInt(params.get("page"), 10) || 0;
        if (params.get("sort") !== null) state.searchSort = params.get("sort");
    } finally {
        state.hashLoading = false;
    }
}

/**
 * Programmatically jump to the search tab with a given filter set.
 *
 * `filterUpdates` is an object of `{date_from, date_to, source, shape, ...}`
 * (whatever subset you want to set). Anything not specified is left
 * untouched. `clearFirst=true` resets all filters first.
 *
 * Used by:
 *  - Timeline bar clicks (set date range and optionally source)
 *  - Map popup "View all in this city" links (future)
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
            const inp = document.getElementById("search-input");
            if (inp) inp.value = "";
        }
        Object.entries(filterUpdates || {}).forEach(([key, value]) => {
            const field = FILTER_FIELDS.find(f => f.key === key);
            if (field) {
                const el = document.getElementById(field.id);
                if (el) el.value = value == null ? "" : String(value);
            } else if (key === "q") {
                const inp = document.getElementById("search-input");
                if (inp) inp.value = value || "";
            }
        });
        state.searchPage = 0;
    } finally {
        state.hashLoading = false;
    }
    switchTab("search");
    // doSearch is called below so we update both the panel and the hash.
    doSearch();
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
function initMap() {
    state.map = L.map("map", {
        center: [39, -98],
        zoom: 4,
        preferCanvas: true,
    });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 18,
    }).addTo(state.map);

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

    // Load data on move end, debounced + abortable. A casual pan fires
    // multiple moveend events in rapid succession; without this each one
    // triggered a full /api/map request and stale responses could arrive
    // out of order, overwriting fresh data with old markers.
    let reloadSuppressed = false;
    let mapReloadTimer = null;
    let mapReloadAbort = null;

    function scheduleMapReload() {
        if (reloadSuppressed) return;
        clearTimeout(mapReloadTimer);
        mapReloadTimer = setTimeout(() => {
            // Cancel any in-flight request from a previous pan
            if (mapReloadAbort) mapReloadAbort.abort();
            mapReloadAbort = new AbortController();
            const signal = mapReloadAbort.signal;
            if (state.mapMode === "heatmap") loadHeatmap(signal);
            else loadMapMarkers(signal);
        }, 200);
    }

    state.map.on("moveend", scheduleMapReload);
    state.map.on("popupopen", () => { reloadSuppressed = true; });
    state.map.on("popupclose", () => {
        reloadSuppressed = false;
        scheduleMapReload();
    });

    // Initial load
    loadMapMarkers();
}

// Build a marker popup with cross-tab pivots. The popup has up to 3
// links: View Details (modal), View all in this city (Search filtered),
// and Drill into this month (Timeline monthly view). The latter two
// are only added when the marker has the requisite metadata.
function buildMarkerPopupHTML(m) {
    const loc = formatLocation(m.city, m.state, m.country);
    const links = [
        `<a href="#" class="popup-link" onclick="openDetail(${m.id}); return false;">View details →</a>`,
    ];
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
    return `
        <div class="popup">
            <div class="popup-date">${escapeHtml(m.date || "Unknown date")}</div>
            <div class="popup-loc">${escapeHtml(loc) || "Unknown location"}</div>
            <div>${sourceBadge(m.source)} ${m.shape ? `<span class="shape-tag">${escapeHtml(m.shape)}</span>` : ""}</div>
            ${links.join("")}
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
    // yearMonth is "YYYY-MM". Switch to Timeline tab, set the
    // drill-down state to that year, and trigger a load. The user
    // can then click the month bar to jump to filtered search.
    const [year, month] = yearMonth.split("-");
    state.timelineYear = year;
    switchTab("timeline");
    // The timeline panel reads state.timelineYear in loadTimeline,
    // which switchTab calls automatically. The monthly view will
    // appear with the year drill-down already applied.
}
window.drillToMonth = drillToMonth;

async function loadMapMarkers(signal) {
    const status = document.getElementById("map-status");
    const mapEl = document.getElementById("map");
    status.innerHTML = '<span class="loading-pulse">Plotting sightings...</span>';
    mapEl?.classList.add("is-loading");

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
        mapEl?.classList.remove("is-loading");
    }
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
    if (total > 30000 && state.mapMode === "clusters" && !btn.classList.contains("confirming")) {
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
// Map Mode Toggle (Clusters / Heatmap)
// =========================================================================
function toggleMapMode(mode) {
    if (mode === state.mapMode) return;
    state.mapMode = mode;

    // Update toggle button styles
    document.querySelectorAll(".map-mode-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === mode);
    });

    if (mode === "heatmap") {
        // Remove clusters, add heat
        state.map.removeLayer(state.markerLayer);
        state.map.addLayer(state.heatLayer);
        loadHeatmap();
    } else {
        // Remove heat, add clusters
        state.map.removeLayer(state.heatLayer);
        state.map.addLayer(state.markerLayer);
        loadMapMarkers();
    }
}

async function loadHeatmap(signal) {
    const status = document.getElementById("map-status");
    const mapEl = document.getElementById("map");
    status.innerHTML = '<span class="loading-pulse">Building heatmap from sightings...</span>';
    mapEl?.classList.add("is-loading");

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
        mapEl?.classList.remove("is-loading");
    }
}

// =========================================================================
// Timeline
// =========================================================================
async function loadTimeline() {
    const params = getFilterParams();
    const year = state.timelineYear;
    if (year) params.set("year", year);

    const titleEl = document.getElementById("timeline-title");
    const backBtn = document.getElementById("timeline-back");
    const viewBtn = document.getElementById("timeline-view-results");

    titleEl.textContent = year ? `Sightings in ${year} by Month` : "Sightings by Year";
    backBtn.style.display = year ? "inline-block" : "none";

    const data = await fetchJSON(`/api/timeline?${params}`);

    // Build datasets per source
    const periods = Object.keys(data.data).sort();
    const sourceNames = new Set();
    periods.forEach(p => {
        Object.keys(data.data[p]).forEach(s => sourceNames.add(s));
    });
    const sourceList = Array.from(sourceNames);

    const datasets = sourceList.map(name => {
        const c = sourceColor(name);
        return {
            label: name,
            data: periods.map(p => data.data[p][name] || 0),
            backgroundColor: c.bg,
            borderColor: c.border,
            borderWidth: 1,
        };
    });

    // Labels
    const labels = periods.map(p => {
        if (data.mode === "monthly" && p.length >= 7) {
            const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
            const mi = parseInt(p.substring(5, 7), 10) - 1;
            return monthNames[mi] || p;
        }
        return p;
    });

    // Total across all visible bars (used by the "View N results" button label)
    let visibleTotal = 0;
    periods.forEach(p => {
        Object.values(data.data[p]).forEach(v => { visibleTotal += v; });
    });

    // Configure the "View results" button to jump to search filtered to
    // the currently displayed range. In yearly view, it filters by the
    // currently active date_from/date_to (or "all years" if no filters).
    // In monthly view, it filters to the whole displayed year.
    if (viewBtn) {
        if (visibleTotal > 0) {
            viewBtn.textContent = `View ${visibleTotal.toLocaleString()} sightings →`;
            viewBtn.style.display = "inline-block";
            viewBtn.onclick = () => {
                if (year) {
                    navigateToSearch({ date_from: `${year}-01-01`, date_to: `${year}-12-31` });
                } else {
                    // Yearly view: just switch to search with current global filters
                    navigateToSearch({});
                }
            };
        } else {
            viewBtn.style.display = "none";
        }
    }

    // Destroy old chart
    if (state.chart) {
        state.chart.destroy();
    }

    // Use a closure to lift the data we need into the click handler
    const onChartClick = (evt, elements) => {
        if (!elements.length) return;
        const el = elements[0];
        const period = periods[el.index];
        const sourceName = sourceList[el.datasetIndex];

        // Visual feedback: fade the chart container while the drill-down
        // or navigation happens. loadTimeline rebuilds the chart anyway
        // (which clears this class via the next render), but for the
        // month-click path (which navigates away) we remove it on a
        // short delay so the user sees a flash of acknowledgement.
        const chartContainer = document.querySelector(".chart-container");
        chartContainer?.classList.add("is-loading");

        if (data.mode === "yearly") {
            // Click year bar -> drill down to monthly view (existing behavior).
            // If user clicked a stacked source segment we ALSO remember to
            // pre-filter the monthly drill-down by source via the global
            // filter dropdown.
            if (sourceName) {
                const sourceObj = (window.__sourceMap || {})[sourceName];
                if (sourceObj) {
                    document.getElementById("filter-source").value = String(sourceObj.id);
                }
            }
            state.timelineYear = period;
            loadTimeline().finally(() => chartContainer?.classList.remove("is-loading"));
        } else {
            // Navigating away — clear the fade after a short delay
            setTimeout(() => chartContainer?.classList.remove("is-loading"), 300);
            // Monthly view -> jump to filtered search for that month.
            // period is "YYYY-MM"
            const [y, m] = period.split("-");
            const lastDay = lastDayOfMonth(y, m);
            const filterUpdates = {
                date_from: `${y}-${m}-01`,
                date_to:   `${y}-${m}-${String(lastDay).padStart(2, "0")}`,
            };
            // If user clicked a stacked segment, also filter by that source.
            if (sourceName) {
                const sourceObj = (window.__sourceMap || {})[sourceName];
                if (sourceObj) filterUpdates.source = sourceObj.id;
            }
            navigateToSearch(filterUpdates);
        }
    };

    const ctx = document.getElementById("timeline-chart").getContext("2d");
    state.chart = new Chart(ctx, {
        type: "bar",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { position: "top" },
                tooltip: {
                    callbacks: {
                        footer: (items) => {
                            const total = items.reduce((s, i) => s + i.parsed.y, 0);
                            const hint = data.mode === "yearly"
                                ? "Click to drill into months"
                                : "Click to view records";
                            return `Total: ${total.toLocaleString()}\n${hint}`;
                        }
                    }
                }
            },
            scales: {
                x: { stacked: true },
                y: { stacked: true, beginAtZero: true },
            },
            onClick: onChartClick,
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "";
            },
        },
    });
}

// =========================================================================
// Search
// =========================================================================
async function doSearch() {
    state.searchPage = 0;
    await executeSearch();
}

async function executeSearch() {
    renderActiveFilterChips();
    const q = document.getElementById("search-input").value.trim();
    const params = getFilterParams();
    if (q) params.set("q", q);
    params.set("page", state.searchPage);

    const info     = document.getElementById("search-info");
    const resultsEl= document.getElementById("search-results");
    const pagerEl  = document.getElementById("search-pager");
    const searchBtn= document.getElementById("btn-search");

    // Disable the submit button so a double-click doesn't fire two requests
    const restoreSearchBtn = disableButtonWhilePending(searchBtn, "Searching…");

    // Skeleton loading state
    info.innerHTML = '<span class="loading-pulse">Searching...</span>';
    resultsEl.innerHTML = `
        <div class="result-card skeleton"></div>
        <div class="result-card skeleton"></div>
        <div class="result-card skeleton"></div>
    `;
    pagerEl.innerHTML = "";

    try {
        const data = await fetchJSON(`/api/search?${params}`);
        state.searchTotal = data.total;

        // Sort client-side if asked. Backend always returns date_desc.
        const sorted = (state.searchSort === "date_asc")
            ? data.results.slice().sort((a, b) => (a.date || "").localeCompare(b.date || ""))
            : data.results;

        if (data.total === 0) {
            resultsEl.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon"><svg class="icon icon-xl" viewBox="0 0 64 32" aria-hidden="true"><ellipse cx="32" cy="20" rx="28" ry="6"/><path d="M14 18 C14 10, 22 4, 32 4 C42 4, 50 10, 50 18"/><circle cx="32" cy="10" r="1.5" fill="currentColor" stroke="none"/><line x1="14" y1="26" x2="10" y2="30"/><line x1="32" y1="27" x2="32" y2="31"/><line x1="50" y1="26" x2="54" y2="30"/></svg></div>
                    <div class="empty-state-title">No sightings found</div>
                    <div class="empty-state-detail">
                        Try clearing some filters above, or
                        <a href="#" onclick="clearFilters(); document.getElementById('search-input').value=''; doSearch(); return false;">reset everything</a>.
                    </div>
                </div>
            `;
            info.textContent = `0 results${q ? ` for "${q}"` : ""}`;
            return;
        }

        info.innerHTML = `<strong>${data.total.toLocaleString()}</strong> results` +
            (q ? ` for <em>"${escapeHtml(q)}"</em>` : "") +
            ` &middot; page ${data.page + 1} of ${data.pages.toLocaleString()}`;

        const hl = q ? new RegExp(`(${escapeRegExp(q)})`, "gi") : null;
        resultsEl.innerHTML = "";
        sorted.forEach(r => {
            const card = document.createElement("div");
            card.className = "result-card";
            card.onclick = () => openDetail(r.id);

            const loc = formatLocation(r.city, r.state, r.country);
            const desc = r.description || "";
            const descHtml = hl
                ? escapeHtml(desc).replace(hl, '<mark>$1</mark>')
                : escapeHtml(desc);

            const meta = [
                r.hynek    ? `<span class="meta-pill">Hynek: ${escapeHtml(r.hynek)}</span>` : "",
                r.witnesses? `<span class="meta-pill">${r.witnesses} witness${r.witnesses === 1 ? '' : 'es'}</span>` : "",
                r.duration ? `<span class="meta-pill">${escapeHtml(r.duration)}</span>` : "",
            ].filter(Boolean).join("");

            card.innerHTML = `
                <div class="result-header">
                    <span class="result-date">${escapeHtml(r.date || "Unknown date")}</span>
                    ${sourceBadge(r.source)}
                    ${r.shape ? `<span class="shape-tag">${escapeHtml(r.shape)}</span>` : ""}
                </div>
                <div class="result-loc">${escapeHtml(loc) || "Unknown location"}</div>
                <div class="result-desc">${descHtml}</div>
                ${meta ? `<div class="result-meta">${meta}</div>` : ""}
            `;
            resultsEl.appendChild(card);
        });

        renderPager(data.page, data.pages);
    } catch (err) {
        info.textContent = "Couldn't run that search";
        resultsEl.innerHTML = `<div class="empty-state"><div class="empty-state-icon"><svg class="icon icon-xl" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></div><div class="empty-state-title">Search failed</div><div class="empty-state-detail">Check your filters or try again. <br><span style="opacity:0.6">${escapeHtml(err.message || String(err))}</span></div></div>`;
        console.error(err);
    } finally {
        restoreSearchBtn();
    }
}

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

function escapeRegExp(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderActiveFilterChips() {
    const el = document.getElementById("search-active-filters");
    if (!el) return;
    const chips = [];
    FILTER_FIELDS.forEach(({ id, key, label }) => {
        if (key === "coords") return;
        const input = document.getElementById(id);
        if (!input || !input.value) return;
        let display = input.value;
        // For <select>, use the selected option's text as the display label
        if (input.tagName === "SELECT") {
            const opt = input.options[input.selectedIndex];
            display = opt && opt.text ? opt.text : input.value;
        }
        chips.push(`
            <span class="chip" data-field="${id}">
                <span class="chip-label">${label}: ${escapeHtml(display)}</span>
                <button class="chip-x" title="Remove this filter" onclick="removeFilter('${id}')">&times;</button>
            </span>
        `);
    });
    if (chips.length === 0) {
        el.innerHTML = "";
        el.style.display = "none";
    } else {
        chips.push(`<button class="chip-clear" onclick="clearFilters(); doSearch();">Clear all</button>`);
        el.innerHTML = chips.join("");
        el.style.display = "flex";
    }
}

function removeFilter(inputId) {
    const el = document.getElementById(inputId);
    if (el) el.value = "";
    state.searchPage = 0;
    doSearch();
}

function renderPager(currentPage, totalPages) {
    const pagerEl = document.getElementById("search-pager");
    if (!pagerEl) return;
    if (totalPages <= 1) { pagerEl.innerHTML = ""; return; }

    const buttons = [];
    const cur = currentPage; // 0-indexed
    const last = totalPages - 1;

    function btn(label, page, disabled = false, active = false) {
        const cls = ["pager-btn"];
        if (disabled) cls.push("disabled");
        if (active)   cls.push("active");
        const onclick = (disabled || active) ? "" : `onclick="goToPage(${page})"`;
        return `<button class="${cls.join(' ')}" ${onclick}>${label}</button>`;
    }

    buttons.push(btn("« First", 0, cur === 0));
    buttons.push(btn("‹ Prev",  cur - 1, cur === 0));

    // Sliding window of page numbers around the current page
    const windowSize = 2;
    const start = Math.max(0, cur - windowSize);
    const end   = Math.min(last, cur + windowSize);
    if (start > 0) buttons.push(`<span class="pager-ellipsis">…</span>`);
    for (let i = start; i <= end; i++) {
        buttons.push(btn(String(i + 1), i, false, i === cur));
    }
    if (end < last) buttons.push(`<span class="pager-ellipsis">…</span>`);

    buttons.push(btn("Next ›", cur + 1, cur === last));
    buttons.push(btn("Last »", last,    cur === last));

    pagerEl.innerHTML = buttons.join("");
}

function goToPage(page) {
    state.searchPage = page;
    executeSearch();
    // Scroll the results container back to the top — it's overflow-y:auto
    // inside a full-height panel, so scrollIntoView on the window does
    // nothing visible. Setting scrollTop directly is the right call.
    const results = document.getElementById("search-results");
    if (results) results.scrollTop = 0;
}

// =========================================================================
// Duplicates
// =========================================================================
function scoreColor(score) {
    if (score >= 0.9) return "var(--green)";
    if (score >= 0.7) return "var(--accent)";
    if (score >= 0.5) return "var(--orange)";
    if (score >= 0.3) return "#b07d10";
    return "var(--text-muted)";
}

function scoreLabel(score) {
    if (score >= 0.9) return "Certain";
    if (score >= 0.7) return "Likely";
    if (score >= 0.5) return "Possible";
    if (score >= 0.3) return "Weak";
    return "Unlikely";
}

async function loadDuplicates(append = false) {
    const info = document.getElementById("dupes-info");
    const resultsEl = document.getElementById("dupes-results");
    const moreBtn = document.getElementById("btn-dupes-more");
    const applyBtn = document.getElementById("btn-dupes-apply");

    const restoreApplyBtn = disableButtonWhilePending(applyBtn, "Loading…");
    info.innerHTML = '<span class="loading-pulse">Finding possible duplicate pairs — this can take a few seconds.</span>';

    const params = new URLSearchParams();
    params.set("page", state.dupesPage);

    const conf = document.getElementById("dupes-confidence").value;
    if (conf) {
        const [min, max] = conf.split(",");
        params.set("min_score", min);
        params.set("max_score", max);
    }

    const method = document.getElementById("dupes-method").value;
    if (method) params.set("method", method);

    const source = document.getElementById("dupes-source").value;
    if (source) params.set("source", source);

    try {
        const data = await fetchJSON(`/api/duplicates?${params}`);
        state.dupesTotal = data.total;

        if (!append) {
            resultsEl.innerHTML = "";
        }

        info.textContent = `${data.total.toLocaleString()} duplicate pairs` +
            (data.total > 0 ? ` (page ${data.page + 1} of ${data.pages})` : "");

        data.results.forEach(r => {
            const card = document.createElement("div");
            card.className = "dupe-card";

            const pct = r.score !== null ? (r.score * 100).toFixed(0) : "?";
            const label = r.score !== null ? scoreLabel(r.score) : "";
            const color = r.score !== null ? scoreColor(r.score) : "var(--text-muted)";

            const locA = formatLocation(r.a.city, r.a.state, "");
            const locB = formatLocation(r.b.city, r.b.state, "");

            card.innerHTML = `
                <div class="dupe-card-header">
                    <span class="dupe-card-score" style="color:${color}">${pct}% ${label}</span>
                    <span class="dupe-card-method">${r.method || ""}</span>
                </div>
                <div class="dupe-card-pair">
                    <div class="dupe-card-side" onclick="openDetail(${r.a.id})">
                        ${sourceBadge(r.a.source)}
                        <div class="dupe-card-date">${r.a.date || "Unknown"}</div>
                        <div class="dupe-card-loc">${escapeHtml(locA)}</div>
                        ${r.a.shape ? `<span class="shape-tag">${escapeHtml(r.a.shape)}</span>` : ""}
                        <div class="dupe-card-desc">${escapeHtml(r.a.desc || "")}</div>
                    </div>
                    <div class="dupe-card-vs">vs</div>
                    <div class="dupe-card-side" onclick="openDetail(${r.b.id})">
                        ${sourceBadge(r.b.source)}
                        <div class="dupe-card-date">${r.b.date || "Unknown"}</div>
                        <div class="dupe-card-loc">${escapeHtml(locB)}</div>
                        ${r.b.shape ? `<span class="shape-tag">${escapeHtml(r.b.shape)}</span>` : ""}
                        <div class="dupe-card-desc">${escapeHtml(r.b.desc || "")}</div>
                    </div>
                </div>
            `;
            resultsEl.appendChild(card);
        });

        const hasMore = (state.dupesPage + 1) < data.pages;
        moreBtn.style.display = hasMore ? "block" : "none";
    } catch (err) {
        info.textContent = "Couldn't load duplicate pairs — try again. (" + (err.message || err) + ")";
        console.error(err);
    } finally {
        restoreApplyBtn();
    }
}

// =========================================================================
// Detail Modal
// =========================================================================
// =========================================================================
// Insights (Sentiment & Emotion)
// =========================================================================
async function loadInsights() {
    const statusEl = document.getElementById("insights-status");
    statusEl.textContent = "Loading how witnesses felt...";

    const params = getFilterParams();
    const qs = params.toString();

    try {
        const [overview, timeline, bySource, byShape] = await Promise.all([
            fetchJSON(`/api/sentiment/overview?${qs}`),
            fetchJSON(`/api/sentiment/timeline?${qs}`),
            fetchJSON(`/api/sentiment/by-source?${qs}`),
            fetchJSON(`/api/sentiment/by-shape?${qs}`),
        ]);

        if (!overview.total_analyzed || overview.total_analyzed === 0) {
            document.getElementById("insights-grid").innerHTML =
                '<div class="insights-empty" style="grid-column:1/-1">' +
                'No sentiment scores match these filters.<br>' +
                'Sentiment is only computed for sightings with descriptions — try broadening your filters or picking a different date range.</div>';
            statusEl.textContent = "No data";
            return;
        }

        // Restore grid if it was showing empty message
        const grid = document.getElementById("insights-grid");
        if (grid.querySelector(".insights-empty")) {
            grid.innerHTML = `
                <div class="insight-card"><h3>Emotion Distribution</h3><div class="insight-chart-wrap"><canvas id="emotion-radar-chart"></canvas></div></div>
                <div class="insight-card"><h3>Sentiment Over Time</h3><div class="insight-chart-wrap"><canvas id="sentiment-timeline-chart"></canvas></div></div>
                <div class="insight-card"><h3>Emotions by Source</h3><div class="insight-chart-wrap"><canvas id="emotion-source-chart"></canvas></div></div>
                <div class="insight-card"><h3>Emotions by Shape (Top 10)</h3><div class="insight-chart-wrap"><canvas id="emotion-shape-chart"></canvas></div></div>
            `;
        }

        statusEl.textContent = `${overview.total_analyzed.toLocaleString()} sightings analyzed | Avg sentiment: ${overview.avg_compound.toFixed(3)}`;

        renderEmotionRadar(overview);
        renderSentimentTimeline(timeline);
        renderEmotionBySource(bySource);
        renderEmotionByShape(byShape);
    } catch (err) {
        statusEl.textContent = "Couldn't load insights — try again or change filters.";
        console.error("loadInsights error:", err);
    }
}

function renderEmotionRadar(overview) {
    if (state.insightsCharts.radar) state.insightsCharts.radar.destroy();

    const values = EMOTION_NAMES.map(e => overview[e] || 0);
    const total = values.reduce((a, b) => a + b, 1);
    const normalized = values.map(v => v / total);

    const ctx = document.getElementById("emotion-radar-chart").getContext("2d");
    state.insightsCharts.radar = new Chart(ctx, {
        type: "radar",
        data: {
            labels: EMOTION_NAMES.map(e => e.charAt(0).toUpperCase() + e.slice(1)),
            datasets: [{
                label: "Emotion Distribution",
                data: normalized,
                backgroundColor: "rgba(88, 166, 255, 0.2)",
                borderColor: "rgba(88, 166, 255, 0.8)",
                borderWidth: 2,
                pointBackgroundColor: EMOTION_NAMES.map(e => EMOTION_COLORS[e].border),
                pointBorderColor: "#fff",
                pointRadius: 5,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                r: {
                    beginAtZero: true,
                    grid: { color: "rgba(48, 54, 61, 0.6)" },
                    angleLines: { color: "rgba(48, 54, 61, 0.6)" },
                    pointLabels: { color: "#e6edf3", font: { size: 12 } },
                    ticks: { display: false },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const raw = values[ctx.dataIndex];
                            const pct = (normalized[ctx.dataIndex] * 100).toFixed(1);
                            return `${raw.toLocaleString()} words (${pct}%)`;
                        }
                    }
                }
            },
        },
    });
}

function renderSentimentTimeline(timeline) {
    if (state.insightsCharts.timeline) state.insightsCharts.timeline.destroy();

    const data = timeline.data;
    const years = data.map(d => d.year);
    const compounds = data.map(d => d.avg_compound);
    const counts = data.map(d => d.count);

    const ctx = document.getElementById("sentiment-timeline-chart").getContext("2d");
    state.insightsCharts.timeline = new Chart(ctx, {
        type: "line",
        data: {
            labels: years,
            datasets: [
                {
                    label: "Avg Sentiment (VADER)",
                    data: compounds,
                    borderColor: "#58a6ff",
                    backgroundColor: "rgba(88, 166, 255, 0.1)",
                    fill: true,
                    tension: 0.3,
                    pointRadius: 1,
                    yAxisID: "y",
                },
                {
                    label: "Records Analyzed",
                    data: counts,
                    borderColor: "rgba(139, 148, 158, 0.5)",
                    backgroundColor: "rgba(139, 148, 158, 0.1)",
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0,
                    borderDash: [4, 4],
                    yAxisID: "y1",
                }
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { position: "top", labels: { color: "#e6edf3" } },
            },
            scales: {
                x: { ticks: { color: "#8b949e", maxTicksLimit: 20 } },
                y: {
                    title: { display: true, text: "VADER Compound", color: "#8b949e" },
                    min: -1, max: 1,
                    ticks: { color: "#8b949e" },
                    grid: { color: "rgba(48, 54, 61, 0.4)" },
                },
                y1: {
                    position: "right",
                    title: { display: true, text: "Record Count", color: "#8b949e" },
                    ticks: { color: "#8b949e" },
                    grid: { display: false },
                },
            },
        },
    });
}

function renderEmotionBySource(bySource) {
    if (state.insightsCharts.source) state.insightsCharts.source.destroy();

    const sources = bySource.data.map(d => d.source_name);

    const datasets = EMOTION_NAMES.map(emo => {
        const c = EMOTION_COLORS[emo];
        return {
            label: emo.charAt(0).toUpperCase() + emo.slice(1),
            data: bySource.data.map(d => {
                const total = EMOTION_NAMES.reduce((sum, e) => sum + (d[e] || 0), 0);
                return total > 0 ? (d[emo] || 0) / total : 0;
            }),
            backgroundColor: c.bg,
            borderColor: c.border,
            borderWidth: 1,
        };
    });

    const ctx = document.getElementById("emotion-source-chart").getContext("2d");
    state.insightsCharts.source = new Chart(ctx, {
        type: "bar",
        data: { labels: sources, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "top", labels: { color: "#e6edf3", boxWidth: 12 } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.dataset.label}: ${(ctx.raw * 100).toFixed(1)}%`
                    }
                }
            },
            scales: {
                x: { ticks: { color: "#8b949e" }, stacked: false },
                y: {
                    beginAtZero: true,
                    ticks: { color: "#8b949e", callback: (v) => (v * 100) + "%" },
                    grid: { color: "rgba(48, 54, 61, 0.4)" },
                    stacked: false,
                },
            },
        },
    });
}

function renderEmotionByShape(byShape) {
    if (state.insightsCharts.shape) state.insightsCharts.shape.destroy();

    const shapes = byShape.data.map(d => d.shape);

    const datasets = EMOTION_NAMES.map(emo => {
        const c = EMOTION_COLORS[emo];
        return {
            label: emo.charAt(0).toUpperCase() + emo.slice(1),
            data: byShape.data.map(d => {
                const total = EMOTION_NAMES.reduce((sum, e) => sum + (d[e] || 0), 0);
                return total > 0 ? (d[emo] || 0) / total : 0;
            }),
            backgroundColor: c.bg,
            borderColor: c.border,
            borderWidth: 1,
        };
    });

    const ctx = document.getElementById("emotion-shape-chart").getContext("2d");
    state.insightsCharts.shape = new Chart(ctx, {
        type: "bar",
        data: { labels: shapes, datasets },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.dataset.label}: ${(ctx.raw * 100).toFixed(1)}%`
                    }
                }
            },
            scales: {
                x: {
                    stacked: true,
                    ticks: { color: "#8b949e", callback: (v) => (v * 100) + "%" },
                    grid: { color: "rgba(48, 54, 61, 0.4)" },
                },
                y: {
                    stacked: true,
                    ticks: { color: "#8b949e" },
                },
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

        // Description
        if (r.description || r.summary) {
            html += `<div class="detail-section detail-full-width"><h3>Description</h3>`;
            if (r.summary) html += `<div class="detail-row"><strong>${escapeHtml(r.summary)}</strong></div>`;
            if (r.description) html += `<div class="detail-desc">${escapeHtml(r.description)}</div>`;
            html += `</div>`;
        }

        // Resolution / Explanation
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

        // Raw JSON toggle
        if (r.raw_json) {
            html += `<div class="detail-section detail-full-width">
                <h3><a href="#" onclick="document.getElementById('raw-json-block').style.display = document.getElementById('raw-json-block').style.display === 'none' ? 'block' : 'none'; return false;">Raw JSON (toggle)</a></h3>
                <pre id="raw-json-block" style="display:none" class="raw-json">${escapeHtml(JSON.stringify(r.raw_json, null, 2))}</pre>
            </div>`;
        }

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
                    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                        maxZoom: 18,
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
// Sprint 3: Filter bar polish — auto-apply, is-dirty, advanced drawer,
// mobile collapse, active filter counts
// =========================================================================

// Selects that should auto-apply with a debounce. Text inputs (the
// date range) keep the explicit Apply button because you don't want
// fire-on-keystroke there.
const AUTO_APPLY_SELECT_IDS = [
    "filter-shape",
    "filter-collection",
    "filter-source",
    "filter-country",
    "filter-state",
    "filter-hynek",
    "filter-vallee",
];

// Text inputs that mark the Apply button "dirty" on input. User must
// still click Apply to commit these since typing a date char-by-char
// would thrash queries.
const DIRTY_INPUT_IDS = [
    "filter-date-from",
    "filter-date-to",
];

// Which filters live in the advanced drawer — used for the count badge.
const ADVANCED_FILTER_IDS = [
    "filter-collection",
    "filter-hynek",
    "filter-vallee",
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

    // ----- "More filters" drawer toggle + active-count badge -----
    const moreBtn = document.getElementById("btn-more-filters");
    const drawer = document.getElementById("filters-advanced");
    const moreCount = document.getElementById("more-filter-count");

    function updateMoreCount() {
        if (!moreCount) return;
        const n = ADVANCED_FILTER_IDS.reduce((count, id) => {
            const el = document.getElementById(id);
            return count + (el && el.value ? 1 : 0);
        }, 0);
        if (n > 0) {
            moreCount.textContent = String(n);
            moreCount.hidden = false;
        } else {
            moreCount.hidden = true;
        }
    }

    if (moreBtn && drawer) {
        moreBtn.addEventListener("click", () => {
            const willShow = drawer.hidden;
            drawer.hidden = !willShow;
            moreBtn.setAttribute("aria-expanded", String(willShow));
        });
        // Auto-open the drawer if any advanced filter is active on page load
        const hasActive = ADVANCED_FILTER_IDS.some(id => {
            const el = document.getElementById(id);
            return el && el.value;
        });
        if (hasActive) {
            drawer.hidden = false;
            moreBtn.setAttribute("aria-expanded", "true");
        }
        // Keep the count fresh on every change
        ADVANCED_FILTER_IDS.forEach(id => {
            const el = document.getElementById(id);
            el?.addEventListener("change", updateMoreCount);
        });
        updateMoreCount();
    }

    // ----- Mobile filter bar toggle + active-count badge -----
    const mobileBtn = document.getElementById("btn-mobile-filters");
    const bar = document.getElementById("filters-bar");
    const mobileCount = document.getElementById("mobile-filter-count");

    function updateMobileCount() {
        if (!mobileCount) return;
        // Count every FILTER_FIELD that has a value, excluding "coords=all"
        const n = FILTER_FIELDS.reduce((count, f) => {
            if (f.key === "coords") return count;
            const el = document.getElementById(f.id);
            return count + (el && el.value ? 1 : 0);
        }, 0);
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
            if (f.key === "coords") return;
            const el = document.getElementById(f.id);
            el?.addEventListener("change", updateMobileCount);
            el?.addEventListener("input", updateMobileCount);
        });
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

function initSearchActions() {
    const csvBtn = document.getElementById("btn-export-csv");
    const jsonBtn = document.getElementById("btn-export-json");
    const copyBtn = document.getElementById("btn-copy-link");

    function downloadExport(format) {
        // Reuse the same filter params the search panel just used
        const params = getFilterParams();
        const q = document.getElementById("search-input")?.value?.trim();
        if (q) params.set("q", q);
        const url = `/api/export.${format}?${params}`;
        // Trigger a download by creating a temporary anchor — using
        // window.location would replace the SPA URL hash.
        const a = document.createElement("a");
        a.href = url;
        a.download = `ufosint-export.${format}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    if (csvBtn)  csvBtn.addEventListener("click", () => downloadExport("csv"));
    if (jsonBtn) jsonBtn.addEventListener("click", () => downloadExport("json"));

    if (copyBtn) {
        copyBtn.addEventListener("click", async () => {
            const url = window.location.origin + window.location.pathname + window.location.hash;
            const original = copyBtn.innerHTML;
            try {
                await navigator.clipboard.writeText(url);
                copyBtn.innerHTML = "✓ Copied";
            } catch (err) {
                // Fallback: execCommand for older browsers / restrictive contexts
                const ta = document.createElement("textarea");
                ta.value = url;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand("copy"); copyBtn.innerHTML = "✓ Copied"; }
                catch (_) { copyBtn.innerHTML = "✗ Copy failed"; }
                document.body.removeChild(ta);
            }
            setTimeout(() => { copyBtn.innerHTML = original; }, 1500);
        });
    }
}


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
            // Stash the most recent search args so the "view all in Search"
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
    _thinkingEl = appendBubble("thinking", '<div class="ai-msg-body"><span class="loading-pulse">Thinking...</span></div>');
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
        const more = result.total > result.results.length ? `<div class="ai-result-more">+${(result.total - result.results.length).toLocaleString()} more — <a href="#" onclick="navigateToSearchFromAI(${JSON.stringify(getLastSearchArgs()).replace(/"/g, '&quot;')}); return false;">view all in Search →</a></div>` : "";
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

// Last search args, captured for the "view all in Search" link
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
window.removeFilter = removeFilter;
window.goToPage     = goToPage;
window.clearFilters = clearFilters;
window.doSearch     = doSearch;

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
