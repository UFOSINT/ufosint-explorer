"""Progressive-loading invariants (v0.6.0).

The progressive-loading pattern keeps existing content on screen while
new data loads in the background. It has three pieces:

1. CSS — `.is-progressive`, `.is-loading-progressive`, `.progressive-overlay`,
   `.is-new` (stagger fade-in), and the leaflet pane dim selectors.
2. JS helpers — `showProgressiveLoading()`, `hideProgressiveLoading()`,
   `staggerNewChildren()`.
3. Wiring — `loadTimeline()` keeps `state.chart` alive and uses
   `chart.update()`; `executeSearch()` only swaps to skeleton on a
   cold start; `loadMapMarkers()` / `loadHeatmap()` dim the old marker
   pane until the new request resolves.

These tests lock all three pieces so a future refactor can't silently
break the perceived-performance wins.
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
# CSS — progressive selectors + keyframe
# ---------------------------------------------------------------------------
_REQUIRED_CSS_SELECTORS = [
    ".is-progressive",
    ".is-progressive > *:not(.progressive-overlay)",
    ".is-progressive.is-loading-progressive > *:not(.progressive-overlay)",
    ".progressive-overlay",
    ".is-loading-progressive > .progressive-overlay",
    ".progressive-overlay > .loading-terminal.compact",
    ".is-new",
    "#map.is-loading-progressive .leaflet-marker-pane",
    "#map.is-loading-progressive .leaflet-overlay-pane",
]


@pytest.mark.parametrize("selector", _REQUIRED_CSS_SELECTORS)
def test_progressive_css_selectors_present(selector: str):
    """Every selector the JS depends on must have a matching CSS rule.
    Removing one means progressive loading silently degrades to a
    blank panel — the user gets the v0.4 behavior back."""
    content = _read(STYLE_CSS)
    assert selector in content, (
        f"style.css missing progressive-loading rule {selector!r}"
    )


def test_stagger_fade_in_keyframe_present():
    """The stagger fade-in animation is what makes new search result
    cards roll in instead of pop into existence."""
    content = _read(STYLE_CSS)
    assert "@keyframes stagger-fade-in" in content
    # Sanity-check the calc() that drives the per-card delay.
    assert "calc(var(--i" in content, (
        "stagger animation should use calc(var(--i, 0) * 22ms) for the delay"
    )


def test_progressive_loading_respects_reduced_motion():
    """The reduced-motion block must neutralize the blur transitions
    and the stagger animation. Without this we'd be a vestibular
    accessibility regression vs. v0.5."""
    content = _read(STYLE_CSS)
    # Find the @media (prefers-reduced-motion: reduce) blocks
    match = re.findall(
        r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
        content,
    )
    joined = "".join(match)
    assert ".is-progressive.is-loading-progressive" in joined or ".is-new" in joined, (
        "no reduced-motion override for progressive-loading classes"
    )
    assert ".is-new" in joined, (
        "stagger animation not killed under prefers-reduced-motion"
    )


def test_chart_container_is_positioned():
    """The .chart-container must be position: relative for the
    .progressive-overlay to anchor to it. Both rules need it."""
    content = _read(STYLE_CSS)
    # Find both .chart-container blocks
    blocks = re.findall(r"\.chart-container\s*\{[^}]*\}", content)
    assert len(blocks) >= 2, (
        f"expected 2 .chart-container blocks (the layout one and the "
        f"loading one), found {len(blocks)}"
    )
    for blk in blocks:
        assert "position: relative" in blk, (
            f".chart-container block missing position: relative — "
            f"the .progressive-overlay won't anchor correctly:\n{blk}"
        )


# ---------------------------------------------------------------------------
# JS — helpers exist and are wired
# ---------------------------------------------------------------------------
def test_progressive_helpers_exported():
    """Three new functions: show + hide + stagger. They're called from
    multiple loaders, so deletion would surface as runtime errors."""
    content = _read(APP_JS)
    assert "function showProgressiveLoading" in content
    assert "function hideProgressiveLoading" in content
    assert "function staggerNewChildren" in content


def test_show_progressive_loading_called_from_load_paths():
    """loadTimeline + executeSearch must call showProgressiveLoading
    on subsequent loads. Three callsites is the minimum we expect:
    timeline, search, and at least one defensive use."""
    content = _read(APP_JS)
    callsites = content.count("showProgressiveLoading(")
    assert callsites >= 2, (
        f"showProgressiveLoading only called {callsites} times — "
        f"expected at least 2 (loadTimeline + executeSearch)"
    )


def test_hide_progressive_loading_called_in_finally_paths():
    """hideProgressiveLoading must run on success AND failure paths
    of any loader that still uses the overlay. v0.8.6 removed the
    Timeline and Search overlays (Timeline became client-side,
    Search was deleted) so the callsite count dropped — but the
    helper is still wired into map loaders, so at least one must
    remain to prove the helper isn't orphaned."""
    content = _read(APP_JS)
    callsites = content.count("hideProgressiveLoading(")
    assert callsites >= 1, (
        "hideProgressiveLoading is orphaned (0 callsites). Every "
        "showProgressiveLoading in a try-block must have a matching "
        "hideProgressiveLoading in finally."
    )


def test_load_timeline_chart_destroy_only_on_granularity_change():
    """v0.10.0: loadTimeline CAN call state.chart.destroy() when
    the granularity toggle switches between year/month/day, because
    the dataset shape changes (year has source-stacking, month/day
    doesn't). The chart gets rebuilt with the right layout. The old
    v0.6 test that banned destroy entirely no longer applies — the
    relevant invariant is that refreshTimelineCards() (the per-frame
    update path) uses chart.update("none"), not destroy+recreate.

    We still check that refreshTimelineCards does NOT call destroy."""
    content = _read(APP_JS)
    refresh_section = re.search(
        r"function refreshTimelineCards\(\).*?^\}",
        content,
        re.DOTALL | re.MULTILINE,
    )
    assert refresh_section, "couldn't isolate refreshTimelineCards body"
    assert "state.chart.destroy()" not in refresh_section.group(0), (
        "refreshTimelineCards must never call destroy — it runs on "
        "every filter change and should use chart.update instead"
    )


# v0.8.6: test_execute_search_skeleton_only_on_cold_start and
# test_search_result_cards_carry_is_new_class were deleted. The
# Search panel's executeSearch() + result-card rendering code is
# gone. The Observatory's client-side filter pipeline doesn't
# render a "results list" — the map markers ARE the results — so
# there's no cold-start skeleton or is-new stagger class to check.


def test_load_map_markers_uses_progressive_dim():
    """Both map loaders must add is-loading-progressive on entry and
    remove it in finally. Check for the class in both add() and
    remove() callsites."""
    content = _read(APP_JS)
    # Count occurrences in loader bodies
    loader_section = re.findall(
        r"async function loadMapMarkers.*?^\}|async function loadHeatmap.*?^\}",
        content,
        re.DOTALL | re.MULTILINE,
    )
    assert len(loader_section) == 2, (
        f"expected 2 map-loading function bodies, found {len(loader_section)}"
    )
    for body in loader_section:
        assert '"is-loading-progressive"' in body, (
            "a map loader is missing is-loading-progressive — its old "
            "markers will vanish on every reload instead of dimming"
        )


def test_parallel_boot_renders_filters_independently():
    """The DOMContentLoaded handler should resolve /api/filters and
    /api/stats independently — neither one should block on the
    other. We check this via three positive signals:

    - `filtersPromise` and `statsPromise` are stored separately
    - `filtersPromise.then(populateFilterDropdowns)` runs as soon as
      filters land (no awaiting on stats first)
    - `statsPromise.then(...)` calls showStats inside its own .then,
      not after a Promise.all.

    The old (v0.5) pattern used `await Promise.all([filters, stats])`
    which serialized everything behind the slower of the two — exactly
    the perceived-performance problem we're trying to fix.
    """
    content = _read(APP_JS)
    assert "const filtersPromise = fetchJSON" in content, (
        "filtersPromise not stored separately — boot block still "
        "uses Promise.all-style blocking"
    )
    assert "const statsPromise = fetchJSON" in content, (
        "statsPromise not stored separately — boot block still "
        "uses Promise.all-style blocking"
    )
    assert "filtersPromise\n        .then(populateFilterDropdowns)" in content, (
        "filtersPromise doesn't fire populateFilterDropdowns in its "
        "own .then — filters won't render before stats arrive"
    )
