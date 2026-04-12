"""v0.9.0 — TimeBrush zoom/pan + mobile responsive layout.

Four thread-lines, all static source-code inspection:

1. TimeBrush gains zoom state (viewMinT/viewMaxT), view-aware
   draw helpers, scroll-wheel zoom, drag-to-pan, and a resetView
   method + Reset View button.
2. Selection-drag math uses _viewSpan() so precision scales with
   zoom level.
3. Touch-primary feature detection adds body.is-touch via
   matchMedia("(hover: none) and (pointer: coarse)").
4. Observatory rail sections become accordion-ready (collapse
   buttons + bodies) and collapse by default on mobile except
   for Data Quality. Filter bar, stats badge, and brush gain
   responsive rules.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    pat = re.compile(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
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


def _extract_js_method(src: str, class_name: str, method_name: str) -> str:
    """Extract a method body from a class. Finds
    `    methodName(...) {` inside `class ClassName {` and walks
    forward with brace-depth counting. Works for both arrow
    methods and regular methods."""
    class_pat = re.compile(r"class\s+" + re.escape(class_name) + r"\s*\{")
    cm = class_pat.search(src)
    if not cm:
        return ""
    # Find the method inside the class body.
    method_pat = re.compile(
        r"\n\s+" + re.escape(method_name) + r"\s*\(",
    )
    mm = method_pat.search(src, cm.end())
    if not mm:
        return ""
    i = src.find("{", mm.end())
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


# =============================================================================
# Phase 1 — TimeBrush zoom state
# =============================================================================

def test_timebrush_has_view_state():
    src = _read(APP_JS)
    # The constructor body (or anywhere in the TimeBrush class) must
    # initialise viewMinT and viewMaxT.
    cm = re.search(r"class TimeBrush\s*\{", src)
    assert cm, "couldn't locate class TimeBrush"
    class_body_start = cm.end()
    # Crude but effective: slice the next 10k chars of the class
    # and search for the assignments.
    slab = src[class_body_start:class_body_start + 10000]
    assert "this.viewMinT" in slab, (
        "TimeBrush must initialise this.viewMinT for v0.9.0 zoom state"
    )
    assert "this.viewMaxT" in slab, (
        "TimeBrush must initialise this.viewMaxT for v0.9.0 zoom state"
    )
    assert "_minViewSpanMs" in slab, (
        "TimeBrush must define _minViewSpanMs to cap max zoom-in"
    )


def test_timebrush_has_view_helpers():
    src = _read(APP_JS)
    # Check for the three view-space helpers. They can be defined as
    # methods (preferred) or as inline functions.
    for helper in ("_viewSpan", "_pxToViewTime", "_viewTimeToPx"):
        assert f"{helper}(" in src, (
            f"TimeBrush must define {helper}() for view-aware "
            f"coordinate math"
        )


def test_timebrush_has_reset_view_method():
    src = _read(APP_JS)
    assert "resetView(" in src, (
        "TimeBrush must have a resetView() method (zooms back to "
        "full range)"
    )
    assert "_updateResetViewBtn(" in src, (
        "TimeBrush must have _updateResetViewBtn() to show/hide the "
        "Reset View button based on zoom state"
    )


def test_timebrush_draw_is_view_aware():
    """_draw() must read viewMinT/viewMaxT to decide which bars to
    render. The old code used BRUSH_MIN_YEAR/BRUSH_MAX_YEAR constants
    directly; v0.9.0 must NOT do that inside the draw body."""
    src = _read(APP_JS)
    # Find the _draw method of TimeBrush. Use a regex on the method
    # signature since _extract_js_method is class-aware.
    m = re.search(r"\n    _draw\(\)\s*\{", src)
    assert m, "couldn't locate TimeBrush._draw"
    # Extract by brace counting.
    i = src.find("{", m.end() - 1)
    depth = 0
    start = i
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = src[start:j + 1]
                break
    else:
        raise AssertionError("couldn't find end of _draw body")

    assert "viewMinT" in body or "viewMinYear" in body, (
        "_draw must compute bar x-positions from the view range, "
        "not from BRUSH_MIN_YEAR"
    )
    assert "_viewTimeToPx" in body, (
        "_draw must call _viewTimeToPx to map bar year → pixel x"
    )


def test_sync_window_clips_to_view():
    """_syncWindow must clip the selection rectangle to the current
    view range. Window entirely outside the view = display:none."""
    src = _read(APP_JS)
    m = re.search(r"\n    _syncWindow\(\)\s*\{", src)
    assert m, "couldn't locate TimeBrush._syncWindow"
    i = src.find("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = src[i:j + 1]
                break
    assert "viewMinT" in body, (
        "_syncWindow must reference viewMinT to clip the selection "
        "rectangle to the current view"
    )
    assert "clippedL" in body or "Math.max(winL, viewL)" in body, (
        "_syncWindow must clamp the selection rectangle's left edge "
        "to the view's left edge"
    )
    assert "_formatWindowLabel" in body, (
        "_syncWindow must call _formatWindowLabel so sub-year "
        "selections get month/day precision in the readout"
    )


def test_format_window_label_exists():
    src = _read(APP_JS)
    assert "_formatWindowLabel(" in src, (
        "TimeBrush must define _formatWindowLabel(d0, d1) helper for "
        "variable-precision window readouts"
    )


# =============================================================================
# Phase 3 — Wheel zoom + drag pan
# =============================================================================

def test_timebrush_binds_wheel_event():
    src = _read(APP_JS)
    # Find _bindEvents body
    m = re.search(r"\n    _bindEvents\(\)\s*\{", src)
    assert m, "couldn't locate TimeBrush._bindEvents"
    i = src.find("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = src[i:j + 1]
                break
    assert 'addEventListener("wheel"' in body, (
        "_bindEvents must register a wheel listener for scroll-zoom"
    )
    # Wheel handler body should reference cursor-centered zoom math
    assert "onWheel" in body or "deltaY" in body, (
        "_bindEvents wheel handler must read e.deltaY for zoom "
        "direction"
    )


def test_timebrush_has_pan_mode():
    """onPointerDown / onPointerMove must handle a 'pan' mode for
    drag-on-empty-canvas view panning."""
    src = _read(APP_JS)
    m = re.search(r"\n    _bindEvents\(\)\s*\{", src)
    assert m
    i = src.find("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = src[i:j + 1]
                break
    assert '"pan"' in body or "'pan'" in body, (
        "_bindEvents must introduce a 'pan' mode in the dragging "
        "state for drag-to-pan the view"
    )
    assert "startViewL" in body, (
        "pan mode must stash startViewL (the view's left edge at "
        "drag start) so subsequent moves can translate from that "
        "baseline"
    )


def test_timebrush_binds_dblclick_reset():
    src = _read(APP_JS)
    m = re.search(r"\n    _bindEvents\(\)\s*\{", src)
    assert m
    i = src.find("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = src[i:j + 1]
                break
    assert '"dblclick"' in body or "'dblclick'" in body, (
        "_bindEvents must wire a dblclick handler on the canvas to "
        "reset the zoom view (classic 'zoom out to fit' pattern)"
    )
    assert "resetView" in body, (
        "dblclick handler must call this.resetView()"
    )


def test_selection_drag_uses_view_span():
    """The selection-drag math (existing move/l/r modes) must use
    _viewSpan() not (maxT - minT) so precision scales with zoom."""
    src = _read(APP_JS)
    m = re.search(r"\n    _bindEvents\(\)\s*\{", src)
    assert m
    i = src.find("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = src[i:j + 1]
                break
    # The new code should compute span via _viewSpan() somewhere in
    # the pointer move handler.
    assert "_viewSpan()" in body, (
        "selection drag must compute span via this._viewSpan() so "
        "dragging 100px moves the selection by 100px-worth of view "
        "time (not full-dataset time)"
    )


# =============================================================================
# Phase 4 — Reset view button
# =============================================================================

def test_index_html_has_reset_view_button():
    html = _read(INDEX_HTML)
    assert 'id="brush-reset-view"' in html, (
        "index.html must contain #brush-reset-view in the brush header"
    )


def test_reset_view_button_hidden_default():
    """The button should ship with hidden attribute — JS flips it
    visible when zoomed in via _updateResetViewBtn."""
    html = _read(INDEX_HTML)
    m = re.search(r'<button[^>]*id="brush-reset-view"[^>]*>', html)
    assert m, "couldn't find #brush-reset-view button"
    assert "hidden" in m.group(0), (
        "#brush-reset-view must start with the `hidden` attribute — "
        "_updateResetViewBtn shows it when zoomed"
    )


# =============================================================================
# Phase 5 — Feature detect + accordion rail
# =============================================================================

def test_app_js_has_touch_feature_detect():
    src = _read(APP_JS)
    assert 'matchMedia("(hover: none) and (pointer: coarse)")' in src, (
        "app.js must use matchMedia for touch-primary feature "
        "detection (classList.toggle('is-touch'))"
    )
    assert 'classList.toggle("is-touch"' in src, (
        "app.js must toggle body.is-touch based on the matchMedia "
        "result"
    )


def test_app_js_has_rail_collapse_handler():
    src = _read(APP_JS)
    assert "function hydrateRailCollapsibles(" in src, (
        "app.js must define hydrateRailCollapsibles to wire rail "
        "accordion buttons"
    )


def test_index_html_has_rail_collapse_buttons():
    html = _read(INDEX_HTML)
    # Should have 5 rail sections now wrapped in collapse buttons
    # (Data Quality, Sources, Shapes, Visible, Time window)
    count = html.count('class="rail-collapse-btn"')
    assert count >= 5, (
        f"index.html must have at least 5 .rail-collapse-btn "
        f"elements (one per rail section), found {count}"
    )


def test_index_html_has_rail_body_wrappers():
    html = _read(INDEX_HTML)
    count = html.count('class="rail-body"')
    assert count >= 5, (
        f"index.html must have at least 5 .rail-body wrappers "
        f"(one per rail section), found {count}"
    )


def test_index_html_has_rail_chevrons():
    html = _read(INDEX_HTML)
    count = html.count("rail-chevron")
    assert count >= 5, (
        f"index.html must have at least 5 rail-chevron SVG "
        f"references, found {count}"
    )


def test_css_has_collapse_rules():
    css = _read(STYLE_CSS)
    assert ".rail-collapse-btn" in css
    assert ".rail-chevron" in css
    # Chevron must rotate on collapse
    assert re.search(r"rail-chevron[^}]*transform:\s*rotate", css), (
        ".rail-chevron needs a transform:rotate rule for the "
        "collapsed state animation"
    )


def test_css_has_mobile_observatory_rules():
    css = _read(STYLE_CSS)
    # Media query for narrow viewports
    assert "@media (max-width: 700px)" in css, (
        "style.css must have @media (max-width: 700px) for "
        "narrow-viewport fallback"
    )
    # Media query block should target .observatory-stage
    m = re.search(
        r"@media \(max-width: 700px\)\s*\{([\s\S]*?)\n\}",
        css,
    )
    assert m
    mobile_block = m.group(1)
    assert ".observatory-stage" in mobile_block, (
        "mobile @media block must retarget .observatory-stage"
    )
    assert "grid-template-columns: 1fr" in mobile_block, (
        "mobile .observatory-stage must collapse to a single column"
    )


def test_css_has_touch_observatory_rules():
    css = _read(STYLE_CSS)
    assert "body.is-touch .observatory-stage" in css, (
        "style.css must have a body.is-touch branch for the "
        "Observatory stage"
    )
    assert "body.is-touch .observatory-rail" in css, (
        "style.css must have a body.is-touch branch for the rail"
    )


def test_css_brush_height_bumped_on_touch():
    """Touch targets need ~44px min; brush was ~60px with 6px
    handles. v0.9.0 bumps the brush to 115px on touch and
    expands the handle hit areas."""
    css = _read(STYLE_CSS)
    # Search for a rule that sets the brush to a taller height under
    # a touch or narrow selector.
    touch_block = re.search(
        r"body\.is-touch \.observatory-time-brush\s*\{([^}]*)\}",
        css,
    )
    assert touch_block, (
        "style.css must have a body.is-touch .observatory-time-brush "
        "rule bumping the brush height"
    )
    assert "height:" in touch_block.group(1), (
        "touch brush rule must set an explicit height"
    )


# =============================================================================
# Phase 6 — Responsive polish
# =============================================================================

def test_stats_badge_optional_chips_wrapped():
    """showStats should wrap derived-count chips in
    .stats-chip-optional so CSS can hide them on narrow viewports."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "showStats")
    assert body, "couldn't locate showStats body"
    assert 'stats-chip-optional' in body, (
        "showStats must wrap the high-quality / with-movement / "
        "duplicates chips in <span class='stats-chip-optional'> so "
        "they can be hidden on mobile"
    )


def test_css_hides_optional_chips_on_mobile():
    css = _read(STYLE_CSS)
    assert ".stats-chip-optional" in css
    # The hide rule should appear inside either the @media or
    # body.is-touch block.
    assert re.search(
        r"(body\.is-touch|@media[^\{]*)[^}]*stats-chip-optional[^}]*display:\s*none",
        css,
    ), "stats-chip-optional must be hidden under a touch / mobile rule"


def test_css_filter_bar_responsive():
    css = _read(STYLE_CSS)
    # The @media block must touch #filters-bar or .filter-group
    assert re.search(
        r"@media \(max-width: 700px\)[\s\S]*?#filters-bar",
        css,
    ), "mobile @media block must restyle #filters-bar"
