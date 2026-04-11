"""v0.8.0 — /api/points-bulk endpoint + deck.gl client integration.

Locks the v0.8.0 contract:

  1. /api/points-bulk route is registered.
  2. Meta request (`?meta=1`) returns a well-formed JSON sidecar with
     lookup tables + schema descriptor.
  3. Default request returns a gzipped octet-stream with an ETag.
  4. `If-None-Match` with a matching ETag returns 304.
  5. The packed binary has exactly `count * bytes_per_row` bytes after
     gunzip, matches the struct layout (uint32/float32/float32/u8/u8/u16),
     and the id/lat/lng round-trip within float32 precision.
  6. The in-process LRU cache keeps the buffer hot across requests.
  7. Schema version constant + bytes-per-row constant exist in app.py
     so a future refactor can't silently change the wire format.
  8. Frontend app.js has the deck.gl + bulk-load bootstrap paths.
"""
from __future__ import annotations

import gzip
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"
INDEX_HTML = ROOT / "static" / "index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-level contract — constants and helpers must exist
# ---------------------------------------------------------------------------
def test_points_bulk_schema_version_constant():
    src = _read(APP_PY)
    assert "_POINTS_BULK_SCHEMA_VERSION" in src, (
        "The schema version is how we invalidate every cached browser "
        "copy when the binary layout changes. Must be a module constant."
    )


def test_points_bulk_bytes_per_row_constant():
    src = _read(APP_PY)
    assert "_POINTS_BULK_BYTES_PER_ROW = 16" in src, (
        "The 16-byte row layout is part of the wire contract — the "
        "client hard-codes it into its DataView offsets. Lock it here "
        "so a refactor can't silently bump it."
    )


def test_points_bulk_struct_format():
    src = _read(APP_PY)
    assert '_POINTS_BULK_STRUCT = "<IffBBH"' in src, (
        "The struct format locks the on-wire order: uint32 id, float32 "
        "lat, float32 lng, uint8 source, uint8 shape, uint16 year. "
        "Little-endian so the JS DataView reads it directly."
    )


def test_points_bulk_etag_function_exists():
    src = _read(APP_PY)
    assert "def _points_bulk_etag" in src
    assert "_POINTS_BULK_SCHEMA_VERSION" in src


def test_points_bulk_build_is_lru_cached():
    src = _read(APP_PY)
    # Locate the build function and check it has an @functools.lru_cache
    # decorator just above it.
    idx = src.find("def _points_bulk_build")
    assert idx != -1, "build function missing"
    # Look backwards ~200 chars for the decorator
    preceding = src[max(0, idx - 200):idx]
    assert "@functools.lru_cache" in preceding, (
        "_points_bulk_build must be @lru_cache'd on the etag so every "
        "request after the first is a zero-work cache hit"
    )


def test_points_bulk_route_registered(flask_app):
    rules = [r.rule for r in flask_app.url_map.iter_rules()]
    assert "/api/points-bulk" in rules


# ---------------------------------------------------------------------------
# Functional tests — fake DB, exercise the real route handler
# ---------------------------------------------------------------------------
class _FakeBulkCursor:
    """Minimal cursor stub for the points-bulk path.

    Scripted to answer three kinds of queries:
      1. ETag aggregates  → (count, max_id)
      2. source_database  → list of (id, name)
      3. distinct shapes  → list of (shape,)
      4. full geocoded scan → list of sighting rows

    Drives both _points_bulk_etag() and _points_bulk_build() without a
    real Postgres.
    """

    def __init__(self, sightings):
        self.sightings = sightings
        self._current = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        s = sql.lower()
        if "count(*)" in s and "coalesce(max" in s:
            max_id = max((r[0] for r in self.sightings), default=0)
            self._current = [(len(self.sightings), max_id)]
        elif "from source_database" in s:
            self._current = [(1, "MUFON"), (2, "NUFORC"), (3, "UFOCAT")]
        elif "distinct shape" in s:
            self._current = [("Circle",), ("Light",), ("Triangle",)]
        elif "s.source_db_id" in s and "s.date_event" in s:
            self._current = list(self.sightings)
        else:
            self._current = []

    def fetchone(self):
        if self._current is None:
            return None
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current or [])

    def __iter__(self):
        return iter(self._current or [])

    def close(self):
        pass


class _FakeBulkConn:
    def __init__(self, sightings):
        self._cursor = _FakeBulkCursor(sightings)

    def cursor(self, *a, **kw):
        return self._cursor

    def close(self):
        pass


_SAMPLE_SIGHTINGS = [
    # (id, lat, lng, source_db_id, shape, date_event)
    (1, 35.7796, -78.6382, 1, "Circle", "1995-06-12"),
    (2, 51.5074, -0.1278, 2, "Triangle", "1977-11-09"),
    (3, 40.7128, -74.0060, 3, "Light", "2005-03-14"),
    (4, -33.8688, 151.2093, 2, None, "2012-01-01"),
    (5, 48.8566, 2.3522, 1, "Circle", None),  # unknown year
]


def _install_fake(monkeypatch, sightings):
    import app as _app
    _app.cache.clear()
    # Bust the lru_cache on the build function, otherwise a previous
    # test's sightings might still be cached under a colliding etag.
    _app._points_bulk_build.cache_clear()
    conn = _FakeBulkConn(sightings)
    monkeypatch.setattr(_app, "get_db", lambda: conn)
    return conn, _app


def test_points_bulk_meta_returns_schema_and_lookups(client, monkeypatch):
    _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)
    resp = client.get("/api/points-bulk?meta=1")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    meta = resp.get_json()

    # Shape of the sidecar
    assert meta["count"] == len(_SAMPLE_SIGHTINGS)
    assert meta["schema_version"] == "v080-1"
    assert meta["schema"]["bytes_per_row"] == 16
    assert meta["schema"]["endian"] == "little"

    # Field list is complete and in byte order
    names = [f["name"] for f in meta["schema"]["fields"]]
    assert names == ["id", "lat", "lng", "source_idx", "shape_idx", "year"]

    # Lookups: index 0 reserved for unknown/none, real entries start at 1
    assert meta["sources"][0] is None
    assert "MUFON" in meta["sources"]
    assert meta["shapes"][0] is None
    assert "Circle" in meta["shapes"]

    # ETag + cache headers present
    assert resp.headers.get("ETag")
    assert "max-age" in (resp.headers.get("Cache-Control") or "")


def test_points_bulk_default_returns_gzipped_binary(client, monkeypatch):
    _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)
    resp = client.get("/api/points-bulk")
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert resp.headers.get("ETag")

    # Ungzip and verify size is an exact multiple of 16
    raw = gzip.decompress(resp.data)
    assert len(raw) == len(_SAMPLE_SIGHTINGS) * 16, (
        "packed buffer length must equal count * bytes_per_row"
    )

    # Advertised uncompressed size header matches
    assert int(resp.headers["X-Uncompressed-Size"]) == len(raw)


def test_points_bulk_binary_roundtrip(client, monkeypatch):
    """Unpack the packed bytes and verify every field matches the input.

    float32 precision loss is fine for lat/lng (< 1 cm at the equator);
    the integer fields and the year should survive byte-for-byte.
    """
    _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)
    resp = client.get("/api/points-bulk")
    raw = gzip.decompress(resp.data)

    # Unpack: struct format must match the server's _POINTS_BULK_STRUCT
    fmt = "<IffBBH"
    row_size = struct.calcsize(fmt)
    assert row_size == 16

    unpacked = [
        struct.unpack_from(fmt, raw, offset=i * row_size)
        for i in range(len(_SAMPLE_SIGHTINGS))
    ]

    # id must be exact
    assert [u[0] for u in unpacked] == [s[0] for s in _SAMPLE_SIGHTINGS]

    # lat/lng within float32 precision
    for orig, got in zip(_SAMPLE_SIGHTINGS, unpacked, strict=False):
        assert abs(orig[1] - got[1]) < 1e-4
        assert abs(orig[2] - got[2]) < 1e-4

    # Year 0 for the row with a NULL date_event
    none_date_row = [u for u, s in zip(unpacked, _SAMPLE_SIGHTINGS, strict=False) if s[5] is None][0]
    assert none_date_row[5] == 0

    # Known-year rows parsed out of YYYY prefix
    year_for_id_1 = [u[5] for u in unpacked if u[0] == 1][0]
    assert year_for_id_1 == 1995


def test_points_bulk_304_on_matching_if_none_match(client, monkeypatch):
    _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)

    # First request establishes the ETag
    first = client.get("/api/points-bulk")
    etag = first.headers.get("ETag")
    assert etag

    # Second request sends the ETag back — should get a 304 with no body.
    resp = client.get("/api/points-bulk", headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert not resp.data


def test_points_bulk_lru_cache_reuses_buffer(client, monkeypatch):
    """Two sequential requests should hit _points_bulk_build exactly
    once because @lru_cache is keyed on the etag."""
    conn, _app = _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)

    # First call builds the buffer.
    resp1 = client.get("/api/points-bulk")
    assert resp1.status_code == 200

    # Count how many times the full geocoded scan ran.
    full_scans_1 = sum(
        1 for sql in conn._cursor.executed if "s.source_db_id" in sql
    )

    # Second call — LRU cache should serve without re-running the scan.
    resp2 = client.get("/api/points-bulk")
    assert resp2.status_code == 200

    full_scans_2 = sum(
        1 for sql in conn._cursor.executed if "s.source_db_id" in sql
    )
    assert full_scans_2 == full_scans_1, (
        "The packed buffer must be served from @lru_cache on the second "
        "call — the expensive full-table scan should only run once."
    )


# ---------------------------------------------------------------------------
# Frontend contract — app.js + index.html
# ---------------------------------------------------------------------------
def test_frontend_deck_js_exists():
    assert DECK_JS.exists(), (
        "static/deck.js is the v0.8.0 bulk-loader + deck.gl integration "
        "module. It must exist and be loaded from index.html."
    )


def test_frontend_fetches_points_bulk():
    js = _read(DECK_JS)
    assert "/api/points-bulk" in js, (
        "deck.js must fetch the bulk dataset on Observatory mount."
    )
    # Both the meta sidecar and the binary buffer are fetched.
    assert "meta=1" in js


def test_frontend_uses_deck_gl():
    """The GPU rendering path must be wired — we're looking for the
    deck.gl layer references even though the library is loaded as a
    vendor UMD bundle."""
    js = _read(DECK_JS)
    assert "ScatterplotLayer" in js, (
        "v0.8.0 uses deck.gl ScatterplotLayer for points mode"
    )
    assert "HexagonLayer" in js, (
        "v0.8.0 uses deck.gl HexagonLayer for hex mode"
    )
    assert "HeatmapLayer" in js, (
        "v0.8.0 uses deck.gl HeatmapLayer for heat mode"
    )


def test_frontend_deserialises_packed_rows():
    """The deserialisation loop must know the exact byte offsets so
    the wire format can't drift silently."""
    js = _read(DECK_JS)
    # DataView with the right accessors for the 16-byte row layout.
    assert "getFloat32" in js
    assert "getUint32" in js
    assert "getUint16" in js
    # Byte offsets from the schema must be present (not reading a
    # dynamic offset array — the hot loop uses constants for speed).
    # id@0, lat@4, lng@8, source_idx@12, shape_idx@13, year@14
    assert "o + 4" in js
    assert "o + 8" in js
    assert "o + 12" in js
    assert "o + 13" in js
    assert "o + 14" in js


def test_frontend_has_webgl_fallback_probe():
    """Browsers without WebGL must fall back to the legacy Leaflet
    path — otherwise they'd get a blank map."""
    deck_js = _read(DECK_JS)
    app_js = _read(APP_JS)
    assert "webgl" in deck_js.lower(), (
        "deck.js must probe WebGL capability before attempting to mount"
    )
    assert "useDeckGL" in app_js, (
        "app.js must track a state.useDeckGL boolean so scheduleMapReload "
        "and toggleMapMode can route around the deck.gl path when the "
        "GPU boot fails"
    )


def test_frontend_client_filter_pipeline_present():
    """The whole point of v0.8.0 is that filters don't hit the server.
    Lock the client-side filter wiring so a future refactor can't
    silently bring back the per-filter network round-trip."""
    deck_js = _read(DECK_JS)
    app_js = _read(APP_JS)
    assert "applyClientFilters" in deck_js, (
        "deck.js must expose applyClientFilters so app.js can rebuild "
        "the visible index without touching the server"
    )
    assert "applyClientFilters" in app_js, (
        "app.js applyFilters() must route through applyClientFilters "
        "when the GPU path is active"
    )


def test_index_html_loads_deck_gl_bundle():
    html = _read(INDEX_HTML)
    assert "deck" in html.lower(), (
        "index.html must load the vendored deck.gl UMD bundle in a "
        "<script> tag — otherwise the window.deck global won't exist"
    )
