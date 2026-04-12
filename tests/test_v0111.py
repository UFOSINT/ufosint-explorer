"""v0.11.1 — Playback smoothness + Data Quality gear popup.

Three improvements to the playback + cross-tab UX:

1. **Playback performance quick wins.** refreshInsightsClientCards
   and refreshTimelineCards now accept a `playback` boolean parameter.
   When true (passed from the TimeBrush step() throttle), they skip:
   - Coverage strip computation (_computeInsightsCoverage walks 396k rows)
   - Coverage strip DOM updates (_mountAllCoverageStrips)
   - Cross-filter setup (_getCrossFilteredIndices)
   This cuts per-frame JS from ~20ms to ~8ms.

2. **Data Quality gear popup on Timeline + Insights.** A small gear
   icon in each tab header opens a floating popover with the same 5
   quality toggles as the Observatory rail. State is shared — toggling
   in the popup updates the global qualityFilter and calls applyFilters.

3. **Playback progress bar.** A thin accent-colored strip at the
   bottom of the brush header shows how far through the dataset range
   the playback window has advanced.
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
# 1. Playback performance — playback parameter
# =============================================================================

def test_refresh_timeline_cards_accepts_playback_param():
    src = _read(APP_JS)
    assert "function refreshTimelineCards(playback)" in src, (
        "refreshTimelineCards must accept a playback parameter"
    )


def test_refresh_insights_accepts_playback_param():
    src = _read(APP_JS)
    assert "function refreshInsightsClientCards(playback)" in src, (
        "refreshInsightsClientCards must accept a playback parameter"
    )


def test_playback_skips_coverage_computation():
    """When playback=true, _computeInsightsCoverage should be skipped."""
    src = _read(APP_JS)
    # Find the refreshInsightsClientCards body
    m = re.search(
        r"function refreshInsightsClientCards\([^)]*\)([\s\S]*?)\n\}\n",
        src,
    )
    assert m
    body = m.group(1)
    assert "if (!playback)" in body, (
        "refreshInsightsClientCards must gate coverage computation "
        "behind !playback"
    )


def test_playback_skips_coverage_strips():
    """During playback, _mountAllCoverageStrips should not be called."""
    src = _read(APP_JS)
    m = re.search(
        r"function refreshInsightsClientCards\([^)]*\)([\s\S]*?)\n\}\n",
        src,
    )
    assert m
    body = m.group(1)
    # _mountAllCoverageStrips should be inside a !playback guard
    idx_strips = body.find("_mountAllCoverageStrips")
    assert idx_strips > 0
    # There should be a !playback check before the call
    preceding = body[:idx_strips]
    assert "!playback" in preceding[-100:], (
        "_mountAllCoverageStrips must be gated behind !playback"
    )


def test_throttle_passes_playback_true():
    """The playback step throttle must pass true to the refresh fns."""
    src = _read(APP_JS)
    assert "refreshTimelineCards(true)" in src, (
        "playback throttle must call refreshTimelineCards(true)"
    )
    assert "refreshInsightsClientCards(true)" in src, (
        "playback throttle must call refreshInsightsClientCards(true)"
    )


# =============================================================================
# 2. Data Quality gear popup
# =============================================================================

def test_dq_gear_btn_exists_in_timeline():
    html = _read(INDEX_HTML)
    assert 'id="timeline-dq-gear"' in html, (
        "Timeline header must have a Data Quality gear button"
    )


def test_dq_gear_btn_exists_in_insights():
    html = _read(INDEX_HTML)
    assert 'id="insights-dq-gear"' in html, (
        "Insights header must have a Data Quality gear button"
    )


def test_dq_gear_popup_exists_in_timeline():
    html = _read(INDEX_HTML)
    assert 'id="timeline-dq-popup"' in html
    assert 'id="timeline-dq-list"' in html


def test_dq_gear_popup_exists_in_insights():
    html = _read(INDEX_HTML)
    assert 'id="insights-dq-popup"' in html
    assert 'id="insights-dq-list"' in html


def test_mount_dq_gear_popup_function():
    src = _read(APP_JS)
    assert "function _mountDqGearPopup(" in src


def test_populate_dq_list_function():
    src = _read(APP_JS)
    assert "function _populateDqList(" in src


def test_sync_dq_gear_badges_function():
    src = _read(APP_JS)
    assert "function _syncDqGearBadges(" in src


def test_dq_gear_wired_in_load_timeline():
    src = _read(APP_JS)
    # loadTimeline must call _mountDqGearPopup for the timeline gear
    m = re.search(r"async function loadTimeline\(\)([\s\S]*?)\n\}\n", src)
    assert m
    body = m.group(1)
    assert "_mountDqGearPopup" in body, (
        "loadTimeline must mount the DQ gear popup"
    )


def test_dq_gear_wired_in_load_insights():
    src = _read(APP_JS)
    m = re.search(r"async function loadInsights\(\)([\s\S]*?)\n\}\n", src)
    assert m
    body = m.group(1)
    assert "_mountDqGearPopup" in body, (
        "loadInsights must mount the DQ gear popup"
    )


def test_css_dq_gear_rules():
    css = _read(STYLE_CSS)
    for cls in (".dq-gear-wrap", ".dq-gear-btn", ".dq-gear-popup",
                ".dq-gear-title", ".dq-gear-list", ".dq-gear-badge"):
        assert cls in css, f"style.css missing {cls!r}"


# =============================================================================
# 3. Playback progress bar
# =============================================================================

def test_progress_bar_exists_in_html():
    html = _read(INDEX_HTML)
    assert 'id="brush-play-progress"' in html, (
        "brush header must have a playback progress bar"
    )
    assert "brush-play-progress-fill" in html


def test_progress_bar_css_rules():
    css = _read(STYLE_CSS)
    assert ".brush-play-progress" in css
    assert ".brush-play-progress-fill" in css


def test_progress_bar_shown_on_play():
    """togglePlay must show the progress bar when starting playback."""
    src = _read(APP_JS)
    assert "brush-play-progress" in src
    # The progress bar should be shown (hidden = false) on play start
    assert "progressEl.hidden = false" in src or "progressEl.hidden=false" in src


def test_progress_bar_hidden_on_stop():
    """togglePlay must hide the progress bar when stopping."""
    src = _read(APP_JS)
    assert "progressEl.hidden = true" in src or "progressEl) progressEl.hidden = true" in src
