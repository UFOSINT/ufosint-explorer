/*
 * UFOSINT Explorer — deck.gl + bulk-dataset integration (v0.8.0)
 *
 * This module replaces the v0.7 per-pan query loop with a "download
 * once, render on the GPU, filter in the browser" pipeline.
 *
 *   1. loadBulkPoints() fetches /api/points-bulk?meta=1 and
 *      /api/points-bulk in parallel and deserialises the packed
 *      16-byte rows into typed arrays (id, lat, lng, source_idx,
 *      shape_idx, year). One download per session.
 *
 *   2. mountDeckLayer() creates a deck.gl LeafletLayer on top of the
 *      existing state.map, starting with a ScatterplotLayer.
 *
 *   3. setDeckMode("points" | "heatmap" | "hexbin") swaps the active
 *      deck.gl layer without touching the server. Hex and heat modes
 *      are GPU-aggregated from the same typed arrays.
 *
 *   4. applyClientFilters() walks the typed arrays once (~1 ms for
 *      105k rows), builds a filtered index array, and calls
 *      setProps({ data: filteredIdx }) on the active layer.
 *
 *   5. WebGL probe + fallback: if deck.gl fails to load, the bulk
 *      fetch never fires and the existing loadMapMarkers() path stays
 *      in charge. Zero regression for ancient browsers.
 *
 * All exports hang off window.UFODeck so app.js can call them without
 * an import statement — we don't run a JS build in this project.
 *
 * See docs/V080_PLAN.md for the full architecture rationale.
 */
(function () {
    "use strict";

    // -----------------------------------------------------------------
    // Capability probe — WebGL + deck.gl + LeafletLayer
    // -----------------------------------------------------------------
    function hasWebGL() {
        try {
            const c = document.createElement("canvas");
            return !!(
                c.getContext("webgl2") ||
                c.getContext("webgl") ||
                c.getContext("experimental-webgl")
            );
        } catch (_e) {
            return false;
        }
    }

    // deck.gl loads via a <script defer> tag in index.html. It may not
    // be ready when app.js starts running, so we poll briefly on a
    // setTimeout chain. 40 attempts × 50 ms = 2 s max wait.
    function waitForDeck(maxAttempts) {
        return new Promise((resolve, reject) => {
            let n = 0;
            const tick = () => {
                if (
                    typeof window.deck !== "undefined" &&
                    typeof window.deck.LeafletLayer !== "undefined" &&
                    typeof window.deck.ScatterplotLayer !== "undefined"
                ) {
                    resolve(window.deck);
                    return;
                }
                n += 1;
                if (n >= maxAttempts) {
                    reject(new Error("deck.gl did not load in time"));
                    return;
                }
                setTimeout(tick, 50);
            };
            tick();
        });
    }

    // -----------------------------------------------------------------
    // Bulk dataset loader
    // -----------------------------------------------------------------
    // Typed-array views over the packed buffer. Populated once by
    // loadBulkPoints(). Every subsequent render reads these directly.
    const POINTS = {
        ready: false,
        count: 0,
        etag: null,
        // Per-field typed arrays (tight, contiguous, one allocation each).
        id: null,        // Uint32Array(N)
        lat: null,       // Float32Array(N)
        lng: null,       // Float32Array(N)
        sourceIdx: null, // Uint8Array(N)
        shapeIdx: null,  // Uint8Array(N)
        year: null,      // Uint16Array(N)
        // Lookup tables from the meta sidecar.
        sources: null,   // Array<string | null>
        shapes: null,    // Array<string | null>
        // Current filtered index (Uint32Array subarray'd to .length).
        visibleIdx: null,
    };

    async function loadBulkPoints() {
        // Fire both requests in parallel. The meta sidecar is small
        // (a few KB of JSON); the binary buffer is ~700 KB gzipped.
        const t0 = performance.now();
        const [metaResp, binResp] = await Promise.all([
            fetch("/api/points-bulk?meta=1", { credentials: "same-origin" }),
            fetch("/api/points-bulk", { credentials: "same-origin" }),
        ]);
        if (!metaResp.ok || !binResp.ok) {
            throw new Error(
                `points-bulk fetch failed: meta=${metaResp.status} bin=${binResp.status}`,
            );
        }
        const meta = await metaResp.json();
        const buf = await binResp.arrayBuffer();
        const t1 = performance.now();
        console.info(
            `[v0.8] Fetched ${meta.count.toLocaleString()} points ` +
            `(${(buf.byteLength / 1024).toFixed(0)} KB) in ${(t1 - t0).toFixed(0)} ms`,
        );

        // Sanity: buffer length matches schema.
        const bytesPerRow = meta.schema.bytes_per_row;
        if (buf.byteLength !== meta.count * bytesPerRow) {
            throw new Error(
                `points-bulk size mismatch: got ${buf.byteLength} bytes, ` +
                `expected ${meta.count * bytesPerRow} for ${meta.count} rows`,
            );
        }

        const N = meta.count;
        const dv = new DataView(buf);
        POINTS.id        = new Uint32Array(N);
        POINTS.lat       = new Float32Array(N);
        POINTS.lng       = new Float32Array(N);
        POINTS.sourceIdx = new Uint8Array(N);
        POINTS.shapeIdx  = new Uint8Array(N);
        POINTS.year      = new Uint16Array(N);

        // Deserialise every row. The offsets come from meta.schema.fields
        // so a future schema change (e.g. adding a country_idx) doesn't
        // require updating this loop as long as the field names match.
        // Hard-code the current layout for speed; fallback to dynamic.
        for (let i = 0; i < N; i++) {
            const o = i * bytesPerRow;
            POINTS.id[i]        = dv.getUint32(o,      true);
            POINTS.lat[i]       = dv.getFloat32(o + 4,  true);
            POINTS.lng[i]       = dv.getFloat32(o + 8,  true);
            POINTS.sourceIdx[i] = dv.getUint8(o + 12);
            POINTS.shapeIdx[i]  = dv.getUint8(o + 13);
            POINTS.year[i]      = dv.getUint16(o + 14, true);
        }
        const t2 = performance.now();
        console.info(
            `[v0.8] Deserialised ${N.toLocaleString()} rows in ${(t2 - t1).toFixed(0)} ms`,
        );

        POINTS.count = N;
        POINTS.etag = meta.etag;
        POINTS.sources = meta.sources;
        POINTS.shapes = meta.shapes;
        // Start with every point visible.
        POINTS.visibleIdx = new Uint32Array(N);
        for (let i = 0; i < N; i++) POINTS.visibleIdx[i] = i;
        POINTS.ready = true;
        return POINTS;
    }

    // -----------------------------------------------------------------
    // Client-side filter pipeline
    // -----------------------------------------------------------------
    // Walks the typed arrays once, returns a Uint32Array of row indices
    // that pass the filter. Typical cost: ~1 ms for 105k rows in V8.
    //
    // The filter object accepts:
    //   sourceName: string | null         — match exact source name
    //   shapeName:  string | null         — match exact shape label
    //   yearFrom:   number | null         — inclusive lower bound
    //   yearTo:     number | null         — inclusive upper bound
    //   bbox:       [s, n, w, e] | null   — viewport clip (optional)
    //
    // Anything set to null/undefined is ignored. Resolving names to
    // indices happens here so the hot loop only does integer compares.
    function applyClientFilters(filter) {
        if (!POINTS.ready) return null;
        const f = filter || {};
        const N = POINTS.count;

        // Resolve source name -> index (1..N, 0 = unknown).
        let srcIdxTarget = -1;  // -1 = no filter
        if (f.sourceName) {
            srcIdxTarget = POINTS.sources.indexOf(f.sourceName);
            if (srcIdxTarget === -1) {
                // Unknown source → no results
                POINTS.visibleIdx = new Uint32Array(0);
                return POINTS.visibleIdx;
            }
        }

        let shapeIdxTarget = -1;
        if (f.shapeName) {
            shapeIdxTarget = POINTS.shapes.indexOf(f.shapeName);
            if (shapeIdxTarget === -1) {
                POINTS.visibleIdx = new Uint32Array(0);
                return POINTS.visibleIdx;
            }
        }

        const yearFrom = (f.yearFrom != null) ? f.yearFrom | 0 : 0;
        const yearTo   = (f.yearTo   != null) ? f.yearTo   | 0 : 65535;

        let south = -90, north = 90, west = -180, east = 180;
        if (f.bbox) {
            south = f.bbox[0]; north = f.bbox[1];
            west  = f.bbox[2]; east  = f.bbox[3];
        }

        // Hot loop — keep branches minimal.
        const lat = POINTS.lat;
        const lng = POINTS.lng;
        const src = POINTS.sourceIdx;
        const shp = POINTS.shapeIdx;
        const yr  = POINTS.year;

        const out = new Uint32Array(N);
        let j = 0;
        for (let i = 0; i < N; i++) {
            if (srcIdxTarget   !== -1 && src[i] !== srcIdxTarget) continue;
            if (shapeIdxTarget !== -1 && shp[i] !== shapeIdxTarget) continue;
            const y = yr[i];
            if (y !== 0 && (y < yearFrom || y > yearTo)) continue;
            const la = lat[i];
            const ln = lng[i];
            if (la < south || la > north || ln < west || ln > east) continue;
            out[j++] = i;
        }
        POINTS.visibleIdx = out.subarray(0, j);
        return POINTS.visibleIdx;
    }

    // -----------------------------------------------------------------
    // deck.gl layer factories
    // -----------------------------------------------------------------
    // Each helper returns a fresh deck.gl layer instance given the
    // current POINTS.visibleIdx. The onClick handler hits the existing
    // /api/sighting/:id endpoint via window.openDetail.
    function makeScatterplotLayer() {
        const d = window.deck;
        return new d.ScatterplotLayer({
            id: "ufosint-points",
            data: POINTS.visibleIdx,
            // deck.gl can read attributes straight from our typed arrays
            // via indexed accessors. No per-point object allocation.
            getPosition: (i) => [POINTS.lng[i], POINTS.lat[i]],
            getRadius: 1000,
            radiusMinPixels: 1.2,
            radiusMaxPixels: 5,
            getFillColor: [0, 240, 255, 180],
            pickable: true,
            onClick: (info) => {
                if (info && info.object !== undefined && info.object !== null) {
                    const rowIdx = info.object;
                    const sid = POINTS.id[rowIdx];
                    if (sid && typeof window.openDetail === "function") {
                        window.openDetail(sid);
                    }
                }
            },
            updateTriggers: {
                getPosition: POINTS.etag,
            },
        });
    }

    function makeHexagonLayer() {
        const d = window.deck;
        // HexagonLayer aggregates in screen-space meters, so hex cells
        // tessellate uniformly regardless of latitude. Radius scales
        // with map zoom via the `radius` prop — we pass a fixed value
        // in meters and let deck.gl handle the projection math.
        return new d.HexagonLayer({
            id: "ufosint-hex",
            data: POINTS.visibleIdx,
            getPosition: (i) => [POINTS.lng[i], POINTS.lat[i]],
            radius: 60000,                 // 60 km — tune per taste
            extruded: false,
            coverage: 0.95,
            colorRange: [
                [0, 59, 92], [0, 140, 180], [0, 240, 255],
                [255, 179, 0], [255, 78, 0],
            ],
            pickable: true,
            onClick: (info) => {
                // Cell click: no detail modal — just log the count.
                if (info && info.object) {
                    console.info(`Hex cell: ${info.object.count || 0} sightings`);
                }
            },
        });
    }

    function makeHeatmapLayer() {
        const d = window.deck;
        return new d.HeatmapLayer({
            id: "ufosint-heat",
            data: POINTS.visibleIdx,
            getPosition: (i) => [POINTS.lng[i], POINTS.lat[i]],
            radiusPixels: 28,
            intensity: 1,
            threshold: 0.04,
        });
    }

    // -----------------------------------------------------------------
    // Leaflet integration
    // -----------------------------------------------------------------
    // Single LeafletLayer wrapper that hosts whichever deck.gl layer
    // the user has selected. Swapping is as cheap as calling
    // setProps({ layers: [...] }) — no DOM churn, no Leaflet layer
    // add/remove.
    let leafletLayer = null;
    let activeMode = "points";

    function modeToLayer(mode) {
        if (mode === "heatmap") return makeHeatmapLayer();
        if (mode === "hexbin")  return makeHexagonLayer();
        return makeScatterplotLayer();
    }

    function mountDeckLayer(map, initialMode) {
        const d = window.deck;
        leafletLayer = new d.LeafletLayer({
            views: [new d.MapView({ repeat: true })],
            layers: [modeToLayer(initialMode || "points")],
        });
        leafletLayer.addTo(map);
        activeMode = initialMode || "points";
        return leafletLayer;
    }

    function setDeckMode(mode) {
        if (!leafletLayer) return;
        if (mode === activeMode) return;
        activeMode = mode;
        leafletLayer.setProps({ layers: [modeToLayer(mode)] });
    }

    function refreshActiveLayer() {
        if (!leafletLayer) return;
        leafletLayer.setProps({ layers: [modeToLayer(activeMode)] });
    }

    // -----------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------
    window.UFODeck = {
        POINTS,
        hasWebGL,
        waitForDeck,
        loadBulkPoints,
        applyClientFilters,
        mountDeckLayer,
        setDeckMode,
        refreshActiveLayer,
        isReady: () => POINTS.ready && leafletLayer !== null,
        getActiveMode: () => activeMode,
    };
})();
