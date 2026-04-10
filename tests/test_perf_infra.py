"""Performance infrastructure — Redis cache backend + pg_prewarm.

Locks the post-v0.7.3 performance wiring so a future refactor can't
silently drop:

  1. Redis support via the REDIS_URL env var. When set, the Flask app
     must configure Flask-Caching with CACHE_TYPE=RedisCache and a
     versioned key prefix so multiple deploys can share one Redis
     instance without colliding.
  2. The SimpleCache fallback when REDIS_URL is unset, so local dev
     and CI still run without a Redis server.
  3. The pg_prewarm startup step + the scripts/pg_tuning.sql helper
     that documents the Azure server-parameter changes and loads hot
     tables into shared_buffers.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
REQUIREMENTS_TXT = ROOT / "requirements.txt"
PG_TUNING_SQL = ROOT / "scripts" / "pg_tuning.sql"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# scripts/pg_tuning.sql
# ---------------------------------------------------------------------------
def test_pg_tuning_sql_exists():
    assert PG_TUNING_SQL.exists(), (
        "scripts/pg_tuning.sql is required — it documents the Azure "
        "server-parameter changes and prewarms the PG buffer cache."
    )


def test_pg_tuning_sql_has_pg_prewarm():
    sql = _read(PG_TUNING_SQL)
    assert "CREATE EXTENSION IF NOT EXISTS pg_prewarm" in sql
    assert "pg_prewarm(" in sql or "pg_prewarm('" in sql, (
        "pg_tuning.sql must call pg_prewarm to load hot relations "
        "into shared_buffers"
    )


def test_pg_tuning_sql_mentions_hot_tables():
    sql = _read(PG_TUNING_SQL)
    # The most important relations to prewarm — if any of these is dropped
    # we want the test to fail loudly so someone explains why.
    for rel in ("sighting", "location", "idx_location_coords"):
        assert rel in sql, f"pg_tuning.sql should prewarm {rel!r}"


def test_pg_tuning_sql_documents_memory_params():
    sql = _read(PG_TUNING_SQL)
    for param in (
        "shared_buffers",
        "effective_cache_size",
        "work_mem",
        "maintenance_work_mem",
        "random_page_cost",
    ):
        assert param in sql, (
            f"pg_tuning.sql should document the {param} server parameter"
        )


def test_pg_tuning_sql_has_sanity_check_query():
    sql = _read(PG_TUNING_SQL)
    assert "pg_settings" in sql, (
        "pg_tuning.sql should query pg_settings at the end so the operator "
        "can verify the portal values actually took effect"
    )


# ---------------------------------------------------------------------------
# Redis client is a declared dependency
# ---------------------------------------------------------------------------
def test_redis_client_in_requirements():
    txt = _read(REQUIREMENTS_TXT)
    assert "redis" in txt.lower(), (
        "requirements.txt must pin the redis client — Flask-Caching's "
        "RedisCache backend imports it lazily when REDIS_URL is set."
    )


# ---------------------------------------------------------------------------
# app.py: Redis backend selection + SimpleCache fallback
# ---------------------------------------------------------------------------
def test_app_py_reads_redis_url_env():
    src = _read(APP_PY)
    assert 'os.environ.get("REDIS_URL"' in src, (
        "app.py must check os.environ.get('REDIS_URL') so operators can "
        "flip to a shared Redis cache without a code change."
    )


def test_app_py_has_redis_and_simple_cache_branches():
    src = _read(APP_PY)
    assert '"RedisCache"' in src, (
        "app.py should set CACHE_TYPE=RedisCache when REDIS_URL is present"
    )
    assert '"SimpleCache"' in src, (
        "app.py must still fall back to SimpleCache when REDIS_URL is unset "
        "(local dev + CI have no Redis server)"
    )
    assert "CACHE_KEY_PREFIX" in src, (
        "Redis branch should set CACHE_KEY_PREFIX so multiple deploys "
        "sharing one Redis instance don't collide"
    )


def test_default_cache_backend_is_simple_cache(monkeypatch):
    """With REDIS_URL unset, the live app object must use SimpleCache."""
    import app as _app

    assert _app.cache.config["CACHE_TYPE"] in (
        "SimpleCache",
        "flask_caching.backends.SimpleCache",
    )


def test_redis_url_switches_backend(monkeypatch):
    """Setting REDIS_URL before import must produce a RedisCache config.

    We don't actually connect to Redis (that would require a real server);
    we only verify the config dict Flask-Caching receives. This exercises
    the branch logic end to end.
    """
    # Flask-Caching's RedisCache factory tries to `import redis` at
    # construction time, so we need the client library available. On
    # CI the pinned dep in requirements.txt makes this a real import;
    # locally, skip cleanly if the user hasn't `pip install`-ed the
    # dev requirements yet.
    pytest.importorskip("redis")

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake"
    )

    # Re-import app.py with the env var set. conftest already stubbed
    # ConnectionPool, so this is safe.
    for mod in ("app",):
        sys.modules.pop(mod, None)
    _app = importlib.import_module("app")

    cfg_type = _app.cache.config["CACHE_TYPE"]
    assert "Redis" in str(cfg_type), (
        f"Expected a Redis backend when REDIS_URL is set, got {cfg_type!r}"
    )
    assert _app.cache.config["CACHE_REDIS_URL"] == "redis://localhost:6379/0"
    prefix = _app.cache.config.get("CACHE_KEY_PREFIX", "")
    assert prefix.startswith("ufosint:"), (
        f"Cache key prefix should namespace by app + version, got {prefix!r}"
    )

    # Leave the module in a clean state for later tests by reverting to
    # the no-REDIS_URL path.
    monkeypatch.delenv("REDIS_URL", raising=False)
    sys.modules.pop("app", None)
    importlib.import_module("app")


# ---------------------------------------------------------------------------
# app.py: pg_prewarm startup hook
# ---------------------------------------------------------------------------
def test_app_py_has_pg_prewarm_startup_hook():
    src = _read(APP_PY)
    assert "_pg_prewarm_relations" in src, (
        "app.py should define a _pg_prewarm_relations() helper that warms "
        "PG's shared_buffers on worker boot"
    )
    assert "pg_prewarm" in src, (
        "app.py should call pg_prewarm() against the hot relations"
    )


def test_pg_prewarm_covers_critical_relations():
    src = _read(APP_PY)
    # The list that actually runs at startup lives in _PREWARM_RELATIONS.
    # We just spot-check that the biggest-impact relations are in there.
    for rel in (
        "sighting",
        "location",
        "idx_location_coords",
        "idx_sighting_date",
    ):
        assert rel in src, (
            f"_PREWARM_RELATIONS must include {rel!r} so the landing-page "
            "map query is warm on first hit"
        )


def test_pg_prewarm_skips_when_extension_missing():
    """The prewarm step must tolerate environments without pg_prewarm.

    Local dev / CI / a managed PG that hasn't enabled the extension
    should all see a one-line skip log, not a crash.
    """
    src = _read(APP_PY)
    assert "pg_extension" in src and "pg_prewarm" in src, (
        "app.py should check pg_extension for pg_prewarm before calling "
        "it, so environments without the extension just log a skip"
    )
