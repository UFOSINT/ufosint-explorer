"""v0.8.7 — Filter bar cleanup + Movement cluster + Quality rail bug fix.

Locks the v0.8.7 contract:

  1. Five dead top-bar dropdowns deleted (country, state, hynek,
     vallee, collection) + coords-filter + More Filters drawer.
  2. New Movement cluster (10 checkboxes, OR-mask semantics, bit-
     packed into the existing movement_flags uint16 at offset 28).
  3. Color + Emotion dropdowns surface the already-working filter
     logic (byte slots 21 and 22 in the bulk row).
  4. Shape dropdown populated from POINTS.shapes (standardized list)
     not from /api/filters raw shape strings.
  5. Quality rail no longer permanently disables itself when the
     bulk buffer isn't ready at Observatory mount time.
  6. add_common_filters + init_filters + _COMMON_FILTER_KEYS pruned
     to the 6 surviving filters.

All tests are static source-code inspection — no Flask, no DB, no
browser. The goal is a cheap regression tripwire against someone
re-introducing a dead dropdown or re-adding the mount guard.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    """Best-effort extractor for a top-level JS function body.

    Matches `function name(` or `async function name(` and walks
    forward until brace depth returns to zero. Good enough for
    source-inspection tests; not a real parser.
    """
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


# =============================================================================
# Phase 1 — Quality rail bug fix
# =============================================================================

def test_quality_rail_dataset_mounted_guard_gone():
    """The idempotent `dataset.mounted` guard was the root cause of
    the permanently-disabled rail. Removing it lets _wireTimeBrushToDeck
    re-mount with real coverage after the bulk buffer lands.

    We check for the actual executable pattern (assignment or
    comparison) rather than bare text, so the explanatory comment
    in the function body doesn't trigger a false positive.
    """
    src = _read(APP_JS)
    body = _extract_js_function(src, "mountQualityRail")
    assert body, "couldn't locate mountQualityRail body"
    # Match only executable forms — not the mention in the comment.
    bad_patterns = [
        r'host\.dataset\.mounted\s*===',    # comparison guard
        r'host\.dataset\.mounted\s*=\s*"',  # assignment
        r'dataset\.mounted\s*===\s*"1"',    # generic comparison
    ]
    for pat in bad_patterns:
        assert not re.search(pat, body), (
            f"mountQualityRail still has the dataset.mounted guard "
            f"(pattern {pat!r}). v0.8.7 removes it so re-mount works "
            f"after POINTS.ready."
        )


def test_quality_rail_remount_from_boot_deck_gl():
    """bootDeckGL must call mountQualityRail after _wireTimeBrushToDeck
    so the rail picks up real coverage once POINTS.ready flips."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "bootDeckGL")
    assert body, "couldn't locate bootDeckGL body"
    assert "mountQualityRail" in body, (
        "bootDeckGL must call mountQualityRail() so the rail re-renders "
        "with real coverage after the bulk buffer lands. See v0.8.7 "
        "Phase 1b."
    )


def test_quality_rail_populate_dropdowns_from_boot_deck_gl():
    """Same hook: bootDeckGL populates the shape/color/emotion/
    movement dropdowns from POINTS metadata."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "bootDeckGL")
    assert "populateFilterDropdownsFromDeck" in body, (
        "bootDeckGL must call populateFilterDropdownsFromDeck() so "
        "the dropdowns use the canonical standardized lists"
    )


def test_quality_rail_coverage_gate_checks_points_ready():
    """mountQualityRail's coverage lookup should gate on POINTS.ready,
    not just the existence of the UFODeck export."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "mountQualityRail")
    assert "POINTS.ready" in body, (
        "mountQualityRail should only read coverage when POINTS.ready "
        "is true — otherwise it mounts a disabled rail that never "
        "retries"
    )


def test_quality_rail_disabled_css_rule_still_present():
    """The .rail-toggle-disabled CSS class is kept — it still applies
    when a derived column legitimately has zero rows populated. The
    v0.8.7 fix was to stop triggering it spuriously, not to delete
    the styling."""
    css = _read(STYLE_CSS)
    assert ".rail-toggle-disabled" in css


# =============================================================================
# Phase 2 — deck.js movementCats filter
# =============================================================================

def test_deck_rebuild_visible_handles_movement_cats():
    """_rebuildVisible must handle the new movementCats filter field
    with an OR bitmask lookup into POINTS.movements."""
    src = _read(DECK_JS)
    body = _extract_js_function(src, "_rebuildVisible")
    assert body, "couldn't locate _rebuildVisible body"
    assert "movementCats" in body, (
        "_rebuildVisible must read the new movementCats filter field"
    )
    assert "mvMask" in body, (
        "_rebuildVisible must compute a uint16 mask from the selected "
        "category names"
    )
    assert "POINTS.movements" in body, (
        "_rebuildVisible must resolve category names against "
        "POINTS.movements for bit-position lookup"
    )


def test_deck_movement_cats_uses_bitwise_and():
    """The hot-loop filter check should use `(mvf[i] & mvMask) === 0`
    for an OR-across-categories semantic."""
    src = _read(DECK_JS)
    body = _extract_js_function(src, "_rebuildVisible")
    # The mask check should exist in the hot loop
    assert "mvf[i] & mvMask" in body or "mvMask !== 0" in body, (
        "movement filter check missing — expected (mvf[i] & mvMask) === 0 "
        "pattern in the hot loop"
    )


def test_deck_hot_loop_aliases_movement_flags():
    """For V8 optimisation, the hot loop should alias
    POINTS.movementFlags into a local `mvf` variable alongside the
    other typed-array aliases."""
    src = _read(DECK_JS)
    body = _extract_js_function(src, "_rebuildVisible")
    assert "const mvf = POINTS.movementFlags" in body, (
        "hot loop should alias POINTS.movementFlags → mvf for speed"
    )


# =============================================================================
# Phase 3 — app.js wiring
# =============================================================================

def test_app_js_has_read_movement_cats_helper():
    src = _read(APP_JS)
    assert "function _readMovementCats(" in src


def test_app_js_has_populate_from_deck():
    src = _read(APP_JS)
    assert "function populateFilterDropdownsFromDeck(" in src


def test_app_js_has_mount_movement_cluster():
    src = _read(APP_JS)
    assert "function _mountMovementCluster(" in src


def test_app_js_has_populate_lookup_dropdown_helper():
    src = _read(APP_JS)
    assert "function _populateLookupDropdown(" in src


def test_apply_client_filters_reads_movement_cats():
    src = _read(APP_JS)
    body = _extract_js_function(src, "applyClientFilters")
    assert body, "couldn't locate applyClientFilters body"
    assert "movementCats" in body, (
        "applyClientFilters must pass movementCats into the filter "
        "descriptor for _rebuildVisible to read"
    )
    assert "_readMovementCats" in body, (
        "applyClientFilters must call _readMovementCats() to collect "
        "the currently checked categories"
    )


def test_count_active_filters_counts_movement_cluster():
    src = _read(APP_JS)
    body = _extract_js_function(src, "_countActiveFilters")
    assert body, "couldn't locate _countActiveFilters body"
    assert "movementCats" in body


def test_filter_fields_trimmed_to_six():
    """FILTER_FIELDS should contain exactly the 6 surviving entries.
    Country / State / Hynek / Vallee / Collection / Coords are gone."""
    src = _read(APP_JS)
    m = re.search(
        r"const FILTER_FIELDS\s*=\s*\[([\s\S]*?)\];",
        src,
    )
    assert m, "couldn't locate FILTER_FIELDS declaration"
    block = m.group(1)
    # Each entry is `{ id: "filter-xxx", ... }` — count braces.
    entries = re.findall(r"\{\s*id:", block)
    assert len(entries) == 6, (
        f"FILTER_FIELDS should have exactly 6 entries, found {len(entries)}"
    )
    for dead in ("filter-country", "filter-state", "filter-hynek",
                 "filter-vallee", "filter-collection", "coords-filter"):
        assert dead not in block, f"FILTER_FIELDS still references {dead!r}"
    for live in ("filter-date-from", "filter-date-to", "filter-shape",
                 "filter-source", "filter-color", "filter-emotion"):
        assert live in block, f"FILTER_FIELDS missing {live!r}"


def test_get_filter_params_dropped_dead_ids():
    """getFilterParams must no longer read the dead filter IDs."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "getFilterParams")
    assert body, "couldn't locate getFilterParams body"
    for dead in ("filter-country", "filter-state", "filter-hynek",
                 "filter-vallee", "filter-collection", "coords-filter"):
        assert dead not in body, (
            f"getFilterParams still reads {dead!r}"
        )
    assert "filter-color" in body
    assert "filter-emotion" in body
    assert "movement" in body  # serializes _readMovementCats result


def test_clear_filters_uses_filter_fields_loop():
    """clearFilters should drive off FILTER_FIELDS instead of
    hardcoding each dead ID."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "clearFilters")
    assert body, "couldn't locate clearFilters body"
    # Dead ID references should all be gone.
    for dead in ("filter-country", "filter-state", "filter-hynek",
                 "filter-vallee", "filter-collection"):
        assert dead not in body, f"clearFilters still clears {dead!r}"
    # Movement cluster reset must be present.
    assert ".movement-cluster input" in body, (
        "clearFilters must uncheck the movement cluster checkboxes"
    )
    # Quality rail reset must be present.
    assert "qualityFilter" in body, (
        "clearFilters should also reset state.qualityFilter"
    )


def test_populate_filter_dropdowns_trimmed():
    """populateFilterDropdowns should only touch the source dropdown.
    Shape/color/emotion come from populateFilterDropdownsFromDeck."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "populateFilterDropdowns")
    assert body, "couldn't locate populateFilterDropdowns body"
    # Dead dropdown population should be gone.
    for dead in ("filter-country", "filter-state", "filter-hynek",
                 "filter-vallee", "filter-collection"):
        assert dead not in body, (
            f"populateFilterDropdowns still touches {dead!r}"
        )


# =============================================================================
# Phase 4 — index.html filter bar redesign
# =============================================================================

def test_index_html_dead_filter_ids_gone():
    html = _read(INDEX_HTML)
    for dead in (
        'id="filter-country"',
        'id="filter-state"',
        'id="filter-hynek"',
        'id="filter-vallee"',
        'id="filter-collection"',
        'id="coords-filter"',
        'id="btn-more-filters"',
        'id="filters-advanced"',
    ):
        assert dead not in html, (
            f"index.html still contains {dead!r} — v0.8.7 should have "
            f"deleted it"
        )


def test_index_html_new_filter_ids_present():
    html = _read(INDEX_HTML)
    for live in (
        'id="filter-color"',
        'id="filter-emotion"',
        'id="filter-movement-cluster"',
    ):
        assert live in html, f"index.html missing {live!r}"


def test_index_html_movement_row_classes():
    html = _read(INDEX_HTML)
    assert 'class="filter-movement-row"' in html
    assert 'class="movement-cluster"' in html
    assert 'class="movement-label"' in html


def test_index_html_still_has_shape_and_source():
    """Shape and Source dropdowns are the two surviving faceted
    filters from the original top bar."""
    html = _read(INDEX_HTML)
    assert 'id="filter-shape"' in html
    assert 'id="filter-source"' in html


# =============================================================================
# Phase 5 — CSS
# =============================================================================

def test_css_has_movement_cluster_rules():
    css = _read(STYLE_CSS)
    for cls in (
        ".filter-movement-row",
        ".movement-cluster",
        ".movement-chip-label",
        ".movement-label",
    ):
        assert cls in css, f"style.css missing {cls!r}"


def test_css_has_movement_chip_checked_state():
    """The `:has(input:checked)` selector is the visual feedback for
    a pill being active."""
    css = _read(STYLE_CSS)
    assert ":has(input:checked)" in css, (
        "movement-chip-label needs a :has(input:checked) rule so "
        "selected pills render in the accent colour"
    )


# =============================================================================
# Phase 6 — Backend cleanup
# =============================================================================

def test_add_common_filters_trimmed():
    """add_common_filters must reference only the surviving filter
    keys. Country / state / hynek / vallee / collection / coords
    branches should be gone."""
    src = _read(APP_PY)
    # Extract the function body — it's a Python def ending at the
    # next top-level def.
    m = re.search(
        r"def add_common_filters\(.*?\n(?=\n\S)",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate add_common_filters body"
    body = m.group(0)

    for dead in (
        'params.get("collection")',
        'params.get("hynek")',
        'params.get("vallee")',
        'params.get("country")',
        'params.get("state")',
        'params.get("coords")',
        "l.geocode_src",
    ):
        assert dead not in body, (
            f"add_common_filters still handles {dead!r} — v0.8.7 "
            f"should have deleted it"
        )

    # Surviving filters
    for live in (
        'params.get("shape")',
        'params.get("source")',
        'params.get("color")',
        'params.get("emotion")',
        'params.get("date_from")',
        'params.get("date_to")',
        "standardized_shape",   # v0.8.7 column switch
        "primary_color",
        "dominant_emotion",
    ):
        assert live in body, f"add_common_filters missing {live!r}"


def test_add_common_filters_shape_uses_standardized():
    """v0.8.7 switched the shape filter from raw `s.shape` to
    `s.standardized_shape` so it matches the Observatory dropdown's
    canonical list."""
    src = _read(APP_PY)
    m = re.search(
        r"def add_common_filters\(.*?\n(?=\n\S)",
        src,
        re.DOTALL,
    )
    body = m.group(0)
    assert "{p}.standardized_shape" in body, (
        "shape filter must use standardized_shape column, not raw shape"
    )
    assert "{p}.shape = " not in body, (
        "old raw shape filter clause should be gone"
    )


def test_common_filter_keys_trimmed_to_six():
    src = _read(APP_PY)
    m = re.search(
        r"_COMMON_FILTER_KEYS\s*=\s*frozenset\(\{([^}]*)\}\)",
        src,
    )
    assert m, "couldn't locate _COMMON_FILTER_KEYS declaration"
    block = m.group(1)
    keys = set(re.findall(r'"(\w+)"', block))
    expected = {"shape", "source", "color", "emotion", "date_from", "date_to"}
    assert keys == expected, (
        f"_COMMON_FILTER_KEYS expected {sorted(expected)}, got "
        f"{sorted(keys)}"
    )


def test_stats_has_mapped_sightings_helper():
    """v0.8.7.2 — the stats badge was labelling `geocoded_locations`
    (distinct-place count from mv_stats_summary) as "mapped", which
    dramatically understated the true mapped count (~106k places vs
    ~396k sightings). The fix adds `_api_stats_mapped_count(conn)`
    that counts via `sighting JOIN location` and is wired into both
    the MV happy path and the live fallback so the response carries
    a `mapped_sightings` field alongside the legacy
    `geocoded_locations`.
    """
    src = _read(APP_PY)
    assert "def _api_stats_mapped_count(" in src, (
        "_api_stats_mapped_count helper missing — v0.8.7.2 needs "
        "sighting-level mapped count via JOIN"
    )
    # Extract the helper body and verify it actually uses the JOIN
    # (not another COUNT on location).
    m = re.search(
        r"def _api_stats_mapped_count\(.*?\n(?=\n\S)",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate _api_stats_mapped_count body"
    body = m.group(0)
    assert "JOIN location" in body, (
        "_api_stats_mapped_count must JOIN location — otherwise it's "
        "just re-computing the wrong distinct-place count"
    )
    assert "sighting s" in body or "FROM sighting" in body, (
        "_api_stats_mapped_count must count FROM sighting so the "
        "result is per-sighting, not per-location"
    )


def test_stats_response_includes_mapped_sightings():
    """Both the MV happy path and the live fallback must add
    `mapped_sightings` to the response dict."""
    src = _read(APP_PY)
    # Both return statements live inside _api_stats_from_mv and
    # _api_stats_from_live. Each must carry the new field.
    mv_body = re.search(
        r"def _api_stats_from_mv\(.*?\n(?=\n\S)",
        src,
        re.DOTALL,
    )
    assert mv_body
    assert "mapped_sightings" in mv_body.group(0)

    live_body = re.search(
        r"def _api_stats_from_live\(.*?\n(?=\n\S)",
        src,
        re.DOTALL,
    )
    assert live_body
    assert "mapped_sightings" in live_body.group(0)


def test_show_stats_renders_mapped_sightings():
    """The frontend badge should prefer data.mapped_sightings and
    fall back to data.geocoded_locations. Direct reference to the
    old field alone without the fallback is a regression."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "showStats")
    assert body, "couldn't locate showStats body"
    assert "mapped_sightings" in body, (
        "showStats must read data.mapped_sightings (the sighting-"
        "level count) instead of data.geocoded_locations which is "
        "the distinct-place count"
    )
    # The chip label should now read "mapped" sourced from the new
    # field, not the old one.
    assert "${mapped} mapped" in body, (
        "badge chip should render `${mapped} mapped` where `mapped` "
        "resolves from mapped_sightings"
    )


def test_app_js_has_no_orphaned_dead_id_references():
    """Guard against the v0.8.7.1 hotfix bug.

    The initial v0.8.7 landing left a bootstrap event listener on
    `document.getElementById("coords-filter")` at app.js:137 that
    crashed the whole page with `Cannot read properties of null`
    because the element was deleted in Phase 4. pytest didn't catch
    it because the dead reference was outside any testable function
    body — a bare call at module-level script execution.

    This test scans the entire app.js source for any reference to
    the deleted element IDs in a context that would null-deref at
    runtime: `.getElementById("dead-id")` followed by anything
    other than `?.` or `)?`. If someone re-introduces one, the
    test fails immediately.
    """
    src = _read(APP_JS)
    dead_ids = [
        "filter-country",
        "filter-state",
        "filter-hynek",
        "filter-vallee",
        "filter-collection",
        "coords-filter",
        "btn-more-filters",
        "filters-advanced",
        "more-filter-count",
    ]
    for dead in dead_ids:
        # Unsafe pattern: getElementById("x").something
        # Safe pattern:   getElementById("x")?.something
        unsafe = re.compile(
            r'getElementById\(\s*"' + re.escape(dead) + r'"\s*\)\s*\.',
        )
        match = unsafe.search(src)
        assert not match, (
            f"app.js has an unsafe getElementById({dead!r}).xxx "
            f"call — this element was deleted in v0.8.7 so the "
            f"lookup returns null and the .xxx access crashes the "
            f"page on bootstrap. Found at char offset "
            f"{match.start()}."
        )


def test_init_filters_trimmed():
    """init_filters at startup should only query the 4 surviving
    filter vocabularies. hynek/vallee/collections/countries/states/
    match_methods queries should be gone."""
    src = _read(APP_PY)
    m = re.search(
        r"def init_filters\(\).*?\n(?=\n\S)",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate init_filters body"
    body = m.group(0)

    for dead in (
        'FILTER_CACHE["hynek"]',
        'FILTER_CACHE["vallee"]',
        'FILTER_CACHE["collections"]',
        'FILTER_CACHE["countries"]',
        'FILTER_CACHE["states"]',
        'FILTER_CACHE["match_methods"]',
    ):
        assert dead not in body, (
            f"init_filters still populates {dead!r}"
        )

    for live in (
        'FILTER_CACHE["shapes"]',
        'FILTER_CACHE["sources"]',
        'FILTER_CACHE["colors"]',
        'FILTER_CACHE["emotions"]',
    ):
        assert live in body, f"init_filters missing {live!r}"

    # Shape query uses standardized_shape, not raw shape
    assert "standardized_shape" in body
