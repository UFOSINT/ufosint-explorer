"""v0.8.3 — Raw text retirement (search + detail rewire).

Regression tests that lock the v0.8.3 contract: no code path should
read `description`, `summary`, `notes`, or `raw_json` from the
sighting table, and no response should expose those keys. This
makes scripts/strip_raw_for_public.py safe to run.

The 4 columns being dropped:
  - description (big narrative text)
  - summary (short narrative)
  - notes (free-text annotations)
  - raw_json (full original JSON from the source database)

Columns explicitly PRESERVED in the public schema (operator choice):
  - date_event_raw, time_raw
  - witness_names, witness_age, witness_sex
  - explanation, characteristics, weather, terrain

These are short structured-ish free text and are flagged in
docs/V083_BACKLOG.md for science-team cleanup in v0.8.4+.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
STYLE_CSS = ROOT / "static" / "style.css"
CHANGELOG = ROOT / "CHANGELOG.md"
PLAN_DOC = ROOT / "docs" / "V083_PLAN.md"
STRIP_PY = ROOT / "scripts" / "strip_raw_for_public.py"
STRIP_SQL = ROOT / "scripts" / "drop_raw_text_columns.sql"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Plan doc
# ---------------------------------------------------------------------------
def test_v083_plan_doc_exists():
    assert PLAN_DOC.exists(), (
        "docs/V083_PLAN.md is the architecture doc for v0.8.3. Must "
        "exist in-tree before the raw-text drop lands."
    )


def test_v083_plan_doc_covers_key_concepts():
    doc = _read(PLAN_DOC)
    for concept in (
        "strip_raw_for_public.py",
        "faceted",
        "/api/sighting/:id",
        "/api/search",
        "Data Quality",
        "Derived Metadata",
        "description",
        "summary",
        "raw_json",
    ):
        assert concept in doc, f"plan doc missing coverage of {concept!r}"


# ---------------------------------------------------------------------------
# CHANGELOG (added below when shipping — softly asserted for now)
# ---------------------------------------------------------------------------
def test_changelog_has_v083_section():
    log = _read(CHANGELOG)
    assert "[0.8.3]" in log, (
        "CHANGELOG must have a [0.8.3] section before shipping"
    )


# ---------------------------------------------------------------------------
# Backend — app.py no longer reads the 4 raw columns
# ---------------------------------------------------------------------------
def test_api_sighting_uses_explicit_column_list():
    """The sighting detail endpoint must NOT use SELECT s.* anymore
    (which would implicitly pull every column including the 4 being
    dropped). It uses an explicit _SIGHTING_DETAIL_COLUMNS tuple."""
    src = _read(APP_PY)
    # Find api_sighting function body
    start = src.find("def api_sighting(")
    assert start != -1, "api_sighting not found"
    end = src.find("\n@app.route", start + 1)
    if end == -1:
        end = len(src)
    body = src[start:end]
    assert "SELECT s.*" not in body, (
        "api_sighting must use an explicit SELECT list, not SELECT s.* — "
        "s.* would pull description/summary/notes/raw_json and fail when "
        "strip_raw_for_public.py drops those columns"
    )
    # And the explicit list constant exists
    assert "_SIGHTING_DETAIL_COLUMNS" in src


def _sighting_detail_columns_block(src: str) -> str:
    """Return the full text of the _SIGHTING_DETAIL_COLUMNS tuple
    literal. Uses a balanced-paren scan so individual column entries
    like `s.country, l.region,` don't terminate the slice early."""
    start = src.find("_SIGHTING_DETAIL_COLUMNS = (")
    assert start != -1, "_SIGHTING_DETAIL_COLUMNS constant not found"
    # Scan forward from the opening paren of the tuple literal, tracking
    # depth so nested parens (there shouldn't be any but be defensive)
    # don't throw us off.
    open_paren = src.index("(", start)
    depth = 0
    for i in range(open_paren, len(src)):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unterminated _SIGHTING_DETAIL_COLUMNS tuple")


def test_sighting_detail_columns_has_no_raw_text():
    """_SIGHTING_DETAIL_COLUMNS must not name any of the 4 dropped
    columns."""
    block = _sighting_detail_columns_block(_read(APP_PY))
    for forbidden in ("s.description", "s.summary", "s.notes", "s.raw_json"):
        assert forbidden not in block, (
            f"_SIGHTING_DETAIL_COLUMNS names {forbidden!r}, which will "
            f"fail after strip_raw_for_public.py drops the column"
        )


def test_sighting_detail_columns_has_derived_fields():
    """The explicit SELECT must include all 9 v0.8.2 derived fields so
    the detail modal can render Data Quality + Derived Metadata sections."""
    block = _sighting_detail_columns_block(_read(APP_PY))
    for col in (
        "s.standardized_shape",
        "s.primary_color",
        "s.dominant_emotion",
        "s.quality_score",
        "s.richness_score",
        "s.hoax_likelihood",
        "s.has_description",
        "s.has_media",
        "s.sighting_datetime",
    ):
        assert col in block, f"_SIGHTING_DETAIL_COLUMNS missing derived field {col!r}"


def test_api_sighting_drops_raw_json_parse_block():
    """The legacy `json.loads(record['raw_json'])` block must be gone."""
    src = _read(APP_PY)
    start = src.find("def api_sighting(")
    end = src.find("\n@app.route", start + 1)
    body = src[start:end] if end != -1 else src[start:]
    # The v0.8.2-and-earlier code parsed raw_json. Must not anymore.
    assert 'json.loads(record["raw_json"])' not in body
    assert "json.loads(record['raw_json'])" not in body


def test_api_search_no_longer_ilike_description():
    """The /api/search q parameter must not reference s.description or
    s.summary in its WHERE clause anymore. It uses a 7-column faceted
    match over location/shape/color/emotion/source."""
    src = _read(APP_PY)
    start = src.find("def api_search(")
    assert start != -1
    end = src.find("\n# ---", start + 1)
    body = src[start:end] if end != -1 else src[start:start + 5000]
    assert "s.description ILIKE" not in body
    assert "s.summary ILIKE" not in body
    # And the new faceted clause is in place
    assert "standardized_shape" in body
    assert "primary_color" in body
    assert "dominant_emotion" in body
    assert "sd.name" in body  # source name match


def test_api_search_response_has_no_description_key():
    """The results dicts built by api_search must not write a
    `description` key anymore."""
    src = _read(APP_PY)
    start = src.find("def api_search(")
    end = src.find("\n# ---", start + 1)
    body = src[start:end] if end != -1 else src[start:start + 5000]
    # Look for a dict literal writing the description key
    assert '"description":' not in body, (
        "api_search response still writes a description key"
    )


def test_api_search_response_has_derived_fields():
    """New response carries the derived fields the UI needs to
    render the compound card."""
    src = _read(APP_PY)
    start = src.find("def api_search(")
    end = src.find("\n# ---", start + 1)
    body = src[start:end] if end != -1 else src[start:start + 5000]
    for key in (
        '"quality_score":',
        '"hoax_likelihood":',
        '"dominant_emotion":',
        '"has_description":',
        '"has_media":',
    ):
        assert key in body, f"api_search missing {key}"


def test_export_columns_drop_description_summary():
    """EXPORT_COLUMNS must not include description/summary."""
    src = _read(APP_PY)
    start = src.find("EXPORT_COLUMNS = [")
    assert start != -1
    end = src.find("]", start) + 1
    block = src[start:end]
    assert '"description"' not in block
    assert '"summary"' not in block


def test_export_columns_add_derived_fields():
    src = _read(APP_PY)
    start = src.find("EXPORT_COLUMNS = [")
    end = src.find("]", start) + 1
    block = src[start:end]
    for col in (
        '"quality_score"',
        '"hoax_likelihood"',
        '"dominant_emotion"',
        '"has_description"',
        '"standardized_shape"',
    ):
        assert col in block, f"EXPORT_COLUMNS missing {col}"


def test_build_export_query_no_description_ilike():
    src = _read(APP_PY)
    start = src.find("def _build_export_query(")
    end = src.find("\n@app.route", start + 1)
    body = src[start:end] if end != -1 else src[start:start + 3000]
    assert "s.description ILIKE" not in body
    assert "s.summary ILIKE" not in body
    # And the SELECT list never references them
    assert "s.description" not in body
    assert "s.summary" not in body


# ---------------------------------------------------------------------------
# Frontend — app.js no longer renders r.description / r.summary / r.raw_json
# ---------------------------------------------------------------------------
def test_open_detail_does_not_render_description():
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("\n// ===", start + 1)
    if end == -1:
        end = js.find("function closeModal", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 15000]

    # The v0.8.2 body used r.description / r.summary in the
    # "Description" section. v0.8.3 dropped that whole section.
    # We look for the specific rendering patterns that would
    # inject raw text into the DOM, not mere name mentions in
    # comments.
    for pat in (
        "r.description ||",
        "r.summary ||",
        "escapeHtml(r.description)",
        "escapeHtml(r.summary)",
        "JSON.stringify(r.raw_json",
        'innerHTML += "<div class="detail-desc"',
        "<h3>Description</h3>",
    ):
        assert pat not in body, (
            f"openDetail still renders {pat!r} — the v0.8.3 rewrite "
            f"should have replaced the Description section with "
            f"Data Quality + Derived Metadata"
        )


def test_open_detail_renders_data_quality_section():
    """New Data Quality section must render quality_score / richness_score /
    hoax_likelihood bars when present."""
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("function closeModal", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 15000]
    assert "Data Quality" in body
    assert "r.quality_score" in body
    assert "r.hoax_likelihood" in body
    assert "quality-bar" in body


def test_open_detail_renders_derived_metadata_section():
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("function closeModal", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 15000]
    assert "Derived Metadata" in body
    assert "r.standardized_shape" in body
    assert "r.primary_color" in body
    assert "r.dominant_emotion" in body


def test_execute_search_card_has_no_description_snippet():
    """Result cards must not inject r.description anymore."""
    js = _read(APP_JS)
    start = js.find("async function executeSearch(")
    end = js.find("\nfunction ", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 8000]

    assert "r.description" not in body, (
        "executeSearch still references r.description in the result "
        "card template"
    )
    # <mark> highlighting also goes away
    assert "escapeRegExp(q)" not in body, (
        "The q-highlight regex is obsolete — there's no description "
        "text to highlight anymore"
    )
    # No .result-desc class in the template (only in legacy CSS)
    assert 'class="result-desc"' not in body


def test_execute_search_card_has_derived_metadata_line():
    js = _read(APP_JS)
    start = js.find("async function executeSearch(")
    end = js.find("\nfunction ", start + 1)
    body = js[start:end] if end != -1 else js[start:start + 8000]
    assert "r.quality_score" in body or "quality_score" in body
    assert "result-derived" in body


# ---------------------------------------------------------------------------
# CSS — new classes exist
# ---------------------------------------------------------------------------
def test_css_has_quality_bar_rules():
    css = _read(STYLE_CSS)
    assert ".quality-bar" in css
    assert ".quality-bar-fill" in css
    assert ".quality-bar-hoax" in css  # inverted-danger variant


def test_css_has_result_derived_rule():
    css = _read(STYLE_CSS)
    assert ".result-derived" in css


# ---------------------------------------------------------------------------
# strip_raw_for_public.py / drop_raw_text_columns.sql — column list trimmed
# ---------------------------------------------------------------------------
def test_strip_script_raw_columns_list_has_exactly_four():
    """The operator's sign-off was 4 columns. v0.8.3 trims the script
    from its original 6-column list down to the 4 the operator chose.
    If someone re-adds date_event_raw or time_raw without updating
    docs, this test flags it."""
    src = _read(STRIP_PY)
    start = src.find("RAW_COLUMNS = [")
    assert start != -1
    end = src.find("]", start) + 1
    block = src[start:end]
    # The 4 columns that MUST be in the list:
    for col in ('"description"', '"summary"', '"notes"', '"raw_json"'):
        assert col in block, f"RAW_COLUMNS missing {col}"
    # The 2 columns that must NOT be in the list (operator trimmed):
    for col in ('"date_event_raw"', '"time_raw"'):
        assert col not in block, (
            f"RAW_COLUMNS includes {col}, but the operator chose to keep "
            f"it in the public schema. See docs/V083_PLAN.md."
        )


def test_drop_sql_matches_strip_script_column_list():
    """The ALTER TABLE DROP COLUMN list in drop_raw_text_columns.sql
    must match the Python script's RAW_COLUMNS. Otherwise a user who
    runs the SQL file directly (not through the Python wrapper) gets
    a different outcome."""
    sql = _read(STRIP_SQL)
    # Each of the 4 must appear in a DROP COLUMN clause
    for col in ("description", "summary", "notes", "raw_json"):
        pattern = rf"DROP COLUMN IF EXISTS {col}"
        assert re.search(pattern, sql), (
            f"drop_raw_text_columns.sql must DROP COLUMN {col}"
        )
    # The 2 that must NOT be dropped:
    for col in ("date_event_raw", "time_raw"):
        pattern = rf"DROP COLUMN IF EXISTS {col}"
        assert not re.search(pattern, sql), (
            f"drop_raw_text_columns.sql still drops {col}, but the operator "
            f"chose to keep it. See docs/V083_PLAN.md."
        )
