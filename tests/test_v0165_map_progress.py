"""v0.16.5 — map load progress.

The bulk point buffer is ~5.8 MB gzipped / 15.8 MB decoded, and the map is
blank until it lands. There was no indication of that on a returning visit:
the cinematic intro is gated behind the tour's localStorage key, so only a
visitor's first ever load showed anything, and even that was a fixed 3-second
animation unconnected to the actual fetch.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_bulk_loader_streams_instead_of_awaiting_whole_body():
    js = _read(DECK_JS)
    assert "getReader" in js, "download progress needs an incremental read"
    assert "_readWithProgress" in js


def test_progress_denominator_is_uncompressed_size():
    """content-length is the gzipped figure; the reader yields decoded bytes.

    Dividing by content-length races the bar to 100% about a third of the way
    through the actual download.
    """
    js = _read(DECK_JS)
    assert "x-uncompressed-size" in js
    start = js.find("function _readWithProgress")
    body = js[start:start + 1800]
    assert "content-length" not in body.lower(), (
        "must not size the download from the compressed length"
    )


def test_reader_falls_back_when_streams_unavailable():
    js = _read(DECK_JS)
    start = js.find("function _readWithProgress")
    body = js[start:start + 1800]
    assert "arrayBuffer()" in body, "must degrade to the non-streaming path"


def test_download_is_not_the_whole_bar():
    """Decode and layer build take real time; a bar that sits at 100% through
    them is worse than none."""
    js = _read(DECK_JS)
    assert "PROGRESS_DOWNLOAD_SHARE" in js
    assert "decoding" in js and "building" in js


def test_progress_element_exists_and_is_hidden_by_default():
    html = _read(INDEX_HTML)
    assert 'id="map-progress"' in html
    start = html.find('id="map-progress"')
    block = html[start:start + 800]
    assert "hidden" in block, "must not be visible before a load starts"
    assert 'role="progressbar"' in block
    assert 'aria-valuemin="0"' in block and 'aria-valuemax="100"' in block


def test_progress_runs_on_every_visit_not_just_the_first():
    """It must not be gated behind the tour's localStorage key the way the
    cinematic intro is."""
    js = _read(APP_JS)
    start = js.find("function _createMapProgress")
    body = js[start:start + 2500]
    assert "localStorage" not in body, (
        "progress must not depend on first-visit state"
    )


def test_progress_reveal_is_delayed_to_avoid_flashing_on_a_warm_cache():
    js = _read(APP_JS)
    assert "_MAP_PROGRESS_DELAY_MS" in js
    start = js.find("function _createMapProgress")
    body = js[start:start + 2500]
    assert "setTimeout" in body and "clearTimeout" in body


def test_progress_is_hidden_again_when_the_load_finishes():
    js = _read(APP_JS)
    start = js.find("function _createMapProgress")
    body = js[start:start + 2500]
    assert "done()" in body or "done(" in body
    assert "hidden = true" in body


def test_progress_never_goes_backwards():
    js = _read(APP_JS)
    start = js.find("function _createMapProgress")
    body = js[start:start + 2500]
    assert "Math.max" in body, "phases can overlap; the bar must be monotonic"


def test_indeterminate_state_exists_for_unknown_totals():
    js = _read(APP_JS)
    css = _read(STYLE_CSS)
    assert "is-indeterminate" in js
    assert ".map-progress.is-indeterminate" in css


def test_reduced_motion_is_respected():
    css = _read(STYLE_CSS)
    tail = css[css.find(".map-progress"):]
    assert "prefers-reduced-motion" in tail


def test_loader_is_still_called_with_a_size_check():
    """The streaming rewrite must not drop the existing guard that the buffer
    matches count * bytes_per_row."""
    js = _read(DECK_JS)
    assert "size mismatch" in js


def test_intro_source_count_is_not_stale():
    html = _read(INDEX_HTML)
    assert "CONNECTING TO 5 SOURCES" not in html, "corpus has four sources"
