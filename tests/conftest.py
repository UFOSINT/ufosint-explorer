"""Shared pytest fixtures.

The Flask app imports psycopg_pool.ConnectionPool at module load time and
immediately tries to open a connection. For unit tests we don't want a
real PostgreSQL server — we stub ConnectionPool with a no-op before the
app module loads, then import.

Tests that need to exercise real queries can patch `_pool.getconn` with
a MagicMock that returns a fake cursor.
"""
from __future__ import annotations

import os
import sys
import contextlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakePool:
    """No-op replacement for psycopg_pool.ConnectionPool.

    Just enough of the interface that `app.py` can import cleanly without
    a real database. Tests that exercise routes which hit the DB will
    need to monkeypatch this at the call site.
    """

    def __init__(self, *args, **kwargs):
        self._open = True

    def open(self, **kwargs):
        self._open = True

    def close(self, timeout: float = 5.0):
        self._open = False

    def getconn(self, timeout: float = 30.0):
        raise RuntimeError("Tests should not hit the database through the pool")

    def putconn(self, conn):
        pass

    @contextlib.contextmanager
    def connection(self, timeout: float = 30.0):
        raise RuntimeError("Tests should not hit the database through the pool")
        yield  # unreachable, makes Python treat this as a generator


@pytest.fixture(scope="session", autouse=True)
def _stub_database_and_load_app():
    """Stub the connection pool and load the app module once per session."""
    os.environ.setdefault(
        "DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake"
    )

    import psycopg_pool  # installed as a real dep; we only replace the class
    psycopg_pool.ConnectionPool = _FakePool  # type: ignore[assignment]

    # Evict any cached app module so the stub takes effect.
    for mod in ("app", "mcp_http", "tools_catalog"):
        sys.modules.pop(mod, None)

    import app as _app  # noqa: F401 - import side effect

    yield


@pytest.fixture
def flask_app():
    """Return the loaded Flask app object."""
    import app as _app
    return _app.app


@pytest.fixture
def client(flask_app):
    """A Flask test client bound to the stubbed app."""
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


@pytest.fixture
def asset_version():
    """The computed asset version string (mtime hash fallback in tests)."""
    import app as _app
    return _app.ASSET_VERSION
