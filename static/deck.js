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

    // deck.gl + deck.gl-leaflet load via <script defer> tags in
    // index.html. The main deck.gl UMD exposes window.deck; the
    // community deck.gl-leaflet UMD exposes window.DeckGlLeaflet
    // containing the LeafletLayer constructor. We poll for both
    // globals to be defined before resolving. 40 × 50 ms = 2 s
    // max wait.
    //
    // v0.8.2-hotfix: the earlier code looked for
    // window.deck.LeafletLayer, which was correct if @deck.gl/leaflet
    // existed under that scope — but it doesn't and never has. The
    // community deck.gl-leaflet package attaches to a different
    // global, so the old check always timed out, the catch in
    // bootDeckGL() fired, and every browser silently fell back to
    // the legacy /api/map polling path. Fixed by checking the
    // actual global the UMD exposes.
    function waitForDeck(maxAttempts) {
        return new Promise((resolve, reject) => {
            let n = 0;
            const tick = () => {
                const deckReady = (
                    typeof window.deck !== "undefined" &&
                    typeof window.deck.ScatterplotLayer !== "undefined" &&
                    typeof window.deck.HexagonLayer !== "undefined" &&
                    typeof window.deck.HeatmapLayer !== "undefined"
                );
                const leafletReady = (
                    typeof window.DeckGlLeaflet !== "undefined" &&
                    typeof window.DeckGlLeaflet.LeafletLayer !== "undefined"
                );
                if (deckReady && leafletReady) {
                    resolve({
                        deck: window.deck,
                        DeckGlLeaflet: window.DeckGlLeaflet,
                    });
                    return;
                }
                n += 1;
                if (n >= maxAttempts) {
                    const missing = [];
                    if (!deckReady) missing.push("deck.gl core (window.deck.ScatterplotLayer/HexagonLayer/HeatmapLayer)");
                    if (!leafletReady) missing.push("deck.gl-leaflet bridge (window.DeckGlLeaflet.LeafletLayer)");
                    reject(new Error("deck.gl did not load in time: missing " + missing.join(", ")));
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
    //
    // v0.8.2: expanded from 6 fields (16 bytes) to 15 fields (28 bytes)
    // to carry the science-team derived fields. Score fields (quality,
    // hoax, richness) use 255 as a sentinel meaning "unknown" — the
    // filter loop treats 255 as failing any threshold test, which is
    // exactly the right semantic.
    //
    // v0.8.5 (v0.8.3b data layer): expanded to 17 fields in 32 bytes
    // to carry the science-team movement classification
    // (has_movement_mentioned + movement_categories bitmask). Row is
    // still 4-byte aligned so V8's optimised Uint32Array reads on
    // `id` land on aligned offsets.
    const SCORE_UNKNOWN = 255;
    const FLAG_HAS_DESC     = 0x01;
    const FLAG_HAS_MEDIA    = 0x02;
    const FLAG_HAS_MOVEMENT = 0x04;  // v0.8.5

    const POINTS = {
        ready: false,
        count: 0,
        etag: null,
        // Per-field typed arrays (tight, contiguous, one allocation each).
        id: null,            // Uint32Array(N)
        lat: null,           // Float32Array(N)
        lng: null,           // Float32Array(N)
        dateDays: null,      // Uint32Array(N) — days since 1900-01-01, 0 = unknown
        sourceIdx: null,     // Uint8Array(N)
        shapeIdx: null,      // Uint8Array(N)
        qualityScore: null,  // Uint8Array(N) — 0-100, 255 = unknown
        hoaxScore: null,     // Uint8Array(N) — 0-100, 255 = unknown
        richnessScore: null, // Uint8Array(N) — 0-100, 255 = unknown
        colorIdx: null,      // Uint8Array(N)
        emotionIdx: null,    // Uint8Array(N)
        flags: null,         // Uint8Array(N) — bit0=desc, bit1=media, bit2=movement
        numWitnesses: null,  // Uint8Array(N)
        durationLog2: null,  // Uint16Array(N) — log2(sec+1), 0 = unknown
        // v0.8.5 — movement_flags is a 10-bit bitmask packed into a
        // uint16. See POINTS.movements for the bit→name lookup.
        movementFlags: null, // Uint16Array(N)
        // Lookup tables from the meta sidecar.
        sources: null,       // Array<string | null>
        shapes: null,        // Array<string | null>
        colors: null,        // Array<string | null> (v0.8.2)
        emotions: null,      // Array<string | null> (v0.8.2)
        movements: null,     // Array<string> of 10 category names in bit order (v0.8.5)
        // Coverage + schema metadata for the UI.
        coverage: null,      // { quality_score: 0, hoax_score: 0, ... }
        columnsPresent: null,// { quality_score: false, ... }
        shapeSource: null,   // "standardized" | "raw"
        // Current filtered index (Uint32Array subarray'd to .length).
        visibleIdx: null,
    };

    async function loadBulkPoints() {
        // Fire both requests in parallel. The meta sidecar is small
        // (a few KB of JSON); the binary buffer is ~4 MB gzipped in
        // v0.8.2 (up from 2.85 MB in v0.8.0 with the new fields).
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
            `[v0.8.2] Fetched ${meta.count.toLocaleString()} points ` +
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
        // v0.8.5 expects exactly 32-byte rows (v0.8.3b data layer).
        // If the server sends a different size the schema changed on
        // us and we should bail loudly rather than silently corrupt
        // every marker.
        if (bytesPerRow !== 32) {
            throw new Error(
                `unexpected row size ${bytesPerRow} — expected 32 (v0.8.5). ` +
                `Deploy probably has stale server code.`,
            );
        }

        const N = meta.count;
        const dv = new DataView(buf);
        POINTS.id            = new Uint32Array(N);
        POINTS.lat           = new Float32Array(N);
        POINTS.lng           = new Float32Array(N);
        POINTS.dateDays      = new Uint32Array(N);
        POINTS.sourceIdx     = new Uint8Array(N);
        POINTS.shapeIdx      = new Uint8Array(N);
        POINTS.qualityScore  = new Uint8Array(N);
        POINTS.hoaxScore     = new Uint8Array(N);
        POINTS.richnessScore = new Uint8Array(N);
        POINTS.colorIdx      = new Uint8Array(N);
        POINTS.emotionIdx    = new Uint8Array(N);
        POINTS.flags         = new Uint8Array(N);
        POINTS.numWitnesses  = new Uint8Array(N);
        POINTS.durationLog2  = new Uint16Array(N);
        POINTS.movementFlags = new Uint16Array(N);  // v0.8.5

        // Hot deserialisation loop. Hard-coded offsets for speed —
        // anything dynamic would cost 5-10 ms per 100k rows. The
        // schema version assertion above guards against silent
        // layout drift.
        for (let i = 0; i < N; i++) {
            const o = i * 32;
            POINTS.id[i]            = dv.getUint32(o,      true);
            POINTS.lat[i]           = dv.getFloat32(o + 4,  true);
            POINTS.lng[i]           = dv.getFloat32(o + 8,  true);
            POINTS.dateDays[i]      = dv.getUint32(o + 12, true);
            POINTS.sourceIdx[i]     = dv.getUint8(o + 16);
            POINTS.shapeIdx[i]      = dv.getUint8(o + 17);
            POINTS.qualityScore[i]  = dv.getUint8(o + 18);
            POINTS.hoaxScore[i]     = dv.getUint8(o + 19);
            POINTS.richnessScore[i] = dv.getUint8(o + 20);
            POINTS.colorIdx[i]      = dv.getUint8(o + 21);
            POINTS.emotionIdx[i]    = dv.getUint8(o + 22);
            POINTS.flags[i]         = dv.getUint8(o + 23);
            POINTS.numWitnesses[i]  = dv.getUint8(o + 24);
            // byte 25 is _reserved, skip
            POINTS.durationLog2[i]  = dv.getUint16(o + 26, true);
            // v0.8.5 — movement_flags bitmask at offset 28. Bytes
            // 30-31 are _reserved2 and skipped.
            POINTS.movementFlags[i] = dv.getUint16(o + 28, true);
        }
        const t2 = performance.now();
        console.info(
            `[v0.8.5] Deserialised ${N.toLocaleString()} rows in ${(t2 - t1).toFixed(0)} ms`,
        );

        POINTS.count = N;
        POINTS.etag = meta.etag;
        POINTS.sources   = meta.sources || [null];
        POINTS.shapes    = meta.shapes || [null];
        POINTS.colors    = meta.colors || [null];
        POINTS.emotions  = meta.emotions || [null];
        POINTS.movements = meta.movements || [];  // v0.8.5
        POINTS.coverage  = meta.coverage || {};
        POINTS.columnsPresent = meta.columns_present || {};
        POINTS.shapeSource = meta.shape_source || "raw";
        // Start with every point visible.
        POINTS.visibleIdx = new Uint32Array(N);
        for (let i = 0; i < N; i++) POINTS.visibleIdx[i] = i;
        POINTS.ready = true;
        return POINTS;
    }

    // -----------------------------------------------------------------
    // Client-side filter pipeline
    // -----------------------------------------------------------------
    // The filter state lives in two pieces of module state:
    //
    //   _activeFilter — the UI filter (source / shape / year range /
    //     bbox). Mutated by applyClientFilters() on every user
    //     interaction (dropdown change, "Apply Filters" click).
    //
    //   _timeState   — the timeline-playback window, orthogonal to the
    //     UI filter. Driven by TimeBrush via setTimeWindow() at 60 fps
    //     during playback. Off by default; the UI year range (from
    //     #filter-date-from/to) is used instead when disabled.
    //
    // Every change to either piece of state calls _rebuildVisible(),
    // which walks POINTS in one tight loop against the intersection
    // of both filter layers. The hot loop writes its output into
    // _visibleScratch (reused every frame, never reallocated) and
    // POINTS.visibleIdx becomes a subarray view of the first j slots.
    // That avoids allocating a fresh ~1.6 MB Uint32Array per frame
    // during playback.
    //
    // Year bookkeeping: _yearStats caches min/max (across non-zero
    // rows) and the full-range histogram so the TimeBrush can render
    // without fetching /api/timeline. Both are computed lazily on
    // first access and cached for the lifetime of the bulk buffer.

    let _activeFilter = {};
    const _timeState = {
        enabled: false,      // false → use year range from _activeFilter
        // Time window is expressed in days-since-1900. Year filtering is
        // done by converting year to day-range during the rebuild so
        // both modes share the same hot path.
        dayFrom: 0,
        dayTo: 0xFFFFFFFF,
        cumulative: false,   // cumulative pins dayFrom to dataset min
    };
    const _yearStats = { min: null, max: null, histogram: null };
    const _dayStats = { min: null, max: null };
    let _visibleScratch = null;

    // Convert year integer → days-since-1900 (Jan 1 of that year).
    // Matches the server's _epoch_days_1900 helper.
    function _yearToDays(year) {
        if (year == null || year <= 0) return 0;
        return Math.floor((Date.UTC(year, 0, 1) - Date.UTC(1900, 0, 1)) / 86400000);
    }

    // Year range resolved from the actual data (non-zero rows only).
    // Lazily computed and cached; the bulk buffer never changes shape
    // so one walk is enough. Walks POINTS.dateDays (v0.8.2) and derives
    // year bounds for the histogram.
    function _ensureYearStats() {
        if (_yearStats.min != null) return;
        if (!POINTS.ready) return;
        let dmn = 0xFFFFFFFF, dmx = 0;
        const dd = POINTS.dateDays;
        const N = POINTS.count;
        for (let i = 0; i < N; i++) {
            const d = dd[i];
            if (d === 0) continue;
            if (d < dmn) dmn = d;
            if (d > dmx) dmx = d;
        }
        if (dmx === 0) {
            // No rows with dates at all.
            _yearStats.min = null;
            _yearStats.max = null;
            _dayStats.min = null;
            _dayStats.max = null;
            return;
        }
        _dayStats.min = dmn;
        _dayStats.max = dmx;
        // Convert day bounds back to year bounds. Days / 365.25 is
        // approximate but since we only use this for the histogram,
        // being off by at most a day on the edges is fine.
        const minDate = new Date(Date.UTC(1900, 0, 1) + dmn * 86400000);
        const maxDate = new Date(Date.UTC(1900, 0, 1) + dmx * 86400000);
        _yearStats.min = minDate.getUTCFullYear();
        _yearStats.max = maxDate.getUTCFullYear();
    }

    // Reusable backing buffer for the filtered index. Sized to
    // POINTS.count on first use; never reallocated because the bulk
    // buffer size is fixed for the session.
    function _ensureScratch() {
        if (!_visibleScratch || _visibleScratch.length !== POINTS.count) {
            _visibleScratch = new Uint32Array(POINTS.count);
        }
        return _visibleScratch;
    }

    // Walk POINTS once, intersect _activeFilter with _timeState,
    // update POINTS.visibleIdx. Used by both applyClientFilters()
    // (UI changes) and setTimeWindow() (playback frames).
    //
    // Filter object fields (v0.8.2 + v0.8.5):
    //   sourceName       — exact match in POINTS.sources
    //   shapeName        — exact match in POINTS.shapes
    //   colorName        — exact match in POINTS.colors
    //   emotionName      — exact match in POINTS.emotions
    //   qualityMin       — quality_score >= threshold (255-sentinel fails)
    //   hoaxMax          — hoax_score   <= threshold (255-sentinel fails)
    //   richnessMin      — richness_score >= threshold (255-sentinel fails)
    //   hasDescription   — true: flag bit 0 set; false: flag bit 0 clear
    //   hasMedia         — true: flag bit 1 set; false: flag bit 1 clear
    //   hasMovement      — v0.8.5: true: flag bit 2 set; false: clear
    //   yearFrom/yearTo  — legacy year range (converted to day range)
    //   bbox             — [s, n, w, e] viewport clip
    function _rebuildVisible() {
        if (!POINTS.ready) return null;
        const f = _activeFilter || {};
        const N = POINTS.count;

        // Resolve string names to typed-array indices once per rebuild.
        // A target of -1 means "no filter"; a target of -2 means "name
        // not in the lookup → every row fails → empty result".
        const unknownName = _ensureScratch().subarray(0, 0);

        let srcIdxTarget = -1;
        if (f.sourceName) {
            srcIdxTarget = POINTS.sources.indexOf(f.sourceName);
            if (srcIdxTarget === -1) { POINTS.visibleIdx = unknownName; return unknownName; }
        }
        let shapeIdxTarget = -1;
        if (f.shapeName) {
            shapeIdxTarget = POINTS.shapes.indexOf(f.shapeName);
            if (shapeIdxTarget === -1) { POINTS.visibleIdx = unknownName; return unknownName; }
        }
        let colorIdxTarget = -1;
        if (f.colorName) {
            colorIdxTarget = POINTS.colors.indexOf(f.colorName);
            if (colorIdxTarget === -1) { POINTS.visibleIdx = unknownName; return unknownName; }
        }
        let emotionIdxTarget = -1;
        if (f.emotionName) {
            emotionIdxTarget = POINTS.emotions.indexOf(f.emotionName);
            if (emotionIdxTarget === -1) { POINTS.visibleIdx = unknownName; return unknownName; }
        }

        // Scores: thresholds in [0, 100]. -1 = no filter.
        const qMin = (f.qualityMin != null) ? (f.qualityMin | 0) : -1;
        const hMax = (f.hoaxMax    != null) ? (f.hoaxMax    | 0) : -1;
        const rMin = (f.richnessMin != null) ? (f.richnessMin | 0) : -1;

        // Flag bit filters: null = no filter, true = require set,
        // false = require clear.
        const fDesc = (f.hasDescription != null) ? !!f.hasDescription : null;
        const fMedia = (f.hasMedia != null) ? !!f.hasMedia : null;
        // v0.8.5 — has_movement_mentioned is flag bit 2.
        const fMove = (f.hasMovement != null) ? !!f.hasMovement : null;

        // Time window → day range. Timeline playback wins; otherwise
        // fall back to the UI year range (converted to days).
        let dayFrom, dayTo;
        let timeFilterActive = false;
        if (_timeState.enabled) {
            dayFrom = _timeState.dayFrom | 0;
            dayTo   = _timeState.dayTo   | 0;
            timeFilterActive = true;
        } else if (f.yearFrom != null || f.yearTo != null) {
            dayFrom = (f.yearFrom != null) ? _yearToDays(f.yearFrom) : 0;
            // Include the whole of yearTo: end-of-year = next year's day 0 - 1
            const yt = (f.yearTo != null) ? f.yearTo : 9999;
            dayTo = _yearToDays(yt + 1) - 1;
            timeFilterActive = true;
        } else {
            dayFrom = 0;
            dayTo = 0xFFFFFFFF;
        }

        let south = -90, north = 90, west = -180, east = 180;
        if (f.bbox) {
            south = f.bbox[0]; north = f.bbox[1];
            west  = f.bbox[2]; east  = f.bbox[3];
        }

        // Snapshot typed-array references for the hot loop (V8 can
        // optimise the property access across iterations this way).
        const lat = POINTS.lat;
        const lng = POINTS.lng;
        const src = POINTS.sourceIdx;
        const shp = POINTS.shapeIdx;
        const dd  = POINTS.dateDays;
        const qs  = POINTS.qualityScore;
        const hs  = POINTS.hoaxScore;
        const rs  = POINTS.richnessScore;
        const ci  = POINTS.colorIdx;
        const ei  = POINTS.emotionIdx;
        const fl  = POINTS.flags;
        const UNK = SCORE_UNKNOWN;

        const out = _ensureScratch();
        let j = 0;
        for (let i = 0; i < N; i++) {
            if (srcIdxTarget     !== -1 && src[i] !== srcIdxTarget)     continue;
            if (shapeIdxTarget   !== -1 && shp[i] !== shapeIdxTarget)   continue;
            if (colorIdxTarget   !== -1 && ci[i]  !== colorIdxTarget)   continue;
            if (emotionIdxTarget !== -1 && ei[i]  !== emotionIdxTarget) continue;

            // Score filters. 255-sentinel fails any threshold test,
            // which is the right semantic: a row with unknown quality
            // should NOT pass "high quality only".
            if (qMin !== -1) {
                const q = qs[i];
                if (q === UNK || q < qMin) continue;
            }
            if (hMax !== -1) {
                const h = hs[i];
                if (h === UNK || h > hMax) continue;
            }
            if (rMin !== -1) {
                const r = rs[i];
                if (r === UNK || r < rMin) continue;
            }

            // Flag bit filters.
            if (fDesc !== null) {
                const hasDesc = (fl[i] & FLAG_HAS_DESC) !== 0;
                if (hasDesc !== fDesc) continue;
            }
            if (fMedia !== null) {
                const hasMedia = (fl[i] & FLAG_HAS_MEDIA) !== 0;
                if (hasMedia !== fMedia) continue;
            }
            if (fMove !== null) {
                const hasMove = (fl[i] & FLAG_HAS_MOVEMENT) !== 0;
                if (hasMove !== fMove) continue;
            }

            // Day range. 0 (unknown) fails any active time filter.
            const d = dd[i];
            if (timeFilterActive) {
                if (d === 0 || d < dayFrom || d > dayTo) continue;
            }

            const la = lat[i];
            const ln = lng[i];
            if (la < south || la > north || ln < west || ln > east) continue;
            out[j++] = i;
        }
        POINTS.visibleIdx = out.subarray(0, j);
        return POINTS.visibleIdx;
    }

    // UI filter entry point. Stashes the filter descriptor and
    // rebuilds visibleIdx. Called by app.js applyClientFilters()
    // when the user touches a filter control.
    function applyClientFilters(filter) {
        if (!POINTS.ready) return null;
        _activeFilter = filter || {};
        return _rebuildVisible();
    }

    // Timeline-driven filter entry point. Overwrites the time
    // window and rebuilds. Call at 60 fps from TimeBrush.step().
    //
    //   yearFrom, yearTo  — inclusive integer years (legacy, v0.8.1)
    //                       OR days-since-1900 (v0.8.2 day precision)
    //   opts.cumulative   — true = pin lower bound to dataset min
    //                       (right edge advances, left edge stays put)
    //   opts.dayPrecision — true = interpret yearFrom/yearTo as days
    //                       since 1900-01-01 instead of years
    //
    // Backward compatible: callers that don't pass dayPrecision get
    // year-level filtering like v0.8.1 did. The TimeBrush detects
    // day-precision availability via POINTS.coverage.date_days > 0
    // and passes dayPrecision=true in that case for smooth month-
    // granular playback instead of chunky year jumps.
    //
    // Does NOT touch _activeFilter, so source/shape/bbox/quality
    // filters from the UI remain in effect during playback.
    function setTimeWindow(yearFrom, yearTo, opts) {
        if (!POINTS.ready) return null;
        const cumulative = !!(opts && opts.cumulative);
        const dayPrecision = !!(opts && opts.dayPrecision);
        _ensureYearStats();
        _timeState.enabled = true;
        _timeState.cumulative = cumulative;

        let dayFrom, dayTo;
        if (dayPrecision) {
            // Caller passed day values directly.
            dayFrom = yearFrom | 0;
            dayTo   = yearTo   | 0;
        } else {
            // Convert year integers to day range.
            dayFrom = _yearToDays(yearFrom);
            // Include the whole of yearTo: end = next year's day 0 - 1
            dayTo = _yearToDays((yearTo | 0) + 1) - 1;
        }

        if (cumulative) {
            dayFrom = _dayStats.min != null ? _dayStats.min : 0;
        }
        _timeState.dayFrom = dayFrom;
        _timeState.dayTo = dayTo;

        _rebuildVisible();
        refreshActiveLayer();
        return POINTS.visibleIdx;
    }

    // Stop timeline-driven filtering and revert to the UI year
    // range. Called from TimeBrush.reset() and TimeBrush.togglePlay
    // when the user stops playback.
    function clearTimeWindow() {
        _timeState.enabled = false;
        _timeState.cumulative = false;
        _rebuildVisible();
        refreshActiveLayer();
        return POINTS.visibleIdx;
    }

    // Compute a year histogram from POINTS.dateDays. Returns an array
    // of { year, count } objects, one per year in [min, max]. Cached
    // on first call (~3 ms for 396k rows) and reused forever.
    //
    // v0.8.2: walks dateDays instead of the legacy `year` field. We
    // derive the year from day-index by binary-searching a small
    // precomputed "first day of year N" lookup table, which is faster
    // than calling `new Date()` per row.
    function getYearHistogram() {
        if (_yearStats.histogram) return _yearStats.histogram;
        if (!POINTS.ready) return null;
        _ensureYearStats();
        const min = _yearStats.min;
        const max = _yearStats.max;
        if (min == null || max == null) return [];
        const span = max - min + 1;
        const bins = new Uint32Array(span);

        // Precompute "first day of year" cutoffs for binary search.
        // At N ~ 400k rows and span ~ 130 years, this is 130 entries
        // and each row does ~7 integer comparisons to locate its bin —
        // way faster than `new Date(days).getUTCFullYear()` per row.
        const yearStarts = new Uint32Array(span + 1);
        for (let y = 0; y <= span; y++) {
            yearStarts[y] = _yearToDays(min + y);
        }

        const dd = POINTS.dateDays;
        const N = POINTS.count;
        for (let i = 0; i < N; i++) {
            const d = dd[i];
            if (d === 0) continue;
            // Binary search: find largest y with yearStarts[y] <= d
            let lo = 0, hi = span;
            while (lo < hi) {
                const mid = (lo + hi + 1) >>> 1;
                if (yearStarts[mid] <= d) lo = mid;
                else hi = mid - 1;
            }
            if (lo >= 0 && lo < span) bins[lo]++;
        }

        const out = new Array(span);
        for (let i = 0; i < span; i++) {
            out[i] = { year: min + i, count: bins[i] };
        }
        _yearStats.histogram = out;
        return out;
    }

    // Integer min/max year across non-zero rows. Caches on first
    // call via _ensureYearStats().
    function getYearRange() {
        if (!POINTS.ready) return { min: null, max: null };
        _ensureYearStats();
        return { min: _yearStats.min, max: _yearStats.max };
    }

    // v0.8.2 — min/max days-since-1900 across non-zero rows. Used by
    // TimeBrush for day-precision playback.
    function getDayRange() {
        if (!POINTS.ready) return { min: null, max: null };
        _ensureYearStats();
        return { min: _dayStats.min, max: _dayStats.max };
    }

    // v0.8.2 — coverage map from the meta sidecar, telling the UI
    // which derived fields actually have data. Falsy values → disable
    // the corresponding filter control with a tooltip.
    function getCoverage() {
        if (!POINTS.ready) return {};
        return POINTS.coverage || {};
    }

    // v0.8.2 — whether each derived column EXISTS in the live schema
    // (as opposed to being populated). Useful for telling apart "the
    // v0.8.2 migration hasn't run" from "the pipeline hasn't populated
    // the column yet".
    function getColumnsPresent() {
        if (!POINTS.ready) return {};
        return POINTS.columnsPresent || {};
    }

    // v0.8.2 — lookup helpers so app.js can populate filter dropdowns
    // without reaching into POINTS directly.
    function getShapes()   { return POINTS.shapes   || [null]; }
    function getColors()   { return POINTS.colors   || [null]; }
    function getEmotions() { return POINTS.emotions || [null]; }
    function getSources()  { return POINTS.sources  || [null]; }
    function getShapeSource() { return POINTS.shapeSource || "raw"; }

    // -----------------------------------------------------------------
    // Theme palettes (v0.8.4)
    // -----------------------------------------------------------------
    // Each entry defines the colors the three deck.gl layers use for
    // the named body theme. When app.js calls UFODeck.setTheme(name),
    // we swap the _theme pointer and refreshActiveLayer() so a freshly
    // instantiated layer picks up the new palette. Arrays of [r,g,b]
    // (uint8) or [r,g,b,a] for the scatterplot getFillColor.
    //
    //   signal  — cyan-on-void. Cold-plasma → hot-plasma hex range,
    //             cyan scatterplot dots that pop on Dark Matter tiles.
    //   declass — ink-on-paper. Cream → burgundy → wine range that
    //             mirrors the #B8001F DECLASS accent and reads clearly
    //             on Voyager's warm cream tiles. Dark near-black dots
    //             for max contrast against the light tile background.
    const THEME_PALETTES = {
        signal: {
            scatter: [0, 240, 255, 180],
            hexRange: [
                [0, 59, 92],      // cold plasma
                [0, 140, 180],
                [0, 240, 255],    // hot plasma
                [255, 179, 0],    // amber
                [255, 78, 0],     // hot
            ],
        },
        declass: {
            scatter: [15, 23, 42, 200],  // near-black, reads on cream tiles
            hexRange: [
                [233, 219, 180],  // pale cream (almost the paper color)
                [200, 150, 100],  // tan
                [150, 80, 60],    // rust
                [120, 20, 30],    // burgundy — matches DECLASS accent
                [80, 0, 15],      // deep wine
            ],
        },
    };

    let _theme = "signal";  // set by setTheme() below

    function _activePalette() {
        return THEME_PALETTES[_theme] || THEME_PALETTES.signal;
    }

    // -----------------------------------------------------------------
    // deck.gl layer factories
    // -----------------------------------------------------------------
    // Each helper returns a fresh deck.gl layer instance given the
    // current POINTS.visibleIdx AND the active theme palette. The
    // layer is reconstructed from scratch on every refreshActiveLayer
    // call (cheap — deck.gl's internal GPU buffers only rebuild if
    // the data reference changes), so swapping themes just needs the
    // palette to be read fresh each call.
    function makeScatterplotLayer() {
        const d = window.deck;
        const palette = _activePalette();
        return new d.ScatterplotLayer({
            id: "ufosint-points",
            data: POINTS.visibleIdx,
            // deck.gl can read attributes straight from our typed arrays
            // via indexed accessors. No per-point object allocation.
            getPosition: (i) => [POINTS.lng[i], POINTS.lat[i]],
            getRadius: 1000,
            radiusMinPixels: 1.2,
            radiusMaxPixels: 5,
            getFillColor: palette.scatter,
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
                getFillColor: _theme,  // bust GPU attr cache on theme swap
            },
        });
    }

    function makeHexagonLayer() {
        const d = window.deck;
        const palette = _activePalette();
        // HexagonLayer aggregates in screen-space meters, so hex cells
        // tessellate uniformly regardless of latitude. Radius scales
        // with map zoom via the `radius` prop — we pass a fixed value
        // in meters and let deck.gl handle the projection math.
        //
        // v0.8.2-hotfix: color scale was flat because deck.gl defaults
        // to a linear quantize scale that maps [min, max] evenly across
        // the color ramp. UFOSINT cell counts are dominated by a handful
        // of huge metropolitan buckets (NYC, LA, London) with 5,000+
        // sightings each, while most cells have 1-50 sightings. A
        // linear domain from 1 to 5,000 puts every small cell in the
        // bottom bucket — they all render cold-plasma and the map
        // reads as "same color everywhere with one hot spot".
        //
        // Fixed by switching to colorScaleType: 'quantile', which
        // assigns cells to buckets by percentile rank instead of
        // absolute value. 20% of cells get each of the 5 colors, so
        // the high end gets hot regardless of skew. upperPercentile:99
        // also clips the top 1% of cells (the massive outliers) to
        // the highest color bucket rather than letting them distort
        // the domain. Result: the full color ramp is visible and the
        // relative density of cells is legible across 4 orders of
        // magnitude of count.
        //
        // v0.8.4: colorRange is now read from THEME_PALETTES so the
        // hex ramp matches the active theme. Signal keeps the cold-
        // hot plasma ramp; declass uses cream → rust → burgundy.
        return new d.HexagonLayer({
            id: "ufosint-hex",
            data: POINTS.visibleIdx,
            getPosition: (i) => [POINTS.lng[i], POINTS.lat[i]],
            radius: 60000,                 // 60 km — tune per taste
            extruded: false,
            coverage: 0.95,
            colorRange: palette.hexRange,
            colorScaleType: "quantile",
            upperPercentile: 99,
            pickable: true,
            onClick: (info) => {
                // Cell click: no detail modal — just log the count.
                if (info && info.object) {
                    console.info(`Hex cell: ${info.object.count || 0} sightings`);
                }
            },
            updateTriggers: {
                colorRange: _theme,
            },
        });
    }

    function makeHeatmapLayer() {
        const d = window.deck;
        const palette = _activePalette();
        return new d.HeatmapLayer({
            id: "ufosint-heat",
            data: POINTS.visibleIdx,
            getPosition: (i) => [POINTS.lng[i], POINTS.lat[i]],
            radiusPixels: 28,
            intensity: 1,
            threshold: 0.04,
            // v0.8.4: colorRange mirrors the hex palette so the heatmap
            // reads coherently with the rest of the map.
            colorRange: palette.hexRange,
            updateTriggers: {
                colorRange: _theme,
            },
        });
    }

    // v0.8.4 — public API for the main-thread setTheme() call.
    // Updates the palette pointer and reconstructs the active layer
    // so the new colors land without a page reload. Safe to call
    // before the deck.gl layer is mounted (refreshActiveLayer is a
    // no-op when leafletLayer is null).
    function setDeckTheme(name) {
        if (name !== "signal" && name !== "declass") return;
        if (name === _theme) return;
        _theme = name;
        refreshActiveLayer();
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
        // v0.8.2-hotfix: LeafletLayer lives on window.DeckGlLeaflet
        // (from the deck.gl-leaflet community bridge), NOT on the
        // main window.deck global. MapView still comes from the
        // core deck.gl package.
        const d = window.deck;
        const DGL = window.DeckGlLeaflet;
        leafletLayer = new DGL.LeafletLayer({
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

        // v0.8.1 — temporal animation API
        setTimeWindow,
        clearTimeWindow,
        getYearHistogram,
        getYearRange,
        isTimeWindowActive: () => _timeState.enabled,

        // v0.8.2 — derived-fields API
        getDayRange,
        getCoverage,
        getColumnsPresent,
        getShapes,
        getColors,
        getEmotions,
        getSources,
        getShapeSource,

        // v0.8.4 — theme API
        setTheme: setDeckTheme,
        getTheme: () => _theme,
    };
})();
