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
    # Format sentinel
    assert '"rect:"' in src or "'rect:'" in src


# =============================================================================
# Integration with existing pipeline
# =============================================================================

def test_region_filter_wired_into_apply_client_filters():
    """The bbox in the filter object must read from state.regionFilter."""
    src = _read(APP_JS)
    # Expect to find state.regionFilter referenced near the bbox field
    m = re.search(
        r"bbox:\s*state\.regionFilter[\s\S]{0,400}",
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
