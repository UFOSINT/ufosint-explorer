"""v0.8.1 — Client-side temporal animation (TimeBrush + deck.js fast path).

Locks the v0.8.1 contract:

  1. deck.js exposes setTimeWindow, clearTimeWindow, getYearHistogram,
     getYearRange, isTimeWindowActive on window.UFODeck.
  2. The filter pipeline is refactored to _rebuildVisible() with a
     _timeState + _activeFilter pair so the UI filter and the
     timeline filter compose.
  3. The hot loop reuses _visibleScratch (no per-frame allocation).
  4. TimeBrush.useDeckFastPath + playMode + _cumulativeLeft + setPlayMode
     are present and wired into togglePlay / reset.
  5. TimeBrush.ensureData prefers UFODeck.getYearHistogram() when
     available, falling back to /api/timeline on legacy browsers.
  6. index.html has a #brush-mode button next to #brush-play.
  7. style.css styles .brush-mode-btn.
  8. The CHANGELOG has a v0.8.1 section.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"
CHANGELOG = ROOT / "CHANGELOG.md"
PLAN_DOC = ROOT / "docs" / "V081_PLAN.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Plan doc is required — the user asked us to document first.
# ---------------------------------------------------------------------------
def test_v081_plan_doc_exists():
    assert PLAN_DOC.exists(), (
        "docs/V081_PLAN.md is the architecture doc for v0.8.1. The "
        "whole point of v0.8.1 is a GPU-driven PLAY loop — the plan "
        "must exist in-tree so future contributors know why we "
        "bypassed the debounced applyFilters path."
    )


def test_v081_plan_doc_covers_key_concepts():
    doc = _read(PLAN_DOC)
    for concept in (
        "setTimeWindow",
        "cumulative",
        "sliding",
        "getYearHistogram",
        "deckFastPath",
        "requestAnimationFrame",
    ):
        assert concept in doc, f"plan doc missing coverage of {concept!r}"


# ---------------------------------------------------------------------------
# deck.js — public API surface
# ---------------------------------------------------------------------------
def test_deck_js_exports_time_window_api():
    js = _read(DECK_JS)
    for name in (
        "setTimeWindow",
        "clearTimeWindow",
        "getYearHistogram",
        "getYearRange",
        "isTimeWindowActive",
    ):
        assert name in js, f"deck.js must expose {name}"
    # Public surface actually wires each of these into window.UFODeck
    assert "window.UFODeck" in js
    # Spot-check at least one — if the object literal exists, the rest
    # are trivially present by grep above.
    assert "setTimeWindow," in js or "setTimeWindow:" in js


def test_deck_js_filter_pipeline_is_refactored():
    """v0.8.1 moved the filter state into module-level _activeFilter
    + _timeState so setTimeWindow() can overlay a time range onto
    the current UI filter without losing source/shape/bbox filters."""
    js = _read(DECK_JS)
    assert "_activeFilter" in js
    assert "_timeState" in js
    assert "_rebuildVisible" in js, (
        "the shared rebuild function is how applyClientFilters and "
        "setTimeWindow stay in sync — must exist"
    )


def test_deck_js_reuses_scratch_buffer():
    """Allocating a fresh 1.6 MB Uint32Array per frame during 60 fps
    playback creates ~96 MB/sec of GC pressure. v0.8.1 uses a
    persistent scratch buffer and returns .subarray(0, j) views."""
    js = _read(DECK_JS)
    assert "_visibleScratch" in js
    assert "subarray(0, j)" in js or "subarray(0,j)" in js, (
        "the hot loop must return a subarray view of _visibleScratch "
        "instead of allocating a fresh typed array"
    )


def test_deck_js_histogram_is_cached():
    """_yearStats.histogram is computed once and reused forever. The
    cache key is implicit: the bulk buffer never changes shape
    during a session."""
    js = _read(DECK_JS)
    assert "_yearStats" in js
    assert "histogram" in js
    # Must short-circuit if already computed
    assert "_yearStats.histogram" in js


def test_deck_js_year_range_helper():
    """getYearRange() returns { min, max } over non-zero years only,
    so points with an unknown year don't poison the bounds.

    v0.8.2: year stats are derived from POINTS.dateDays instead of a
    dedicated `year` field, and the "unknown" sentinel is a zero
    date_days value. The hot loop skips `d === 0` rows.
    """
    js = _read(DECK_JS)
    assert "getYearRange" in js
    # v0.8.2 walks dateDays with `d === 0` as the unknown sentinel
    assert "d === 0" in js


def test_deck_js_cumulative_mode_supported():
    """setTimeWindow takes a { cumulative } option that pins the
    lower bound to the dataset minimum, for the 'watch the dataset
    fill up' replay style."""
    js = _read(DECK_JS)
    assert "cumulative" in js


# ---------------------------------------------------------------------------
# app.js — TimeBrush rewire
# ---------------------------------------------------------------------------
def test_time_brush_has_fast_path_hook():
    js = _read(APP_JS)
    assert "useDeckFastPath" in js, (
        "TimeBrush.useDeckFastPath is how app.js hands the brush a "
        "direct callback into deck.js.setTimeWindow — must exist"
    )
    assert "this.deckFastPath" in js


def test_time_brush_has_play_mode_state():
    js = _read(APP_JS)
    assert "this.playMode" in js
    assert '"sliding"' in js
    assert '"cumulative"' in js


def test_time_brush_set_play_mode_updates_button():
    """setPlayMode must update the #brush-mode button's label and
    data-mode attr so the CSS + screen readers stay in sync."""
    js = _read(APP_JS)
    assert "setPlayMode" in js
    assert "CUMULATIVE" in js
    assert "SLIDING" in js
    # The button is toggled via data-mode
    assert 'btn.dataset.mode = mode' in js or "dataset.mode = mode" in js


def test_time_brush_play_loop_bypasses_debounce_on_gpu_path():
    """The whole point of v0.8.1 is smooth 60 fps playback. The
    togglePlay step() closure must call this.deckFastPath() directly
    (not this.onChange, which is debounced 300 ms)."""
    js = _read(APP_JS)
    # Find the actual togglePlay method definition, not any mention
    # of "togglePlay()" in a comment or elsewhere.
    start = js.find("    togglePlay() {")
    assert start != -1, "togglePlay() method not found"
    end = js.find("    reset() {", start)
    assert end != -1, "reset() method not found after togglePlay()"
    body = js[start:end]
    # v0.9.3: the old fastPath indirection was replaced with a
    # direct call to UFODeck.setTimeWindow with dayPrecision:true.
    # Both patterns bypass the debounced onChange → applyFilters
    # pipeline, which is the core invariant this test guards.
    assert (
        "fastPath" in body
        or "deckFastPath" in body
        or "setTimeWindow" in body
    ), (
        "togglePlay's step closure must bypass the debounced onChange "
        "path — either via the old fastPath or a direct setTimeWindow "
        "call — for smooth 60fps playback"
    )
    assert "requestAnimationFrame" in body, (
        "playback must be driven by requestAnimationFrame"
    )


def test_time_brush_ensure_data_prefers_client_histogram():
    """ensureData() should check window.UFODeck first and only
    fall back to /api/timeline when the bulk data isn't ready
    (legacy browsers / delayed bulk fetch)."""
    js = _read(APP_JS)
    start = js.find("async ensureData()")
    end = js.find("\n    _", start + 10)  # next private method
    body = js[start:end] if start != -1 else ""
    assert "UFODeck" in body, (
        "ensureData() must check for UFODeck before hitting /api/timeline"
    )
    assert "getYearHistogram" in body


def test_time_brush_reset_clears_time_window_on_gpu_path():
    """TimeBrush.reset() must call UFODeck.clearTimeWindow() so
    the map snaps back to the full range instantly, not after
    the 300 ms debounce."""
    js = _read(APP_JS)
    start = js.find("    reset() {")
    end = js.find("}\n}", start) + 1
    body = js[start:end]
    assert "clearTimeWindow" in body


def test_app_js_wires_brush_to_deck_after_boot():
    """bootDeckGL() and loadObservatory() must both call
    _wireTimeBrushToDeck() — whichever finishes first, the other
    picks up on the second pass."""
    js = _read(APP_JS)
    assert "_wireTimeBrushToDeck" in js
    # bootDeckGL path
    boot = js.find("async function bootDeckGL")
    assert boot != -1
    boot_end = js.find("\n}\n", boot)
    assert "_wireTimeBrushToDeck" in js[boot:boot_end + 3]


def test_app_js_wires_brush_mode_button():
    """wireObservatoryModeToggle must also bind the #brush-mode
    click handler so the user can switch between sliding and
    cumulative playback."""
    js = _read(APP_JS)
    start = js.find("function wireObservatoryModeToggle")
    end = js.find("\n}\n", start)
    body = js[start:end]
    assert "brush-mode" in body
    assert "setPlayMode" in body


# ---------------------------------------------------------------------------
# HTML + CSS
# ---------------------------------------------------------------------------
def test_index_html_has_brush_mode_button():
    html = _read(INDEX_HTML)
    assert 'id="brush-mode"' in html
    assert 'class="brush-mode-btn"' in html
    assert 'data-mode="sliding"' in html  # default state


def test_style_css_has_brush_mode_rules():
    css = _read(STYLE_CSS)
    assert ".brush-mode-btn" in css
    # Cumulative state visual affordance
    assert '[data-mode="cumulative"]' in css


# ---------------------------------------------------------------------------
# CHANGELOG
# ---------------------------------------------------------------------------
def test_changelog_has_v081_section():
    log = _read(CHANGELOG)
    assert "[0.8.1]" in log
    # At minimum mention the key user-visible changes
    assert "PLAY" in log or "playback" in log.lower()
    assert "cumulative" in log.lower() or "histogram" in log.lower()
