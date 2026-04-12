"""v0.8.6 — Timeline redesign + Insights buildout + cleanup.

Locks the v0.8.6 contract:

  1. /api/search and /api/duplicates routes are removed from app.py.
  2. _points_bulk_etag() includes a has_movement_mentioned data-content
     signal so content-replace reloads invalidate the lru_cache.
  3. deck.js has the new aggregate helpers:
     getYearHistogramBySource, getYearHistogramForVisible,
     computeMedianByYear, computeMovementShareByYear, countVisible.
  4. app.js TimeBrush has the retally() method + split drag handlers
     (pointermove is visual-only, pointerup commits via the raw
     un-debounced callback).
  5. app.js has the 3 new Timeline render helpers (main, quality, movement)
     and the 4 new Insight render helpers (quality dist, movement
     taxonomy, shape × movement, hoax curve).
  6. The client-side Insight cards are gated on POINTS.ready so they
     render even when the sentiment endpoints return empty.
  7. index.html has the new Timeline canvas IDs + 4 new insight
     canvas IDs, and no longer has #panel-search / #panel-duplicates
     or the data-tab="search" / data-tab="duplicates" buttons.
  8. debounce() flush/cancel methods actually work (prior versions
     had a no-op flush that dropped the trailing commit).

The style is static source-code inspection — these tests don't hit
the DB or a Flask test client. They're a cheap tripwire against
future regressions that would re-introduce dead search code or
break the v0.8.6 client-side aggregate pipeline.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# =============================================================================
# Phase 1 — Backend cleanup + etag fix
# =============================================================================

def test_api_search_route_deleted():
    src = _read(APP_PY)
    assert '@app.route("/api/search")' not in src, (
        "v0.8.6 deleted /api/search — the Observatory rail + bulk "
        "buffer replaced it. Don't re-add without a plan doc update."
    )
    assert "def api_search(" not in src, (
        "api_search handler body must be gone too"
    )


def test_api_duplicates_route_deleted():
    src = _read(APP_PY)
    assert '@app.route("/api/duplicates")' not in src, (
        "v0.8.6 deleted /api/duplicates — the v0.8.3b export ships "
        "zero duplicate_candidate rows so the endpoint had nothing "
        "to return. Per-sighting duplicates stay on /api/sighting/<id>."
    )
    assert "def api_duplicates(" not in src


def test_export_routes_survive():
    """`/api/export.csv` and `/api/export.json` stay even though the
    Search panel that wired them up was deleted. They're still
    reachable via direct URL and share `_build_export_query` with
    `add_common_filters`."""
    src = _read(APP_PY)
    assert '@app.route("/api/export.csv")' in src
    assert '@app.route("/api/export.json")' in src
    assert "def _build_export_query(" in src


def test_points_bulk_etag_includes_movement_count():
    src = _read(APP_PY)
    # Find the _points_bulk_etag function body (between the def and
    # the next top-level def).
    m = re.search(
        r"def _points_bulk_etag\(\)[\s\S]*?(?=\ndef )",
        src,
    )
    assert m, "couldn't locate _points_bulk_etag() body"
    body = m.group(0)

    # Must reference has_movement_mentioned in the SQL aggregate
    assert "has_movement_mentioned" in body, (
        "_points_bulk_etag must include a has_movement count as a "
        "data-content signal — see v0.8.6 plan, Phase 1b"
    )
    # Must fold that count into the returned etag string via an "mv"
    # prefix so the format is parseable and stable.
    assert '"-mv{mv_count}-"' in body or "'-mv{mv_count}-'" in body or "mv{mv_count}" in body, (
        "etag must include the mv_count as a segment named 'mv'"
    )
    # UndefinedColumn guard must exist so pre-v0.8.3 schemas still
    # compute an etag.
    assert "UndefinedColumn" in body, (
        "etag function must catch psycopg.errors.UndefinedColumn so "
        "pre-v0.8.3 schemas still produce a valid etag"
    )


# =============================================================================
# Phase 2 — deck.js aggregate helpers
# =============================================================================

def test_deck_has_year_histogram_by_source():
    src = _read(DECK_JS)
    assert "function getYearHistogramBySource(" in src
    # Must actually walk sourceIdx to stack by source
    m = re.search(
        r"function getYearHistogramBySource[\s\S]*?\n    \}\n",
        src,
    )
    assert m, "couldn't locate getYearHistogramBySource body"
    assert "sourceIdx" in m.group(0), (
        "getYearHistogramBySource must read POINTS.sourceIdx to "
        "split bars by source"
    )


def test_deck_has_filtered_year_histogram():
    src = _read(DECK_JS)
    assert "function getYearHistogramForVisible(" in src


def test_deck_has_compute_median_by_year():
    src = _read(DECK_JS)
    assert "function computeMedianByYear(" in src
    # The 255 sentinel is used to flag unknown scores in the bulk
    # buffer; computeMedianByYear must skip those rows.
    m = re.search(
        r"function computeMedianByYear[\s\S]*?\n    \}\n",
        src,
    )
    assert m, "couldn't locate computeMedianByYear body"
    assert "255" in m.group(0), (
        "computeMedianByYear must skip the UNK=255 sentinel"
    )


def test_deck_has_compute_movement_share_by_year():
    src = _read(DECK_JS)
    assert "function computeMovementShareByYear(" in src


def test_deck_has_count_visible():
    src = _read(DECK_JS)
    assert "function countVisible(" in src


def test_deck_exports_new_helpers_on_ufodeck():
    """Every new helper must be wired into the window.UFODeck export
    so app.js can call it without reaching inside the IIFE."""
    src = _read(DECK_JS)
    # Find the `window.UFODeck = { ... };` block
    m = re.search(r"window\.UFODeck\s*=\s*\{([\s\S]*?)\};", src)
    assert m, "couldn't locate window.UFODeck export block"
    block = m.group(1)
    for helper in (
        "getYearHistogramBySource",
        "getYearHistogramForVisible",
        "computeMedianByYear",
        "computeMovementShareByYear",
        "countVisible",
    ):
        assert helper in block, (
            f"window.UFODeck.{helper} not exported — add it to the "
            f"export block at the bottom of deck.js"
        )


# =============================================================================
# Phase 3 — TimeBrush drag + retally
# =============================================================================

def test_timebrush_has_retally_method():
    src = _read(APP_JS)
    assert "retally(bins)" in src, (
        "TimeBrush.retally(bins) method must exist — see v0.8.6 plan "
        "Phase 3b. Takes a filtered year histogram and redraws the "
        "brush with the filtered overlay."
    )
    assert "this.binsFiltered" in src, (
        "retally should stash the filtered bins in this.binsFiltered"
    )


def test_timebrush_stores_raw_onchange():
    """v0.8.6: _onChangeRaw is the un-debounced reference used by
    pointerup and togglePlay so the filter commits within one frame
    of release."""
    src = _read(APP_JS)
    assert "this._onChangeRaw" in src


def test_timebrush_pointerup_bypasses_debounce():
    """After drag end, the brush must commit via _onChangeRaw, not
    via the debounced onChange. Check the onPointerUp handler body."""
    src = _read(APP_JS)
    m = re.search(r"const onPointerUp = \(\)[\s\S]*?\};", src)
    assert m, "couldn't locate onPointerUp handler"
    body = m.group(0)
    assert "_onChangeRaw" in body, (
        "onPointerUp must commit the filter via the raw un-debounced "
        "callback so drag-release is instant"
    )


def test_debounce_has_working_flush_and_cancel():
    """Upgraded debounce helper: flush() actually fires pending calls,
    and cancel() clears the scheduled timer."""
    src = _read(APP_JS)
    m = re.search(r"function debounce\(fn, ms\)[\s\S]*?\n\}\n", src)
    assert m, "couldn't locate debounce() body"
    body = m.group(0)
    assert "wrapped.flush = " in body
    assert "wrapped.cancel = " in body
    assert "pendingArgs" in body, (
        "debounce() must track pending args so flush() can fire them"
    )


def test_apply_client_filters_retallies_brush():
    """applyClientFilters() must call timeBrush.retally after the
    filter commit so the brush histogram shape follows the current
    filter state."""
    src = _read(APP_JS)
    m = re.search(
        r"function applyClientFilters\(\)[\s\S]*?\n\}\n",
        src,
    )
    assert m, "couldn't locate applyClientFilters() body"
    body = m.group(0)
    assert "timeBrush" in body and "retally" in body, (
        "applyClientFilters must call state.timeBrush.retally(...) "
        "to refresh the brush histogram against the new visible set"
    )


# =============================================================================
# Phase 4 — Timeline page redesign
# =============================================================================

def test_index_html_has_new_timeline_canvas_ids():
    html = _read(INDEX_HTML)
    for cid in (
        "timeline-main-chart",
        "timeline-quality-chart",
        "timeline-movement-chart",
        "timeline-range-label",
        "timeline-visible-count",
    ):
        assert f'id="{cid}"' in html, (
            f'index.html missing id="{cid}" — Timeline panel '
            f"should have 3 new canvases + header meta"
        )


def test_index_html_dropped_old_timeline_chart():
    html = _read(INDEX_HTML)
    assert 'id="timeline-chart"' not in html, (
        "old single-chart timeline-chart canvas should be gone — "
        "replaced by timeline-main-chart + 2 other cards"
    )
    assert 'id="timeline-back"' not in html, (
        "old timeline-back button is gone with the drill-down UX"
    )


def test_app_js_has_timeline_render_helpers():
    src = _read(APP_JS)
    for fn in (
        "function refreshTimelineCards",
        "function renderTimelineMainChart",
        "function renderTimelineQualityChart",
        "function renderTimelineMovementChart",
    ):
        assert fn in src, f"app.js missing {fn!r}"


def test_load_timeline_drops_api_timeline_fetch():
    """v0.8.6: loadTimeline is now pure client-side. The `/api/timeline`
    endpoint stays alive as a brush fallback but the Timeline tab
    itself no longer fetches it."""
    src = _read(APP_JS)
    m = re.search(
        r"async function loadTimeline\(\)[\s\S]*?\n\}\n",
        src,
    )
    assert m, "couldn't locate loadTimeline() body"
    body = m.group(0)
    assert "/api/timeline" not in body, (
        "loadTimeline() must not hit /api/timeline — the Timeline "
        "tab is now client-side only. See Phase 4b in the plan."
    )


# =============================================================================
# Phase 5 — 4 new Insight cards
# =============================================================================

def test_index_html_has_new_insight_canvas_ids():
    html = _read(INDEX_HTML)
    for cid in (
        "quality-distribution-chart",
        "movement-taxonomy-chart",
        "shape-movement-chart",
        "hoax-curve-chart",
    ):
        assert f'id="{cid}"' in html, (
            f'index.html missing id="{cid}" — Insights grid should '
            f"have 4 new cards"
        )


def test_app_js_has_new_insight_renderers():
    src = _read(APP_JS)
    for fn in (
        "function renderQualityDistribution",
        "function renderMovementTaxonomy",
        "function renderShapeMovementMatrix",
        "function renderHoaxCurve",
        "function refreshInsightsClientCards",
    ):
        assert fn in src, f"app.js missing {fn!r}"


def test_new_insight_cards_use_visible_idx():
    """Every new card walks POINTS.visibleIdx so it respects the
    current filter state."""
    src = _read(APP_JS)
    for fn in (
        "renderQualityDistribution",
        "renderMovementTaxonomy",
        "renderShapeMovementMatrix",
        "renderHoaxCurve",
    ):
        m = re.search(rf"function {fn}\(\)[\s\S]*?\n\}}\n", src)
        assert m, f"couldn't locate {fn} body"
        body = m.group(0)
        assert "visibleIdx" in body, (
            f"{fn} must walk POINTS.visibleIdx so filter state "
            f"flows through"
        )


def test_insights_client_cards_decoupled_from_sentiment_endpoints():
    """refreshInsightsClientCards must NOT fetch from /api/sentiment/*
    endpoints — all cards read from the bulk buffer's typed arrays.
    v0.11 renamed some cards to 'Sentiment*' but they still use
    POINTS.vaderCompound / emotion28Group, not the old endpoints."""
    src = _read(APP_JS)
    m = re.search(
        r"function refreshInsightsClientCards\([^)]*\)[\s\S]*?\n\}\n",
        src,
    )
    assert m, "couldn't locate refreshInsightsClientCards() body"
    body = m.group(0)
    # Must NOT fetch from the old sentiment API
    assert "/api/sentiment" not in body, (
        "refreshInsightsClientCards must not call /api/sentiment/*"
    )
    assert "POINTS.ready" in body or "POINTS &&" in body, (
        "refreshInsightsClientCards must gate on POINTS.ready"
    )


# =============================================================================
# Phase 6 — Search + Duplicates cleanup
# =============================================================================

def test_index_html_search_nav_button_gone():
    html = _read(INDEX_HTML)
    assert 'data-tab="search"' not in html, (
        "Search nav button must be deleted from index.html"
    )
    assert 'data-tab="duplicates"' not in html, (
        "Duplicates nav button must be deleted from index.html"
    )


def test_index_html_search_panel_gone():
    html = _read(INDEX_HTML)
    assert 'id="panel-search"' not in html
    assert 'id="panel-duplicates"' not in html


def test_app_js_dead_search_functions_deleted():
    src = _read(APP_JS)
    for fn in (
        "async function doSearch(",
        "async function executeSearch(",
        "function renderActiveFilterChips(",
        "function renderPager(",
        "function goToPage(",
        "function removeFilter(",
        "async function loadDuplicates(",
        "function scoreColor(",
        "function scoreLabel(",
    ):
        assert fn not in src, (
            f"app.js still defines {fn!r} — v0.8.6 deleted all "
            f"Search + Duplicates panel functions"
        )


def test_valid_tabs_dropped_search_and_duplicates():
    src = _read(APP_JS)
    m = re.search(r"const VALID_TABS[\s\S]*?\]\);", src)
    assert m, "couldn't locate VALID_TABS declaration"
    body = m.group(0)
    assert '"search"' not in body
    assert '"duplicates"' not in body
    assert '"observatory"' in body  # sanity: observatory still there


def test_disable_button_while_pending_survived():
    """Shared utility kept from the old search block — still used by
    applyFilters() and the AI/map place search paths."""
    src = _read(APP_JS)
    assert "function disableButtonWhilePending(" in src
