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
    # v0.11: bumped to 40-byte rows for the transformer emotion
    # columns (32 existing + 8 new bytes).
    assert "_POINTS_BULK_BYTES_PER_ROW = 40" in src, (
        "The row layout is part of the wire contract — the client "
        "hard-codes it into its DataView offsets. Lock it here so a "
        "refactor can't silently bump it without a schema version change."
    )


def test_points_bulk_struct_format():
    src = _read(APP_PY)
    # v0.11: 40-byte row = v0.8.5 32-byte layout + 8 new uint8
    # fields (emotion_28_idx, emotion_28_group, emotion_7_idx,
    # vader_compound, roberta_sentiment, 3× reserved).
    assert '_POINTS_BULK_STRUCT = "<IffIBBBBBBBBBBHHHBBBBBBBB"' in src, (
        "The struct format locks the on-wire order. Little-endian so "
        "the JS DataView reads it directly. 26 fields, 40 bytes total."
    )


def test_points_bulk_etag_function_exists():
    src = _read(APP_PY)
    assert "def _points_bulk_etag" in src
    assert "_POINTS_BULK_SCHEMA_VERSION" in src


def test_points_bulk_build_is_lru_cached():
    src = _read(APP_PY)
    # v0.8.2-cleanup-3: the build function was split into a coalescing
    # wrapper (_points_bulk_build) and the @lru_cache'd implementation
    # (_points_bulk_build_cached). The wrapper takes a per-etag lock so
    # concurrent first-requests share one build instead of stampeding
    # the DB. Either name is acceptable as long as something is
    # @functools.lru_cache'd on the etag.
    idx = src.find("def _points_bulk_build_cached")
    if idx == -1:
        idx = src.find("def _points_bulk_build")
    assert idx != -1, "build function missing"
    # Look backwards ~400 chars for the decorator
    preceding = src[max(0, idx - 400):idx]
    assert "@functools.lru_cache" in preceding, (
        "_points_bulk_build_cached must be @lru_cache'd on the etag so "
        "every request after the first is a zero-work cache hit"
    )


def test_points_bulk_route_registered(flask_app):
    rules = [r.rule for r in flask_app.url_map.iter_rules()]
    assert "/api/points-bulk" in rules


# ---------------------------------------------------------------------------
# Functional tests — fake DB, exercise the real route handler
# ---------------------------------------------------------------------------
class _FakeBulkCursor:
    """Minimal cursor stub for the v082-1 points-bulk path.

    Scripted to answer every query _points_bulk_build() issues:
      1. ETag aggregates (count, max_id)
      2. information_schema.columns column probe
      3. source_database lookup
      4. DISTINCT standardized_shape (or shape) lookup
      5. DISTINCT primary_color lookup
      6. DISTINCT dominant_emotion lookup
      7. Full geocoded sighting scan (17-tuple rows)
    """

    def __init__(self, sightings, present_columns=frozenset()):
        self.sightings = sightings
        self.present_columns = set(present_columns)
        self._current = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        s = sql.lower()
        if "count(*)" in s and "coalesce(max" in s:
            max_id = max((r[0] for r in self.sightings), default=0)
            self._current = [(len(self.sightings), max_id)]
        elif "has_movement_mentioned = 1" in s:
            # v0.8.6 — _points_bulk_etag() probes this count as a
            # data-content signal. Count rows whose 18th tuple slot
            # (has_movement_mentioned) is truthy. Sightings here are
            # 19-tuples per _make_sighting()'s order.
            n = sum(1 for r in self.sightings if len(r) >= 18 and r[17])
            self._current = [(n,)]
        elif "information_schema.columns" in s:
            # The endpoint probes which derived columns exist.
            self._current = [(c,) for c in self.present_columns]
        elif "from source_database" in s:
            self._current = [(1, "MUFON"), (2, "NUFORC"), (3, "UFOCAT")]
        elif "distinct standardized_shape" in s:
            self._current = [("circle", "circle"), ("triangle", "triangle")]
        elif "distinct shape" in s and "standardized" not in s:
            self._current = [("Circle", "circle"), ("Light", "light"), ("Triangle", "triangle")]
        elif "distinct primary_color" in s:
            self._current = [("red", "red"), ("white", "white")]
        elif "distinct dominant_emotion" in s:
            self._current = [("fear", "fear"), ("surprise", "surprise")]
        elif "distinct emotion_28_dominant" in s:
            # v0.11 — GoEmotions 28-class lookup
            self._current = [("confusion",), ("fear",), ("neutral",)]
        elif "distinct emotion_7_dominant" in s:
            # v0.11 — 7-class RoBERTa lookup
            self._current = [("fear",), ("neutral",), ("surprise",)]
        elif "from sighting s" in s and "join location l" in s and "order by s.id" in s:
            # The big geocoded scan. Must return 17-tuple rows matching
            # the SELECT list in _points_bulk_build().
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
    def __init__(self, sightings, present_columns=frozenset()):
        self._cursor = _FakeBulkCursor(sightings, present_columns)

    def cursor(self, *a, **kw):
        return self._cursor

    def close(self):
        pass


def _make_sighting(
    sid, lat, lng, *,
    src=1, raw_shape="Circle", date_event="1995-06-12", duration=None,
    witnesses=None, sighting_dt=None, std_shape=None,
    prim_color=None, dom_emotion=None,
    quality=None, richness=None, hoax=None,
    has_desc=0, has_media=0,
    has_movement=0, movement_cats_json=None,
    emo28_dom=None, emo28_group=None, emo7_dom=None,
    vader_comp=None, roberta_sent=None,
):
    """Build a 24-tuple matching the v011-1 SELECT list.

    Order must match _points_bulk_build():
      id, latitude, longitude, source_db_id, raw_shape, date_event,
      duration_seconds, raw_num_witnesses,
      sighting_datetime, standardized_shape, primary_color, dominant_emotion,
      quality_score, richness_score, hoax_likelihood,
      has_description, has_media,
      has_movement_mentioned, movement_categories,
      emotion_28_dominant, emotion_28_group, emotion_7_dominant,
      vader_compound, roberta_sentiment
    """
    return (
        sid, lat, lng, src, raw_shape, date_event,
        duration, witnesses,
        sighting_dt, std_shape, prim_color, dom_emotion,
        quality, richness, hoax, has_desc, has_media,
        has_movement, movement_cats_json,
        emo28_dom, emo28_group, emo7_dom,
        vader_comp, roberta_sent,
    )


_SAMPLE_SIGHTINGS = [
    _make_sighting(1, 35.7796, -78.6382, raw_shape="Circle", date_event="1995-06-12"),
    _make_sighting(2, 51.5074, -0.1278,  raw_shape="Triangle", date_event="1977-11-09"),
    _make_sighting(3, 40.7128, -74.0060, raw_shape="Light", date_event="2005-03-14"),
    _make_sighting(4, -33.8688, 151.2093, raw_shape=None, date_event="2012-01-01"),
    _make_sighting(5, 48.8566, 2.3522, raw_shape="Circle", date_event=None),  # unknown date
]


def _install_fake(monkeypatch, sightings):
    import app as _app
    _app.cache.clear()
    # Bust the lru_cache on the build function, otherwise a previous
    # test's sightings might still be cached under a colliding etag.
    # v0.8.2-cleanup-3: the cached function is now
    # _points_bulk_build_cached (the wrapper _points_bulk_build adds
    # per-etag locking). Tests need to clear the cached impl, not the
    # wrapper. Both names exist for back-compat with the prior contract.
    cache_clear = getattr(_app, "_points_bulk_build_cached", _app._points_bulk_build).cache_clear
    cache_clear()
    conn = _FakeBulkConn(sightings)
    monkeypatch.setattr(_app, "get_db", lambda: conn)
    return conn, _app


def test_points_bulk_meta_returns_schema_and_lookups(client, monkeypatch):
    _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)
    resp = client.get("/api/points-bulk?meta=1")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    meta = resp.get_json()

    # v0.8.5 shape (v0.8.3b data layer)
    assert meta["count"] == len(_SAMPLE_SIGHTINGS)
    # v0.11: schema bumped from v083-1 (32B) to v011-1 (40B)
    assert meta["schema_version"] == "v011-1"
    assert meta["schema"]["bytes_per_row"] == 40
    assert meta["schema"]["endian"] == "little"
    assert meta["schema"]["score_unknown"] == 255
    assert meta["schema"]["date_epoch"] == "1900-01-01"

    # Field list is complete and in byte order
    names = [f["name"] for f in meta["schema"]["fields"]]
    assert names == [
        "id", "lat", "lng", "date_days",
        "source_idx", "shape_idx",
        "quality_score", "hoax_score", "richness_score",
        "color_idx", "emotion_idx", "flags",
        "num_witnesses", "_reserved", "duration_log2",
        # v0.8.5 additions
        "movement_flags", "_reserved2",
        # v0.11 additions
        "emotion_28_idx", "emotion_28_group",
        "emotion_7_idx", "vader_compound", "roberta_sentiment",
        "_reserved3a", "_reserved3b", "_reserved3c",
    ]

    # v0.8.5 flag bits map: bit 0 = has_desc, bit 1 = has_media,
    # bit 2 = has_movement
    assert meta["schema"]["flag_bits"]["has_description"] == 0
    assert meta["schema"]["flag_bits"]["has_media"] == 1
    assert meta["schema"]["flag_bits"]["has_movement"] == 2

    # Lookups: index 0 reserved for unknown/none, real entries
    # start at 1. v0.9.1 changed meta["sources"][0] from None to
    # the literal string "(unknown)" so client-side charts that
    # render per-source categories have a labelled bucket for
    # orphaned-FK rows instead of silently dropping them. Other
    # lookups (shapes/colors/emotions) still use None at index 0.
    assert meta["sources"][0] == "(unknown)"
    assert "MUFON" in meta["sources"]
    assert meta["shapes"][0] is None
    assert "Circle" in meta["shapes"]
    # New v0.8.2 lookups
    assert "colors" in meta
    assert "emotions" in meta
    assert meta["colors"][0] is None
    assert meta["emotions"][0] is None

    # v0.8.5 — movements lookup is a flat list in bit order
    assert "movements" in meta
    assert len(meta["movements"]) == 10
    assert meta["movements"][0] == "hovering"
    assert meta["movements"][9] == "landed"

    # Coverage + columns_present maps for the UI to decide which
    # filter toggles to enable.
    assert "coverage" in meta
    assert "columns_present" in meta
    # In the fake DB none of the derived columns are present, so every
    # columns_present entry should be False.
    assert meta["columns_present"]["quality_score"] is False
    assert meta["columns_present"]["hoax_likelihood"] is False
    assert meta["columns_present"]["has_movement_mentioned"] is False
    assert meta["columns_present"]["movement_categories"] is False

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

    # Ungzip and verify size is an exact multiple of 40 (v0.11 row size)
    raw = gzip.decompress(resp.data)
    assert len(raw) == len(_SAMPLE_SIGHTINGS) * 40, (
        "packed buffer length must equal count * bytes_per_row"
    )

    # Advertised uncompressed size header matches
    assert int(resp.headers["X-Uncompressed-Size"]) == len(raw)


def test_points_bulk_binary_roundtrip(client, monkeypatch):
    """Unpack the packed bytes and verify key fields match the input.

    float32 precision loss is fine for lat/lng (< 1 cm at the equator).
    The v0.11 row format carries 25 unpacked fields in 40 bytes.
    """
    _install_fake(monkeypatch, _SAMPLE_SIGHTINGS)
    resp = client.get("/api/points-bulk")
    raw = gzip.decompress(resp.data)

    fmt = "<IffIBBBBBBBBBBHHHBBBBBBBB"
    row_size = struct.calcsize(fmt)
    assert row_size == 40

    unpacked = [
        struct.unpack_from(fmt, raw, offset=i * row_size)
        for i in range(len(_SAMPLE_SIGHTINGS))
    ]

    # Unpacked tuple order:
    #  (id, lat, lng, date_days, source_idx, shape_idx,
    #   quality, hoax, richness, color_idx, emotion_idx, flags,
    #   num_witnesses, _reserved, duration_log2,
    #   movement_flags, _reserved2)

    # id must be exact
    assert [u[0] for u in unpacked] == [s[0] for s in _SAMPLE_SIGHTINGS]

    # lat/lng within float32 precision
    for orig, got in zip(_SAMPLE_SIGHTINGS, unpacked, strict=False):
        assert abs(orig[1] - got[1]) < 1e-4
        assert abs(orig[2] - got[2]) < 1e-4

    # date_days: 0 for the row with NULL date_event, positive otherwise
    # Sighting index 4 has date_event=None → date_days should be 0
    by_id = {u[0]: u for u in unpacked}
    assert by_id[5][3] == 0  # sid=5 has no date

    # Sighting 1: 1995-06-12 → days since 1900-01-01
    # (this is deterministic; we just want > 0 and < 100000)
    assert by_id[1][3] > 0 and by_id[1][3] < 100000

    # With no derived columns populated in the fake DB, every score
    # should be the 255 sentinel.
    for u in unpacked:
        assert u[6] == 255   # quality_score
        assert u[7] == 255   # hoax_score
        assert u[8] == 255   # richness_score
        # flags byte should be 0 (no has_description, no has_media,
        # no has_movement). All three bits off.
        assert u[11] == 0
        # v0.8.5 — movement_flags should be 0 when the column is
        # NULL (pre-reload state). _reserved2 always 0.
        assert u[15] == 0   # movement_flags
        assert u[16] == 0   # _reserved2


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
    the wire format can't drift silently.

    v0.8.2 uses a 28-byte row; the offsets below are locked to the
    v082-1 schema in docs/V082_PLAN.md.
    """
    js = _read(DECK_JS)
    # DataView with the right accessors for the 28-byte row layout.
    assert "getFloat32" in js
    assert "getUint32" in js
    assert "getUint16" in js
    # v0.8.2 offsets: id@0 lat@4 lng@8 date_days@12 source@16 shape@17
    # quality@18 hoax@19 richness@20 color@21 emotion@22 flags@23
    # num_witnesses@24 (byte 25 reserved) duration_log2@26
    assert "o + 4" in js     # lat
    assert "o + 8" in js     # lng
    assert "o + 12" in js    # date_days
    assert "o + 16" in js    # source_idx
    assert "o + 17" in js    # shape_idx
    assert "o + 18" in js    # quality_score
    assert "o + 26" in js    # duration_log2


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


def test_index_html_loads_both_deck_bundles():
    """v0.8.2-hotfix regression guard: make sure BOTH the deck.gl core
    bundle AND the community deck.gl-leaflet bridge are loaded.

    v0.8.0 originally shipped with a <script> tag pointing at
    @deck.gl/leaflet@9.0.38 — a package scope that has never existed
    on npm. unpkg returned 404, waitForDeck() timed out forever, and
    every browser silently fell back to the legacy /api/map polling
    path, defeating the entire point of the deck.gl rewrite.

    Lock the correct bundles here so a future dep bump can't silently
    re-introduce the 404.
    """
    html = _read(INDEX_HTML)
    # deck.gl core — unpkg URL, any @version is fine
    assert "unpkg.com/deck.gl@" in html, (
        "missing deck.gl core UMD <script> tag"
    )
    # deck.gl-leaflet community bridge — MUST be the community package
    # name, not @deck.gl/leaflet (which has never existed).
    assert "deck.gl-leaflet" in html, (
        "missing deck.gl-leaflet community bridge <script> tag — this "
        "is what exposes window.DeckGlLeaflet.LeafletLayer. If a future "
        "refactor switches back to '@deck.gl/leaflet', unpkg will 404 "
        "and the Observatory will silently fall back to the legacy "
        "per-pan /api/map polling path."
    )
    # Sanity: the URL is NOT the old broken one. Checking the literal
    # src URL rather than the package name, so this file's own
    # documentation comments don't trip the assertion.
    assert "unpkg.com/@deck.gl/leaflet" not in html, (
        "@deck.gl/leaflet was the v0.8.0 bug — that scope has never "
        "existed on npm. Must use the community deck.gl-leaflet package."
    )


def test_deck_js_waits_for_correct_globals():
    """deck.js's waitForDeck() must poll for window.DeckGlLeaflet (the
    community bridge global), NOT window.deck.LeafletLayer.

    This is the v0.8.2-hotfix regression guard. The v0.8.0 code was
    waiting for a global that the shipped bundle never sets, so the
    GPU path was silently broken for every user on every browser
    for weeks."""
    js = _read(DECK_JS)
    assert "DeckGlLeaflet" in js, (
        "deck.js must reference window.DeckGlLeaflet — that's the "
        "actual global the deck.gl-leaflet UMD bundle exposes"
    )
    # And mountDeckLayer must actually instantiate the class from that
    # global, not from the incorrect window.deck.LeafletLayer path.
    assert "DGL.LeafletLayer" in js or "DeckGlLeaflet.LeafletLayer" in js, (
        "mountDeckLayer must call new DeckGlLeaflet.LeafletLayer(...)"
    )
