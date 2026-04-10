"""v0.7 — Observatory + HexBin + theme toggle + 504 fix.

These tests lock the v0.7 contract so future refactors can't silently
revert the Observatory redesign, re-introduce the 504, or drop the
theme toggle. See docs/ARCHITECTURE.md section on the Observatory
for how the pieces fit together.

What's covered:
- duplicate_candidate indexes exist in both the migration file and
  the canonical pg_schema.sql
- /api/sighting has the @cache.cached decorator (prevents a revert
  that would bring back the 504 on cold cache)
- /api/hexbin route is registered
- Observatory panel + tab button present in index.html
- SIGNAL + DECLASS theme CSS blocks exist and define --accent
- Theme toggle markup present in the gear menu
- Theme pre-paint script present in <head>
- Time brush markup present (canvas + window + annotations)
- key_sightings.json is valid JSON with the expected annotations
- loadObservatory / loadHexBins / TimeBrush class exist in app.js
- Observatory stage CSS rules exist
- Progressive-overlay cleanup: no showProgressiveLoading call on the
  search results container
- Rotating radar sweep is NO LONGER attached to any element
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"
PG_SCHEMA = ROOT / "scripts" / "pg_schema.sql"
V07_INDEXES = ROOT / "scripts" / "add_v07_indexes.sql"
KEY_SIGHTINGS = ROOT / "static" / "data" / "key_sightings.json"
COMPUTE_HEX_BINS = ROOT / "scripts" / "compute_hex_bins.py"
REQUIREMENTS_DEPLOY = ROOT / "requirements-deploy.txt"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 504 fix: indexes + cache decorator
# ---------------------------------------------------------------------------

def test_v07_indexes_sql_exists():
    assert V07_INDEXES.exists(), (
        "scripts/add_v07_indexes.sql is missing — the deploy workflow "
        "needs this file to run CREATE INDEX IF NOT EXISTS on every deploy"
    )


def test_v07_indexes_sql_contains_both_duplicate_indexes():
    content = _read(V07_INDEXES)
    assert "idx_duplicate_a" in content
    assert "idx_duplicate_b" in content
    # Must be IF NOT EXISTS so re-running is safe
    assert content.count("IF NOT EXISTS") >= 2, (
        "indexes must use IF NOT EXISTS so the migration is idempotent"
    )


def test_pg_schema_also_declares_duplicate_indexes():
    """Fresh installs read pg_schema.sql directly; the v0.7 indexes
    must live there too so a new deployment gets them."""
    content = _read(PG_SCHEMA)
    assert "idx_duplicate_a" in content
    assert "idx_duplicate_b" in content


def test_api_sighting_is_cached():
    """The /api/sighting/<id> route needs @cache.cached to survive a
    cold worker. Before v0.7 it had none, and cold queries against
    duplicate_candidate timed out with 504."""
    content = _read(APP_PY)
    # Look for the decorator directly above the api_sighting definition
    match = re.search(
        r"(@cache\.cached[^\n]*\n)+\s*def api_sighting\(",
        content,
    )
    assert match, (
        "def api_sighting is missing its @cache.cached decorator — "
        "the 504 fix will regress under load"
    )


def test_api_sighting_duplicate_query_uses_union_all():
    """The UNION ALL rewrite lets the planner use idx_duplicate_a and
    idx_duplicate_b independently. A revert to the OR pattern would
    bring back the 504 even with indexes present."""
    content = _read(APP_PY)
    # Extract the api_sighting function body (rough regex)
    match = re.search(
        r"def api_sighting\(sid\):.*?(?=^\w|\Z)",
        content,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "couldn't find api_sighting function body"
    body = match.group(0)
    # Must contain a UNION ALL and must NOT contain an OR that ANDs sighting_id_a + sighting_id_b
    assert "UNION ALL" in body, (
        "api_sighting duplicate query no longer uses UNION ALL"
    )
    assert "OR dc.sighting_id_b" not in body and "OR sighting_id_b" not in body, (
        "api_sighting duplicate query reintroduced the OR pattern — "
        "this defeats the idx_duplicate_a/b indexes"
    )


# ---------------------------------------------------------------------------
# /api/hexbin endpoint
# ---------------------------------------------------------------------------

def test_api_hexbin_route_registered(flask_app):
    rules = {r.rule for r in flask_app.url_map.iter_rules()}
    assert "/api/hexbin" in rules, "/api/hexbin route is not registered"


def test_api_hexbin_handles_missing_mv(client):
    """When the hex_bin_counts MV hasn't been populated yet (fresh
    deploy), the endpoint should return 503 with an error JSON so the
    client can disable the HexBin toggle gracefully instead of seeing
    a raw 500."""
    # We don't hit a real DB here — the tests stub the pool. The route
    # will raise trying to query, catch it, and return 503. What we're
    # asserting is that it does NOT return 500 (the old behavior).
    resp = client.get("/api/hexbin?zoom=4")
    assert resp.status_code in (200, 503), (
        f"/api/hexbin returned {resp.status_code} on a stubbed DB — "
        f"should be 503 (no data) or 200 (success), never 500"
    )


def test_zoom_to_res_mapping():
    """The zoom→resolution helper must cover all Leaflet zoom levels."""
    import importlib
    app_mod = importlib.import_module("app")
    z2r = app_mod._zoom_to_res
    assert z2r(0) == 2
    assert z2r(3) == 2
    assert z2r(4) == 3
    assert z2r(5) == 3
    assert z2r(6) == 4
    assert z2r(7) == 4
    assert z2r(8) == 5
    assert z2r(9) == 5
    assert z2r(10) == 6
    assert z2r(18) == 6


# ---------------------------------------------------------------------------
# Hex-bin deployment artifacts
# ---------------------------------------------------------------------------

def test_compute_hex_bins_script_exists():
    assert COMPUTE_HEX_BINS.exists()


def test_compute_hex_bins_creates_expected_ddl():
    content = _read(COMPUTE_HEX_BINS)
    # Must build location_hex + hex_bin_counts
    assert "CREATE TABLE IF NOT EXISTS location_hex" in content
    assert "CREATE MATERIALIZED VIEW hex_bin_counts" in content
    # All 5 resolutions
    for res in ("res_2", "res_3", "res_4", "res_5", "res_6"):
        assert res in content, f"location_hex missing {res} column"


def test_requirements_deploy_exists_and_pins_h3():
    assert REQUIREMENTS_DEPLOY.exists()
    content = _read(REQUIREMENTS_DEPLOY)
    assert "h3" in content.lower()
    # Must inherit from runtime requirements
    assert "-r requirements.txt" in content


def test_compute_hex_bins_workflow_exists():
    p = ROOT / ".github" / "workflows" / "compute-hex-bins.yml"
    assert p.exists(), "compute-hex-bins.yml workflow is missing"
    content = _read(p)
    assert "workflow_dispatch" in content, (
        "compute-hex-bins.yml must be workflow_dispatch only to avoid "
        "running on every push"
    )
    assert "compute_hex_bins.py" in content


def test_refresh_hex_bins_workflow_exists():
    p = ROOT / ".github" / "workflows" / "refresh-hex-bins.yml"
    assert p.exists(), "refresh-hex-bins.yml workflow is missing"
    content = _read(p)
    assert "REFRESH MATERIALIZED VIEW hex_bin_counts" in content


# ---------------------------------------------------------------------------
# Observatory tab — HTML + CSS + JS
# ---------------------------------------------------------------------------

def test_index_html_has_observatory_tab_and_panel():
    content = _read(INDEX_HTML)
    assert 'data-tab="observatory"' in content, "Observatory tab button missing"
    assert 'id="panel-observatory"' in content, "Observatory panel missing"
    # Legacy Map + Timeline tabs are hidden, not deleted
    assert 'data-tab="map"' in content and "hidden" in content.split('data-tab="map"')[1][:40]


def test_observatory_has_rail_and_topbar():
    content = _read(INDEX_HTML)
    assert 'id="rail-source-list"' in content
    assert 'id="rail-shape-list"' in content
    assert 'id="rail-visible-count"' in content
    assert 'class="mode-toggle"' in content
    # Three render modes
    assert 'data-mode="points"' in content
    assert 'data-mode="heatmap"' in content
    assert 'data-mode="hexbin"' in content


def test_observatory_time_brush_markup():
    content = _read(INDEX_HTML)
    assert 'id="brush-canvas"' in content, "brush canvas missing"
    assert 'id="brush-window"' in content
    assert 'id="brush-play"' in content
    assert 'id="brush-reset"' in content
    assert 'id="brush-annotations"' in content


def test_observatory_css_rules_present():
    content = _read(STYLE_CSS)
    assert ".observatory-stage" in content
    assert ".observatory-rail" in content
    assert ".observatory-canvas-wrap" in content
    assert ".observatory-time-brush" in content
    assert ".mode-toggle" in content
    assert ".brush-window" in content
    assert ".hud-brackets" in content


def test_app_js_has_load_observatory():
    content = _read(APP_JS)
    assert "function loadObservatory" in content
    assert "function loadHexBins" in content
    assert "function mountObservatoryRail" in content
    assert "class TimeBrush" in content
    # v0.7.1: the switchTab alias maps only "map" → "observatory";
    # Timeline is its own first-class tab again. Asserted more
    # specifically in test_switch_tab_no_longer_aliases_timeline.
    assert 'if (tab === "map") {' in content


def test_app_js_has_hex_mode_branch():
    content = _read(APP_JS)
    # toggleMapMode must handle "hexbin" distinct from heatmap/points
    assert 'mode === "hexbin"' in content
    assert "state.hexLayer" in content


# ---------------------------------------------------------------------------
# Theme toggle
# ---------------------------------------------------------------------------

def test_theme_toggle_markup_in_gear_menu():
    content = _read(INDEX_HTML)
    assert 'class="theme-toggle"' in content
    assert 'data-theme="signal"' in content
    assert 'data-theme="declass"' in content


def test_pre_paint_theme_script_present():
    """The inline script that applies theme-signal or theme-declass
    BEFORE the stylesheet loads — prevents flash of default theme on
    refresh."""
    content = _read(INDEX_HTML)
    assert "ufosint-theme" in content
    assert "theme-signal" in content
    assert "theme-declass" in content


def test_both_theme_css_blocks_present():
    content = _read(STYLE_CSS)
    # Both body.theme-signal and body.theme-declass must define the
    # core palette variables. We check for --accent: specifically since
    # it's the most-visible token.
    signal_match = re.search(r"body\.theme-signal\s*\{[^}]*--accent:", content, re.DOTALL)
    declass_match = re.search(r"body\.theme-declass\s*\{[^}]*--accent:", content, re.DOTALL)
    assert signal_match, "body.theme-signal block missing or doesn't define --accent"
    assert declass_match, "body.theme-declass block missing or doesn't define --accent"


def test_h1_is_ufosint_explorer():
    """v0.7.1: the header H1 became 'UFOSINT Explorer' (matches the
    domain and the repo name). Previously it was just 'UFO Explorer'."""
    content = _read(INDEX_HTML)
    assert "<h1>UFOSINT Explorer</h1>" in content, (
        "Header H1 is no longer 'UFOSINT Explorer' — the rename was reverted"
    )
    assert "<h1>UFO Explorer</h1>" not in content


def test_timeline_tab_is_restored_and_visible():
    """v0.7.1: Timeline is a first-class tab again (the Observatory
    time brush is a compact filter, but users still want the full
    Chart.js drill-down for year→month exploration). Only the Map
    tab should remain hidden as a legacy alias."""
    content = _read(INDEX_HTML)
    # The Timeline tab button exists and is NOT hidden / NOT legacy.
    # We look for the exact shape: data-tab="timeline" followed by >Timeline<
    # with no `hidden` attribute on the same button.
    import re as _re
    tl = _re.search(r'<button[^>]*data-tab="timeline"[^>]*>Timeline</button>', content)
    assert tl, "Timeline tab button missing or Timeline text changed"
    assert " hidden" not in tl.group(0), (
        "Timeline tab is still hidden — v0.7.1 restores it as a real tab"
    )
    assert "legacy-tab" not in tl.group(0), (
        "Timeline tab still carries legacy-tab class — should be first-class"
    )


def test_map_tab_stays_hidden_as_legacy_alias():
    """Map stays hidden since Observatory replaces the full-screen
    map tab entirely. Timeline got restored but Map did not."""
    content = _read(INDEX_HTML)
    import re as _re
    m = _re.search(r'<button[^>]*data-tab="map"[^>]*>Map</button>', content)
    assert m, "Map tab button was deleted — we still want it in the DOM for deep link aliasing"
    assert " hidden" in m.group(0), "Map tab should be hidden in v0.7.1"


def test_switch_tab_no_longer_aliases_timeline():
    """v0.7.1: the switchTab() alias branch should map only "map" →
    "observatory". Timeline is its own tab again and must not fall
    through to Observatory."""
    content = _read(APP_JS)
    # The old v0.7 alias was `if (tab === "map" || tab === "timeline")`.
    # v0.7.1 drops the timeline half.
    assert 'tab === "map" || tab === "timeline"' not in content, (
        "switchTab still aliases timeline → observatory in v0.7.1"
    )
    assert 'if (tab === "map") {' in content, (
        "switchTab lost the map → observatory alias entirely — legacy "
        "#/map?... deep links won't work anymore"
    )


def test_switch_tab_has_timeline_branch():
    """v0.7.2 regression guard: switchTab() must call loadTimeline()
    when the user clicks the Timeline tab. When the timeline → observatory
    alias was removed in v0.7.1 the corresponding `else if (tab ===
    "timeline") loadTimeline()` branch was missing, so clicking Timeline
    activated the panel but never rendered the chart. Keep the branch
    pinned."""
    content = _read(APP_JS)
    import re as _re
    # Must contain the exact `else if (tab === "timeline")` branch
    # followed by a loadTimeline() call, all inside the switchTab body.
    tl_branch = _re.search(
        r'else if \(tab === "timeline"\)\s*\{\s*[^}]*loadTimeline\(\)',
        content,
    )
    assert tl_branch, (
        "switchTab() is missing the `else if (tab === \"timeline\") { "
        "loadTimeline() }` branch — the Timeline tab will activate the "
        "panel but the chart will never render."
    )


def test_default_date_range_helper_exists_and_is_called_at_boot():
    """v0.7.2: applyDefaultDateRange() seeds filter-date-from=1900
    and filter-date-to=<current year> on fresh page loads so every
    view opens on the modern sighting era. The helper has to run
    BEFORE applyHashToFilters() so deep-link hashes still win."""
    content = _read(APP_JS)
    assert "function applyDefaultDateRange()" in content, (
        "applyDefaultDateRange() helper is missing from app.js"
    )
    assert 'fromEl.value = "1900"' in content, (
        "applyDefaultDateRange() doesn't seed 1900 as the default from year"
    )
    # Must be called at boot before the hash restore runs. We assert
    # the call appears somewhere in the DOMContentLoaded handler.
    assert "applyDefaultDateRange();" in content, (
        "applyDefaultDateRange() is defined but never called at boot"
    )


def test_time_brush_min_year_is_1900():
    """v0.7.2: the Observatory time brush floor moved from 1947 to
    1900. Keeps the pre-Roswell context visible (foo fighters, 1896
    airship wave references, etc) on the histogram."""
    content = _read(APP_JS)
    assert "const BRUSH_MIN_YEAR = 1900;" in content, (
        "BRUSH_MIN_YEAR is no longer 1900 — the brush floor regressed"
    )


def test_map_place_search_is_at_bottom_middle():
    """v0.7.1: the place search moved from top-left (where it covered
    the mode toggle) to bottom-middle. The CSS rule must use `bottom`
    + `left: 50%` + a translate transform instead of top-left."""
    content = _read(STYLE_CSS)
    import re as _re
    rule = _re.search(r"\.map-place-search\s*\{[^}]*\}", content, _re.DOTALL)
    assert rule, ".map-place-search CSS rule is missing entirely"
    body = rule.group(0)
    assert "bottom:" in body, (
        ".map-place-search no longer uses `bottom:` — it's back at the "
        "top where it covered the mode toggle"
    )
    assert "left: 50%" in body, (
        ".map-place-search should be horizontally centered via left: 50%"
    )
    assert "translateX(-50%)" in body, (
        ".map-place-search should use translateX(-50%) to center itself"
    )
    # Make sure the old top-left positioning is gone
    assert "top: var(--s-3)" not in body, (
        "leftover top: var(--s-3) from the v0.7 top-left placement"
    )


def test_declass_stamp_overlay_removed():
    """v0.7.1: the rotated 'TOP SECRET // PLOTTED' classification stamp
    was removed from the DECLASS theme. It overlapped the gear icon on
    narrower viewports and the user asked to drop it. The DECLASS theme
    is now defined purely by the palette + Courier Prime + paper-
    gradient canvas. This test keeps the removal pinned so a future
    refactor doesn't silently bring it back."""
    content = _read(STYLE_CSS)
    # The old position: fixed body::after stamp should be gone.
    # A scoped canvas-only stamp would be fine if we ever want it back,
    # but the position: fixed global overlay must not return.
    # Look for the exact old selector+content combo.
    import re as _re
    bad = _re.search(
        r"body\.theme-declass::after\s*\{[^}]*content:\s*['\"]TOP SECRET",
        content,
        _re.DOTALL,
    )
    assert bad is None, (
        "DECLASS classification stamp overlay is back — "
        "it covered the gear icon and the user asked to remove it"
    )


def test_app_js_has_theme_handler():
    content = _read(APP_JS)
    assert "function initThemeToggle" in content
    assert "function setTheme" in content
    assert 'localStorage.setItem("ufosint-theme"' in content


# ---------------------------------------------------------------------------
# Key sightings data file
# ---------------------------------------------------------------------------

def test_key_sightings_json_is_valid():
    assert KEY_SIGHTINGS.exists(), "static/data/key_sightings.json is missing"
    data = json.loads(_read(KEY_SIGHTINGS))
    assert isinstance(data, list)
    assert len(data) >= 6, "expected at least 6 key sightings annotations"


def test_key_sightings_has_canonical_events():
    data = json.loads(_read(KEY_SIGHTINGS))
    labels = {item["label"] for item in data}
    # Canonical entries the UX team specified
    for expected in ("ROSWELL", "RENDLESHAM", "PHOENIX LIGHTS", "TIC-TAC"):
        assert expected in labels, f"key_sightings.json missing {expected}"


def test_key_sightings_years_are_plausible():
    data = json.loads(_read(KEY_SIGHTINGS))
    for item in data:
        assert 1940 <= item["year"] <= 2030, (
            f"key sighting {item['label']} year {item['year']} is out of range"
        )


# ---------------------------------------------------------------------------
# Progressive-overlay cleanup
# ---------------------------------------------------------------------------

def test_search_overlay_removed_from_execute_search():
    """executeSearch must not wrap the result cards in a progressive
    overlay anymore — the v0.6 overlay covered content the user was
    trying to read. Compact terminal in the info bar is fine."""
    content = _read(APP_JS)
    # Look specifically for a showProgressiveLoading(resultsEl call. We
    # allow it in loadTimeline (covers chart bars, which is fine since
    # the chart stays visible underneath).
    assert "showProgressiveLoading(resultsEl" not in content, (
        "executeSearch still mounts a progressive overlay on the "
        "results container — this blocks the user from reading cards"
    )


def test_rotating_radar_sweep_detached():
    """The rotating conic-gradient radar sweep (#map.is-loading::before)
    tinted live markers and was the primary visual complaint in v0.6.
    It must no longer be attached to any element. The @keyframes can
    stay (test_loading_system.py still asserts it for stability)."""
    content = _read(STYLE_CSS)
    # A rule that uses `animation: map-radar-spin` on #map.is-loading::before
    # should NOT exist.
    bad = re.search(
        r"#map\.is-loading::before\s*\{[^}]*animation:\s*map-radar-spin",
        content,
        re.DOTALL,
    )
    assert bad is None, (
        "#map.is-loading::before still animates with map-radar-spin — "
        "the rotating sweep was removed in v0.7"
    )
