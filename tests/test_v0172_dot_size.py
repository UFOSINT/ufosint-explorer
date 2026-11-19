"""v0.17.2 — the smallest dot size is the default dot size.

The Points SIZE slider used to start at 3 and go down to 1 while
deck.js actually rendered 2.5px on load, so the slider misreported
the current size and could shrink dots below the default. The default
is now also the floor: the slider only enlarges dots.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DECK = (ROOT / "static" / "deck.js").read_text(encoding="utf-8")


def _slider_attr(name):
    tag = re.search(r'<input[^>]*id="dot-size-slider"[^>]*>', INDEX, re.S).group(0)
    return float(re.search(rf'\b{name}="([0-9.]+)"', tag).group(1))


def _deck_default():
    return float(re.search(r"const DOT_SIZE_DEFAULT = ([0-9.]+);", DECK).group(1))


def test_slider_min_equals_default():
    assert _slider_attr("min") == _deck_default()


def test_slider_initial_value_matches_rendered_default():
    assert _slider_attr("value") == _deck_default()
    assert "let _dotSizePixels = DOT_SIZE_DEFAULT;" in DECK


def test_set_dot_size_clamps_to_default():
    assert "Math.max(DOT_SIZE_DEFAULT," in DECK
    assert "Math.max(0.5," not in DECK
