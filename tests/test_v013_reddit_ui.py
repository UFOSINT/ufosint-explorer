"""v0.13 — Reddit r/UFOs UI surfacing.

Regression tests that lock in:
  - /api/sighting/<id> SELECTs the new Reddit + LLM columns
  - /api/map payload carries source_db_id for frontend styling
  - openDetail renders the Reddit "View original" link, Narrative
    section, and LLM Analysis section when the relevant fields are
    populated
  - Map marker palette includes the Reddit source
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
STYLE_CSS = ROOT / "static" / "style.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

def test_sighting_detail_columns_include_reddit_fields():
    src = _read(APP_PY)
    # The v0.13 additions must be in the explicit SELECT tuple so the
    # /api/sighting/<id> response carries them.
    for col in (
        "s.reddit_post_id",
        "s.reddit_url",
        "s.llm_confidence",
        "s.llm_anomaly_assessment",
        "s.llm_prosaic_candidate",
        "s.llm_strangeness_rating",
        "s.llm_model",
        "s.description",
        "s.has_photo",
        "s.has_video",
        "s.source_db_id",
    ):
        assert col in src, (
            f"_SIGHTING_DETAIL_COLUMNS must include {col!r} "
            "(v0.13 Reddit schema)"
        )


def test_api_map_payload_includes_source_db_id():
    """/api/map markers carry source_db_id so the frontend can style
    by source (e.g. Reddit-orange for id=6 without a string-compare
    on name)."""
    src = _read(APP_PY)
    # Look for the marker-dict assembly
    start = src.find("markers.append({")
    assert start != -1, "markers.append({...}) block not found in /api/map"
    end = src.find("})", start) + 2
    block = src[start:end]
    assert '"source_db_id"' in block or "'source_db_id'" in block, (
        "/api/map marker payload must include source_db_id for v0.13 "
        "Reddit styling"
    )


# ---------------------------------------------------------------------------
# Frontend — popup
# ---------------------------------------------------------------------------

def test_open_detail_renders_reddit_link_when_reddit_url_present():
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("function closeModal", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 30000]

    # Conditional render on r.reddit_url
    assert "r.reddit_url" in body, (
        "openDetail must branch on r.reddit_url to render the "
        "\"View on r/UFOs\" link"
    )
    assert "reddit-link" in body, (
        "Reddit link should use the .reddit-link CSS class so it can "
        "be styled distinctly"
    )
    # And must use rel=\"noopener\" on the external link for security
    assert "noopener" in body, (
        "Reddit external link must carry rel=\"noopener\" to prevent "
        "window.opener leakage"
    )


def test_open_detail_renders_llm_summary_narrative():
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("function closeModal", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 30000]

    # Narrative section renders r.description
    assert "r.description" in body, (
        "openDetail must render r.description (the LLM summary for "
        "Reddit; NULL for legacy sources)"
    )
    assert "detail-narrative" in body, (
        "Narrative section should use the .detail-narrative CSS class"
    )
    # Must use escapeHtml — never raw interpolation
    assert "escapeHtml(r.description)" in body, (
        "r.description must go through escapeHtml() — XSS defense"
    )


def test_open_detail_renders_llm_analysis_section():
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("function closeModal", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 30000]

    # All four LLM fields are conditionally rendered
    for pat in (
        "r.llm_strangeness_rating",
        "r.llm_confidence",
        "r.llm_anomaly_assessment",
        "r.llm_prosaic_candidate",
    ):
        assert pat in body, (
            f"openDetail must check {pat!r} for the LLM Analysis section"
        )
    # Strangeness meter element
    assert "strangeness-meter" in body
    # Chip elements for confidence / assessment
    assert "llm-chip" in body


# ---------------------------------------------------------------------------
# Frontend — marker palette
# ---------------------------------------------------------------------------

def test_source_colors_include_reddit():
    js = _read(APP_JS)
    start = js.find("const SOURCE_COLORS = {")
    end = js.find("};", start) + 2
    block = js[start:end]

    # Either "r/UFOs" (v0.13 canonical) or "Reddit-UFOs" (compat alias)
    # must be present
    assert "r/UFOs" in block or "Reddit-UFOs" in block, (
        "SOURCE_COLORS must include an entry for the Reddit source"
    )
    # And the color should be reddish-orange — loose check, any hex
    # starting with #ff or #cc/ee in the red range
    assert "#ff4500" in block or "#ff" in block.lower(), (
        "Reddit source expected to use Reddit-orange (#ff4500)"
    )


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def test_css_has_reddit_and_llm_rules():
    css = _read(STYLE_CSS)
    # Reddit link button
    assert ".reddit-link" in css
    # Narrative section
    assert ".detail-narrative" in css
    # Strangeness meter
    assert ".strangeness-meter" in css
    # LLM chip base + at least one variant
    assert ".llm-chip" in css
    assert ".llm-chip-anomalous" in css or ".llm-chip-high" in css
