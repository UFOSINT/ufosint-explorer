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


def test_migration_rebuilds_the_view_on_every_deploy():
    """DROP + CREATE rather than CREATE IF NOT EXISTS: the column set has to
    be able to change. CREATE populates, so no separate REFRESH is needed —
    but the data must be current after every deploy either way."""
    sql = _read(MIGRATION)
    assert "DROP MATERIALIZED VIEW IF EXISTS mv_points_bulk" in sql
    assert "CREATE MATERIALIZED VIEW mv_points_bulk AS" in sql
    assert sql.find("DROP MATERIALIZED VIEW") < sql.find("CREATE MATERIALIZED VIEW")


def test_migration_runs_on_every_deploy():
    wf = _read(WORKFLOW)
    assert "add_v0166_points_bulk_mv.sql" in wf
    # must be in both the sparse-checkout list and the psql loop
    assert wf.count("add_v0166_points_bulk_mv.sql") >= 2


def test_mv_carries_every_column_the_packer_selects():
    """The first v0.16.6 attempt shipped an MV missing latitude, longitude,
    shape and date_event. The query died with "missing FROM-clause entry for
    table l" and the map went dark. This walks the packer's select list and
    checks each name against the migration."""
    src = _read(APP_PY)
    sql = _read(MIGRATION)

    start = src.find("select_parts = [")
    block = src[start: src.find("]", start)]

    names = set(re.findall(r'"s\.([a-z0-9_]+)', block))
    names |= set(re.findall(r'\{_loc\}\.([a-z0-9_]+)', block))
    names |= set(re.findall(r'_col_expr\("([a-z0-9_]+)"', block))
    assert "latitude" in names and "longitude" in names, "select list parse failed"

    mv = sql[sql.find("CREATE MATERIALIZED VIEW"): sql.find("FROM sighting s")]
    missing = sorted(n for n in names if n not in mv)
    assert not missing, f"mv_points_bulk is missing columns the packer selects: {missing}"


def test_location_columns_are_aliased_to_the_active_source():
    """l.latitude only exists on the fallback join; the MV carries it under s."""
    src = _read(APP_PY)
    assert '_loc = "s" if _points_bulk_has_mv(conn) else "l"' in src
    start = src.find("select_parts = [")
    block = src[start: src.find("]", start)]
    assert '"l.latitude"' not in block, "hardcoded l.latitude breaks the MV path"
    assert '"l.longitude"' not in block
