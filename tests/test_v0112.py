"""v0.11.2 — Cinematic landing + guided tooltip tour.

Three interconnected features for the first-visit experience:

1. **Cinematic intro overlay** — full-screen dark overlay with a
   terminal-style counter ticking up to the total sighting count,
   status messages cycling, then dissolving to reveal the map.

2. **Guided tooltip tour** — 5-step spotlight walkthrough: map,
   rail, TimeBrush, tabs, stats badge. Uses clip-path cutout on a
   backdrop + positioned tooltip with arrow.

3. **Help button** — persistent `?` icon in the header nav. Replays
   the tour (without the cinematic) on click.
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
# HTML Structure
# =============================================================================

def test_intro_overlay_exists():
    html = _read(INDEX_HTML)
    assert 'id="intro-overlay"' in html
    assert 'class="intro-overlay"' in html


def test_intro_counter_value_element():
    html = _read(INDEX_HTML)
    assert 'id="intro-counter-value"' in html


def test_intro_status_element():
    html = _read(INDEX_HTML)
    assert 'id="intro-status"' in html


def test_tour_backdrop_exists():
    html = _read(INDEX_HTML)
    assert 'id="tour-backdrop"' in html
    assert 'class="tour-backdrop"' in html


def test_tour_tooltip_exists():
    html = _read(INDEX_HTML)
    assert 'id="tour-tooltip"' in html
    assert 'role="dialog"' in html


def test_tour_skip_and_next_buttons():
    html = _read(INDEX_HTML)
    assert 'id="tour-skip"' in html
    assert 'id="tour-next"' in html


def test_help_tour_button_exists():
    html = _read(INDEX_HTML)
    assert 'id="help-tour-btn"' in html
    assert 'aria-label="Replay feature tour"' in html


def test_intro_overlay_before_modal():
    """The intro overlay must come before the modal overlay in DOM
    order so it renders on top during the boot sequence."""
    html = _read(INDEX_HTML)
    intro_pos = html.find('id="intro-overlay"')
    modal_pos = html.find('id="modal-overlay"')
    assert intro_pos > 0 and modal_pos > 0
    assert intro_pos < modal_pos, (
        "intro-overlay must appear before modal-overlay in the DOM"
    )


def test_help_button_before_settings():
    """The help button should appear before the settings gear in
    the nav so it's visually to the left."""
    html = _read(INDEX_HTML)
    help_pos = html.find('id="help-tour-btn"')
    settings_pos = html.find('id="settings-btn"')
    assert help_pos > 0 and settings_pos > 0
    assert help_pos < settings_pos


# =============================================================================
# CSS Rules
# =============================================================================

def test_intro_overlay_css():
    css = _read(STYLE_CSS)
    assert ".intro-overlay" in css
    assert "z-index: 10000" in css


def test_intro_dissolving_css():
    css = _read(STYLE_CSS)
    assert ".intro-overlay.intro-dissolving" in css
    assert "opacity: 0" in css


def test_intro_done_css():
    css = _read(STYLE_CSS)
    assert ".intro-overlay.intro-done" in css
    assert "display: none" in css


def test_tour_backdrop_css():
    css = _read(STYLE_CSS)
    assert ".tour-backdrop" in css
    assert "z-index: 10001" in css


def test_tour_tooltip_css():
    css = _read(STYLE_CSS)
    assert ".tour-tooltip" in css
    assert "z-index: 10002" in css


def test_reduced_motion_respected():
    css = _read(STYLE_CSS)
    assert "prefers-reduced-motion" in css
    # Should reference intro or tour classes
    m = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)([\s\S]*?\})",
        css,
    )
    assert m, "must have a prefers-reduced-motion media query"


def test_tour_tooltip_arrow_positions():
    css = _read(STYLE_CSS)
    for pos in ("bottom", "top", "left", "right"):
        assert f'[data-position="{pos}"]' in css, (
            f"tour tooltip arrow must support data-position={pos}"
        )


def test_intro_uses_theme_vars():
    css = _read(STYLE_CSS)
    # .intro-overlay should use var(--bg)
    m = re.search(r"\.intro-overlay\s*\{([^}]+)\}", css)
    assert m
    assert "var(--bg)" in m.group(1)


def test_tour_tooltip_uses_theme_vars():
    css = _read(STYLE_CSS)
    m = re.search(r"\.tour-tooltip\s*\{([^}]+)\}", css)
    assert m
    body = m.group(1)
    assert "var(--surface-1)" in body or "var(--bg-panel)" in body
    assert "var(--accent)" in body


# =============================================================================
# JavaScript Functions
# =============================================================================

def test_tour_storage_key_defined():
    src = _read(APP_JS)
    assert "TOUR_STORAGE_KEY" in src
    assert '"ufosint-intro-seen"' in src


def test_tour_steps_array():
    src = _read(APP_JS)
    assert "TOUR_STEPS" in src
    # Must have 5 target selectors
    for target in (
        ".observatory-canvas-wrap",
        ".observatory-rail",
        ".observatory-time-brush",
        ".tabs",
        "#stats-badge",
    ):
        assert f'target: "{target}"' in src, (
            f"TOUR_STEPS missing target: {target}"
        )


def test_run_cinematic_intro_function():
    src = _read(APP_JS)
    assert "function runCinematicIntro(" in src


def test_skip_cinematic_intro_function():
    src = _read(APP_JS)
    assert "function skipCinematicIntro(" in src


def test_start_tour_function():
    src = _read(APP_JS)
    assert "function startTour(" in src


def test_show_tour_step_function():
    src = _read(APP_JS)
    assert "function _showTourStep(" in src


def test_advance_tour_function():
    src = _read(APP_JS)
    assert "function _advanceTour(" in src


def test_end_tour_function():
    src = _read(APP_JS)
    assert "function _endTour(" in src


def test_tour_escape_handler():
    src = _read(APP_JS)
    assert "function _tourEscapeHandler(" in src
    # Must check for Escape key
    m = re.search(
        r"function _tourEscapeHandler\([^)]*\)([\s\S]*?)\}",
        src,
    )
    assert m
    assert '"Escape"' in m.group(1) or "'Escape'" in m.group(1)


def test_init_help_tour_button_function():
    src = _read(APP_JS)
    assert "function initHelpTourButton(" in src


def test_help_button_calls_start_tour():
    src = _read(APP_JS)
    m = re.search(
        r"function initHelpTourButton\([^)]*\)([\s\S]*?)\n\}",
        src,
    )
    assert m
    body = m.group(1)
    assert "startTour(true)" in body, (
        "help button must call startTour(true) to skip cinematic"
    )


# =============================================================================
# Boot Integration
# =============================================================================

def test_localstorage_check_in_boot():
    """The DOMContentLoaded handler must check localStorage for the
    tour key and call startTour or skipCinematicIntro."""
    src = _read(APP_JS)
    m = re.search(
        r'addEventListener\("DOMContentLoaded"[\s\S]*?\n\}\);',
        src,
    )
    assert m, "couldn't locate DOMContentLoaded handler"
    body = m.group(0)
    assert "TOUR_STORAGE_KEY" in body
    assert "startTour" in body
    assert "skipCinematicIntro" in body


def test_help_button_wired_in_boot():
    src = _read(APP_JS)
    m = re.search(
        r'addEventListener\("DOMContentLoaded"[\s\S]*?\n\}\);',
        src,
    )
    assert m
    body = m.group(0)
    assert "initHelpTourButton()" in body


def test_stats_data_stashed_on_state():
    """Stats data must be stored on state so the tour can read
    mapped_sightings for the tooltip text."""
    src = _read(APP_JS)
    assert "state.statsData = statsData" in src


# =============================================================================
# Clip-path spotlight
# =============================================================================

def test_clip_path_used_for_spotlight():
    """The tour must use clip-path to cut a hole in the backdrop
    around the highlighted element."""
    src = _read(APP_JS)
    assert "clipPath" in src
    assert "polygon" in src


def test_position_tooltip_function():
    src = _read(APP_JS)
    assert "function _positionTooltip(" in src
