"""v0.11.4 — Region (geofence) draw tool.

Rectangle MVP per Issue #7 UX spec. Click REGION button in the topbar,
click-drag on the map to define a bounding box. The rectangle filters
all sightings to that spatial region across Observatory, Timeline, and
Insights tabs. Persists in URL hash as `region=rect:s,w;n,e`.

These are static analysis tests — they read the source files and assert
structural properties (HTML elements, CSS rules, JS function signatures)
without running a live browser.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"
DECK_JS = ROOT / "static" / "deck.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# =============================================================================
# HTML structure
# =============================================================================

def test_region_draw_button_exists():
    html = _read(INDEX_HTML)
    assert 'id="region-draw-btn"' in html
    assert 'class="region-draw-btn"' in html
    assert ">REGION<" in html or "REGION</span>" in html


def test_region_banner_exists():
    html = _read(INDEX_HTML)
    assert 'id="region-banner"' in html
    assert 'id="region-cancel-btn"' in html
    # hidden by default
    assert 'id="region-banner" class="region-banner" hidden' in html


def test_region_chip_exists():
    html = _read(INDEX_HTML)
    assert 'id="region-chip"' in html
    assert 'id="region-chip-bounds"' in html
    assert 'id="region-clear-btn"' in html


def test_region_drag_rect_exists():
    html = _read(INDEX_HTML)
    assert 'id="region-drag-rect"' in html
    assert 'class="region-drag-rect"' in html


def test_region_button_before_points_controls():
    """REGION should be between the mode-toggle and the points-controls."""
    html = _read(INDEX_HTML)
    btn_pos = html.find('id="region-draw-btn"')
    pc_pos = html.find('id="points-controls"')
    mt_pos = html.find('class="mode-toggle"')
    assert mt_pos < btn_pos < pc_pos


# =============================================================================
# CSS rules
# =============================================================================

def test_region_css_draw_button():
    css = _read(STYLE_CSS)
    assert ".region-draw-btn" in css
    assert ".region-draw-btn.active" in css


def test_region_css_banner():
    css = _read(STYLE_CSS)
    assert ".region-banner" in css
    assert ".region-banner-cancel" in css


def test_region_css_chip():
    css = _read(STYLE_CSS)
    assert ".region-chip" in css
    assert ".region-chip-clear" in css


def test_region_css_drag_rect():
    css = _read(STYLE_CSS)
    assert ".region-drag-rect" in css
    # Must be pointer-events: none so it doesn't intercept map events
    m = re.search(r"\.region-drag-rect\s*\{([^}]+)\}", css)
    assert m
    assert "pointer-events: none" in m.group(1)


def test_region_css_crosshair_cursor_during_draw():
    css = _read(STYLE_CSS)
    assert ".region-drawing" in css
    assert "crosshair" in css


def test_region_css_reduced_motion():
    """Banner pulse animation must respect prefers-reduced-motion."""
    css = _read(STYLE_CSS)
    assert "region-banner-pulse" in css
    # Reduced motion media query should reference .region-banner somewhere
    assert "prefers-reduced-motion" in css


# =============================================================================
# JavaScript functions
# =============================================================================

def test_init_region_draw_tool_function():
    src = _read(APP_JS)
    assert "function initRegionDrawTool(" in src


def test_enter_exit_region_draw_mode():
    src = _read(APP_JS)
    assert "function _enterRegionDrawMode(" in src
    assert "function _exitRegionDrawMode(" in src


def test_region_pointer_handlers():
    src = _read(APP_JS)
    assert "function _regionPointerDown(" in src
    assert "function _regionPointerMove(" in src
    assert "function _regionPointerUp(" in src


def test_apply_region_filter_function():
    src = _read(APP_JS)
    assert "function applyRegionFilter(" in src
    assert "function clearRegionFilter(" in src
    # clearRegionFilter must be exposed globally for the hash-restore path
    assert "window.clearRegionFilter" in src


def test_region_chip_renderer():
    src = _read(APP_JS)
    assert "function _renderRegionChip(" in src


def test_region_hash_encode_decode():
    src = _read(APP_JS)
    assert "function _encodeRegionHash(" in src
    assert "function _decodeRegionHash(" in src
    # Format sentinels for all three shape types (v0.11.6: ellipse replaces circle)
    assert "rect" in src
    assert "ellipse" in src
    assert "poly:" in src


# =============================================================================
# Integration with existing pipeline
# =============================================================================

def test_region_filter_wired_into_apply_client_filters():
    """The bbox in the filter object must read from state.regionFilter."""
    src = _read(APP_JS)
    # Expect to find state.regionFilter referenced in the bbox field.
    # v0.11.4: also guarded by _regionActive so the TimeBrush toggle
    # can flip the filter without clearing the drawn geometry.
    m = re.search(
        r"bbox:[\s\S]{0,120}state\.regionFilter",
        src,
    )
    assert m, "bbox field in filter object must read from state.regionFilter"


def test_draw_tool_initialized_in_boot_sequence():
    """initRegionDrawTool() must be called inside DOMContentLoaded."""
    src = _read(APP_JS)
    m = re.search(
        r'addEventListener\("DOMContentLoaded"[\s\S]*?\n\}\);',
        src,
    )
    assert m
    body = m.group(0)
    assert "initRegionDrawTool()" in body


def test_pending_region_filter_applied_after_map_init():
    """Hash-restore path: _applyPendingRegionFilter runs post-initMap."""
    src = _read(APP_JS)
    assert "_applyPendingRegionFilter" in src
    assert "state.pendingRegionFilter" in src


def test_region_encoded_in_write_hash():
    """writeHash() must emit region= param when state.regionFilter is set."""
    src = _read(APP_JS)
    m = re.search(
        r"function writeHash\(\)([\s\S]*?)\n\}",
        src,
    )
    assert m
    body = m.group(1)
    assert "region" in body
    assert "_encodeRegionHash" in body


def test_region_decoded_in_apply_hash_to_filters():
    """applyHashToFilters() must parse region= param."""
    src = _read(APP_JS)
    m = re.search(
        r"function applyHashToFilters\(params\)([\s\S]*?)\n\}",
        src,
    )
    assert m
    body = m.group(1)
    assert '"region"' in body or "'region'" in body
    assert "_decodeRegionHash" in body


# =============================================================================
# deck.js integration — the hot loop already has bbox support, just
# verify nothing regressed it.
# =============================================================================

def test_deck_hot_loop_still_has_bbox_check():
    """Sanity: _rebuildVisible must still reference bbox filtering
    since the region filter depends on it."""
    src = _read(DECK_JS)
    # Look for the lat/lng range check in _rebuildVisible
    assert "south" in src and "north" in src and "west" in src and "east" in src


# =============================================================================
# v0.11.4 — TimeBrush region toggle button (on/off without clearing)
# =============================================================================

def test_brush_region_toggle_button_exists():
    html = _read(INDEX_HTML)
    assert 'id="brush-region-toggle"' in html
    assert 'class="brush-region-toggle"' in html


def test_brush_region_toggle_css():
    css = _read(STYLE_CSS)
    assert ".brush-region-toggle" in css
    # Disabled state via aria-pressed="false"
    assert '.brush-region-toggle[aria-pressed="false"]' in css


def test_toggle_region_filter_function():
    src = _read(APP_JS)
    assert "function toggleRegionFilter(" in src
    assert "function _syncRegionToggleUi(" in src


def test_region_active_flag_respected_in_pipeline():
    """bbox must only flow into the filter when _regionActive is true."""
    src = _read(APP_JS)
    # The bbox and regionShape fields should both check _regionActive
    # so toggling OFF bypasses the entire region filter without
    # clearing the drawn geometry.
    assert re.search(
        r"bbox:[\s\S]{0,200}_regionActive",
        src,
    ), "bbox field must AND-check _regionActive so toggle OFF bypasses the filter"
    assert re.search(
        r"regionShape:[\s\S]{0,200}_regionActive",
        src,
    ), "regionShape field must AND-check _regionActive too"


def test_region_chip_disabled_state_css():
    css = _read(STYLE_CSS)
    assert ".region-chip.is-disabled" in css


# =============================================================================
# v0.11.4 — TimeBrush histogram normalization
# =============================================================================

def test_timebrush_uses_independent_max_for_layers():
    """The histogram must compute separate max values for the
    ghost (unfiltered) and fg (filtered) layers so filtered bars
    normalize to their own peak instead of vanishing."""
    src = _read(APP_JS)
    # Both per-layer max values must exist
    assert "maxFull" in src
    assert "maxFiltered" in src


def test_timebrush_draw_layer_takes_max_param():
    """drawLayer helper must accept a per-layer max parameter."""
    src = _read(APP_JS)
    # Pattern: drawLayer arrow function signature includes maxVal (or similar)
    m = re.search(
        r"drawLayer\s*=\s*\([^)]*\bmaxVal\b[^)]*\)\s*=>",
        src,
    )
    assert m, "drawLayer must accept a maxVal parameter"


def test_timebrush_draw_layer_called_with_both_maxes():
    """Both drawLayer calls must pass a distinct max."""
    src = _read(APP_JS)
    assert "drawLayer(ghost," in src and "maxFull" in src
    assert "drawLayer(fg," in src and "maxFiltered" in src


# =============================================================================
# v0.11.5 — Polygon + Circle shape modes
# =============================================================================

def test_shape_mode_menu_in_html():
    html = _read(INDEX_HTML)
    assert 'id="region-mode-menu"' in html
    # All three shape options present as menu items (v0.11.6: ellipse replaces circle)
    assert 'data-region-mode="rect"' in html
    assert 'data-region-mode="polygon"' in html
    assert 'data-region-mode="ellipse"' in html


def test_region_draw_svg_overlay_exists():
    html = _read(INDEX_HTML)
    assert 'id="region-draw-svg"' in html
    assert 'id="region-draw-line"' in html
    assert 'id="region-draw-poly"' in html
    assert 'id="region-draw-ellipse"' in html
    assert 'id="region-draw-vertices"' in html


def test_shape_mode_menu_css():
    css = _read(STYLE_CSS)
    assert ".region-mode-menu" in css
    assert ".region-mode-item" in css
    assert ".region-draw-svg" in css
    assert ".region-draw-poly" in css
    assert ".region-draw-ellipse" in css
    assert ".region-draw-vertex" in css


def test_enter_region_draw_mode_accepts_mode():
    """_enterRegionDrawMode must accept a mode parameter."""
    src = _read(APP_JS)
    assert "function _enterRegionDrawMode(mode)" in src


def test_polygon_drawing_handlers():
    """v0.11.6: switched from click-based to pointerdown/up pattern
    because deck.gl's canvas click handler was eating the events."""
    src = _read(APP_JS)
    assert "function _regionPolyPointerDown(" in src
    assert "function _regionPolyPointerMove(" in src
    assert "function _regionPolyPointerUp(" in src
    assert "function _regionPolyDblclick(" in src
    assert "function _closePolygon(" in src


def test_polygon_vertex_dragging_supported():
    """v0.11.6: users can drag placed vertices to reposition them."""
    src = _read(APP_JS)
    assert "_polyDraggingVertex" in src


def test_ellipse_uses_same_pointer_handlers():
    """Ellipse shares the pointerdown/move/up drag flow with rect —
    just with different geometry in the update function."""
    src = _read(APP_JS)
    assert "function _updateDragEllipseVisual(" in src


def test_shape_bbox_computed_for_all_types():
    """_computeShapeBbox must handle rect, polygon, and ellipse."""
    src = _read(APP_JS)
    m = re.search(
        r"function _computeShapeBbox\([\s\S]*?\n\}",
        src,
    )
    assert m
    body = m.group(0)
    assert '"rect"' in body or "'rect'" in body
    assert '"polygon"' in body or "'polygon'" in body
    assert '"ellipse"' in body or "'ellipse'" in body


def test_deck_js_handles_polygon_and_ellipse():
    """_rebuildVisible in deck.js must include point-in-polygon
    and point-in-ellipse tests."""
    src = _read(DECK_JS)
    assert "regionShape" in src
    assert 'regionShape.type === "polygon"' in src or "'polygon'" in src
    assert 'regionShape.type === "ellipse"' in src or "'ellipse'" in src


def test_chip_label_varies_by_shape():
    """_renderRegionChip must produce different labels per shape."""
    src = _read(APP_JS)
    m = re.search(
        r"function _renderRegionChip\([\s\S]*?\n\}",
        src,
    )
    assert m
    body = m.group(0)
    # Each shape type should be handled
    assert '"ellipse"' in body or "'ellipse'" in body
    assert '"polygon"' in body or "'polygon'" in body


def test_hash_encodes_all_three_shape_types():
    src = _read(APP_JS)
    m = re.search(
        r"function _encodeRegionHash\([\s\S]*?\n\}",
        src,
    )
    assert m
    body = m.group(0)
    # v0.11.6: format sentinels must appear in the encoded output.
    # We check for the literal prefix strings as they appear in the code.
    assert "rect" in body
    assert "ellipse" in body
    assert "poly:" in body


def test_hash_decodes_all_three_shape_types():
    src = _read(APP_JS)
    m = re.search(
        r"function _decodeRegionHash\([\s\S]*?\n\}",
        src,
    )
    assert m
    body = m.group(0)
    assert "rect" in body and "ellipse" in body and "poly" in body
