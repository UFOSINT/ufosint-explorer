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


def test_refresh_insights_calls_all_eight_renderers():
    src = _read(APP_JS)
    body = _extract_js_function(src, "refreshInsightsClientCards")
    assert body, "couldn't locate refreshInsightsClientCards body"
    # v0.8.8 emotion cards
    for fn in (
        "renderEmotionRadar",
        "renderEmotionOverTime",
        "renderEmotionBySource",
        "renderEmotionByShape",
    ):
        assert fn in body, (
            f"refreshInsightsClientCards must call {fn} — all 4 "
            f"emotion cards are now client-side in v0.8.8"
        )
    # v0.8.6 derived cards still run
    for fn in (
        "renderQualityDistribution",
        "renderMovementTaxonomy",
        "renderShapeMovementMatrix",
        "renderHoaxCurve",
    ):
        assert fn in body, (
            f"refreshInsightsClientCards must still call {fn}"
        )


def test_render_emotion_radar_reads_points_emotion_idx():
    """The radar chart should walk POINTS.visibleIdx + emotionIdx
    instead of reading an overview object from the server response."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "renderEmotionRadar")
    assert body, "couldn't locate renderEmotionRadar body"
    assert "POINTS" in body, "renderEmotionRadar must read POINTS"
    assert "emotionIdx" in body or "_collectEmotionCounts" in body, (
        "renderEmotionRadar must use POINTS.emotionIdx (or its helper)"
    )
    # The function now takes no arguments — it reads state directly.
    m = re.search(r"function renderEmotionRadar\s*\(([^)]*)\)", src)
    assert m, "couldn't find renderEmotionRadar signature"
    assert m.group(1).strip() == "", (
        "renderEmotionRadar should take no arguments in v0.8.8 "
        f"(got {m.group(1)!r})"
    )


def test_render_emotion_over_time_exists():
    """v0.8.8 renamed renderSentimentTimeline → renderEmotionOverTime
    because we no longer have a VADER compound score — just emotion
    distributions stacked by year."""
    src = _read(APP_JS)
    assert "function renderEmotionOverTime(" in src, (
        "v0.8.8 must define renderEmotionOverTime (replacing "
        "renderSentimentTimeline which depended on compound scores)"
    )
    body = _extract_js_function(src, "renderEmotionOverTime")
    # Must read dateDays + emotionIdx to build the stacked series
    assert "dateDays" in body
    assert "emotionIdx" in body


def test_render_emotion_by_source_client_side():
    src = _read(APP_JS)
    body = _extract_js_function(src, "renderEmotionBySource")
    assert body
    assert "POINTS" in body
    assert "sourceIdx" in body
    assert "emotionIdx" in body
    # Should take no arguments (client-side, reads POINTS)
    m = re.search(r"function renderEmotionBySource\s*\(([^)]*)\)", src)
    assert m and m.group(1).strip() == ""


def test_render_emotion_by_shape_client_side():
    src = _read(APP_JS)
    body = _extract_js_function(src, "renderEmotionByShape")
    assert body
    assert "POINTS" in body
    assert "shapeIdx" in body
    assert "emotionIdx" in body
    m = re.search(r"function renderEmotionByShape\s*\(([^)]*)\)", src)
    assert m and m.group(1).strip() == ""


def test_emotion_renderers_have_collect_helper():
    """_collectEmotionCounts + _emotionColor should exist as helpers
    shared across the 4 emotion cards."""
    src = _read(APP_JS)
    assert "function _collectEmotionCounts(" in src
    assert "function _emotionColor(" in src


def test_old_render_sentiment_timeline_is_gone():
    """The v0.8.6 renderSentimentTimeline took a server response
    object. v0.8.8 replaces it with renderEmotionOverTime. The old
    name should no longer be defined."""
    src = _read(APP_JS)
    assert "function renderSentimentTimeline(" not in src, (
        "renderSentimentTimeline should be gone in v0.8.8 "
        "(replaced by renderEmotionOverTime)"
    )


# =============================================================================
# Phase 3 — Methodology page additions
# =============================================================================

def test_methodology_has_mapped_section():
    html = _read(INDEX_HTML)
    assert "How Sightings Get Mapped" in html, (
        "methodology must include a How Sightings Get Mapped section"
    )


def test_methodology_has_mapped_count_table():
    """The new section must include the three-query table that
    explains the sighting / mapped / distinct-place distinction."""
    html = _read(INDEX_HTML)
    assert "Sightings on the map" in html
    assert "Distinct geocoded places" in html or "distinct" in html.lower()
    # The three key numbers should all appear in the section
    assert "614,505" in html
    assert "396,158" in html
    assert "105,854" in html


def test_methodology_has_movement_section():
    html = _read(INDEX_HTML)
    assert "Movement + Quality Classification" in html or \
           "Movement + Quality" in html
    # The 10 movement categories should be listed
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
    """quality_score, hoax_likelihood, richness_score all described."""
    html = _read(INDEX_HTML)
    assert "quality_score" in html
    assert "hoax_likelihood" in html
    assert "richness_score" in html
    # The 60-threshold for "High quality only" should be documented
    assert "60" in html  # yes, this is a loose check; we also look for context
    assert "High quality only" in html


def test_methodology_has_v083b_pipeline_notes():
    """The retirement notes section should cover the three things
    that changed in v0.8.3b: raw text stripping, duplicates table,
    sentiment table."""
    html = _read(INDEX_HTML)
    assert "v0.8.3b Data Pipeline" in html or \
           "v0.8.3b" in html
    # Raw text retirement
    assert "strip_raw_for_public" in html or \
           "raw narrative text" in html.lower()
    # Duplicates table empty
    assert "duplicate_candidate" in html
    # Sentiment table disabled
    assert "sentiment_analysis" in html or \
           "sentiment analysis" in html.lower()


def test_methodology_removed_coords_dropdown_mention():
    """The methodology previously mentioned the All Coords /
    Original Only / Geocoded Only dropdown that was deleted in v0.8.7.
    That mention should be gone."""
    html = _read(INDEX_HTML)
    # The dropdown literal strings, plus the verb "dropdown to toggle"
    # that specifically referenced the old control.
    assert "dropdown to toggle" not in html, (
        "methodology still mentions the deleted coords-toggle dropdown"
    )
    # But the geocode_src column description itself stays — that's
    # still accurate, just no longer referenced by a UI control.
    assert "geocode_src" in html
