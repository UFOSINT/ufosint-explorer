"""v0.16.6 — /api/points-bulk reads from a narrow materialized view.

Loading 468,251 descriptions into `sighting` took its heap from ~150 MB to
836 MB. The map buffer needs ~30 narrow columns, but every scan then dragged
the inline description text along: the build query went from inside the 25 s
statement_timeout to 48.8 s and the endpoint 500'd for every visitor. The
page showed "some data may be missing" and silently fell back to the legacy
Leaflet cluster layer, because bootDeckGL() throws when that fetch fails.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
MIGRATION = ROOT / "scripts" / "add_v0166_points_bulk_mv.sql"
WORKFLOW = ROOT / ".github" / "workflows" / "azure-deploy.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_buffer_reads_from_the_materialized_view():
    src = _read(APP_PY)
    assert "_POINTS_BULK_MV" in src
    assert "mv_points_bulk" in src
    assert "_points_bulk_from_clause" in src


def test_falls_back_to_the_join_when_the_view_is_absent():
    """A database without the MV must still serve the map, slowly, not 500."""
    src = _read(APP_PY)
    start = src.find("def _points_bulk_from_clause")
    body = src[start:start + 400]
    assert "_POINTS_BULK_FALLBACK_FROM" in body
    fb = src[src.find("_POINTS_BULK_FALLBACK_FROM = "):][:400]
    assert "JOIN location l" in fb, "fallback must reproduce the original join"


def test_column_probe_uses_pg_catalog_not_information_schema():
    """information_schema does not list materialized views, so probing it
    would report every column missing once the buffer reads from the MV."""
    src = _read(APP_PY)
    start = src.find("def _points_bulk_column_set")
    body = src[start:start + 1200]
    assert "pg_attribute" in body
    # The comment and docstring legitimately name information_schema while
    # explaining why it is no longer used; what must be gone is the query.
    assert "FROM information_schema" not in body


def test_migration_exists_and_is_idempotent():
    sql = _read(MIGRATION)
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS mv_points_bulk" in sql
    assert "REFRESH MATERIALIZED VIEW mv_points_bulk" in sql, (
        "a deploy must publish current data, not just create the view"
    )


def test_migration_runs_on_every_deploy():
    wf = _read(WORKFLOW)
    assert "add_v0166_points_bulk_mv.sql" in wf
    # must be in both the sparse-checkout list and the psql loop
    assert wf.count("add_v0166_points_bulk_mv.sql") >= 2


def test_mv_columns_cover_what_the_buffer_packs():
    """Every column the packer reads must exist in the view, or the buffer
    silently loses that field for every row."""
    sql = _read(MIGRATION)
    src = _read(APP_PY)
    start = src.find("_POINTS_BULK_DERIVED_COLS")
    block = src[start:start + 1500]
    cols = set(re.findall(r'"([a-z0-9_]+)"', block))
    # only assert on the ones that are real sighting columns in the packer
    missing = [c for c in cols
               if c in {"lat", "lng", "standardized_shape", "quality_score",
                        "hoax_likelihood", "richness_score", "primary_color",
                        "dominant_emotion", "has_description", "has_media",
                        "has_movement_mentioned", "movement_categories",
                        "emotion_28_dominant", "emotion_28_group",
                        "emotion_7_dominant", "vader_compound",
                        "roberta_sentiment"}
               and c not in sql]
    assert not missing, f"mv_points_bulk is missing packed columns: {missing}"
