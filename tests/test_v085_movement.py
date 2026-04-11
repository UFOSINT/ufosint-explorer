"""v0.8.5 — Movement classification + v0.8.3b data layer.

Locks the v0.8.5 contract (app side of the science team's v0.8.3b
handoff):

  1. docs/V085_MOVEMENT_PLAN.md exists and covers the new fields.
  2. scripts/add_v083_derived_columns.sql exists + is idempotent.
  3. The deploy workflow runs the new migration after the v0.8.2
     migration (not before, not skipped).
  4. migrate_sqlite_to_pg.py has both new columns in its TABLES list.
  5. /api/points-bulk schema version bumped to "v083-1".
  6. Row size bumped to 32 bytes + new struct format.
  7. _MOVEMENT_CATS tuple has exactly 10 entries in bit order.
  8. _POINTS_BULK_DERIVED_COLS includes the two new columns.
  9. _SIGHTING_DETAIL_COLUMNS includes the two new columns.
 10. deck.js POINTS has `movementFlags` + `movements` fields.
 11. deck.js deserialises the movement_flags uint16 at offset 28.
 12. deck.js filter pipeline honors `hasMovement`.
 13. app.js Quality rail has a "hasMovement" toggle entry.
 14. app.js applyClientFilters passes hasMovement through.
 15. openDetail() renders movement_categories chips.
 16. CSS defines .movement-chip.
 17. CHANGELOG has [0.8.5] section.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
STYLE_CSS = ROOT / "static" / "style.css"
CHANGELOG = ROOT / "CHANGELOG.md"
PLAN_DOC = ROOT / "docs" / "V085_MOVEMENT_PLAN.md"
MIGRATION_SQL = ROOT / "scripts" / "add_v083_derived_columns.sql"
MIGRATOR = ROOT / "scripts" / "migrate_sqlite_to_pg.py"
DEPLOY_YML = ROOT / ".github" / "workflows" / "azure-deploy.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Plan doc + changelog
# ---------------------------------------------------------------------------
def test_plan_doc_exists():
    assert PLAN_DOC.exists(), (
        "docs/V085_MOVEMENT_PLAN.md is the architecture doc for v0.8.5. "
        "Must exist in-tree."
    )


def test_plan_doc_covers_key_concepts():
    doc = _read(PLAN_DOC)
    for concept in (
        "has_movement_mentioned",
        "movement_categories",
        "movement_flags",
        "_MOVEMENT_CATS",
        "v083-1",
        "32 bytes",
        "118,320",
        "ufo_public.db",
    ):
        assert concept in doc, f"plan doc missing coverage of {concept!r}"


def test_changelog_has_v085_section():
    log = _read(CHANGELOG)
    assert "[0.8.5]" in log, (
        "CHANGELOG must have a [0.8.5] section before shipping"
    )


# ---------------------------------------------------------------------------
# PG migration SQL
# ---------------------------------------------------------------------------
def test_v083_migration_sql_exists():
    assert MIGRATION_SQL.exists(), (
        "scripts/add_v083_derived_columns.sql must exist in-tree"
    )


def test_v083_migration_sql_is_idempotent():
    sql = _read(MIGRATION_SQL)
    assert "ADD COLUMN IF NOT EXISTS has_movement_mentioned" in sql
    assert "ADD COLUMN IF NOT EXISTS movement_categories" in sql
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in sql


def test_v083_migration_indexed_on_has_movement():
    """Filter path uses the boolean index; JSON array is display-only
    and doesn't get a btree index."""
    sql = _read(MIGRATION_SQL)
    assert "idx_sighting_has_movement" in sql
    assert "ON sighting(has_movement_mentioned)" in sql


# ---------------------------------------------------------------------------
# Deploy workflow integration
# ---------------------------------------------------------------------------
def test_deploy_workflow_checks_out_v083_sql():
    yml = _read(DEPLOY_YML)
    assert "add_v083_derived_columns.sql" in yml, (
        "azure-deploy.yml must sparse-checkout add_v083_derived_columns.sql "
        "so the psql step can find it"
    )


def test_deploy_workflow_applies_v083_migration():
    yml = _read(DEPLOY_YML)
    assert "Apply v0.8.3b movement-fields migration" in yml
    # Must reference psql -f pointing at the v0.8.3 SQL
    assert "add_v083_derived_columns.sql" in yml


def test_deploy_workflow_v083_runs_after_v082():
    """The v0.8.3b step must come AFTER v0.8.2 so the sighting table
    already has the v0.8.2 columns in place when the v0.8.3b step
    runs. Pure ordering sanity check."""
    yml = _read(DEPLOY_YML)
    v082_pos = yml.find("Apply v0.8.2 derived-field migration")
    v083_pos = yml.find("Apply v0.8.3b movement-fields migration")
    assert v082_pos != -1
    assert v083_pos != -1
    assert v082_pos < v083_pos


# ---------------------------------------------------------------------------
# migrate_sqlite_to_pg.py — column list
# ---------------------------------------------------------------------------
def test_migrator_includes_movement_fields():
    src = _read(MIGRATOR)
    assert '"has_movement_mentioned"' in src
    assert '"movement_categories"' in src


# ---------------------------------------------------------------------------
# app.py — /api/points-bulk v083-1 schema
# ---------------------------------------------------------------------------
def test_points_bulk_schema_version_is_v083_1():
    src = _read(APP_PY)
    assert '_POINTS_BULK_SCHEMA_VERSION = "v083-1"' in src


def test_points_bulk_bytes_per_row_is_32():
    src = _read(APP_PY)
    assert "_POINTS_BULK_BYTES_PER_ROW = 32" in src


def test_points_bulk_struct_has_new_fields():
    src = _read(APP_PY)
    assert '_POINTS_BULK_STRUCT = "<IffIBBBBBBBBBBHHH"' in src, (
        "Struct format must have 3 trailing uint16s (duration_log2, "
        "movement_flags, _reserved2) so the row is exactly 32 bytes."
    )


def test_movement_cats_tuple_is_in_bit_order():
    """The 10 movement categories must appear in the exact order the
    science team documented in the handoff. Changing this order
    silently re-maps every bit in the packed movement_flags uint16,
    breaking every deployed client."""
    src = _read(APP_PY)
    # Find the tuple
    start = src.find("_MOVEMENT_CATS = (")
    assert start != -1
    end = src.find(")\n", start)
    block = src[start:end]
    expected_order = [
        "hovering", "linear", "erratic", "accelerating", "rotating",
        "ascending", "descending", "vanished", "followed", "landed",
    ]
    found = []
    for cat in expected_order:
        cat_pos = block.find(f'"{cat}"')
        assert cat_pos != -1, f"_MOVEMENT_CATS missing {cat!r}"
        found.append((cat_pos, cat))
    # Verify the positions are monotonically increasing
    positions = [p for p, _ in found]
    assert positions == sorted(positions), (
        "_MOVEMENT_CATS bit order is wrong. Expected: "
        + " < ".join(expected_order)
    )


def test_movement_cat_to_bit_mapping_exists():
    src = _read(APP_PY)
    assert "_MOVEMENT_CAT_TO_BIT" in src


def test_derived_cols_include_movement_fields():
    src = _read(APP_PY)
    start = src.find("_POINTS_BULK_DERIVED_COLS = (")
    assert start != -1
    # Balanced-paren scan so the v0.8.5 comment's "(science team's...)"
    # doesn't terminate the slice early.
    open_paren = src.index("(", start)
    depth = 0
    end = -1
    for i in range(open_paren, len(src)):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end != -1, "unterminated _POINTS_BULK_DERIVED_COLS tuple"
    block = src[start:end]
    assert '"has_movement_mentioned"' in block
    assert '"movement_categories"' in block


def test_points_bulk_packs_movement_flags_into_row():
    """The build loop must compute mv_flags from the movement_categories
    JSON and pass it into the pack() call as one of the trailing
    uint16s."""
    src = _read(APP_PY)
    start = src.find("def _points_bulk_build_cached(")
    end = src.find("def _points_bulk_column_set", start)
    if end == -1:
        end = len(src)
    body = src[start:end] if end != -1 else src[start:start + 30000]
    assert "mv_flags" in body
    assert "_MOVEMENT_CAT_TO_BIT.get(cat)" in body
    # The pack call's trailing value must include mv_flags
    assert "mv_flags" in body


def test_points_bulk_meta_exposes_movements_lookup():
    src = _read(APP_PY)
    # The meta dict literal must include a `movements` key
    assert '"movements": list(_MOVEMENT_CATS)' in src


def test_points_bulk_meta_has_flag_bits_map():
    src = _read(APP_PY)
    assert '"flag_bits":' in src
    assert '"has_movement": 2' in src


def test_points_bulk_meta_has_movement_flags_field_descriptor():
    src = _read(APP_PY)
    assert '"name": "movement_flags"' in src
    assert '"offset": 28' in src


# ---------------------------------------------------------------------------
# app.py — /api/sighting/:id
# ---------------------------------------------------------------------------
def test_sighting_detail_columns_include_movement_fields():
    src = _read(APP_PY)
    start = src.find("_SIGHTING_DETAIL_COLUMNS = (")
    # Balanced paren scan (same as the v0.8.3 test helper)
    depth = 0
    open_paren = src.index("(", start)
    for i in range(open_paren, len(src)):
        c = src[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                block = src[start:i + 1]
                break
    else:
        raise AssertionError("unterminated _SIGHTING_DETAIL_COLUMNS tuple")
    assert '"s.has_movement_mentioned"' in block
    assert '"s.movement_categories"' in block


def test_api_sighting_parses_movement_categories_json():
    """The raw TEXT column is a JSON array. api_sighting must parse
    it into a Python list before jsonify() serialises the response,
    so callers receive `["hovering", "vanished"]` instead of the
    JSON-encoded string."""
    src = _read(APP_PY)
    start = src.find("def api_sighting(")
    end = src.find("\n@app.route", start + 1)
    body = src[start:end] if end != -1 else src[start:start + 8000]
    assert "movement_categories" in body
    # Must json.loads the value
    assert "json.loads(raw_mv)" in body or 'json.loads(raw_mv)' in body


# ---------------------------------------------------------------------------
# deck.js — deserialiser + filter
# ---------------------------------------------------------------------------
def test_deck_js_points_has_movement_flags_field():
    js = _read(DECK_JS)
    assert "movementFlags:" in js
    assert "movements:" in js


def test_deck_js_row_size_is_32():
    js = _read(DECK_JS)
    # The schema version assertion in loadBulkPoints
    assert "bytesPerRow !== 32" in js
    assert "expected 32 (v0.8.5)" in js
    # And the hot loop reads at offset 28 for movement_flags
    assert "getUint16(o + 28" in js


def test_deck_js_has_flag_has_movement_constant():
    js = _read(DECK_JS)
    assert "FLAG_HAS_MOVEMENT" in js
    assert "0x04" in js


def test_deck_js_filter_pipeline_honors_has_movement():
    js = _read(DECK_JS)
    # The filter object must name the new field
    assert "hasMovement" in js
    # The hot loop must check the flag bit
    assert "FLAG_HAS_MOVEMENT" in js


# ---------------------------------------------------------------------------
# app.js — Quality rail + filter wiring + detail modal
# ---------------------------------------------------------------------------
def test_app_js_quality_rail_has_movement_toggle():
    js = _read(APP_JS)
    start = js.find("function mountQualityRail")
    end = js.find("\nfunction ", start + 10)
    body = js[start:end]
    # The new toggle entry
    assert 'key: "hasMovement"' in body
    assert '"has_movement"' in body  # coverage key
    assert 'Has movement described' in body


def test_app_js_quality_rail_change_handler_writes_has_movement():
    js = _read(APP_JS)
    start = js.find("function mountQualityRail")
    end = js.find("\nfunction ", start + 10)
    body = js[start:end]
    assert "state.qualityFilter.hasMovement" in body


def test_app_js_apply_client_filters_passes_has_movement():
    js = _read(APP_JS)
    start = js.find("function applyClientFilters()")
    end = js.find("\nfunction _parseYearFilter", start)
    body = js[start:end]
    assert "hasMovement:" in body
    assert "q.hasMovement" in body


def test_open_detail_renders_movement_chips():
    js = _read(APP_JS)
    start = js.find("async function openDetail(")
    end = js.find("function closeModal", start)
    body = js[start:end] if end != -1 else js[start:start + 20000]
    assert "r.movement_categories" in body
    assert "movement-chip" in body


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
def test_css_has_movement_chip_rule():
    css = _read(STYLE_CSS)
    assert ".movement-chip" in css
