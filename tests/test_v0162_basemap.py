"""v0.16.2 — CARTO basemap API key.

CARTO began requiring a key on their raster basemaps in Aug 2026. The failure
mode is unusually quiet: a keyless request returns HTTP 200 with a valid PNG
that has "API KEY REQUIRED" rendered into the image. No exception, no bad
status code, nothing to catch — the map just looks defaced, and it happened
with no deploy on our side.

Two things these tests protect:

  1. The key must never be committed. This repo is public, so a key in the
     tree is a key that gets scraped. It lives in the CARTO_KEY app setting
     and is injected at import time.

  2. A missing key must degrade to a keyless provider, not to a watermarked
     CARTO map. Empty is a supported state, not an error.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The key must not be in the repo
# ---------------------------------------------------------------------------

def test_no_carto_key_committed():
    """No CARTO key literal anywhere in the tracked source.

    CARTO keys look like `cb1_<something>_<n>_<hex>`. If this fires, rotate the
    key at carto.com immediately — a committed key on a public repo is burned.
    """
    pattern = re.compile(r"cb1_[A-Za-z0-9]+_\d+_[0-9a-f]{16,}")
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__", ".venv", "venv"}
               for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico",
                                   ".woff", ".woff2", ".db", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        f"a CARTO API key appears to be committed in {offenders} — "
        "rotate it and move it to the CARTO_KEY app setting"
    )


# ---------------------------------------------------------------------------
# Injection wiring
# ---------------------------------------------------------------------------

def test_app_py_substitutes_carto_key():
    src = _read(APP_PY)
    assert 'os.environ.get("CARTO_KEY"' in src, (
        "app.py must read the key from the environment, not a literal"
    )
    assert '"{{CARTO_KEY}}", CARTO_KEY' in src, (
        "app.py must substitute {{CARTO_KEY}} into the preloaded index.html"
    )


def test_index_html_exposes_placeholder_before_app_js():
    html = _read(INDEX_HTML)
    assert "{{CARTO_KEY}}" in html
    assert "window.CARTO_KEY" in html

    key_at = html.find("window.CARTO_KEY")
    app_at = html.find("/static/app.js")
    assert key_at != -1 and app_at != -1
    assert key_at < app_at, (
        "window.CARTO_KEY must be set before app.js loads — app.js reads it "
        "while building TILE_URLS at module scope"
    )


# ---------------------------------------------------------------------------
# Client behaviour
# ---------------------------------------------------------------------------

def test_app_js_reads_key_from_window():
    js = _read(APP_JS)
    assert "window.CARTO_KEY" in js


def test_app_js_appends_key_to_carto_urls():
    js = _read(APP_JS)
    assert '"?key=" + encodeURIComponent(CARTO_KEY)' in js, (
        "the key must be appended as a query param and URL-encoded"
    )


def test_app_js_falls_back_to_keyless_provider():
    """Without a key we must not request CARTO — those tiles are watermarked."""
    js = _read(APP_JS)
    assert "_FALLBACK_URLS" in js
    start = js.find("const _FALLBACK_URLS")
    block = js[start: js.find("};", start)]
    assert "cartocdn" not in block, (
        "the fallback must not point at CARTO — keyless CARTO tiles carry the "
        "'API KEY REQUIRED' watermark"
    )
    for theme in ("signal", "declass"):
        assert theme in block, f"fallback is missing the {theme} theme"


def test_attribution_follows_the_active_source():
    """Crediting CARTO for tiles Esri served would be wrong, and CARTO's free
    tier is conditional on their attribution being visible when in use."""
    js = _read(APP_JS)
    start = js.find("const TILE_ATTRIBUTION")
    block = js[start:start + 500]
    assert "CARTO_KEY" in block, "attribution must branch on whether a key is set"
    assert "carto.com/attributions" in block
    assert "esri.com" in block.lower()
