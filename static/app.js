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
    dupesPage: 0,
    dupesTotal: 0,
    insightsCharts: {},  // { radar, timeline, source, shape }
};

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
    document.getElementById("btn-load-more").addEventListener("click", loadMoreSearch);

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

    // Init map
    initMap();
});

// =========================================================================
// Helpers
// =========================================================================
async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) {
        let detail = "";
        try { detail = await resp.text(); } catch (_) {}
        throw new Error(`HTTP ${resp.status}: ${detail.substring(0, 200)}`);
    }
    return resp.json();
}

function getFilterParams() {
    const p = new URLSearchParams();
    const df = document.getElementById("filter-date-from").value;
    const dt = document.getElementById("filter-date-to").value;
    const shape = document.getElementById("filter-shape").value;
    const collection = document.getElementById("filter-collection").value;
    const source = document.getElementById("filter-source").value;
    const hynek = document.getElementById("filter-hynek").value;
    const vallee = document.getElementById("filter-vallee").value;
    const coords = document.getElementById("coords-filter").value;

    if (df) p.set("date_from", df);
    if (dt) p.set("date_to", dt);
    if (shape) p.set("shape", shape);
    if (collection) p.set("collection", collection);
    if (source) p.set("source", source);
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
    (data.sources || []).forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = s.name;
        sourceSelect.appendChild(opt);
    });

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
    const total = data.total_sightings.toLocaleString();
    const geo = data.geocoded_locations.toLocaleString();
    const geoOrig = (data.geocoded_original || 0).toLocaleString();
    const geoGN = (data.geocoded_geonames || 0).toLocaleString();
    const dupes = data.duplicate_candidates.toLocaleString();
    badge.textContent = `${total} sightings | ${geo} geocoded (${geoOrig} original + ${geoGN} GeoNames) | ${dupes} duplicate pairs`;
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

    // Hide filters bar on methodology (not applicable)
    const filtersBar = document.getElementById("filters-bar");
    filtersBar.style.display = (tab === "methodology") ? "none" : "flex";

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
    }
}

function applyFilters() {
    if (state.activeTab === "map") {
        if (state.mapMode === "heatmap") loadHeatmap();
        else loadMapMarkers();
    }
    else if (state.activeTab === "timeline") loadTimeline();
    else if (state.activeTab === "search") doSearch();
    else if (state.activeTab === "insights") loadInsights();
}

function clearFilters() {
    document.getElementById("filter-date-from").value = "";
    document.getElementById("filter-date-to").value = "";
    document.getElementById("filter-shape").value = "";
    document.getElementById("filter-collection").value = "";
    document.getElementById("filter-source").value = "";
    document.getElementById("filter-hynek").value = "";
    document.getElementById("filter-vallee").value = "";
    applyFilters();
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

    // Load data on move end, but suppress while a popup is open
    let reloadSuppressed = false;
    state.map.on("moveend", () => {
        if (reloadSuppressed) return;
        if (state.mapMode === "heatmap") loadHeatmap();
        else loadMapMarkers();
    });
    state.map.on("popupopen", () => { reloadSuppressed = true; });
    state.map.on("popupclose", () => {
        reloadSuppressed = false;
        if (state.mapMode === "heatmap") loadHeatmap();
        else loadMapMarkers();
    });

    // Initial load
    loadMapMarkers();
}

async function loadMapMarkers() {
    const status = document.getElementById("map-status");
    status.innerHTML = '<span class="loading-pulse">Loading markers...</span>';

    const bounds = state.map.getBounds();
    const params = getFilterParams();
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west", bounds.getWest().toFixed(4));
    params.set("east", bounds.getEast().toFixed(4));

    try {
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

            const loc = formatLocation(m.city, m.state, m.country);
            marker.bindPopup(`
                <div class="popup">
                    <div class="popup-date">${m.date || "Unknown date"}</div>
                    <div class="popup-loc">${escapeHtml(loc) || "Unknown location"}</div>
                    <div>${sourceBadge(m.source)} ${m.shape ? `<span class="shape-tag">${escapeHtml(m.shape)}</span>` : ""}</div>
                    <a href="#" class="popup-link" onclick="openDetail(${m.id}); return false;">View Details</a>
                </div>
            `);

            return marker;
        });

        state.markerLayer.addLayers(markers);
        updateMapStatus(data.count, data.total_in_view, "markers");
    } catch (err) {
        document.getElementById("map-status").textContent = "Error loading markers";
        document.getElementById("btn-load-all").style.display = "none";
        console.error(err);
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
    const totalText = btn.textContent.replace("Load All ", "").replace(/,/g, "");
    const total = parseInt(totalText) || 100000;

    // Show warning for very large loads
    if (total > 30000 && state.mapMode === "clusters") {
        if (!confirm(`Loading ${total.toLocaleString()} markers may take a moment. Continue?`)) return;
    }

    btn.style.display = "none";
    status.textContent = `Loading all ${total.toLocaleString()}...`;

    const bounds = state.map.getBounds();
    const params = getFilterParams();
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west", bounds.getWest().toFixed(4));
    params.set("east", bounds.getEast().toFixed(4));
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
                const loc = formatLocation(m.city, m.state, m.country);
                marker.bindPopup(`
                    <div class="popup">
                        <div class="popup-date">${m.date || "Unknown date"}</div>
                        <div class="popup-loc">${escapeHtml(loc) || "Unknown location"}</div>
                        <div>${sourceBadge(m.source)} ${m.shape ? `<span class="shape-tag">${escapeHtml(m.shape)}</span>` : ""}</div>
                        <a href="#" class="popup-link" onclick="openDetail(${m.id}); return false;">View Details</a>
                    </div>
                `);
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

async function loadHeatmap() {
    const status = document.getElementById("map-status");
    status.innerHTML = '<span class="loading-pulse">Loading heatmap...</span>';

    const bounds = state.map.getBounds();
    const params = getFilterParams();
    params.set("south", bounds.getSouth().toFixed(4));
    params.set("north", bounds.getNorth().toFixed(4));
    params.set("west", bounds.getWest().toFixed(4));
    params.set("east", bounds.getEast().toFixed(4));

    try {
        const data = await fetchJSON(`/api/heatmap?${params}`);
        state.heatLayer.setLatLngs(data.points);
        updateMapStatus(data.count, data.total_in_view, "points");
    } catch (err) {
        document.getElementById("map-status").textContent = "Error loading heatmap";
        document.getElementById("btn-load-all").style.display = "none";
        console.error(err);
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

    titleEl.textContent = year ? `Sightings in ${year} by Month` : "Sightings by Year";
    backBtn.style.display = year ? "inline-block" : "none";

    const data = await fetchJSON(`/api/timeline?${params}`);

    // Build datasets per source
    const periods = Object.keys(data.data).sort();
    const sourceNames = new Set();
    periods.forEach(p => {
        Object.keys(data.data[p]).forEach(s => sourceNames.add(s));
    });

    const datasets = Array.from(sourceNames).map(name => {
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

    // Destroy old chart
    if (state.chart) {
        state.chart.destroy();
    }

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
                            return `Total: ${total.toLocaleString()}`;
                        }
                    }
                }
            },
            scales: {
                x: { stacked: true },
                y: { stacked: true, beginAtZero: true },
            },
            onClick: (evt, elements) => {
                if (elements.length > 0 && data.mode === "yearly") {
                    const idx = elements[0].index;
                    state.timelineYear = periods[idx];
                    loadTimeline();
                }
            },
        },
    });
}

// =========================================================================
// Search
// =========================================================================
async function doSearch() {
    state.searchPage = 0;
    const results = document.getElementById("search-results");
    results.innerHTML = "";
    await executeSearch();
}

async function loadMoreSearch() {
    state.searchPage++;
    await executeSearch(true);
}

async function executeSearch(append = false) {
    const q = document.getElementById("search-input").value.trim();
    const params = getFilterParams();
    if (q) params.set("q", q);
    params.set("page", state.searchPage);

    const info = document.getElementById("search-info");
    const resultsEl = document.getElementById("search-results");
    const loadMoreBtn = document.getElementById("btn-load-more");

    info.innerHTML = '<span class="loading-pulse">Searching...</span>';

    try {
        const data = await fetchJSON(`/api/search?${params}`);
        state.searchTotal = data.total;

        if (!append) {
            resultsEl.innerHTML = "";
        }

        info.textContent = `${data.total.toLocaleString()} results found` +
            (q ? ` for "${q}"` : "") +
            (data.total > 0 ? ` (page ${data.page + 1} of ${data.pages})` : "");

        data.results.forEach(r => {
            const card = document.createElement("div");
            card.className = "result-card";
            card.onclick = () => openDetail(r.id);

            const loc = formatLocation(r.city, r.state, r.country);
            card.innerHTML = `
                <div class="result-header">
                    <span class="result-date">${r.date || "Unknown date"}</span>
                    ${sourceBadge(r.source)}
                    ${r.shape ? `<span class="shape-tag">${escapeHtml(r.shape)}</span>` : ""}
                </div>
                <div class="result-loc">${escapeHtml(loc)}</div>
                <div class="result-desc">${escapeHtml(r.description)}</div>
                <div class="result-meta">
                    ${r.hynek ? `Hynek: ${escapeHtml(r.hynek)}` : ""}
                    ${r.witnesses ? ` | Witnesses: ${r.witnesses}` : ""}
                    ${r.duration ? ` | Duration: ${escapeHtml(r.duration)}` : ""}
                </div>
            `;
            resultsEl.appendChild(card);
        });

        // Show/hide load more
        const hasMore = (state.searchPage + 1) < data.pages;
        loadMoreBtn.style.display = hasMore ? "block" : "none";
    } catch (err) {
        info.textContent = "Error searching";
        console.error(err);
    }
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

    info.innerHTML = '<span class="loading-pulse">Loading duplicate pairs... This may take a moment on large databases.</span>';

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
        info.textContent = "Error loading duplicates: " + (err.message || err);
        console.error(err);
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
    statusEl.textContent = "Loading sentiment data...";

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
                'No sentiment data available for the current filters.<br>' +
                'Sentiment analysis must be run during the ETL pipeline.</div>';
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
        statusEl.textContent = "Error loading insights";
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
async function openDetail(id) {
    const overlay = document.getElementById("modal-overlay");
    const body = document.getElementById("modal-body");
    const title = document.getElementById("modal-title");

    overlay.style.display = "flex";
    body.innerHTML = "<p>Loading...</p>";

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

// Make openDetail globally available (called from popup links)
window.openDetail = openDetail;

function closeModal() {
    document.getElementById("modal-overlay").style.display = "none";
    document.getElementById("modal-body").innerHTML = "";
}
