"""v0.16.7 — the map opens on the world, not the contiguous US.

The corpus stopped being US-only, but the map still opened at
`center: [39, -98], zoom: 4` — tight on the lower 48. Country filtering
added in v0.16.5 was effectively undiscoverable, because every non-US
sighting sat outside the first thing a visitor ever sees.

The wrapping asserts below are not cosmetic. Leaflet repeats the basemap
horizontally by default; deck.gl draws each point once at its true
longitude. At the zoom levels a world view actually uses, that mismatch
renders as empty duplicate continents flanking the real one.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"


def _code_only(text: str) -> str:
    """Strip // line comments so assertions test code, not prose.

    Learned the hard way in v0.16.6: a test asserting a string was absent
    tripped on the comment explaining why it was absent.
    """
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in text.splitlines()
    )


def _init_map_block() -> str:
    src = _code_only(APP_JS.read_text(encoding="utf-8"))
    start = src.find("function initMap()")
    assert start != -1, "initMap() not found in app.js"
    return src[start:start + 2000]


def test_us_default_center_is_gone():
    """[39, -98] is the centroid of the contiguous US."""
    code = _code_only(APP_JS.read_text(encoding="utf-8"))
    assert not re.search(r"\[\s*39\s*,\s*-98\s*\]", code), (
        "the map still defaults to the US centroid"
    )


def test_initial_view_fits_world_bounds():
    block = _init_map_block()
    assert "fitBounds" in block, (
        "initMap must fit bounds rather than pin a fixed zoom — the world "
        "is 256 * 2^zoom px wide, so no single zoom fits both a phone and "
        "a wide desktop"
    )
    # Longitudes must span effectively the whole range.
    m = re.search(r"fitBounds\(\s*\[\s*\[([-\d.]+),\s*([-\d.]+)\]\s*,\s*"
                  r"\[([-\d.]+),\s*([-\d.]+)\]", block)
    assert m, "could not parse the fitBounds call"
    south, west, north, east = (float(g) for g in m.groups())
    assert west <= -150 and east >= 150, (
        f"longitude span {west}..{east} does not cover the world"
    )
    assert south <= -50 and north >= 70, (
        f"latitude span {south}..{north} omits inhabited land"
    )


def test_world_copies_are_suppressed():
    """Wrapped basemap copies would render as empty continents."""
    block = _init_map_block()
    assert "noWrap: true" in block, "tile layer must set noWrap"
    assert "maxBounds" in block, "map must constrain panning to one world"
