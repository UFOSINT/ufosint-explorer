"""Hackery loading-system invariants.

The v0.5.0 visual-polish pass added a terminal-themed loading system:
blinking cursor, cycling hacker messages, scanline shimmer, radar sweep
on the map. These tests lock the contract between CSS, HTML, and JS so
a future refactor can't silently break one side of the system.

What we're guarding against:
- Someone drops the .loading-terminal component CSS but leaves the JS
  helper calling it → loading states render as unstyled text.
- Someone removes the TERMINAL_MESSAGE_BANKS table → mountLoadingTerminal
  throws at runtime.
- Someone strips the .term-cursor / .term-msg / .term-prompt hooks that
  the JS depends on → cursor stops blinking, typewriter stops working.
- Someone removes the stats-badge boot sequence from index.html → the
  stats badge goes back to a plain "Loading…".
- Someone removes prefers-reduced-motion fallbacks → accessibility
  regression.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "static" / "index.html"
APP_JS = ROOT / "static" / "app.js"
STYLE_CSS = ROOT / "static" / "style.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CSS side — design tokens + component rules
# ---------------------------------------------------------------------------
_REQUIRED_CSS_TOKENS = [
    "--term-green",
    "--term-amber",
    "--term-cyan",
    "--term-glow",
    "--term-scan",
    "--term-bg",
    "--term-border",
]


@pytest.mark.parametrize("token", _REQUIRED_CSS_TOKENS)
def test_terminal_design_tokens_exist(token: str):
    """The terminal palette tokens are referenced by the component CSS
    and by custom JS strings. Dropping one silently breaks colors."""
    content = _read(STYLE_CSS)
    assert f"{token}:" in content, (
        f"style.css missing terminal design token {token!r}"
    )


_REQUIRED_CSS_SELECTORS = [
    ".loading-terminal",
    ".loading-terminal .term-header",
    ".loading-terminal .term-line",
    ".loading-terminal .term-prompt",
    ".loading-terminal .term-msg",
    ".loading-terminal .term-cursor",
    ".loading-terminal .term-progress",
    ".loading-terminal.compact",
    ".stats-badge-boot",
    ".map-scanframe",
    ".map-scanframe > .mscf-tl",
    ".map-scanframe > .mscf-tr",
    ".map-scanframe > .mscf-bl",
    ".map-scanframe > .mscf-br",
    ".map-scanframe-label",
]


@pytest.mark.parametrize("selector", _REQUIRED_CSS_SELECTORS)
def test_loading_system_selectors_present(selector: str):
    """Every selector the JS helpers render into the DOM must have a
    matching CSS rule. A missing rule means the element falls back to
    user-agent defaults, which looks broken next to the rest of the
    theme."""
    content = _read(STYLE_CSS)
    assert selector in content, (
        f"style.css is missing CSS rule for {selector!r}. The JS "
        f"helper in app.js emits this class and expects it to be styled."
    )


_REQUIRED_KEYFRAMES = [
    "@keyframes hack-pulse",
    "@keyframes hack-glitch",
    "@keyframes term-scan-drift",
    "@keyframes term-pulse",
    "@keyframes term-cursor-blink",
    "@keyframes term-typewriter",
    "@keyframes term-progress-slide",
    "@keyframes skeleton-scan",
    "@keyframes map-radar-spin",
    "@keyframes map-loading-slide",
]


@pytest.mark.parametrize("kf", _REQUIRED_KEYFRAMES)
def test_loading_system_keyframes_present(kf: str):
    """All the animations that make the loading feel alive. Missing
    one usually means the animation name got renamed without updating
    every `animation:` property that references it — the animation
    just silently stops running."""
    content = _read(STYLE_CSS)
    assert kf in content, f"style.css missing {kf!r}"


def test_reduced_motion_fallback_present():
    """prefers-reduced-motion must freeze the big animations.
    Accessibility regression guard — vestibular users will see the
    glitch and scanline otherwise."""
    content = _read(STYLE_CSS)
    assert "prefers-reduced-motion" in content, (
        "style.css missing @media (prefers-reduced-motion: reduce) block"
    )
    # Find the reduced-motion block and check it kills at least the
    # heavy animations (loading-terminal scan, skeleton scan).
    match = re.search(
        r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{([^}]*\{[^}]*\}\s*)+\}",
        content,
        re.DOTALL,
    )
    assert match, "couldn't parse a prefers-reduced-motion block"
    block = match.group(0)
    assert ".loading-terminal::after" in block, (
        "reduced-motion block doesn't neutralize the drifting scanline"
    )
    assert ".result-card.skeleton::after" in block or ".detail-skeleton::after" in block, (
        "reduced-motion block doesn't neutralize the skeleton scan pass"
    )


# ---------------------------------------------------------------------------
# HTML side — boot sequence skeleton
# ---------------------------------------------------------------------------
def test_stats_badge_has_boot_sequence_markup():
    """The stats badge must start in its boot state so the user sees
    the terminal effect from the very first paint. If this reverts to
    plain 'Loading…', the boot sequence never runs."""
    content = _read(INDEX_HTML)
    assert 'class="stats-badge-boot"' in content
    assert 'class="stats-boot-msg"' in content
    assert 'class="term-prompt"' in content
    assert 'class="term-cursor"' in content


def test_map_status_uses_loading_pulse_during_loads():
    """The map-status pill should animate via .loading-pulse while a
    load is in flight. v0.7 initial HTML can be "READY" (static text)
    because the Observatory only shows a pulse during active loads, so
    instead of checking the static HTML we check that app.js writes
    the pulse class into map-status from both loadMapMarkers and
    loadHeatmap."""
    content = _read(APP_JS)
    assert content.count('loading-pulse') >= 2, (
        "app.js no longer writes the loading-pulse class into map-status "
        "from at least two loader functions"
    )
    # Both loaders should write a status message
    assert "map-status" in content, "map-status element is no longer referenced in app.js"


# ---------------------------------------------------------------------------
# JS side — helper + message banks + cycling wiring
# ---------------------------------------------------------------------------
def test_app_js_exports_loading_terminal_helper():
    """mountLoadingTerminal must exist and be referenced from at least
    one real loading site (search, duplicates, insights, etc.)."""
    content = _read(APP_JS)
    assert "function mountLoadingTerminal" in content, (
        "mountLoadingTerminal helper removed from app.js"
    )
    assert "function unmountLoadingTerminal" in content, (
        "unmountLoadingTerminal helper removed from app.js"
    )
    # At least 3 real callsites — search, duplicates, insights.
    callsites = content.count("mountLoadingTerminal(")
    assert callsites >= 3, (
        f"mountLoadingTerminal only referenced {callsites} times — "
        f"expected to see it called from search, duplicates, insights"
    )


_REQUIRED_MESSAGE_BANKS = [
    "generic",
    "search",
    "map",
    "timeline",
    "duplicates",
    "insights",
    "boot",
]


@pytest.mark.parametrize("bank", _REQUIRED_MESSAGE_BANKS)
def test_terminal_message_bank_exists(bank: str):
    """Each loading site asks for a named message bank. If a bank is
    removed, mountLoadingTerminal falls back to 'generic' — the user
    just sees the wrong words, which looks confused rather than
    broken. Catch that in CI instead."""
    content = _read(APP_JS)
    # Match `bank:` or `"bank":` as an object key inside TERMINAL_MESSAGE_BANKS
    assert re.search(rf'\b{bank}\s*:\s*\[', content), (
        f"TERMINAL_MESSAGE_BANKS missing {bank!r} bank"
    )


def test_map_scanframe_helper_wired_into_map_loaders():
    """ensureMapScanframe/clearMapScanframe must be called from both
    loadMapMarkers and loadHeatmap so the HUD brackets show up
    consistently regardless of mode."""
    content = _read(APP_JS)
    assert "function ensureMapScanframe" in content
    assert "function clearMapScanframe" in content
    # Both loaders call ensureMapScanframe
    assert content.count("ensureMapScanframe(") >= 2, (
        "ensureMapScanframe should be called from both loadMapMarkers "
        "and loadHeatmap"
    )
    assert content.count("clearMapScanframe(") >= 2, (
        "clearMapScanframe should be called from both loadMapMarkers "
        "and loadHeatmap finally blocks"
    )


def test_stats_badge_boot_cycle_wired():
    """startStatsBadgeBoot must be called before the /api/stats fetch
    and stopped right after showStats() runs. If that pairing breaks,
    the boot sequence either never starts or never stops."""
    content = _read(APP_JS)
    assert "function startStatsBadgeBoot" in content
    assert "startStatsBadgeBoot()" in content
    # The cycle has to stop before showStats replaces the innerHTML,
    # otherwise we're churning .stats-boot-msg that no longer exists.
    # Look for the _stopBadgeBoot() call in the DOMContentLoaded path.
    assert "_stopBadgeBoot" in content, (
        "boot cycle handle not stored/called in DOMContentLoaded"
    )
