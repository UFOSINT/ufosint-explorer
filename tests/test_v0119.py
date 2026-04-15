"""v0.11.9 — Observatory Live Analytics sidebar.

Replaces the old dead Sources/Shapes filter sections in the
Observatory left sidebar with a live dashboard of what's currently
visible. Data Quality controls move to a gear-icon popup matching
the pattern already used on Timeline + Insights.

Sections in the new sidebar:
- Visible count + % of total
- Top Shapes (horizontal bar chart)
- By Source (stacked bar + per-source bars)
- Quality Score Distribution (10-bucket histogram)
- Time window

All render client-side from POINTS.visibleIdx via
refreshRailAnalytics(), called by applyClientFilters().
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


# =============================================================================
# HTML: DQ gear + sidebar structure
# =============================================================================

def test_observatory_dq_gear_button_exists():
    html = _read(INDEX_HTML)
    assert 'id="observatory-dq-gear"' in html
    assert 'id="observatory-dq-popup"' in html
    assert 'id="observatory-dq-list"' in html


def test_sidebar_has_visible_count():
    html = _read(INDEX_HTML)
    assert 'id="rail-visible-count"' in html
    assert 'id="rail-visible-pct"' in html  # new in v0.11.9


def test_sidebar_has_top_shapes_chart():
    html = _read(INDEX_HTML)
    assert 'id="rail-shapes-chart"' in html


def test_sidebar_has_sources_stacked_bar():
    html = _read(INDEX_HTML)
    assert 'id="rail-sources-stacked"' in html
    assert 'id="rail-sources-chart"' in html


def test_sidebar_has_quality_histogram():
    html = _read(INDEX_HTML)
    assert 'id="rail-quality-histogram"' in html


def test_sidebar_keeps_time_label():
    html = _read(INDEX_HTML)
    assert 'id="rail-time-label"' in html


def test_old_dead_sections_removed():
    """The old Sources and Shapes filter lists should be gone from
    the rail. rail-quality-list stays (hidden) so mountQualityRail
    can still render into it for shared state."""
    html = _read(INDEX_HTML)
    assert 'id="rail-source-list"' not in html
    assert 'id="rail-shape-list"' not in html


# =============================================================================
# CSS for analytics visualizations
# =============================================================================

def test_css_rail_analytics_rules():
    css = _read(STYLE_CSS)
    assert ".rail-section.rail-analytics" in css
    assert ".rail-mini-chart" in css
    assert ".rail-mini-chart-fill" in css
    assert ".rail-mini-chart-count" in css


def test_css_stacked_bar_rules():
    css = _read(STYLE_CSS)
    assert ".rail-stacked-bar" in css
    assert ".rail-stacked-bar-seg" in css


def test_css_histogram_rules():
    css = _read(STYLE_CSS)
    assert ".rail-histogram" in css
    assert ".rail-histogram-bar" in css
    assert ".rail-histogram-scale" in css


def test_css_sidebar_source_color_bindings():
    """Each known source gets its categorical color on the mini
    bar chart via a data-src attribute selector."""
    css = _read(STYLE_CSS)
    # At least the main three sources
    for src in ("nuforc", "mufon", "ufocat"):
        assert f'[data-src="{src}"]' in css


# =============================================================================
# JavaScript: refresh function + chart helpers
# =============================================================================

def test_refresh_rail_analytics_function():
    src = _read(APP_JS)
    assert "function refreshRailAnalytics(" in src


def test_render_helpers_exist():
    src = _read(APP_JS)
    assert "function _renderRailChart(" in src
    assert "function _renderRailStackedBar(" in src
    assert "function _renderRailHistogram(" in src


def test_init_observatory_dq_gear_function():
    src = _read(APP_JS)
    assert "function initObservatoryDqGear(" in src


def test_analytics_wired_into_apply_client_filters():
    """refreshRailAnalytics must be called from applyClientFilters
    so the sidebar updates on every filter change."""
    src = _read(APP_JS)
    m = re.search(
        r"function applyClientFilters\(\)([\s\S]*?)\n\}",
        src,
    )
    assert m
    body = m.group(1)
    assert "refreshRailAnalytics" in body


def test_analytics_wired_into_load_observatory():
    """loadObservatory must call initObservatoryDqGear (once) and
    refreshRailAnalytics (on every visit)."""
    src = _read(APP_JS)
    m = re.search(
        r"function loadObservatory\(\)([\s\S]*?)\n\}",
        src,
    )
    assert m
    body = m.group(1)
    assert "initObservatoryDqGear" in body
    assert "refreshRailAnalytics" in body


def test_rail_analytics_aggregates_shapes_sources_quality():
    """The refresh function must iterate visibleIdx and compute
    shape, source, and quality buckets in a single pass."""
    src = _read(APP_JS)
    m = re.search(
        r"function refreshRailAnalytics\(\)([\s\S]*?)\n\}",
        src,
    )
    assert m
    body = m.group(1)
    assert "shapeCounts" in body
    assert "sourceCounts" in body
    assert "qualBuckets" in body
    assert "visibleIdx" in body


def test_source_colors_match_categorical_palette():
    """Source colors used by the stacked bar should match the
    --cat-N palette values from the main theme (desaturated
    Tableau-10). The hex values are defined inline in app.js."""
    src = _read(APP_JS)
    assert "_RAIL_SOURCE_COLORS" in src
    # The palette should include the canonical UFOCAT blue
    assert "#4e79a7" in src
