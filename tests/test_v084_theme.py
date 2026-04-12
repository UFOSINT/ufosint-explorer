"""v0.8.4 — Signal / Declass theme overhaul.

Locks the v0.8.4 contract:

  1. Top-nav `.theme-pill` radio group exists in index.html.
  2. CSS for both the existing `.theme-toggle` (settings menu) AND
     the new `.theme-pill` (top nav) exists in style.css.
  3. app.js defines a `TILE_URLS` constant with both signal/declass
     entries pointing at Carto basemap URLs.
  4. state.tileLayer is created in initMap() and the URL is chosen
     from TILE_URLS[theme], not hardcoded.
  5. setTheme() wires the tile swap via state.tileLayer.setUrl() AND
     the deck.gl recolor via UFODeck.setTheme().
  6. deck.js defines a THEME_PALETTES constant with both signal/declass
     entries, each carrying scatter + hexRange values.
  7. deck.js layer factories (Scatterplot / Hexagon / Heatmap) read
     from the active palette, NOT from hardcoded RGB arrays.
  8. deck.js exposes UFODeck.setTheme as a public method.
  9. Detail modal mini-map also honors the theme.
 10. v0.8.4 plan doc exists and covers the key concepts.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"
CHANGELOG = ROOT / "CHANGELOG.md"
PLAN_DOC = ROOT / "docs" / "V084_THEME_PLAN.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Plan doc + changelog
# ---------------------------------------------------------------------------
def test_v084_plan_doc_exists():
    assert PLAN_DOC.exists(), (
        "docs/V084_THEME_PLAN.md is the architecture doc for the "
        "Signal/Declass theme overhaul. Must exist in-tree."
    )


def test_v084_plan_doc_covers_key_concepts():
    doc = _read(PLAN_DOC)
    for concept in (
        "theme-pill",
        "TILE_URLS",
        "THEME_PALETTES",
        "UFODeck.setTheme",
        "cartocdn",
        "scatter",
        "hexRange",
    ):
        assert concept in doc, f"plan doc missing coverage of {concept!r}"


def test_changelog_has_v084_section():
    log = _read(CHANGELOG)
    assert "[0.8.4]" in log, (
        "CHANGELOG must have a [0.8.4] section before shipping"
    )


# ---------------------------------------------------------------------------
# index.html — top-nav theme pill
# ---------------------------------------------------------------------------
def test_index_html_has_top_nav_theme_pill():
    html = _read(INDEX_HTML)
    # New .theme-pill radio group
    assert 'class="theme-pill"' in html, (
        "index.html must have a .theme-pill radio group in the top nav"
    )
    # Must have both theme-opt-compact buttons
    assert 'data-theme="signal"' in html
    assert 'data-theme="declass"' in html
    assert "theme-opt-compact" in html


def test_theme_pill_is_outside_settings_menu():
    """The point of v0.8.4 is making the toggle visible without needing
    to open the settings menu. The new .theme-pill must live at the
    top nav level, NOT inside #settings-menu."""
    html = _read(INDEX_HTML)
    # Find the positions of the relevant markers
    pill_pos = html.find('class="theme-pill"')
    menu_open = html.find('id="settings-menu"')
    menu_close = html.find("</nav>", menu_open) if menu_open != -1 else -1
    # Both must be present
    assert pill_pos != -1
    assert menu_open != -1
    # The pill must appear BEFORE the settings menu opens
    # (or outside the menu entirely). Same-line comparison is fine
    # because the index.html structure is linear.
    if pill_pos > menu_open and menu_close != -1:
        assert pill_pos > menu_close, (
            ".theme-pill must not be nested inside #settings-menu — it "
            "belongs at the top nav level"
        )


# ---------------------------------------------------------------------------
# style.css — theme pill + existing toggle both styled
# ---------------------------------------------------------------------------
def test_style_css_has_theme_pill_rules():
    css = _read(STYLE_CSS)
    assert ".theme-pill" in css, (
        "style.css must define .theme-pill rules for the new top-nav toggle"
    )
    assert ".theme-opt-compact" in css, (
        "style.css must define .theme-opt-compact rules for the compact buttons"
    )


def test_style_css_theme_pill_uses_tokens():
    """The pill must use CSS variables so it re-skins correctly on
    theme change. Hardcoded colors would lock it to one theme."""
    css = _read(STYLE_CSS)
    # v0.11.2: skip the mobile media-query `.theme-pill { display:none }`
    # and find the actual top-level definition instead.
    start = css.find("\n.theme-pill {")
    assert start != -1
    # Grab the next ~800 chars covering the .theme-pill block + children
    block = css[start:start + 2000]
    # Must reference theme tokens
    assert "var(--accent)" in block or "var(--accent-bg)" in block
    assert "var(--bg-panel)" in block or "var(--bg-card)" in block


def test_style_css_keeps_existing_theme_classes():
    css = _read(STYLE_CSS)
    assert "body.theme-signal" in css
    assert "body.theme-declass" in css


# ---------------------------------------------------------------------------
# app.js — TILE_URLS + state.tileLayer + setTheme wiring
# ---------------------------------------------------------------------------
def test_app_js_has_tile_urls_constant():
    js = _read(APP_JS)
    assert "TILE_URLS" in js
    # Must have BOTH signal + declass entries
    start = js.find("const TILE_URLS")
    assert start != -1
    end = js.find("};", start)
    block = js[start:end]
    assert "signal:" in block
    assert "declass:" in block
    assert "cartocdn" in block, (
        "TILE_URLS must point at Carto basemap URLs (Dark Matter + Voyager)"
    )


def test_init_map_uses_tile_urls():
    js = _read(APP_JS)
    start = js.find("function initMap(")
    end = js.find("\nasync function bootDeckGL", start) if start != -1 else -1
    body = js[start:end] if end != -1 else js[start:start + 8000]
    assert "state.tileLayer" in body, (
        "initMap must stash the tile layer on state.tileLayer so setTheme "
        "can call state.tileLayer.setUrl() later"
    )
    assert "TILE_URLS[" in body, (
        "initMap must read the tile URL from the TILE_URLS map, not hardcode it"
    )
    # The legacy OSM URL must be gone
    assert "tile.openstreetmap.org" not in body, (
        "initMap still references the legacy OSM tile URL instead of "
        "using TILE_URLS. The Carto switch should have removed it."
    )


def test_init_map_no_hardcoded_osm_in_detail_minimap():
    """The detail-modal mini-map also needs to match the theme."""
    js = _read(APP_JS)
    # Hardcoded OSM URL must be gone entirely from app.js
    assert "tile.openstreetmap.org" not in js, (
        "A hardcoded OSM tile URL still exists somewhere in app.js — "
        "the detail-modal mini-map should also honor the theme via "
        "TILE_URLS[_currentTheme()]"
    )


def test_set_theme_calls_tile_layer_set_url():
    js = _read(APP_JS)
    start = js.find("function setTheme(")
    end = js.find("\nasync function loadHeatmap", start)
    if end == -1:
        end = js.find("\nfunction ", start + 1)
    body = js[start:end]
    assert "state.tileLayer" in body
    assert "setUrl" in body
    assert "TILE_URLS" in body


def test_set_theme_calls_ufodeck_set_theme():
    js = _read(APP_JS)
    start = js.find("function setTheme(")
    end = js.find("\nasync function loadHeatmap", start)
    if end == -1:
        end = js.find("\nfunction ", start + 1)
    body = js[start:end]
    assert "window.UFODeck" in body
    assert "UFODeck.setTheme" in body


def test_boot_deck_gl_seeds_theme_before_mount():
    """bootDeckGL must call UFODeck.setTheme(currentTheme) BEFORE
    mountDeckLayer so the first layer instance uses the right palette.
    Without this, a DECLASS user briefly sees cyan dots before the
    later setTheme() call re-renders with the burgundy palette."""
    js = _read(APP_JS)
    start = js.find("async function bootDeckGL(")
    end = js.find("\n}", start + 10)
    body = js[start:end]
    # setTheme call must come before mountDeckLayer
    seed_pos = body.find("UFODeck.setTheme(")
    mount_pos = body.find("mountDeckLayer(")
    assert seed_pos != -1, "bootDeckGL must call UFODeck.setTheme() early"
    assert mount_pos != -1
    assert seed_pos < mount_pos, (
        "UFODeck.setTheme() must be called BEFORE mountDeckLayer() so the "
        "initial layer uses the correct theme palette"
    )


# ---------------------------------------------------------------------------
# deck.js — THEME_PALETTES + setTheme API
# ---------------------------------------------------------------------------
def test_deck_js_has_theme_palettes_constant():
    js = _read(DECK_JS)
    assert "THEME_PALETTES" in js
    start = js.find("const THEME_PALETTES")
    assert start != -1
    end = js.find("};", start)
    block = js[start:end]
    assert "signal:" in block
    assert "declass:" in block
    assert "scatter:" in block
    assert "hexRange:" in block


def test_deck_js_scatterplot_reads_from_palette():
    """The ScatterplotLayer factory must NOT hardcode [0, 240, 255, 180]
    anymore. It reads from the active palette so the dot color
    changes with the theme."""
    js = _read(DECK_JS)
    start = js.find("function makeScatterplotLayer(")
    end = js.find("\n    function makeHexagonLayer", start)
    body = js[start:end]
    # Must reference the palette
    assert "palette.scatter" in body, (
        "makeScatterplotLayer must read getFillColor from palette.scatter, "
        "not hardcode a color array"
    )
    # And the old hardcoded cyan must be gone from this function body
    assert "[0, 240, 255, 180]" not in body


def test_deck_js_hexagon_reads_from_palette():
    js = _read(DECK_JS)
    start = js.find("function makeHexagonLayer(")
    end = js.find("\n    function makeHeatmapLayer", start)
    body = js[start:end]
    assert "palette.hexRange" in body
    # The old hardcoded 5-stop cold-plasma ramp must not appear
    # verbatim in this function anymore
    assert "[0, 59, 92], [0, 140, 180], [0, 240, 255]" not in body


def test_deck_js_heatmap_reads_from_palette():
    js = _read(DECK_JS)
    start = js.find("function makeHeatmapLayer(")
    end = js.find("\n    // v0.8.4", start)
    if end == -1:
        end = js.find("\n    function setDeckTheme", start)
    body = js[start:end] if end != -1 else js[start:start + 2000]
    # Heatmap must also use palette.hexRange for consistency
    assert "palette.hexRange" in body or "palette.heatRange" in body


def test_deck_js_exports_set_theme():
    js = _read(DECK_JS)
    # Public API block
    start = js.find("window.UFODeck = {")
    end = js.find("};", start)
    public = js[start:end]
    assert "setTheme" in public, (
        "UFODeck.setTheme must be in the public API exports"
    )


def test_deck_js_set_theme_function_exists():
    js = _read(DECK_JS)
    assert "function setDeckTheme(" in js, (
        "deck.js must define a setDeckTheme function that gets exported "
        "as UFODeck.setTheme"
    )
    # It must refresh the active layer so the new palette lands
    start = js.find("function setDeckTheme(")
    end = js.find("\n    }", start)
    body = js[start:end] if end != -1 else js[start:start + 500]
    assert "refreshActiveLayer" in body
