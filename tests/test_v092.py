"""v0.9.2 — TimeBrush adaptive-granularity histograms + live commit + Apply button.

Three small but high-value UX improvements on top of the v0.9.0
zoom+pan work:

1. **Adaptive histogram granularity.** The v0.9.0 brush always
   rendered year bars regardless of zoom level. At a 3-month
   zoom the user saw 0-1 bars — a solid useless block. v0.9.2
   adds month and day bar variants. The brush picks the right
   granularity based on view span:

     > 10 years  → year bars
     > ~400 days → month bars
     otherwise    → day bars

   Computation is essentially free: day histogram = 1 pass
   through POINTS.dateDays with integer subtraction per row,
   ~5-10 ms on 396k rows. Cached indefinitely.

2. **Live commit during drag.** v0.9.0's drag semantics were
   visual-only during move + commit on pointerup, which meant
   no live map feedback while dragging. v0.9.2 adds
   `_liveCommit()` that calls `UFODeck.setTimeWindow(days)`
   directly on every pointermove — the same code path the
   Play loop uses at 60fps. Bypasses the form-input +
   URL-hash pipeline (those still happen on pointerup) so
   there's no history churn.

3. **Explicit Apply button.** Next to Reset View. Force-applies
   the current selection via `_onChangeRaw` in case the user
   navigates to the brush via URL hash, programmatic call, or
   an edge case where the live commit didn't run.

Bonus: min selection window dropped from 30 days to 7 days so
sub-year playback actually makes sense now that day bars are
visible.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# =============================================================================
# Phase 1 — deck.js histograms (month + day + unified entry points)
# =============================================================================

def test_deck_has_get_histogram_entry_point():
    """deck.js should export `getHistogram(granularity)` as the
    unified entry point. Year/month/day all route through it."""
    src = _read(DECK_JS)
    assert "function getHistogram(" in src, (
        "deck.js must define getHistogram(granularity) for the "
        "v0.9.2 adaptive brush"
    )


def test_deck_has_get_histogram_for_visible():
    src = _read(DECK_JS)
    assert "function getHistogramForGranularityVisible(" in src


def test_deck_exports_new_histogram_helpers():
    """The window.UFODeck export block must include both new
    helpers so app.js can call them."""
    src = _read(DECK_JS)
    m = re.search(r"window\.UFODeck\s*=\s*\{([\s\S]*?)\};", src)
    assert m, "couldn't locate window.UFODeck export block"
    block = m.group(1)
    assert "getHistogram," in block, (
        "window.UFODeck.getHistogram must be exported"
    )
    assert "getHistogramForGranularityVisible," in block


def test_deck_build_histogram_handles_year_month_day():
    """_buildHistogram should branch on granularity. Look for
    the three string literals in its body."""
    src = _read(DECK_JS)
    assert "function _buildHistogram(" in src, (
        "_buildHistogram shared builder must exist"
    )
    # Extract its body (roughly) and check for the three gran cases
    m = re.search(
        r"function _buildHistogram\([^)]*\)[\s\S]*?\n    \}\n\n",
        src,
    )
    assert m, "couldn't locate _buildHistogram body"
    body = m.group(0)
    assert '"year"' in body or "'year'" in body
    assert '"month"' in body or "'month'" in body
    assert '"day"' in body or "'day'" in body


def test_deck_month_histogram_uses_lut():
    """Month histogram should use a precomputed month-starts LUT
    for efficient binary search, not Date() allocation per row."""
    src = _read(DECK_JS)
    assert "_monthStartsLUT" in src or "monthStarts" in src, (
        "month histogram should use a precomputed month-starts "
        "lookup table for efficient bucket assignment"
    )
    assert "_buildMonthStarts" in src


def test_deck_day_histogram_uses_integer_subtraction():
    """Day histogram is the cheapest — each row's bin is just
    `dayIdx - minDay`. No binary search. Assert the pattern."""
    src = _read(DECK_JS)
    body = _extract_function_body(src, "_buildHistogram")
    assert body
    # The day branch should have a subtraction like `d - minDay`
    assert "minDay" in body, (
        "day histogram branch should reference minDay for "
        "direct-index bucket assignment"
    )


def test_deck_histograms_emit_startms_shape():
    """All three granularity variants should emit { startMs, count }
    bins so TimeBrush._draw can compute x-positions uniformly."""
    src = _read(DECK_JS)
    body = _extract_function_body(src, "_buildHistogram")
    assert body
    assert "startMs" in body, (
        "_buildHistogram bins must have a startMs field for "
        "uniform x-position calculation in TimeBrush._draw"
    )


# =============================================================================
# Phase 2 — TimeBrush adaptive _draw
# =============================================================================

def test_timebrush_has_pick_granularity():
    src = _read(APP_JS)
    assert "_pickGranularity" in src, (
        "TimeBrush._pickGranularity() must exist for v0.9.2 "
        "adaptive bar granularity"
    )


def test_timebrush_pick_granularity_has_three_bands():
    """_pickGranularity should return year/month/day based on
    view span. Check for all three literals in the method body."""
    src = _read(APP_JS)
    m = re.search(
        r"_pickGranularity\(\)\s*\{([\s\S]*?)\n    \}",
        src,
    )
    assert m, "couldn't locate _pickGranularity body"
    body = m.group(1)
    assert '"year"' in body
    assert '"month"' in body
    assert '"day"' in body


def test_timebrush_has_bins_cache_structure():
    """TimeBrush constructor should initialize a three-slot
    _binsCache { year, month, day } — each with { full, filtered }."""
    src = _read(APP_JS)
    assert "_binsCache" in src, (
        "TimeBrush must have a _binsCache for adaptive "
        "granularity caching"
    )
    # The constructor initializes three slots
    assert re.search(
        r"_binsCache\s*=\s*\{[\s\S]*?year[\s\S]*?month[\s\S]*?day",
        src,
    ), (
        "_binsCache should have year/month/day slots"
    )


def test_timebrush_get_full_bins_uses_deck_histogram():
    src = _read(APP_JS)
    assert "_getFullBins" in src, (
        "TimeBrush._getFullBins helper must exist"
    )
    assert "_getFilteredBins" in src, (
        "TimeBrush._getFilteredBins helper must exist"
    )
    # _getFullBins is a class method, not a top-level function
    body = _extract_method_body(src, "_getFullBins")
    assert body, "couldn't extract _getFullBins body"
    assert "getHistogram" in body, (
        "_getFullBins must call window.UFODeck.getHistogram"
    )


def test_timebrush_draw_uses_start_ms():
    """_draw should iterate bins using the startMs field (not
    .year) so day/month/year bins all render through the same
    code path."""
    src = _read(APP_JS)
    body = _extract_method_body(src, "_draw")
    assert body
    assert "startMs" in body, (
        "_draw must use the startMs field so year/month/day bins "
        "all work through one code path"
    )


def test_timebrush_retally_invalidates_cache():
    """retally() should clear the filtered slot of all three
    granularity caches so the next _draw recomputes."""
    src = _read(APP_JS)
    body = _extract_method_body(src, "retally")
    assert body
    # Loop over year/month/day
    assert '"year"' in body and '"month"' in body and '"day"' in body, (
        "retally() must clear all three granularity filtered slots"
    )
    assert "filtered = null" in body or "filtered =null" in body


# =============================================================================
# Phase 3 — Live commit during drag
# =============================================================================

def test_timebrush_has_live_commit_method():
    src = _read(APP_JS)
    assert "_liveCommit(" in src or "_liveCommit =" in src, (
        "TimeBrush._liveCommit must exist"
    )


def test_live_commit_uses_set_time_window():
    """_liveCommit should call UFODeck.setTimeWindow with
    day-precision, bypassing the form-input + hash pipeline."""
    src = _read(APP_JS)
    body = _extract_method_body(src, "_liveCommit")
    assert body
    assert "setTimeWindow" in body, (
        "_liveCommit must call UFODeck.setTimeWindow — same code "
        "path the Play loop uses for 60fps updates"
    )
    assert "dayPrecision" in body, (
        "_liveCommit must pass dayPrecision: true so the day-index "
        "range is honoured"
    )


def test_pointer_move_calls_live_commit():
    """The drag pointermove handler should call _liveCommit
    after _syncWindow for move/l/r modes."""
    src = _read(APP_JS)
    body = _extract_method_body(src, "_bindEvents")
    assert body
    assert "_liveCommit()" in body, (
        "onPointerMove must call this._liveCommit() after "
        "_syncWindow so the map updates live during drag"
    )


def test_min_selection_window_dropped_to_7_days():
    """v0.9.2 dropped the minimum selection window from 30 days
    (1 month) to 7 days so users can set narrower windows now
    that day bars make sub-month precision meaningful."""
    src = _read(APP_JS)
    body = _extract_method_body(src, "_bindEvents")
    assert body
    # 7 * 86400000 should appear (for at least one of l/r handles)
    assert "7 * 86400000" in body, (
        "min window for l/r handles should be 7 days (7 * 86400000)"
    )
    # The old 30 * 86400000 should be gone
    assert "30 * 86400000" not in body, (
        "old 30-day minimum should be replaced with 7-day"
    )


# =============================================================================
# Phase 4 — Apply button
# =============================================================================

def test_index_html_has_apply_button():
    html = _read(INDEX_HTML)
    assert 'id="brush-apply"' in html, (
        "index.html must contain the #brush-apply button in the "
        "brush header (next to Reset View)"
    )


def test_timebrush_has_apply_now_method():
    src = _read(APP_JS)
    assert "applyNow(" in src, (
        "TimeBrush.applyNow() method must exist"
    )


def test_apply_button_wired_in_bind_events():
    src = _read(APP_JS)
    body = _extract_method_body(src, "_bindEvents")
    assert body
    assert "brush-apply" in body, (
        "_bindEvents must wire the #brush-apply click handler"
    )
    assert "applyNow" in body, (
        "Apply button click handler must call this.applyNow()"
    )


def test_apply_now_commits_via_raw_callback():
    """applyNow() should route through _onChangeRaw, same as
    pointerup."""
    src = _read(APP_JS)
    body = _extract_method_body(src, "applyNow")
    assert body
    assert "_onChangeRaw" in body, (
        "applyNow must commit via _onChangeRaw (same path as "
        "pointerup) so form inputs + hash get updated"
    )


# =============================================================================
# Helpers
# =============================================================================

def _extract_function_body(src: str, name: str) -> str:
    """Extract a top-level function body by walking brace depth."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i == -1:
        return ""
    depth = 0
    start = i
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    return src[start:]


def _extract_method_body(src: str, name: str) -> str:
    """Extract a class method body by matching `    methodName(`
    (4-space indent) and walking braces."""
    m = re.search(r"\n    " + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i == -1:
        return ""
    depth = 0
    start = i
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    return src[start:]
