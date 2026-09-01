"""v0.16.7 — the map opens on the world, not the contiguous US.

The corpus stopped being US-only, but the map still opened at
`center: [39, -98], zoom: 4` — tight on the lower 48. Country filtering
added in v0.16.5 was effectively undiscoverable, because every non-US
sighting sat outside the first thing a visitor ever sees.

v0.16.8 amended this twice-over:

  - The default is one zoom step in from the fitted bounds — what a click
    on the "+" control gives you. The bare fit framed the world with dead
    space around it.
  - Horizontal tiling is allowed again. v0.16.7 had set `noWrap` and
    `maxBounds` to hide a real mismatch (Leaflet repeats the basemap;
    deck.gl draws each point once, so the copies carry no dots), but
    stepping in past the fit crops the edges, and walling off the pan at
    one world width is worse than the cosmetic duplication.

So the wrap assertions below are deliberately inverted from v0.16.7's.
Restoring `noWrap` would re-break panning, which is why this is pinned
rather than left to comments.
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


def test_default_view_is_one_step_in_from_the_fit():
    """v0.16.8 — the bare fit leaves the world floating in dead space.

    One zoom step in is what a click on the "+" control does, so the
    default lands where the dot density reads instead of at the widest
    possible framing.
    """
    block = _init_map_block()
    m = re.search(r"zoomIn\(\s*(\d+)", block)
    assert m, "initMap must step in from the fitted bounds via zoomIn()"
    assert int(m.group(1)) == 1, (
        f"expected a single zoom step (one '+' click), got {m.group(1)}"
    )
    # Order matters: zoomIn before fitBounds would be discarded.
    assert block.find("fitBounds") < block.find("zoomIn"), (
        "zoomIn must run after fitBounds or the fit overwrites it"
    )


def test_horizontal_tiling_is_allowed():
    """v0.16.8 reverted v0.16.7's wrap suppression.

    Stepping in past the fit crops the far edges, so panning across the
    antimeridian has to wrap rather than hit a wall. `noWrap` and
    `maxBounds` would each reintroduce that wall.
    """
    block = _init_map_block()
    assert "noWrap" not in block, (
        "noWrap blocks the repeated basemap — horizontal tiling is wanted"
    )
    assert "maxBounds" not in block, (
        "maxBounds walls off horizontal panning at one world width"
    )
