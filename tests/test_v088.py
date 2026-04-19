"""v0.8.8 — Insights sentiment cards rewritten client-side + Methodology expanded.

The v0.8.5 reload truncated the sentiment_analysis table and the
v0.8.3b public export doesn't ship sentiment rows (they were computed
from raw narrative text, which was stripped). The 4 sentiment Insight
cards (Emotion Distribution, Sentiment Over Time, Emotions By Source,
Emotions By Shape) had been rendering blank ever since.

v0.8.8 rewrites all 4 cards to read from POINTS.emotionIdx (uint8 at
offset 22, populated for 149,607 rows and shipped in the bulk buffer).
The /api/sentiment/* endpoints are no longer called from the frontend
(they still exist as orphaned routes for future revival when the
private corpus is re-enabled).

The methodology page also gains three new sections:
  1. How Sightings Get Mapped — explains the 614k → 396k → 105k split
  2. Movement + Quality Classification — covers the v0.8.3b derived cols
  3. Notes on the v0.8.3b Data Pipeline — retirement notes for raw text,
     duplicates, and sentiment

All tests in this file are static source-code inspection, matching
the pattern of test_v086/v087.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    """Same brace-depth walker as tests/test_v087.py."""
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
# Phase 1 — Insight cards are client-side
# =============================================================================

def test_load_insights_no_longer_fetches_sentiment_endpoints():
    """v0.8.8 — loadInsights should not hit /api/sentiment/* anymore.
    Those endpoints return empty data and are effectively dead."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "loadInsights")
    assert body, "couldn't locate loadInsights body"
    for dead in (
        "/api/sentiment/overview",
        "/api/sentiment/timeline",
        "/api/sentiment/by-source",
        "/api/sentiment/by-shape",
    ):
        assert dead not in body, (
            f"loadInsights still fetches {dead!r} — v0.8.8 moved the "
            f"emotion cards to client-side POINTS.emotionIdx reads"
        )


def test_load_insights_gates_on_points_ready():
    """loadInsights must gate on POINTS.ready and schedule a retry
    when the bulk buffer isn't loaded yet. Same pattern as the
    v0.8.6 loadTimeline rewrite."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "loadInsights")
    assert "POINTS.ready" in body, (
        "loadInsights must check window.UFODeck.POINTS.ready"
    )


def test_refresh_insights_calls_all_nine_renderers():
    """v0.11: refreshInsightsClientCards calls 9 renderers — 5 new
    transformer emotion cards + 4 existing quality/movement cards."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "refreshInsightsClientCards")
    assert body, "couldn't locate refreshInsightsClientCards body"
    # v0.11 transformer emotion cards
    for fn in (
        "renderSentimentGroup",
        "renderEmotion7",
        "renderGoEmotions28",
        "renderSentimentScores",
        "renderEmotionBySourceV11",
    ):
        assert fn in body, (
            f"refreshInsightsClientCards must call {fn}"
        )
    # Existing derived cards still run
    for fn in (
        "renderQualityDistribution",
        "renderMovementTaxonomy",
        "renderShapeMovementMatrix",
        "renderHoaxCurve",
    ):
        assert fn in body, (
            f"refreshInsightsClientCards must still call {fn}"
        )


def test_v011_emotion_renderers_exist():
    """v0.11 replaced the v0.8.8 keyword-classifier emotion cards with
    5 new transformer-based renderers. The old renderEmotionRadar,
    renderEmotionOverTime, renderEmotionBySource, renderEmotionByShape,
    _collectEmotionCounts, and _emotionColor are all deleted."""
    src = _read(APP_JS)
    # New renderers must exist
    for fn in (
        "function renderSentimentGroup(",
        "function renderEmotion7(",
        "function renderGoEmotions28(",
        "function renderSentimentScores(",
        "function renderEmotionBySourceV11(",
    ):
        assert fn in src, f"v0.11 must define {fn}"

    # Old renderers must be gone
    for fn in (
        "function renderEmotionRadar(",
        "function renderEmotionOverTime(",
        "function renderEmotionByShape(",
        "function _collectEmotionCounts(",
        "function _emotionColor(",
    ):
        assert fn not in src, f"v0.11 deleted {fn}"


def test_v011_emotion_color_constants():
    """v0.11 uses _SENTI_GROUP_COLORS and _EMO7_COLORS instead of
    the old EMOTION_COLORS/EMOTION_NAMES from the keyword classifier."""
    src = _read(APP_JS)
    assert "_SENTI_GROUP_COLORS" in src
    assert "_EMO7_COLORS" in src


# =============================================================================
# Phase 3 — Methodology page additions
#
# v0.14 — The dedup team rewrote the methodology HTML from scratch in
# docs/METHODOLOGY_SITE_v014.html with a new section layout and updated
# numbers (618,316 sightings, 418,077 mapped). The v0.8.8-era tests
# below used to assert specific heading strings from the old layout;
# they're relaxed to match the new structure while keeping the spirit
# (must document mapping, movement, quality, and reproduction).
# =============================================================================

def test_methodology_has_geocoding_section():
    """v0.14: "How Sightings Get Mapped" was split into distinct
    "Geocoding" and "Pipeline Architecture" sections."""
    html = _read(INDEX_HTML)
    assert "Geocoding" in html, (
        "methodology must include a Geocoding section"
    )
    assert "Pipeline Architecture" in html, (
        "methodology must include the Pipeline Architecture section"
    )


def test_methodology_has_v014_counts():
    """The v0.14 numbers must appear in the methodology: 618,316
    total sightings and 418,077 mapped."""
    html = _read(INDEX_HTML)
    assert "618,316" in html, "methodology must mention the 618,316 total"
    assert "418,077" in html, "methodology must mention the 418,077 mapped count"


def test_methodology_has_movement_section():
    """v0.14: "Movement + Quality Classification" was split into
    separate "Quality Score" and "Movement & Behavior Classification"
    sections. The 10 movement categories should still be listed."""
    html = _read(INDEX_HTML)
    assert "Movement" in html and "Classification" in html
    for cat in (
        "hovering",
        "linear",
        "erratic",
        "accelerating",
        "rotating",
        "ascending",
        "descending",
        "vanished",
        "followed",
        "landed",
    ):
        assert cat in html.lower(), (
            f"methodology must list the {cat!r} movement category"
        )


def test_methodology_has_quality_section():
    """v0.14 has an explicit Quality Score section."""
    html = _read(INDEX_HTML)
    assert "Quality Score" in html, (
        "methodology must include a Quality Score section"
    )


def test_methodology_has_reproduction_notes():
    """v0.14: the v0.8.3b-specific 'Data Pipeline' section was
    replaced by a 'How to Reproduce This Database' section that
    covers the same territory (raw narrative retirement, ufo_public
    export)."""
    html = _read(INDEX_HTML)
    assert "Reproduce" in html or "reproduce" in html.lower()
    assert "ufo_public" in html
    # Raw text retirement mention
    assert "raw narrative" in html.lower() or "strip_raw" in html


def test_methodology_removed_coords_dropdown_mention():
    """The methodology previously mentioned the All Coords /
    Original Only / Geocoded Only dropdown that was deleted in v0.8.7.
    That mention should be gone."""
    html = _read(INDEX_HTML)
    assert "dropdown to toggle" not in html, (
        "methodology still mentions the deleted coords-toggle dropdown"
    )
