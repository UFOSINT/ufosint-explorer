"""v0.7.5 — Materialized view fast paths for /api/stats, /api/timeline,
/api/sentiment/overview.

Locks the v0.7.5 contract so a future refactor can't silently regress:

  1. scripts/add_v075_materialized_views.sql exists and defines the five
     expected materialized views with their unique indexes + REFRESH
     statements.
  2. The deploy workflow runs the migration after the v0.7 index step.
  3. app.py has a _has_common_filters() eligibility helper and each of
     the three rewired endpoints checks it before reading from the MV.
  4. Functional: when get_db() returns a cursor that successfully serves
     the MV query, /api/stats + /api/timeline + /api/sentiment/overview
     all return 200 with the MV-shape payload.
  5. Functional: when the MV query raises psycopg.errors.UndefinedTable
     the endpoint transparently falls back to the live-query path (no
     500, no blank response).
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
MIGRATION = ROOT / "scripts" / "add_v075_materialized_views.sql"
DEPLOY_YML = ROOT / ".github" / "workflows" / "azure-deploy.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Migration SQL
# ---------------------------------------------------------------------------
def test_mv_migration_exists():
    assert MIGRATION.exists(), (
        "scripts/add_v075_materialized_views.sql is the v0.7.5 MV migration "
        "the deploy workflow applies after the index migration"
    )


@pytest.mark.parametrize("mv_name", [
    "mv_stats_summary",
    "mv_stats_by_source",
    "mv_stats_by_collection",
    "mv_timeline_yearly",
    "mv_sentiment_overview",
])
def test_mv_migration_creates_all_five_views(mv_name):
    sql = _read(MIGRATION)
    assert f"CREATE MATERIALIZED VIEW IF NOT EXISTS {mv_name}" in sql, (
        f"migration must create {mv_name}"
    )
    assert f"REFRESH MATERIALIZED VIEW {mv_name}" in sql, (
        f"migration must REFRESH {mv_name} so the deploy picks up new rows"
    )


def test_mv_migration_has_unique_indexes():
    """Unique indexes let us upgrade to REFRESH ... CONCURRENTLY later
    without a rebuild. Also guards against accidental dupes.
    """
    sql = _read(MIGRATION)
    # One per MV (some on the singleton 1, some on natural keys)
    for idx in (
        "mv_stats_summary_singleton",
        "mv_stats_by_source_pk",
        "mv_stats_by_collection_pk",
        "mv_timeline_yearly_pk",
        "mv_sentiment_overview_singleton",
    ):
        assert idx in sql, f"missing unique index: {idx}"


def test_mv_migration_is_idempotent():
    sql = _read(MIGRATION)
    assert "IF NOT EXISTS" in sql, (
        "migration must use CREATE ... IF NOT EXISTS so re-running on "
        "every deploy is safe"
    )


# ---------------------------------------------------------------------------
# Deploy workflow integration
# ---------------------------------------------------------------------------
def test_deploy_workflow_applies_mv_migration():
    yml = _read(DEPLOY_YML)
    assert "add_v075_materialized_views.sql" in yml, (
        "azure-deploy.yml must checkout the MV migration into the "
        "migrations/ sparse-checkout tree"
    )
    assert "Refresh v0.7.5 materialized views" in yml, (
        "azure-deploy.yml must have a dedicated MV refresh step"
    )


def test_deploy_workflow_mv_step_after_index_step():
    """The index migration must run first — the MV SELECT queries rely
    on the v0.7 indexes for the initial population performance."""
    yml = _read(DEPLOY_YML)
    idx_pos = yml.find("Apply v0.7 index migrations")
    mv_pos = yml.find("Refresh v0.7.5 materialized views")
    assert idx_pos != -1
    assert mv_pos != -1
    assert idx_pos < mv_pos, (
        "MV refresh step must come after the index migration step"
    )


# ---------------------------------------------------------------------------
# app.py — source-level contract
# ---------------------------------------------------------------------------
def test_has_common_filters_helper_exists():
    src = _read(APP_PY)
    assert "_has_common_filters" in src, (
        "app.py must define _has_common_filters() — it's the eligibility "
        "check that gates the MV fast path on every rewired endpoint"
    )
    assert "_COMMON_FILTER_KEYS" in src, (
        "The filter key set should be a module constant so it stays "
        "in sync with add_common_filters()"
    )


def test_api_stats_reads_from_mv():
    src = _read(APP_PY)
    assert "_api_stats_from_mv" in src
    assert "mv_stats_summary" in src
    assert "mv_stats_by_source" in src
    assert "mv_stats_by_collection" in src


def test_api_stats_has_live_fallback():
    src = _read(APP_PY)
    assert "_api_stats_from_live" in src, (
        "a live-query fallback must exist for fresh clones + local dev "
        "where the MV migration hasn't been applied"
    )
    assert "psycopg.errors.UndefinedTable" in src, (
        "endpoints must catch UndefinedTable to fall back when the MV "
        "is missing; any other exception should still 500"
    )


def test_api_timeline_reads_from_mv_when_unfiltered():
    src = _read(APP_PY)
    assert "mv_timeline_yearly" in src
    # The MV branch must be gated on BOTH no-year AND no-filters
    assert "not year" in src and "_has_common_filters(request.args)" in src


def test_api_sentiment_overview_reads_from_mv_when_unfiltered():
    src = _read(APP_PY)
    assert "mv_sentiment_overview" in src
    assert "_SENTIMENT_OVERVIEW_COLS" in src, (
        "keep the column list as a module constant so the MV read and "
        "the jsonify stay in sync"
    )


def test_common_filter_keys_match_add_common_filters():
    """_COMMON_FILTER_KEYS must include every key add_common_filters()
    actually reads from request.args. If someone adds a new filter to
    add_common_filters and forgets the set, the MV path would silently
    serve unfiltered data for that filter — a correctness bug, not a
    perf bug.
    """
    src = _read(APP_PY)
    # Extract the keys inside params.get("...") calls INSIDE add_common_filters
    start = src.find("def add_common_filters(")
    end = src.find("\ndef ", start + 1)
    body = src[start:end]
    import re
    expected = set(re.findall(r'params\.get\(["\'](\w+)["\']\)', body))
    # Extract the frozenset literal
    fs_start = src.find("_COMMON_FILTER_KEYS = frozenset({")
    fs_end = src.find("})", fs_start)
    fs_body = src[fs_start:fs_end]
    declared = set(re.findall(r'["\'](\w+)["\']', fs_body))
    missing = expected - declared
    assert not missing, (
        f"_COMMON_FILTER_KEYS is missing filters that add_common_filters "
        f"reads: {sorted(missing)}. MV fast path would be incorrectly "
        f"eligible when those filters are set."
    )


# ---------------------------------------------------------------------------
# Functional tests — mock get_db() to exercise the MV + fallback paths
# ---------------------------------------------------------------------------
class _FakeCursor:
    """Tiny cursor stub. Records every execute() call, lets the test
    script a sequence of responses via fetchone()/fetchall().
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.executed = []
        self._current = None
        self.description = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # Pop the next scripted response. If it's an exception instance,
        # raise it — this is how tests trigger the UndefinedTable fallback.
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        self._current = resp
        # Fake the DB-API description attribute for endpoints that use
        # dict(zip([d[0] for d in cur.description], row))
        if isinstance(resp, dict) and "description" in resp:
            self.description = resp["description"]
        elif isinstance(resp, dict) and "rows" in resp and resp["rows"]:
            # Auto-describe from dict keys of first row if it's a dict
            first = resp["rows"][0]
            if isinstance(first, dict):
                self.description = [(k,) for k in first.keys()]

    def fetchone(self):
        if self._current is None:
            return None
        if isinstance(self._current, dict):
            rows = self._current.get("rows", [])
            return rows[0] if rows else None
        return self._current[0] if self._current else None

    def fetchall(self):
        if self._current is None:
            return []
        if isinstance(self._current, dict):
            return self._current.get("rows", [])
        return self._current

    def close(self):
        pass


class _FakeConn:
    def __init__(self, responses):
        self._cursor = _FakeCursor(responses)

    def cursor(self, *a, **kw):
        return self._cursor

    def close(self):
        pass


def _install_fake_db(client, responses):
    """Monkeypatch app.get_db to return a _FakeConn preloaded with the
    given sequence of cursor responses. Returns the fake cursor so the
    test can inspect `.executed` afterwards.
    """
    import app as _app
    conn = _FakeConn(responses)
    return conn, _app


def test_api_stats_mv_happy_path(client, monkeypatch):
    """When the MV exists, /api/stats should issue three SELECTs against
    mv_stats_summary + mv_stats_by_source + mv_stats_by_collection and
    return the assembled payload.
    """
    import app as _app
    # Disable Flask-Caching so repeated test calls in the same session
    # don't serve stale responses.
    _app.cache.clear()

    # Bust the response cache by using a unique query string per test,
    # since Flask-Caching keys include the query string.
    #
    # v0.8.5: _api_stats_from_mv() also calls _api_stats_derived_counts()
    # which runs two extra SELECTs for quality_score + has_movement.
    # v0.8.7.2: AND _api_stats_mapped_count() runs one more JOIN query
    # for the sighting-level mapped count.
    responses = [
        # mv_stats_summary single row
        [(614505, "0034-04-01", "2026-02-20", 105836, 61893, 43976, 126730)],
        # mv_stats_by_source rows
        [
            ("UFOCAT", 197108, "UFOCAT"),
            ("NUFORC", 159320, "PUBLIUS"),
        ],
        # mv_stats_by_collection rows
        [("PUBLIUS", 362646), ("UFOCAT", 197108)],
        # v0.8.5: derived counts
        [(118320,)],   # high_quality
        [(249217,)],   # with_movement
        # v0.8.7.2: sighting-level mapped count
        [(396165,)],
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    resp = client.get("/api/stats?test=mv_happy")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_sightings"] == 614505
    assert data["duplicate_candidates"] == 126730
    assert data["by_source"][0] == {"name": "UFOCAT", "count": 197108, "collection": "UFOCAT"}
    assert data["by_collection"][0] == {"name": "PUBLIUS", "count": 362646}
    # v0.8.5: derived fields present in response
    assert data["high_quality"] == 118320
    assert data["with_movement"] == 249217
    # v0.8.7.2: sighting-level mapped count is present alongside the
    # legacy distinct-place count.
    assert data["mapped_sightings"] == 396165
    assert data["geocoded_locations"] == 105836

    # All three MV reads happened, in order
    sqls = [e[0] for e in conn._cursor.executed]
    assert any("mv_stats_summary" in s for s in sqls)
    assert any("mv_stats_by_source" in s for s in sqls)
    assert any("mv_stats_by_collection" in s for s in sqls)
    # Live fallback queries (COUNT(*) FROM sighting) must NOT have fired
    assert not any("COUNT(*) FROM sighting" in s for s in sqls), (
        "MV happy path must NOT execute the live-query fallback"
    )


def test_api_stats_falls_back_when_mv_missing(client, monkeypatch):
    """If mv_stats_summary doesn't exist, the endpoint must catch
    psycopg.errors.UndefinedTable and fall back to the live query
    instead of returning 500.
    """
    import app as _app
    _app.cache.clear()

    class FakeUT(psycopg.errors.UndefinedTable):
        def __init__(self):
            pass

    # First execute() raises UndefinedTable (mv_stats_summary missing).
    # Then the fallback live query executes 8 statements in order:
    #   COUNT(*) FROM sighting
    #   by_source aggregate
    #   by_collection aggregate
    #   MIN/MAX date
    #   COUNT geocoded
    #   COUNT geocoded_original
    #   COUNT geocoded_geonames
    #   COUNT duplicate_candidate
    # v0.8.5: then _api_stats_derived_counts() runs 2 more:
    #   COUNT WHERE quality_score >= 60
    #   COUNT WHERE has_movement_mentioned = 1
    # v0.8.7.2: then _api_stats_mapped_count() runs 1 more:
    #   COUNT sighting JOIN location (sighting-level mapped count)
    responses = [
        FakeUT(),                                             # MV miss
        [(614505,)],                                          # total
        [("UFOCAT", 197108, "UFOCAT")],                       # by_source
        [("PUBLIUS", 362646)],                                # by_collection
        [("0034-04-01", "2026-02-20")],                       # date min/max
        [(105836,)],                                          # geocoded
        [(61893,)],                                           # geocoded_original
        [(43976,)],                                           # geocoded_geonames
        [(126730,)],                                          # dupes
        [(118320,)],                                          # v0.8.5 high_quality
        [(249217,)],                                          # v0.8.5 with_movement
        [(396165,)],                                          # v0.8.7.2 mapped_sightings
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    resp = client.get("/api/stats?test=mv_miss")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["total_sightings"] == 614505
    assert data["duplicate_candidates"] == 126730
    # The MV query DID execute (and raised), then the live queries ran
    sqls = [e[0] for e in conn._cursor.executed]
    assert any("mv_stats_summary" in s for s in sqls)
    assert any("COUNT(*) FROM sighting" in s for s in sqls)


def test_api_timeline_uses_mv_when_unfiltered(client, monkeypatch):
    import app as _app
    _app.cache.clear()

    responses = [
        [
            ("1950", "NUFORC", 12),
            ("1950", "MUFON", 5),
            ("1951", "NUFORC", 20),
        ],
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    resp = client.get("/api/timeline?test=mv_happy")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mode"] == "yearly"
    assert data["year"] is None
    assert data["data"]["1950"] == {"NUFORC": 12, "MUFON": 5}
    assert data["data"]["1951"] == {"NUFORC": 20}

    sqls = [e[0] for e in conn._cursor.executed]
    assert any("mv_timeline_yearly" in s for s in sqls)
    # Live query (SUBSTR(s.date_event ...) GROUP BY) must NOT have fired
    assert not any("SUBSTR(s.date_event, 1, 4) as period" in s for s in sqls), (
        "MV happy path must not hit the live aggregate"
    )


def test_api_timeline_skips_mv_when_year_is_set(client, monkeypatch):
    """The monthly drilldown for a specific year has no MV — it must
    unconditionally use the live query so the filter actually applies.
    """
    import app as _app
    _app.cache.clear()

    responses = [
        [("1975-01", "NUFORC", 3), ("1975-02", "NUFORC", 5)],
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    resp = client.get("/api/timeline?year=1975")
    assert resp.status_code == 200
    sqls = [e[0] for e in conn._cursor.executed]
    # The MV must NOT be touched on the monthly path
    assert not any("mv_timeline_yearly" in s for s in sqls)
    assert any("SUBSTR(s.date_event, 1, 7) as period" in s for s in sqls), (
        "year=1975 must hit the monthly live query"
    )


def test_api_timeline_skips_mv_when_filter_is_set(client, monkeypatch):
    """When shape=disk is in the query string, the MV is not eligible —
    add_common_filters() has to actually run against the live tables.
    """
    import app as _app
    _app.cache.clear()

    responses = [
        [("1950", "NUFORC", 3)],
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    resp = client.get("/api/timeline?shape=Disc")
    assert resp.status_code == 200
    sqls = [e[0] for e in conn._cursor.executed]
    assert not any("mv_timeline_yearly" in s for s in sqls), (
        "MV must NOT be used when filters are present"
    )
    # v0.8.7: shape filter now matches against standardized_shape,
    # not the raw shape column, so the filter clause changed.
    assert any("s.standardized_shape = %s" in s for s in sqls)


def test_api_sentiment_overview_mv_happy_path(client, monkeypatch):
    import app as _app
    _app.cache.clear()

    responses = [
        # Single row: 13 columns in _SENTIMENT_OVERVIEW_COLS order
        [(503385, 0.1623, 0.12, 0.08, 0.80, 671980, 570527, 281909,
          100000, 50000, 20000, 200000, 80000)],
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    resp = client.get("/api/sentiment/overview?test=mv_happy")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_analyzed"] == 503385
    assert data["joy"] == 671980
    assert data["fear"] == 570527

    sqls = [e[0] for e in conn._cursor.executed]
    assert any("mv_sentiment_overview" in s for s in sqls)
    # Live-query JOIN sentiment_analysis sa ON s.id = sa.sighting_id
    # must NOT have fired
    assert not any("JOIN sentiment_analysis sa" in s for s in sqls)


def test_api_sentiment_overview_skips_mv_when_filter_is_set(client, monkeypatch):
    import app as _app
    _app.cache.clear()

    # Live path calls cur.description, so we pass a dict with both rows
    # and an explicit description tuple matching _SENTIMENT_OVERVIEW_COLS.
    responses = [
        {
            "rows": [(100, 0.05, 0.1, 0.05, 0.85, 10, 20, 5, 3, 2, 1, 40, 15)],
            "description": [(c,) for c in _app._SENTIMENT_OVERVIEW_COLS],
        },
    ]
    conn = _FakeConn(responses)
    monkeypatch.setattr(_app, "get_db", lambda: conn)

    # v0.8.7: country/state/hynek/vallee/collection were removed from
    # _COMMON_FILTER_KEYS so they no longer bypass the MV. Use a
    # surviving filter (shape) to prove the MV bypass still works.
    resp = client.get("/api/sentiment/overview?shape=Disc")
    assert resp.status_code == 200
    sqls = [e[0] for e in conn._cursor.executed]
    assert not any("mv_sentiment_overview" in s for s in sqls), (
        "shape filter must bypass the MV"
    )
    assert any("s.standardized_shape = %s" in s for s in sqls)
