"""v0.8.2 — Derived public fields + column probe + quality rail UI.

Locks the v0.8.2 contract. The actual binary round-trip + endpoint
behaviour is exercised in tests/test_v080_bulk.py (which was updated
to the v082-1 row layout); this file focuses on:

  1. The ALTER-TABLE migration SQL exists and is idempotent.
  2. The deploy workflow checks out and runs it between the v0.7
     index migration and the v0.7.5 MV refresh.
  3. migrate_sqlite_to_pg.py knows about the new columns and probes
     the target schema so running against a pre-v0.8.2 PG doesn't
     error on missing columns.
  4. app.py has the column probe + dynamic SELECT clause + new
     helper functions (_points_bulk_column_set, _epoch_days_1900,
     _duration_log2).
  5. deck.js exposes the v0.8.2 API (qualityMin, hoaxMax, color /
     emotion / has_desc / has_media filters, dayPrecision).
  6. The Quality rail HTML + CSS + mountQualityRail() wiring exists.
  7. The unapplied drop_raw_text_columns.sql script is present with
     the safety probes.
  8. CHANGELOG has a v0.8.2 section.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"
CHANGELOG = ROOT / "CHANGELOG.md"
PLAN_DOC = ROOT / "docs" / "V082_PLAN.md"
ADD_MIG = ROOT / "scripts" / "add_v082_derived_columns.sql"
DROP_MIG = ROOT / "scripts" / "drop_raw_text_columns.sql"
MIGRATE_PY = ROOT / "scripts" / "migrate_sqlite_to_pg.py"
DEPLOY_YML = ROOT / ".github" / "workflows" / "azure-deploy.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Plan doc + changelog
# ---------------------------------------------------------------------------
def test_plan_doc_exists():
    assert PLAN_DOC.exists(), (
        "docs/V082_PLAN.md must exist — the whole v0.8.2 rollout depends "
        "on the migration + data-policy story being documented in-tree."
    )


def test_plan_doc_covers_key_concepts():
    doc = _read(PLAN_DOC)
    for concept in (
        "standardized_shape",
        "quality_score",
        "hoax_likelihood",
        "column probe",
        "255",  # the sentinel value for unknown scores
        "coverage",
        "date_days",
    ):
        assert concept in doc, f"plan doc missing coverage of {concept!r}"


def test_changelog_has_v082_section():
    log = _read(CHANGELOG)
    assert "[0.8.2]" in log
    assert "derived" in log.lower() or "quality" in log.lower()


# ---------------------------------------------------------------------------
# Migration SQL
# ---------------------------------------------------------------------------
def test_add_migration_exists():
    assert ADD_MIG.exists()


def test_add_migration_is_idempotent():
    sql = _read(ADD_MIG)
    # ALTER TABLE must use ADD COLUMN IF NOT EXISTS so re-running on
    # every deploy is safe.
    assert "ADD COLUMN IF NOT EXISTS" in sql
    # CONCURRENTLY + IF NOT EXISTS on every index so the migration
    # can re-run without errors and without blocking writes.
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in sql


def test_add_migration_covers_all_required_columns():
    sql = _read(ADD_MIG)
    for col in (
        "lat",
        "lng",
        "sighting_datetime",
        "standardized_shape",
        "primary_color",
        "dominant_emotion",
        "quality_score",
        "richness_score",
        "hoax_likelihood",
        "has_description",
        "has_media",
        "topic_id",
    ):
        assert col in sql, f"migration missing column {col!r}"


def test_drop_raw_text_migration_exists_but_is_unapplied():
    assert DROP_MIG.exists()
    sql = _read(DROP_MIG)
    # Has safety probes
    assert "quality_score" in sql
    assert "RAISE EXCEPTION" in sql
    assert "DROP COLUMN" in sql
    # Is NOT in the deploy workflow
    yml = _read(DEPLOY_YML)
    assert "drop_raw_text_columns.sql" not in yml, (
        "drop_raw_text_columns.sql must NOT be in the deploy workflow — "
        "it's a one-off manual migration, not an every-deploy step"
    )


# ---------------------------------------------------------------------------
# Deploy workflow integration
# ---------------------------------------------------------------------------
def test_deploy_workflow_runs_v082_migration():
    yml = _read(DEPLOY_YML)
    assert "add_v082_derived_columns.sql" in yml
    assert "Apply v0.8.2 derived-field migration" in yml


def test_deploy_workflow_v082_runs_after_v07_indexes():
    yml = _read(DEPLOY_YML)
    v07_pos = yml.find("Apply v0.7 index migrations")
    v082_pos = yml.find("Apply v0.8.2 derived-field migration")
    assert v07_pos != -1 and v082_pos != -1
    assert v07_pos < v082_pos


# ---------------------------------------------------------------------------
# migrate_sqlite_to_pg.py — column probe + new columns
# ---------------------------------------------------------------------------
def test_migrator_knows_new_columns():
    src = _read(MIGRATE_PY)
    for col in (
        "quality_score",
        "hoax_likelihood",
        "standardized_shape",
        "primary_color",
        "dominant_emotion",
        "has_description",
        "has_media",
        "sighting_datetime",
    ):
        assert col in src


def test_migrator_has_pg_column_probe():
    """The migrator must intersect its TABLES column list with what
    actually exists in the target PG schema, otherwise a COPY into a
    pre-v0.8.2 schema fails on missing columns."""
    src = _read(MIGRATE_PY)
    assert "def pg_columns" in src
    assert "information_schema.columns" in src


# ---------------------------------------------------------------------------
# app.py — points-bulk schema bump + helpers
# ---------------------------------------------------------------------------
def test_app_schema_version_bumped():
    """v0.8.5 bumped the schema again to v083-1 (32-byte row with
    movement_flags). v082-1 is preserved in git history."""
    src = _read(APP_PY)
    assert '_POINTS_BULK_SCHEMA_VERSION = "v083-1"' in src


def test_app_bytes_per_row_is_28():
    """v0.8.5 grew the row to 32 bytes. See test_v085_movement for
    the full v0.8.3b/v0.8.5 layout contract."""
    src = _read(APP_PY)
    assert "_POINTS_BULK_BYTES_PER_ROW = 32" in src


def test_app_has_column_probe_helper():
    src = _read(APP_PY)
    assert "def _points_bulk_column_set" in src
    assert "information_schema.columns" in src
    assert "_POINTS_BULK_DERIVED_COLS" in src


def test_app_has_date_days_helper():
    src = _read(APP_PY)
    assert "def _epoch_days_1900" in src


def test_app_has_duration_log2_helper():
    src = _read(APP_PY)
    assert "def _duration_log2" in src
    assert "math.log2" in src


def test_app_etag_includes_column_set():
    """The ETag must change when the v0.8.2 migration lands and new
    columns appear, otherwise browsers keep using stale cached
    buffers even after the schema upgrade."""
    src = _read(APP_PY)
    etag_start = src.find("def _points_bulk_etag")
    etag_end = src.find("\n\n\n", etag_start)
    body = src[etag_start:etag_end]
    assert "_points_bulk_column_set" in body


def test_app_score_unknown_sentinel_is_255():
    src = _read(APP_PY)
    assert "_POINTS_BULK_SCORE_UNKNOWN = 255" in src


def test_app_builds_meta_coverage_map():
    """The meta sidecar must include per-field coverage counts so
    the client can disable filter toggles for unpopulated fields."""
    src = _read(APP_PY)
    assert '"coverage":' in src or "'coverage':" in src
    assert '"columns_present":' in src or "'columns_present':" in src


# ---------------------------------------------------------------------------
# deck.js — v0.8.2 API surface
# ---------------------------------------------------------------------------
def test_deck_js_has_new_typed_arrays():
    js = _read(DECK_JS)
    for name in (
        "dateDays",
        "qualityScore",
        "hoaxScore",
        "richnessScore",
        "colorIdx",
        "emotionIdx",
        "flags",
        "numWitnesses",
        "durationLog2",
    ):
        assert name in js, f"deck.js POINTS object missing {name}"


def test_deck_js_filter_pipeline_has_v082_predicates():
    """_rebuildVisible must handle the new filter fields. Locked as
    literal string presence checks so a refactor that drops one
    fails loudly."""
    js = _read(DECK_JS)
    for pred in (
        "qualityMin",
        "hoaxMax",
        "richnessMin",
        "colorName",
        "emotionName",
        "hasDescription",
        "hasMedia",
        "FLAG_HAS_DESC",
        "FLAG_HAS_MEDIA",
        "SCORE_UNKNOWN",
    ):
        assert pred in js, f"deck.js filter pipeline missing {pred}"


def test_deck_js_exposes_v082_public_api():
    js = _read(DECK_JS)
    for name in (
        "getDayRange",
        "getCoverage",
        "getColumnsPresent",
        "getShapes",
        "getColors",
        "getEmotions",
        "getSources",
        "getShapeSource",
    ):
        assert name in js, f"deck.js public API missing {name}"


def test_deck_js_supports_day_precision_time_window():
    """setTimeWindow must accept a { dayPrecision } option so the
    TimeBrush can advance by day during playback instead of jumping
    by whole years."""
    js = _read(DECK_JS)
    assert "dayPrecision" in js


def test_deck_js_year_histogram_walks_date_days():
    """getYearHistogram must walk POINTS.dateDays (not a legacy year
    field) and use a binary-search lookup for performance."""
    js = _read(DECK_JS)
    start = js.find("function getYearHistogram")
    end = js.find("function getYearRange", start)
    body = js[start:end] if start != -1 else ""
    assert "dateDays" in body
    assert "yearStarts" in body  # binary-search lookup table


# ---------------------------------------------------------------------------
# app.js — quality rail mount + filter wiring
# ---------------------------------------------------------------------------
def test_app_js_has_mount_quality_rail():
    js = _read(APP_JS)
    assert "function mountQualityRail" in js
    assert "state.qualityFilter" in js


def test_app_js_quality_filter_wires_into_apply_client_filters():
    """applyClientFilters must read state.qualityFilter and translate
    it into the filter object deck.js understands."""
    js = _read(APP_JS)
    start = js.find("function applyClientFilters")
    end = js.find("\nfunction ", start + 10)
    body = js[start:end]
    assert "qualityMin" in body
    assert "hoaxMax" in body
    assert "hasDescription" in body
    assert "hasMedia" in body


def test_app_js_quality_rail_disables_unpopulated_toggles():
    """mountQualityRail must check UFODeck.getCoverage() and disable
    toggles whose column has 0 populated rows."""
    js = _read(APP_JS)
    start = js.find("function mountQualityRail")
    end = js.find("\nfunction ", start + 10)
    body = js[start:end]
    assert "getCoverage" in body
    assert "disabled" in body.lower() or "rail-toggle-disabled" in body


# ---------------------------------------------------------------------------
# HTML + CSS
# ---------------------------------------------------------------------------
def test_index_html_has_quality_rail_section():
    html = _read(INDEX_HTML)
    assert 'id="rail-quality-list"' in html
    assert "rail-quality" in html


def test_style_css_has_quality_rail_rules():
    css = _read(STYLE_CSS)
    assert ".rail-toggle-list" in css
    assert ".rail-toggle-disabled" in css
    assert ".rail-toggle-sub" in css
