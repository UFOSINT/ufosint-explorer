"""
UFO Explorer — Flask web app for the unified UFO sightings database.

Backed by PostgreSQL (Azure Database for PostgreSQL Flexible Server). The
data is migrated from the canonical SQLite output of the ufo-dedup pipeline
via scripts/migrate_sqlite_to_pg.py.

Run locally:
    export DATABASE_URL='postgresql://user:pass@host:5432/ufo_unified?sslmode=require'
    python app.py
    # http://localhost:5000
"""
import os
import json
import math
import time
import hashlib
import functools
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_caching import Cache
from flask_compress import Compress

import psycopg
from psycopg_pool import ConnectionPool

app = Flask(__name__, static_folder="static")


# ---------------------------------------------------------------------------
# Asset version — tacked onto static asset URLs as ?v=<version> so a new
# deploy busts any browser cache immediately.
#
# Preference order: AZURE env var set by deploy → git SHA → mtime hash of
# the two big static files. Computed once at import time; the route below
# substitutes it into index.html on every request.
# ---------------------------------------------------------------------------
def _compute_asset_version() -> str:
    env_ver = os.environ.get("ASSET_VERSION") or os.environ.get("GITHUB_SHA")
    if env_ver:
        return env_ver[:12]
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if sha:
            return sha
    except Exception:
        pass
    # Final fallback: hash the (mtime, size) of the two big static files.
    h = hashlib.md5()
    for name in ("static/style.css", "static/app.js", "static/index.html"):
        p = Path(__file__).parent / name
        if p.exists():
            st = p.stat()
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:12]


ASSET_VERSION = _compute_asset_version()
print(f"ASSET_VERSION = {ASSET_VERSION}")

# Read index.html once at startup and pre-substitute the {{ASSET_VERSION}}
# placeholder. Avoids a file read + string replace on every request.
_INDEX_HTML_PATH = Path(__file__).parent / "static" / "index.html"
try:
    _INDEX_HTML = _INDEX_HTML_PATH.read_text(encoding="utf-8").replace(
        "{{ASSET_VERSION}}", ASSET_VERSION
    )
except Exception as e:
    print(f"Could not preload index.html: {e}")
    _INDEX_HTML = None

# gzip / brotli on every response > 500 bytes. The 4 MB world-map JSON
# response shrinks to ~700 KB. Free win for everyone, biggest win for
# mobile users on slow connections.
app.config["COMPRESS_MIMETYPES"] = [
    "application/json",
    "text/html", "text/css", "text/javascript", "application/javascript",
    "image/svg+xml",
]
app.config["COMPRESS_LEVEL"] = 6           # gzip default; balances CPU vs ratio
app.config["COMPRESS_MIN_SIZE"] = 500
Compress(app)

# MCP-over-HTTP server, mounted at /mcp. Lets any MCP-aware AI client
# (Claude Desktop, Cursor, Cline, Continue, Windsurf, etc.) call the
# tool catalog directly using their own LLM, with no inference cost
# to us. See mcp_http.py for the JSON-RPC implementation.
# Deliberate late import: mcp_http imports from app, so we register
# the blueprint after the Flask instance exists. (ruff E402)
from mcp_http import mcp_bp  # noqa: E402
app.register_blueprint(mcp_bp)

# ---------------------------------------------------------------------------
# Response cache (Flask-Caching)
#
# If REDIS_URL is set in the environment we use it as a SHARED backend, so
# all gunicorn workers hit the same cache — a single warm key benefits every
# worker, and the cache survives worker restarts. This is the big perceived
# speedup: repeated hits to /api/stats, /api/filters, /api/hexbin become
# sub-millisecond regardless of which worker handles the request.
#
# Without REDIS_URL we fall back to a per-process SimpleCache (each gunicorn
# worker keeps its own ~500-entry LRU). That's the "free tier" path and is
# what local tests use — no redis server needed.
#
# Azure Cache for Redis Basic C0 is ~$16/mo and gives a 250 MB shared cache
# that's easily enough for this workload. Set REDIS_URL to the connection
# string the Azure portal gives you, e.g.:
#   rediss://:<PRIMARY_KEY>@<name>.redis.cache.windows.net:6380/0
# ---------------------------------------------------------------------------
_REDIS_URL = os.environ.get("REDIS_URL", "").strip()
if _REDIS_URL:
    _cache_cfg = {
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_URL": _REDIS_URL,
        "CACHE_DEFAULT_TIMEOUT": 300,
        # Namespace every key so multiple deploys pointed at the same
        # Redis instance don't collide, and a new version auto-invalidates
        # the previous version's cached responses.
        "CACHE_KEY_PREFIX": f"ufosint:{ASSET_VERSION}:",
        # Fail closed: if Redis hiccups we want the request to still work,
        # not 500. flask-caching 2.x silently swallows backend errors and
        # serves the uncached response, which is exactly what we want.
    }
    print(f"[cache] backend=RedisCache prefix=ufosint:{ASSET_VERSION}:")
else:
    _cache_cfg = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 300,
        "CACHE_THRESHOLD": 500,  # max number of cached items per worker
    }
    print("[cache] backend=SimpleCache (set REDIS_URL for a shared cache)")

cache = Cache(app, config=_cache_cfg)

# ---------------------------------------------------------------------------
# Database connection (PostgreSQL via psycopg + connection pool)
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL env var is required. Example:\n"
        "  postgresql://user:pass@host.postgres.database.azure.com:5432/ufo_unified?sslmode=require"
    )

# Pool sizing rule of thumb: at least one connection per gunicorn worker
# thread. Procfile uses 2 workers x 4 threads = 8 concurrent slots, so
# max_size=8 leaves one connection per slot. Burstable B1ms has a small
# max_connections (~50) so we stay well under the per-server cap.
_pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=8,
    open=True,
    timeout=30,
    kwargs={
        # Read-only workload: autocommit avoids the BEGIN/COMMIT overhead
        # on every query, and default_transaction_read_only is a safety
        # net in case anything ever tries to write.
        "autocommit": True,
        "options": "-c default_transaction_read_only=on",
    },
)


class _PooledConn:
    """Thin proxy around a pooled psycopg connection.

    Lets the existing call sites keep using `conn = get_db(); ...; conn.close()`
    without rewriting every route. On `.close()` the underlying connection
    is returned to the pool instead of being torn down.
    """

    __slots__ = ("_real",)

    def __init__(self, real_conn):
        self._real = real_conn

    def cursor(self, *args, **kwargs):
        return self._real.cursor(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        if self._real is not None:
            _pool.putconn(self._real)
            self._real = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def get_db():
    """Check out a pooled, read-only PostgreSQL connection.

    Caller is responsible for calling .close() (which returns the
    underlying connection to the pool) or using a `with` block.
    """
    return _PooledConn(_pool.getconn())


# ---------------------------------------------------------------------------
# HTTP cache headers
# ---------------------------------------------------------------------------
# Routes that are safe to cache in the browser/CDN for a few minutes.
# Keep these in sync with the @cache.cached server-side decorators below.
_BROWSER_CACHEABLE_PREFIXES = (
    "/api/stats",
    "/api/filters",
    "/api/map",
    "/api/heatmap",
    "/api/timeline",
    "/api/search",
    "/api/sentiment/",
    "/api/duplicates",
)

@app.after_request
def add_cache_headers(response):
    """Set Cache-Control on cacheable responses.

    - /static/* with a ?v=<asset_version> query string is safe to cache
      for a year immutable — the version changes on every deploy, so a
      new URL is issued and the old URL gets left behind. This is the
      fingerprint-in-URL pattern.
    - /static/* WITHOUT a version string gets a conservative 1-hour
      max-age + must-revalidate, so an old cached copy can't lock the
      browser out of a new deploy for more than an hour. This is the
      safety net that protects direct-link uses (preload scans, dev
      tools, curl).
    - /api/* cacheable endpoints get 5 min so a pan/zoom session doesn't
      hammer the server.
    - /api/sighting/<id> and /health stay no-cache.
    - `/` (the HTML shell) gets a short 60s cache so the version string
      in the <link>/<script> tags stays fresh after a deploy.
    """
    path = request.path or ""
    if path.startswith("/static/"):
        if request.args.get("v"):
            # Versioned asset URL — immutable for a year.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # Unversioned — short cache with required revalidation.
            response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    elif any(path.startswith(p) for p in _BROWSER_CACHEABLE_PREFIXES):
        response.headers["Cache-Control"] = "public, max-age=300"
    elif path == "/":
        response.headers["Cache-Control"] = "public, max-age=60"
    return response


# ---------------------------------------------------------------------------
# Cache filter values on startup
# ---------------------------------------------------------------------------
FILTER_CACHE = {}


def init_filters():
    """Load distinct filter values once at startup."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT shape FROM sighting
        WHERE shape IS NOT NULL AND shape != ''
        ORDER BY shape
    """)
    FILTER_CACHE["shapes"] = [r[0] for r in cur.fetchall()]

    cur.execute("""
        SELECT DISTINCT hynek FROM sighting
        WHERE hynek IS NOT NULL AND hynek != ''
        ORDER BY hynek
    """)
    FILTER_CACHE["hynek"] = [r[0] for r in cur.fetchall()]

    cur.execute("""
        SELECT DISTINCT vallee FROM sighting
        WHERE vallee IS NOT NULL AND vallee != ''
        ORDER BY vallee
    """)
    FILTER_CACHE["vallee"] = [r[0] for r in cur.fetchall()]

    cur.execute("SELECT id, name FROM source_database ORDER BY name")
    FILTER_CACHE["sources"] = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

    cur.execute("SELECT id, name, display_name FROM source_collection ORDER BY name")
    FILTER_CACHE["collections"] = [{"id": r[0], "name": r[1], "display_name": r[2]} for r in cur.fetchall()]

    # Cache distinct match methods for duplicates filter
    cur.execute("""
        SELECT DISTINCT match_method FROM duplicate_candidate
        WHERE match_method IS NOT NULL
        ORDER BY match_method
    """)
    FILTER_CACHE["match_methods"] = [r[0] for r in cur.fetchall()]

    # Top countries by sighting count, used by the Country filter dropdown.
    # We pick the top 60 to keep the dropdown manageable; users who need
    # more granular geographic filtering can use the place-search box on
    # the Map tab. The HAVING clause excludes single-row noise.
    cur.execute("""
        SELECT l.country, COUNT(*) AS n
        FROM location l
        JOIN sighting s ON s.location_id = l.id
        WHERE l.country IS NOT NULL AND l.country != ''
        GROUP BY l.country
        HAVING COUNT(*) >= 5
        ORDER BY n DESC
        LIMIT 60
    """)
    FILTER_CACHE["countries"] = [
        {"value": r[0], "count": r[1]} for r in cur.fetchall()
    ]

    # Top states by sighting count. The data is dominated by US states +
    # Canadian provinces but also has some UK counties and stray noise;
    # the HAVING + LIMIT keep the dropdown to the meaningful long tail.
    cur.execute("""
        SELECT l.state, COUNT(*) AS n
        FROM location l
        JOIN sighting s ON s.location_id = l.id
        WHERE l.state IS NOT NULL AND l.state != ''
        GROUP BY l.state
        HAVING COUNT(*) >= 50
        ORDER BY n DESC
        LIMIT 100
    """)
    FILTER_CACHE["states"] = [
        {"value": r[0], "count": r[1]} for r in cur.fetchall()
    ]

    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def add_common_filters(params, clauses, args, table_prefix="s"):
    """Add common filter clauses shared across endpoints.

    PostgreSQL uses %s placeholders (vs SQLite's ?). All callers append
    args via this helper so the placeholders match the args list ordering.
    """
    p = table_prefix

    shape = params.get("shape")
    if shape:
        clauses.append(f"{p}.shape = %s")
        args.append(shape)

    source = params.get("source")
    if source:
        clauses.append(f"{p}.source_db_id = %s")
        args.append(int(source))

    collection = params.get("collection")
    if collection:
        clauses.append(f"{p}.source_db_id IN (SELECT id FROM source_database WHERE collection_id = %s)")
        args.append(int(collection))

    hynek = params.get("hynek")
    if hynek:
        clauses.append(f"{p}.hynek = %s")
        args.append(hynek)

    vallee = params.get("vallee")
    if vallee:
        clauses.append(f"{p}.vallee = %s")
        args.append(vallee)

    date_from = params.get("date_from")
    if date_from:
        clauses.append(f"{p}.date_event >= %s")
        args.append(date_from)

    date_to = params.get("date_to")
    if date_to:
        clauses.append(f"{p}.date_event <= %s")
        args.append(date_to + "-12-31" if len(date_to) == 4 else date_to)

    # Geographic filters — exact match on the location table.
    # IMPORTANT: any caller using these (or the coords filter below)
    # must LEFT JOIN location l in its FROM clause.
    country = params.get("country")
    if country:
        clauses.append("l.country = %s")
        args.append(country)

    state = params.get("state")
    if state:
        clauses.append("l.state = %s")
        args.append(state)

    # Coordinate source filter (for map/heatmap)
    coords = params.get("coords")
    if coords == "original":
        clauses.append("l.geocode_src IS NULL")
    elif coords == "geocoded":
        clauses.append("l.geocode_src IS NOT NULL")
    # "all" or empty = no filter (show everything)

    return clauses, args


# Keys that add_common_filters() checks. Used by _has_common_filters()
# below to decide whether a materialized-view fast path is eligible.
# IMPORTANT: keep this set in sync with add_common_filters() above. If you
# add a new filter key there, add it here too or the MV path will serve
# stale/incorrect results for that filter.
_COMMON_FILTER_KEYS = frozenset({
    "shape", "source", "collection", "hynek", "vallee",
    "date_from", "date_to", "country", "state", "coords",
})


def _has_common_filters(params) -> bool:
    """True if any of the filter keys add_common_filters() respects is set.

    Used as the eligibility check for the v0.7.5 materialized-view fast
    paths on /api/stats, /api/timeline, and /api/sentiment/overview. If
    this returns False, the endpoint can safely read from the MV (no
    filters applied). If True, it must fall back to the live query.
    """
    for key in _COMMON_FILTER_KEYS:
        v = params.get(key)
        if v not in (None, "", "all"):
            return True
    return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # Serve the version-substituted index.html so the CSS/JS <link> and
    # <script> tags get a fresh ?v=<version> query param on every deploy.
    # Cache-Control is set by add_cache_headers() below.
    if _INDEX_HTML is not None:
        return Response(_INDEX_HTML, content_type="text/html; charset=utf-8")
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    """Health check. Tries to query the DB; returns 200 'waiting' if the
    pool can't connect yet (e.g. during cold start), 200 'ok' otherwise."""
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sighting")
            count = cur.fetchone()[0]
        finally:
            conn.close()
        return jsonify({"status": "ok", "sightings": count})
    except Exception as e:
        return jsonify({"status": "waiting", "detail": str(e)})


def _api_stats_from_mv(conn):
    """Read /api/stats payload from the v0.7.5 materialized views.

    Returns the same shape as the legacy live query so callers can swap
    between the two paths transparently. Raises on MV miss; the caller
    is responsible for catching and falling back.
    """
    cur = conn.cursor()

    cur.execute("""
        SELECT total_sightings, date_min, date_max,
               geocoded_locations, geocoded_original, geocoded_geonames,
               duplicate_candidates
        FROM mv_stats_summary
    """)
    row = cur.fetchone()
    (total, date_min, date_max,
     geocoded, geocoded_original, geocoded_geonames, dupes) = row

    cur.execute("""
        SELECT name, count, collection
        FROM mv_stats_by_source ORDER BY count DESC
    """)
    by_source = [
        {"name": r[0], "count": r[1], "collection": r[2]}
        for r in cur.fetchall()
    ]

    cur.execute("""
        SELECT name, count FROM mv_stats_by_collection ORDER BY count DESC
    """)
    by_collection = [{"name": r[0], "count": r[1]} for r in cur.fetchall()]

    return {
        "total_sightings": total,
        "by_source": by_source,
        "by_collection": by_collection,
        "date_range": {"min": date_min, "max": date_max},
        "geocoded_locations": geocoded,
        "geocoded_original": geocoded_original,
        "geocoded_geonames": geocoded_geonames,
        "duplicate_candidates": dupes,
    }


def _api_stats_from_live(conn):
    """Original pre-v0.7.5 live-query path. Kept as the fallback for when
    the materialized views haven't been created yet (local dev, fresh
    clone, between the deploy running the migration and the refresh
    step completing).
    """
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM sighting")
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT sd.name, COUNT(s.id), COALESCE(sc.name, 'Unknown') as collection
        FROM source_database sd
        LEFT JOIN source_collection sc ON sd.collection_id = sc.id
        LEFT JOIN sighting s ON s.source_db_id = sd.id
        GROUP BY sd.id, sd.name, sc.name ORDER BY COUNT(s.id) DESC
    """)
    by_source = [{"name": r[0], "count": r[1], "collection": r[2]} for r in cur.fetchall()]

    cur.execute("""
        SELECT sc.name, COUNT(s.id)
        FROM source_collection sc
        JOIN source_database sd ON sd.collection_id = sc.id
        LEFT JOIN sighting s ON s.source_db_id = sd.id
        GROUP BY sc.id, sc.name ORDER BY COUNT(s.id) DESC
    """)
    by_collection = [{"name": r[0], "count": r[1]} for r in cur.fetchall()]

    cur.execute("""
        SELECT MIN(date_event), MAX(date_event)
        FROM sighting WHERE date_event IS NOT NULL
    """)
    row = cur.fetchone()
    date_min, date_max = row[0], row[1]

    cur.execute("""
        SELECT COUNT(*) FROM location
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    geocoded = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM location
        WHERE latitude IS NOT NULL AND geocode_src IS NULL
    """)
    geocoded_original = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM location
        WHERE geocode_src IS NOT NULL
    """)
    geocoded_geonames = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM duplicate_candidate")
    dupes = cur.fetchone()[0]

    return {
        "total_sightings": total,
        "by_source": by_source,
        "by_collection": by_collection,
        "date_range": {"min": date_min, "max": date_max},
        "geocoded_locations": geocoded,
        "geocoded_original": geocoded_original,
        "geocoded_geonames": geocoded_geonames,
        "duplicate_candidates": dupes,
    }


@app.route("/api/stats")
@cache.cached(timeout=600, query_string=True)
def api_stats():
    """Dashboard statistics.

    v0.7.5: reads the mv_stats_summary / mv_stats_by_source /
    mv_stats_by_collection materialized views (~5 ms total). Falls back
    to the live-query path if any MV is missing (e.g. fresh clone before
    scripts/add_v075_materialized_views.sql has been applied).

    /api/stats does not accept any filter params, so the MV path is
    always eligible — there's no `if not _has_common_filters(...)` guard.
    """
    conn = get_db()
    try:
        try:
            payload = _api_stats_from_mv(conn)
        except psycopg.errors.UndefinedTable:
            # MV migration hasn't run yet. Not fatal.
            print("[api_stats] mv_stats_* missing, falling back to live query")
            payload = _api_stats_from_live(conn)
    finally:
        conn.close()

    return jsonify(payload)


@app.route("/api/filters")
def api_filters():
    """Return cached filter options."""
    return jsonify(FILTER_CACHE)


@app.route("/api/tools-catalog")
@cache.cached(timeout=3600)
def api_tools_catalog():
    """Tool definitions in OpenAI / OpenRouter function-calling format.

    Consumed by the BYOK chat UI in static/app.js: the browser fetches
    this once on chat panel open and passes it to the user's chosen
    LLM provider as the `tools` parameter on every chat completion.

    The same tools are exposed to MCP clients via the /mcp endpoint
    (see mcp_http.py); both consumers share tools_catalog.TOOLS as
    the single source of truth.
    """
    from tools_catalog import list_tools_openai
    return jsonify({"tools": list_tools_openai()})


@app.route("/api/tool/<name>", methods=["POST"])
def api_tool_call(name):
    """Direct tool invocation for the BYOK chat.

    The browser-side chat orchestrator hits this whenever the LLM
    requests a tool call. We do server-side execution because the
    tool implementations need PostgreSQL credentials we never want
    to expose to the browser.

    Body: JSON object of tool arguments.
    Response: JSON object with the tool's return value, or {error: ...}.
    """
    from tools_catalog import call_tool
    args = request.get_json(silent=True) or {}
    if not isinstance(args, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    result = call_tool(name, args)
    status = 200 if not (isinstance(result, dict) and "error" in result and len(result) == 1) else 400
    return jsonify(result), status


@app.route("/api/map")
@cache.cached(timeout=300, query_string=True)
def api_map():
    """Map markers with bbox + filters, with zoom-aware spatial sampling.

    Sampling strategy:
      - Low zoom (wide view): hash-based pseudo-random sample, gives an
        even spread across the dataset instead of biasing to recent dates.
      - High zoom (narrow view): grid-bucket sample using a window
        function over a 50x50 grid of the bbox, returns up to K samples
        per cell so dense areas don't crowd out sparse ones.

    Both modes are deterministic so the flask-caching layer above gets
    clean cache hits. Default cap is 25,000 markers; the limit query
    parameter overrides up to 100,000 for "Load All".
    """
    conn = get_db()
    cur = conn.cursor()

    clauses = [
        "l.latitude IS NOT NULL", "l.longitude IS NOT NULL",
        "l.latitude BETWEEN -90 AND 90",
        "l.longitude BETWEEN -180 AND 180",
    ]
    args = []

    # Bounding box filter
    south = request.args.get("south")
    north = request.args.get("north")
    west = request.args.get("west")
    east = request.args.get("east")
    have_bbox = all([south, north, west, east])
    if have_bbox:
        south_f = float(south)
        north_f = float(north)
        west_f = float(west)
        east_f = float(east)
        clauses.append("l.latitude BETWEEN %s AND %s")
        args.extend([south_f, north_f])
        clauses.append("l.longitude BETWEEN %s AND %s")
        args.extend([west_f, east_f])
    else:
        # Fall back to whole-world bbox so the grid-sampling math has
        # something to divide by.
        south_f, north_f, west_f, east_f = -90.0, 90.0, -180.0, 180.0

    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses)

    # Configurable limit (default 25000, max 100000)
    req_limit = request.args.get("limit", 25000)
    try:
        req_limit = min(int(req_limit), 100000)
    except (ValueError, TypeError):
        req_limit = 25000

    # Decide sampling strategy from explicit zoom (preferred) or bbox
    # area as a fallback. Zoom 0 = whole world, ~7 = continent, 10+ =
    # city. Threshold: zoom <= 7 -> hash sample; >= 8 -> grid.
    zoom_param = request.args.get("zoom")
    if zoom_param is not None:
        try:
            use_grid = int(zoom_param) >= 8
        except (ValueError, TypeError):
            use_grid = False
    else:
        bbox_area = max(0.0001, (north_f - south_f) * (east_f - west_f))
        use_grid = bbox_area < 100.0  # < ~10 deg on a side

    if use_grid:
        # 50x50 grid of the visible bbox, up to 10 most-recent samples
        # per cell. Worst case 25k markers; usually far fewer because
        # most cells are empty.
        GRID_SIZE = 50
        K_PER_CELL = 10
        lat_step = max((north_f - south_f) / GRID_SIZE, 1e-9)
        lng_step = max((east_f - west_f) / GRID_SIZE, 1e-9)
        sql = f"""
            WITH cells AS (
                SELECT s.id, l.latitude, l.longitude,
                       s.date_event, s.shape, sd.name AS source_name,
                       COALESCE(l.city, '') AS city,
                       COALESCE(l.state, '') AS state,
                       COALESCE(l.country, '') AS country,
                       COALESCE(sc.name, '') AS collection,
                       (s.description IS NOT NULL AND LENGTH(s.description) > 0) AS has_desc,
                       ROW_NUMBER() OVER (
                           PARTITION BY
                               CAST((l.latitude  - %s) / %s AS INTEGER),
                               CAST((l.longitude - %s) / %s AS INTEGER)
                           ORDER BY s.date_event DESC
                       ) AS rn
                FROM sighting s
                JOIN location l ON s.location_id = l.id
                JOIN source_database sd ON s.source_db_id = sd.id
                LEFT JOIN source_collection sc ON sd.collection_id = sc.id
                WHERE {where}
            )
            SELECT id, latitude, longitude, date_event, shape, source_name,
                   city, state, country, collection, has_desc
            FROM cells
            WHERE rn <= %s
            LIMIT %s
        """
        cur.execute(
            sql,
            [south_f, lat_step, west_f, lng_step] + args + [K_PER_CELL, req_limit],
        )
    else:
        # Hash-based pseudo-random sample using WHERE filter (not ORDER BY).
        # ORDER BY ((id * prime) % M) would force PG to seq-scan, hash, and
        # SORT every matching row before taking the LIMIT. With a WHERE
        # filter the engine can stop scanning as soon as it has enough
        # matching rows. We pick the modulo threshold so the expected sample
        # is ~2x the requested limit (gives the engine some slack so the
        # LIMIT actually clips, and PG's planner picks a sensible scan).
        # Total geocoded population is ~106k; for limit=25000 we want
        # ~50000 matches before the LIMIT, so threshold ~= 50000/106000
        # = 47%. We use a 100-bucket modulo for granularity.
        # Use a generous threshold (50%) for the common case so PG can
        # short-circuit early via the LIMIT.
        sql = f"""
            SELECT s.id, l.latitude, l.longitude,
                   s.date_event, s.shape, sd.name AS source_name,
                   COALESCE(l.city, '') AS city,
                   COALESCE(l.state, '') AS state,
                   COALESCE(l.country, '') AS country,
                   COALESCE(sc.name, '') AS collection,
                   (s.description IS NOT NULL AND LENGTH(s.description) > 0) AS has_desc
            FROM sighting s
            JOIN location l ON s.location_id = l.id
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN source_collection sc ON sd.collection_id = sc.id
            WHERE {where}
              AND ((s.id * 2654435761) %% 100) < 50
            LIMIT %s
        """
        cur.execute(sql, args + [req_limit])

    markers = []
    for r in cur.fetchall():
        markers.append({
            "id": r[0],
            "lat": r[1],
            "lng": r[2],
            "date": r[3],
            "shape": r[4],
            "source": r[5],
            "city": r[6],
            "state": r[7],
            "country": r[8],
            "collection": r[9],
            "has_desc": bool(r[10]),
        })

    # Total-in-view count: only run the COUNT(*) when we hit the limit,
    # since otherwise len(markers) IS the total. Saves a full filtered
    # scan on the common case.
    if len(markers) < req_limit:
        total_in_view = len(markers)
    else:
        cur.execute(
            f"SELECT COUNT(*) FROM sighting s JOIN location l ON s.location_id = l.id WHERE {where}",
            args,
        )
        total_in_view = cur.fetchone()[0]

    conn.close()
    return jsonify({
        "markers": markers,
        "count": len(markers),
        "total_in_view": total_in_view,
        "sample_strategy": "grid" if use_grid else "hash",
    })


@app.route("/api/heatmap")
@cache.cached(timeout=300, query_string=True)
def api_heatmap():
    """Lightweight coordinate-only endpoint for heatmap rendering.

    Returns [lat, lng] pairs. Default 50k limit, supports limit param for Load All.
    Cached for 5 minutes per unique query string.
    """
    conn = get_db()
    cur = conn.cursor()

    clauses = [
        "l.latitude IS NOT NULL", "l.longitude IS NOT NULL",
        "l.latitude BETWEEN -90 AND 90",
        "l.longitude BETWEEN -180 AND 180",
    ]
    args = []

    # Bounding box filter
    south = request.args.get("south")
    north = request.args.get("north")
    west = request.args.get("west")
    east = request.args.get("east")
    if all([south, north, west, east]):
        clauses.append("l.latitude BETWEEN %s AND %s")
        args.extend([float(south), float(north)])
        clauses.append("l.longitude BETWEEN %s AND %s")
        args.extend([float(west), float(east)])

    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses)

    # Configurable limit (default 50000, max 200000 for heatmap since it's just coords)
    req_limit = request.args.get("limit", 50000)
    try:
        req_limit = min(int(req_limit), 200000)
    except (ValueError, TypeError):
        req_limit = 50000

    sql = f"""
        SELECT l.latitude, l.longitude
        FROM sighting s
        JOIN location l ON s.location_id = l.id
        WHERE {where}
        LIMIT %s
    """

    cur.execute(sql, args + [req_limit])
    points = [[r[0], r[1]] for r in cur.fetchall()]

    # Only run COUNT when we hit the limit; otherwise len(points) IS the total.
    if len(points) < req_limit:
        total_in_view = len(points)
    else:
        cur.execute(
            f"SELECT COUNT(*) FROM sighting s JOIN location l ON s.location_id = l.id WHERE {where}",
            args,
        )
        total_in_view = cur.fetchone()[0]

    conn.close()
    return jsonify({
        "points": points,
        "count": len(points),
        "total_in_view": total_in_view,
    })


# ---------------------------------------------------------------------------
# Hex-bin endpoint (v0.7.3 — runtime SQL bucketing, no MV required)
# ---------------------------------------------------------------------------
#
# The v0.7.0 implementation read pre-computed H3 cells from a
# `hex_bin_counts` materialized view populated by scripts/compute_hex_bins.py.
# That required a one-time manual workflow run, a GitHub DATABASE_URL
# secret, and the h3 Python library — and when any of those weren't set
# up the endpoint returned 503 and the client silently fell back to
# Heatmap. Nobody ever ran the setup, so HexBin mode never worked.
#
# v0.7.3 replaces the MV with plain SQL bucketing: FLOOR(lat/size) and
# FLOOR(lng/size) against idx_location_coords (the existing composite
# index on location(latitude, longitude)). No extensions, no MV, no
# pre-compute step — works out of the box on any fresh deploy.
#
# The buckets are square, not true hexes, but the client draws a
# flat-top hexagon around each bucket's centroid so the visual effect
# matches the mockup. With the bbox filter + a LIMIT 2000 on the
# aggregate, query time stays under 100 ms at every zoom level.
#
# Query params:
#   zoom                       — Leaflet zoom (0-18), drives cell size
#   south, north, west, east   — viewport bbox (required; client sends)
#   source / shape / country   — passthrough to add_common_filters
#   date_from / date_to        — passthrough to add_common_filters

# Zoom → cell size in degrees. Bigger zoom = smaller cells. Values tuned
# so a desktop viewport at each zoom yields roughly 200-1200 cells,
# which plots fast in Leaflet without degrading into noise.
_HEX_CELL_SIZES = {
    0: 20.0, 1: 15.0, 2: 10.0, 3: 7.0, 4: 5.0,
    5: 3.0, 6: 2.0, 7: 1.2, 8: 0.7, 9: 0.4,
    10: 0.25, 11: 0.15, 12: 0.08, 13: 0.05,
    14: 0.03, 15: 0.02, 16: 0.015, 17: 0.01, 18: 0.008,
}


def _hex_cell_size(zoom: int) -> float:
    """Return the bucket side length in degrees for a given Leaflet zoom."""
    if zoom < 0:
        return _HEX_CELL_SIZES[0]
    if zoom > 18:
        return _HEX_CELL_SIZES[18]
    return _HEX_CELL_SIZES[zoom]


# Kept from v0.7.0 so existing tests that import _zoom_to_res still pass.
# The value is unused by the new runtime-bucketing path below.
def _zoom_to_res(zoom: int) -> int:
    """Legacy H3 resolution mapping. No longer used at runtime but
    preserved for test compatibility."""
    if zoom <= 3:
        return 2
    if zoom <= 5:
        return 3
    if zoom <= 7:
        return 4
    if zoom <= 9:
        return 5
    return 6


@app.route("/api/hexbin")
@cache.cached(timeout=300, query_string=True)
def api_hexbin():
    """Runtime-computed hex-bin aggregates for the Observatory view.

    Buckets sightings into a lat/lng grid sized for the current Leaflet
    zoom, counts per bucket, and returns centroids + size so the client
    can draw true-hex polygons around them. No materialized view, no
    Postgres extensions — works on any fresh deploy.
    """
    try:
        zoom = int(request.args.get("zoom", 4))
    except (TypeError, ValueError):
        zoom = 4
    size = _hex_cell_size(zoom)

    # Viewport bbox. If missing we default to the whole world so the
    # endpoint stays useful from curl / tests even without a real map.
    def _f(key: str, fallback: float) -> float:
        try:
            return float(request.args.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    south = max(-90.0, _f("south", -85.0))
    north = min(90.0, _f("north", 85.0))
    west = max(-180.0, _f("west", -180.0))
    east = min(180.0, _f("east", 180.0))
    if north <= south or east <= west:
        return jsonify({"zoom": zoom, "size": size, "count": 0, "cells": []})

    try:
        conn = get_db()
    except Exception as e:
        return jsonify({
            "error": f"database unavailable: {e}",
            "cells": [],
            "zoom": zoom,
            "size": size,
        }), 503

    try:
        cur = conn.cursor()

        # Assemble WHERE clauses: bbox first, then the shared filter
        # helper (source / shape / country / date_from / date_to / coords).
        # add_common_filters assumes the location alias is `l` and the
        # sighting alias is `s`, which matches our FROM clause.
        clauses = [
            "l.latitude IS NOT NULL",
            "l.longitude IS NOT NULL",
            "l.latitude BETWEEN %s AND %s",
            "l.longitude BETWEEN %s AND %s",
        ]
        args: list = [south, north, west, east]
        add_common_filters(request.args, clauses, args)
        where = " AND ".join(clauses)

        # v0.7.7: True honeycomb tessellation via offset-row bucketing.
        #
        # For pointy-top hexes with horizontal center-to-center spacing
        # `hex_h` (= `size` in the existing zoom table), the proper
        # vertical row spacing is hex_h * sqrt(3)/2 and odd rows are
        # shifted horizontally by hex_h/2 (the standard "offset-r"
        # coordinate system). This replaces v0.7.6's square grid where
        # each hex was inscribed in its cell and left diagonal gaps.
        #
        # The SQL computes `row` first (from latitude), then uses that
        # row parity to decide whether to shift longitude by hex_h/2
        # before bucketing the column. A subquery materialises row so
        # we only compute the FLOOR once per sighting.
        #
        # Approximation note: rectangular bucket boundaries don't match
        # the actual hexagon edges, so points near hex corners can fall
        # into an adjacent cell. For a density visualisation that's
        # invisible — the cells still tessellate and the counts shift
        # at most by 1-2 percent near the corner boundaries.
        hex_h = size                        # horizontal spacing between hex centers (deg)
        hex_v = size * math.sqrt(3) / 2     # vertical row spacing (≈ 0.866 * size)
        half_h = hex_h / 2.0

        sql = f"""
            SELECT
                row,
                FLOOR(
                    (lng - %s - CASE WHEN (row %% 2) <> 0 THEN %s ELSE 0 END)
                    / %s
                )::int AS col,
                COUNT(*)::int AS cnt
            FROM (
                SELECT
                    l.longitude AS lng,
                    FLOOR((l.latitude - %s) / %s)::int AS row
                FROM sighting s
                JOIN location l ON l.id = s.location_id
                WHERE {where}
            ) sub
            GROUP BY row, col
            ORDER BY cnt DESC
            LIMIT 2000
        """

        cur.execute(sql, [west, half_h, hex_h, south, hex_v, *args])
        rows = cur.fetchall()

        # Cell center for the honeycomb:
        #   cLat = south + (row + 0.5) * hex_v
        #   cLng = west  + (col + 0.5 + 0.5_if_odd) * hex_h
        # which matches the SQL bucketing above on both parities.
        cells = []
        for r in rows:
            row_i = int(r[0])
            col_i = int(r[1])
            row_offset = 0.5 if (row_i % 2) != 0 else 0.0
            cells.append({
                "row": row_i,
                "col": col_i,
                "cnt": int(r[2]),
                "lat": south + (row_i + 0.5) * hex_v,
                "lng": west + (col_i + 0.5 + row_offset) * hex_h,
            })

        return jsonify({
            "zoom": zoom,
            "size": size,
            "count": len(cells),
            "cells": cells,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bulk points endpoint (v0.8.0 — client-side rendering backbone)
# ---------------------------------------------------------------------------
#
# Returns every geocoded sighting as a packed binary buffer (~1.7 MB raw,
# ~700 KB gzipped at current dataset size). The client downloads this ONCE
# per session, deserialises into typed arrays, and renders everything on
# the GPU via deck.gl. Pan, zoom, heat, and hex-bin modes all run off the
# same in-memory buffer with no further server round-trips — the DB only
# hears about clicks (via /api/sighting/:id) and landing-page stats.
#
# Binary row layout (16 bytes, little-endian):
#   [0:4]   uint32  id
#   [4:8]   float32 lat
#   [8:12]  float32 lng
#   [12]    uint8   source_idx   (1..N, 0 = unknown)
#   [13]    uint8   shape_idx    (1..M, 0 = none; capped at 254 distinct)
#   [14:16] uint16  year         (0 if unknown)
#
# The accompanying meta JSON (`?meta=1`) carries the id -> name lookup
# tables, the schema descriptor, and the ETag.
#
# Caching:
#   - In-process: @lru_cache(maxsize=2) keyed on ETag so every worker
#     holds at most two packed buffers in memory (~4 MB max).
#   - HTTP: strong ETag based on schema version + rowcount + max(id).
#     A 304 is ~180 bytes per request.
#   - Browser: Cache-Control public, max-age=3600 + IndexedDB mirror
#     on the client (handled in static/js/bulk.js).

# Bump this whenever the binary layout or meta shape changes. Forces
# every browser + every in-process lru_cache entry to invalidate even
# if the row count and max id happen to match.
#
# v0.8.2 — schema version bumped to "v082-1" for the 28-byte row layout
# that carries the science-team derived fields. See docs/V082_PLAN.md
# for the full layout + rationale.
_POINTS_BULK_SCHEMA_VERSION = "v082-1"
_POINTS_BULK_BYTES_PER_ROW = 28
# Little-endian row format, 28 bytes:
#   I  uint32  id                (offset 0)
#   f  float32 lat               (offset 4)
#   f  float32 lng               (offset 8)
#   I  uint32  date_days         (offset 12, days since 1900-01-01)
#   B  uint8   source_idx        (offset 16)
#   B  uint8   std_shape_idx     (offset 17)
#   B  uint8   quality_score     (offset 18, 255 = unknown)
#   B  uint8   hoax_score        (offset 19, 255 = unknown)
#   B  uint8   richness_score    (offset 20, 255 = unknown)
#   B  uint8   color_idx         (offset 21)
#   B  uint8   emotion_idx       (offset 22)
#   B  uint8   flags             (offset 23, bit0=has_desc, bit1=has_media)
#   B  uint8   num_witnesses     (offset 24, clamped 0-255)
#   B  uint8   _reserved         (offset 25, future growth)
#   H  uint16  duration_log2     (offset 26, log2(sec+1) rounded, 0=unknown)
_POINTS_BULK_STRUCT = "<IffIBBBBBBBBBBH"

# Sentinel value for missing / unknown score bytes. Scores are
# semantically in [0, 100], so 255 is safely out-of-band.
_POINTS_BULK_SCORE_UNKNOWN = 255

# The derived columns ufo-dedup/analyze.py populates on the sighting
# table. The v0.8.2 endpoint probes which of these actually exist in
# the live schema (via information_schema.columns) at startup and
# caches the result so the hot SELECT clause stays in sync with
# reality. Any missing column is substituted with `NULL AS col_name`
# so the row tuple shape stays fixed regardless of schema state.
_POINTS_BULK_DERIVED_COLS = (
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
)


def _epoch_days_1900(iso_str) -> int:
    """Days since 1900-01-01 for an ISO-like date string, 0 if unknown.

    Accepts full timestamps ("2005-06-14T15:30:00Z"), dates
    ("2005-06-14"), year-only ("2005"), or anything else falsy.
    Anything that doesn't parse to a reasonable (>1 AD, <2200 AD)
    date returns 0, which the client treats as "unknown date".
    """
    if not iso_str:
        return 0
    s = str(iso_str).strip()
    if not s:
        return 0
    try:
        # Fast path: "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:..."
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            y = int(s[0:4])
            m = int(s[5:7])
            d = int(s[8:10])
        elif len(s) == 7 and s[4] == "-":
            # "YYYY-MM"
            y = int(s[0:4])
            m = int(s[5:7])
            d = 1
        elif len(s) >= 4 and s[:4].isdigit():
            # Year only: "1974" or free-text starting with a year
            y = int(s[:4])
            m = 1
            d = 1
        else:
            return 0
        if y < 1 or y > 2199:
            return 0
        if m < 1 or m > 12:
            m = 1
        if d < 1 or d > 31:
            d = 1
        import datetime as _dt
        try:
            days = (_dt.date(y, m, d) - _dt.date(1900, 1, 1)).days
        except ValueError:
            # Invalid date (e.g. Feb 30) → fall back to Jan 1 of the year
            days = (_dt.date(y, 1, 1) - _dt.date(1900, 1, 1)).days
        # Clip negative values (pre-1900) to 0 — those sightings are
        # rare and the client treats 0 as unknown anyway. The uint32
        # slot lets the positive range go to 2^32 days ≈ 11M years,
        # so we only need to worry about the lower bound.
        return max(0, days)
    except (ValueError, TypeError):
        return 0


def _points_bulk_column_set(conn) -> frozenset:
    """Return the set of sighting columns that exist in the target
    schema, intersected with _POINTS_BULK_DERIVED_COLS.

    Runs one information_schema query (~1 ms). Called from
    _points_bulk_build() on every build — but builds are @lru_cache'd
    on the etag, so the cost is negligible.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'sighting'
          AND column_name = ANY(%s)
        """,
        (list(_POINTS_BULK_DERIVED_COLS),),
    )
    return frozenset(row[0] for row in cur.fetchall())


def _points_bulk_etag() -> str:
    """Cheap ETag for the bulk points endpoint.

    Derived from the schema version + geocoded row count + max sighting
    id + the set of derived columns present in the schema. Both SQL
    aggregates are O(1) under the existing indexes (idx_location_coords
    and the primary key), so this runs in a few milliseconds and is
    safe to call on every request.

    Including the column set in the ETag means when the v0.8.2
    migration finally lands (adding new columns), the ETag changes
    and every browser-side cache invalidates automatically.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(s.id), 0)
            FROM sighting s
            JOIN location l ON l.id = s.location_id
            WHERE l.latitude IS NOT NULL
              AND l.longitude IS NOT NULL
              AND l.latitude BETWEEN -90 AND 90
              AND l.longitude BETWEEN -180 AND 180
            """
        )
        cnt, max_id = cur.fetchone()
        cols = _points_bulk_column_set(conn)
    finally:
        conn.close()
    cols_tag = "-".join(sorted(cols)) if cols else "base"
    return f"{_POINTS_BULK_SCHEMA_VERSION}-{int(cnt)}-{int(max_id)}-{cols_tag}"


def _duration_log2(sec) -> int:
    """Encode a duration-in-seconds as log2(sec+1) rounded to a uint16.

    Returns 0 if `sec` is None / 0 / negative, else `round(log2(sec+1))`
    clamped to the uint16 range. The client decodes via
    `Math.pow(2, duration_log2) - 1`. Gives ~1% resolution across
    seconds → days without burning 4 bytes per row.
    """
    if not sec:
        return 0
    try:
        v = int(sec)
    except (ValueError, TypeError):
        return 0
    if v <= 0:
        return 0
    # math.log2 with +1 so we don't pass 0 to log
    lg = round(math.log2(v + 1))
    if lg < 0:
        return 0
    if lg > 65535:
        return 65535
    return int(lg)


@functools.lru_cache(maxsize=2)
def _points_bulk_build(etag: str) -> tuple[bytes, bytes, dict]:
    """Build the packed binary buffer + metadata for `etag`.

    Returns (gzipped_buffer, meta_json_bytes, meta_dict). Cached in
    process memory for the lifetime of the etag — since etags change
    only when new data lands or the schema changes, the cache holds
    at most two entries (current + one stale during a deploy).

    v0.8.2 changes vs v0.8.0:
      - Row size 16 → 28 bytes
      - Adds date_days, quality/hoax/richness scores, color/emotion
        indices, flags, num_witnesses, duration_log2
      - SELECT clause is built dynamically against the probed column
        set, so pre-v0.8.2 schemas (missing derived columns) still
        produce valid binary with sentinel NULL values
      - Meta sidecar includes `coverage` and `columns_present` so the
        client knows which filter UI to enable
    """
    import gzip
    import struct

    conn = get_db()
    try:
        cur = conn.cursor()

        # Column probe: which v0.8.2 derived columns actually exist?
        present_cols = _points_bulk_column_set(conn)

        # Stable source index, alphabetical by name. Index 0 is reserved
        # for "unknown" so every shipped source gets 1..N.
        cur.execute("SELECT id, name FROM source_database ORDER BY name")
        source_rows = cur.fetchall()
        source_names = [None] + [r[1] for r in source_rows]
        source_id_to_idx = {r[0]: i + 1 for i, r in enumerate(source_rows)}

        # v0.8.2 — prefer the canonical standardized_shape when the
        # column exists and is populated. Otherwise fall back to the
        # raw `shape` column (same as v0.8.0/0.8.1 behavior). Either
        # way the returned list goes into a single `shapes` lookup
        # so the frontend doesn't need to care which source it came
        # from — it just uses shapes[shape_idx].
        use_std_shape = "standardized_shape" in present_cols
        if use_std_shape:
            cur.execute(
                """
                SELECT DISTINCT standardized_shape, LOWER(standardized_shape) AS lshape
                FROM sighting
                WHERE standardized_shape IS NOT NULL AND standardized_shape <> ''
                ORDER BY lshape
                """
            )
            distinct_shapes = [r[0] for r in cur.fetchall()][:254]
        else:
            cur.execute(
                """
                SELECT DISTINCT shape, LOWER(shape) AS lshape
                FROM sighting
                WHERE shape IS NOT NULL AND shape <> ''
                ORDER BY lshape
                """
            )
            distinct_shapes = [r[0] for r in cur.fetchall()][:254]
        shape_names = [None] + distinct_shapes
        shape_to_idx = {s: i + 1 for i, s in enumerate(distinct_shapes)}

        # v0.8.2 — new lookup tables for primary_color and
        # dominant_emotion. Only populated if the columns exist.
        color_names = [None]
        color_to_idx: dict = {}
        if "primary_color" in present_cols:
            cur.execute(
                """
                SELECT DISTINCT primary_color, LOWER(primary_color) AS lc
                FROM sighting
                WHERE primary_color IS NOT NULL AND primary_color <> ''
                ORDER BY lc
                """
            )
            distinct_colors = [r[0] for r in cur.fetchall()][:254]
            color_names = [None] + distinct_colors
            color_to_idx = {c: i + 1 for i, c in enumerate(distinct_colors)}

        emotion_names = [None]
        emotion_to_idx: dict = {}
        if "dominant_emotion" in present_cols:
            cur.execute(
                """
                SELECT DISTINCT dominant_emotion, LOWER(dominant_emotion) AS le
                FROM sighting
                WHERE dominant_emotion IS NOT NULL AND dominant_emotion <> ''
                ORDER BY le
                """
            )
            distinct_emotions = [r[0] for r in cur.fetchall()][:254]
            emotion_names = [None] + distinct_emotions
            emotion_to_idx = {e: i + 1 for i, e in enumerate(distinct_emotions)}

        # Build the SELECT list. Columns that don't exist in the
        # schema yet get `NULL AS col_name` so the tuple position stays
        # stable regardless of migration state.
        def _col_expr(col: str, table_prefix: str = "s") -> str:
            return f"{table_prefix}.{col}" if col in present_cols else f"NULL AS {col}"

        # Note: we always pull s.shape as a fallback for standardized_shape
        # (used when use_std_shape is False) plus s.date_event as a fallback
        # for sighting_datetime. The fallbacks are free — existing indexes.
        select_parts = [
            "s.id",
            "l.latitude",
            "l.longitude",
            "s.source_db_id",
            "s.shape AS raw_shape",
            "s.date_event",
            "s.duration_seconds",
            "s.num_witnesses AS raw_num_witnesses",
            _col_expr("sighting_datetime"),
            _col_expr("standardized_shape"),
            _col_expr("primary_color"),
            _col_expr("dominant_emotion"),
            _col_expr("quality_score"),
            _col_expr("richness_score"),
            _col_expr("hoax_likelihood"),
            _col_expr("has_description"),
            _col_expr("has_media"),
        ]
        select_sql = ",\n                   ".join(select_parts)

        cur.execute(
            f"""
            SELECT {select_sql}
            FROM sighting s
            JOIN location l ON l.id = s.location_id
            WHERE l.latitude IS NOT NULL
              AND l.longitude IS NOT NULL
              AND l.latitude BETWEEN -90 AND 90
              AND l.longitude BETWEEN -180 AND 180
            ORDER BY s.id
            """
        )

        pack = struct.Struct(_POINTS_BULK_STRUCT).pack
        buf = bytearray()
        count = 0
        UNK = _POINTS_BULK_SCORE_UNKNOWN

        # Coverage counters — incremented as we walk so the meta
        # sidecar can tell the client which fields are populated.
        cov = {
            "date_days": 0,
            "std_shape_idx": 0,
            "quality_score": 0,
            "hoax_score": 0,
            "richness_score": 0,
            "color_idx": 0,
            "emotion_idx": 0,
            "has_description": 0,
            "has_media": 0,
            "num_witnesses": 0,
            "duration_log2": 0,
        }

        for row in cur:
            (
                sid, lat, lng, src_id, raw_shape, date_event,
                duration_sec, raw_num_witnesses,
                sighting_dt, std_shape, prim_color, dom_emotion,
                quality, richness, hoax, has_desc, has_media,
            ) = row

            # Prefer the explicit sighting_datetime when present, fall
            # back to date_event otherwise. _epoch_days_1900 handles
            # every sane ISO-like format.
            date_days = _epoch_days_1900(sighting_dt or date_event)
            if date_days:
                cov["date_days"] += 1

            # Shape: prefer the standardized column when we're using
            # the canonical list. Otherwise the raw shape.
            if use_std_shape:
                shape_val = std_shape or ""
            else:
                shape_val = raw_shape or ""
            shape_idx = shape_to_idx.get(shape_val, 0)
            if use_std_shape and std_shape and shape_idx:
                cov["std_shape_idx"] += 1

            # Score packing: uint8 in [0, 100], 255 = unknown.
            if quality is None:
                q_val = UNK
            else:
                q_val = max(0, min(100, int(quality)))
                cov["quality_score"] += 1

            if richness is None:
                r_val = UNK
            else:
                r_val = max(0, min(100, int(richness)))
                cov["richness_score"] += 1

            # hoax_likelihood is REAL [0.0, 1.0] → scale to [0, 100]
            if hoax is None:
                h_val = UNK
            else:
                h_val = max(0, min(100, int(round(float(hoax) * 100))))
                cov["hoax_score"] += 1

            c_idx = 0
            if prim_color:
                c_idx = color_to_idx.get(prim_color, 0)
                if c_idx:
                    cov["color_idx"] += 1

            e_idx = 0
            if dom_emotion:
                e_idx = emotion_to_idx.get(dom_emotion, 0)
                if e_idx:
                    cov["emotion_idx"] += 1

            # Flags byte: bit 0 = has_description, bit 1 = has_media
            flags = 0
            if has_desc:
                flags |= 0x01
                cov["has_description"] += 1
            if has_media:
                flags |= 0x02
                cov["has_media"] += 1

            # num_witnesses: clamp to uint8 range; unknown → 0
            if raw_num_witnesses is None:
                nw = 0
            else:
                try:
                    nw = max(0, min(255, int(raw_num_witnesses)))
                    if nw > 0:
                        cov["num_witnesses"] += 1
                except (ValueError, TypeError):
                    nw = 0

            # duration: log2-encoded uint16
            dur_log2 = _duration_log2(duration_sec)
            if dur_log2:
                cov["duration_log2"] += 1

            buf.extend(
                pack(
                    int(sid),
                    float(lat),
                    float(lng),
                    date_days,
                    source_id_to_idx.get(src_id, 0),
                    shape_idx,
                    q_val,
                    h_val,
                    r_val,
                    c_idx,
                    e_idx,
                    flags,
                    nw,
                    0,  # _reserved
                    dur_log2,
                )
            )
            count += 1
    finally:
        conn.close()

    raw = bytes(buf)
    gzipped = gzip.compress(raw, compresslevel=6)

    meta = {
        "count": count,
        "etag": etag,
        "schema_version": _POINTS_BULK_SCHEMA_VERSION,
        "sources": source_names,
        "shapes": shape_names,
        "colors": color_names,
        "emotions": emotion_names,
        "shape_source": "standardized" if use_std_shape else "raw",
        "raw_size": len(raw),
        "gzip_size": len(gzipped),
        "coverage": cov,
        "columns_present": {
            c: (c in present_cols) for c in _POINTS_BULK_DERIVED_COLS
        },
        "schema": {
            "bytes_per_row": _POINTS_BULK_BYTES_PER_ROW,
            "endian": "little",
            "score_unknown": _POINTS_BULK_SCORE_UNKNOWN,
            "date_epoch": "1900-01-01",
            "fields": [
                {"name": "id",              "offset": 0,  "type": "uint32",  "len": 4},
                {"name": "lat",             "offset": 4,  "type": "float32", "len": 4},
                {"name": "lng",             "offset": 8,  "type": "float32", "len": 4},
                {"name": "date_days",       "offset": 12, "type": "uint32",  "len": 4},
                {"name": "source_idx",      "offset": 16, "type": "uint8",   "len": 1},
                {"name": "shape_idx",       "offset": 17, "type": "uint8",   "len": 1},
                {"name": "quality_score",   "offset": 18, "type": "uint8",   "len": 1},
                {"name": "hoax_score",      "offset": 19, "type": "uint8",   "len": 1},
                {"name": "richness_score",  "offset": 20, "type": "uint8",   "len": 1},
                {"name": "color_idx",       "offset": 21, "type": "uint8",   "len": 1},
                {"name": "emotion_idx",     "offset": 22, "type": "uint8",   "len": 1},
                {"name": "flags",           "offset": 23, "type": "uint8",   "len": 1},
                {"name": "num_witnesses",   "offset": 24, "type": "uint8",   "len": 1},
                {"name": "_reserved",       "offset": 25, "type": "uint8",   "len": 1},
                {"name": "duration_log2",   "offset": 26, "type": "uint16",  "len": 2},
            ],
        },
    }
    meta_bytes = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return gzipped, meta_bytes, meta


@app.route("/api/points-bulk")
def api_points_bulk():
    """Bulk binary dataset for client-side rendering.

    Three response shapes depending on the request:
      - `If-None-Match` matches current ETag  → 304 Not Modified
      - `?meta=1`                              → application/json
                                                 (count, lookups, schema)
      - default                                → application/octet-stream
                                                 (pre-gzipped packed rows)

    The client fetches `?meta=1` first to get lookup tables + schema, then
    fetches the bare endpoint for the packed buffer. Both requests share
    the same ETag, so a repeat session that still has a fresh cached
    buffer sends two cheap 304s.
    """
    etag = _points_bulk_etag()
    quoted_etag = f'"{etag}"'

    # Client already has the right copy. 304 before we touch the buffer.
    if request.headers.get("If-None-Match") == quoted_etag:
        resp = Response(status=304)
        resp.headers["ETag"] = quoted_etag
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    gzipped, meta_bytes, meta = _points_bulk_build(etag)

    if request.args.get("meta") == "1":
        resp = Response(meta_bytes, mimetype="application/json")
        resp.headers["ETag"] = quoted_etag
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    resp = Response(gzipped, mimetype="application/octet-stream")
    resp.headers["ETag"] = quoted_etag
    resp.headers["Cache-Control"] = "public, max-age=3600"
    # We pre-gzipped the buffer ourselves; advertise it so the browser
    # decompresses on receipt. Flask-Compress leaves octet-stream alone
    # (see COMPRESS_MIMETYPES) so there's no risk of double-gzipping.
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    # Advertise the uncompressed size so the client can pre-allocate
    # the ArrayBuffer without waiting for the meta fetch.
    resp.headers["X-Uncompressed-Size"] = str(meta["raw_size"])
    return resp


@app.route("/api/timeline")
@cache.cached(timeout=600, query_string=True)
def api_timeline():
    """Sighting counts grouped by year (or by month if year param given).

    v0.7.5: when no year is requested and no common filters are set,
    reads from mv_timeline_yearly (~10 ms instead of ~1300 ms warm).
    Falls back to the live query for the filtered / monthly-drilldown
    paths, which retain the @cache.cached(query_string=True) TTL so
    repeated hits with the same filter remain fast after the first.
    """
    conn = get_db()
    cur = conn.cursor()

    year = request.args.get("year")

    # --- MV fast path: unfiltered yearly timeline -----------------------
    if not year and not _has_common_filters(request.args):
        try:
            cur.execute("""
                SELECT period, source_name, cnt
                FROM mv_timeline_yearly
                ORDER BY period
            """)
            data = {}
            for period, source, count in cur.fetchall():
                data.setdefault(period, {})[source] = count
            conn.close()
            return jsonify({"mode": "yearly", "year": None, "data": data})
        except psycopg.errors.UndefinedTable:
            # MV migration hasn't run; reset the cursor state so the live
            # query below can reuse the same connection cleanly.
            print("[api_timeline] mv_timeline_yearly missing, falling back to live query")
            cur = conn.cursor()

    clauses = ["s.date_event IS NOT NULL", "LENGTH(s.date_event) >= 4"]
    args = []

    add_common_filters(request.args, clauses, args)

    if year:
        # Monthly breakdown for a specific year
        clauses.append("SUBSTR(s.date_event, 1, 4) = %s")
        args.append(year)
        where = " AND ".join(clauses)

        sql = f"""
            SELECT SUBSTR(s.date_event, 1, 7) as period,
                   sd.name as source_name,
                   COUNT(*) as cnt
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN location l ON s.location_id = l.id
            WHERE {where}
              AND LENGTH(s.date_event) >= 7
            GROUP BY period, sd.name
            ORDER BY period
        """
    else:
        # Yearly breakdown (includes pre-1900 historic sightings)
        where = " AND ".join(clauses)

        sql = f"""
            SELECT SUBSTR(s.date_event, 1, 4) as period,
                   sd.name as source_name,
                   COUNT(*) as cnt
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN location l ON s.location_id = l.id
            WHERE {where}
            GROUP BY period, sd.name
            ORDER BY period
        """

    cur.execute(sql, args)
    rows = cur.fetchall()

    # Pivot: {period: {source: count, ...}, ...}
    data = {}
    for r in rows:
        period, source, count = r[0], r[1], r[2]
        if period not in data:
            data[period] = {}
        data[period][source] = count

    conn.close()
    return jsonify({
        "mode": "monthly" if year else "yearly",
        "year": year,
        "data": data,
    })


@app.route("/api/search")
@cache.cached(timeout=300, query_string=True)
def api_search():
    """Search sightings by text and filters.

    Uses ILIKE (case-insensitive substring match) backed by the
    pg_trgm GIN indexes from pg_schema.sql so substring queries don't
    require a full table scan.
    """
    conn = get_db()
    cur = conn.cursor()

    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 0))
    per_page = 50
    offset = page * per_page

    clauses = []
    args = []

    if q:
        clauses.append("(s.description ILIKE %s OR s.summary ILIKE %s)")
        like = f"%{q}%"
        args.extend([like, like])

    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses) if clauses else "TRUE"

    # Count total. The LEFT JOIN location is required because the WHERE
    # clause may reference l.country / l.state / l.geocode_src via
    # add_common_filters. Without the join those references would error.
    count_sql = f"""
        SELECT COUNT(*) FROM sighting s
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
    """
    cur.execute(count_sql, args)
    total = cur.fetchone()[0]

    # Fetch page
    sql = f"""
        SELECT s.id, s.date_event, s.shape, s.description, s.summary,
               sd.name as source_name,
               COALESCE(l.city, '') as city,
               COALESCE(l.state, '') as state,
               COALESCE(l.country, '') as country,
               s.hynek, s.num_witnesses, s.duration,
               COALESCE(sc.name, '') as collection
        FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN source_collection sc ON sd.collection_id = sc.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        ORDER BY s.date_event DESC
        LIMIT %s OFFSET %s
    """
    cur.execute(sql, args + [per_page, offset])

    results = []
    for r in cur.fetchall():
        desc = r[3] or r[4] or ""
        results.append({
            "id": r[0],
            "date": r[1],
            "shape": r[2],
            "description": desc[:300] if desc else "",
            "source": r[5],
            "city": r[6],
            "state": r[7],
            "country": r[8],
            "hynek": r[9],
            "witnesses": r[10],
            "duration": r[11],
            "collection": r[12],
        })

    conn.close()
    return jsonify({
        "results": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


# ---------------------------------------------------------------------------
# Export — CSV / JSON download of the current search filter set
# ---------------------------------------------------------------------------
# Reuses the same WHERE clause as /api/search so the file the user
# downloads matches what they saw on screen. Capped at 5,000 rows by
# default; the response always includes the total so the UI can show
# "downloaded 5,000 of 12,408 — for the full dataset use the MCP API".

EXPORT_MAX_ROWS = 5000

EXPORT_COLUMNS = [
    "id", "date_event", "shape", "hynek", "vallee", "num_witnesses",
    "duration", "summary", "description",
    "source", "collection",
    "city", "state", "country",
    "latitude", "longitude",
]

def _build_export_query(request_args):
    """Return (sql, args) for the export query — same filters as /api/search."""
    q = (request_args.get("q") or "").strip()
    clauses = []
    args = []
    if q:
        clauses.append("(s.description ILIKE %s OR s.summary ILIKE %s)")
        like = f"%{q}%"
        args.extend([like, like])
    add_common_filters(request_args, clauses, args)
    where = " AND ".join(clauses) if clauses else "TRUE"
    sql = f"""
        SELECT s.id, s.date_event, s.shape, s.hynek, s.vallee, s.num_witnesses,
               s.duration, s.summary, s.description,
               sd.name AS source,
               COALESCE(sc.name, '') AS collection,
               COALESCE(l.city, '') AS city,
               COALESCE(l.state, '') AS state,
               COALESCE(l.country, '') AS country,
               l.latitude, l.longitude
        FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN source_collection sc ON sd.collection_id = sc.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        ORDER BY s.date_event DESC NULLS LAST
        LIMIT %s
    """
    return sql, args + [EXPORT_MAX_ROWS]


@app.route("/api/export.csv")
def api_export_csv():
    """CSV download of the current search filter set (capped at 5,000 rows)."""
    import csv
    import io
    sql, args = _build_export_query(request.args)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([
            "" if v is None else str(v).replace("\n", " ").replace("\r", " ")
            for v in r
        ])
    csv_data = buf.getvalue()
    filename = "ufosint-export.csv"
    return Response(
        csv_data,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Rows": str(len(rows)),
            "X-Max-Rows": str(EXPORT_MAX_ROWS),
        },
    )


@app.route("/api/export.json")
def api_export_json():
    """JSON download of the current search filter set (capped at 5,000 rows)."""
    sql, args = _build_export_query(request.args)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = [dict(zip(EXPORT_COLUMNS, r, strict=False)) for r in cur.fetchall()]
    finally:
        conn.close()
    payload = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "row_count": len(rows),
        "max_rows": EXPORT_MAX_ROWS,
        "rows": rows,
    }
    body = json.dumps(payload, default=str, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="ufosint-export.json"',
            "X-Total-Rows": str(len(rows)),
            "X-Max-Rows": str(EXPORT_MAX_ROWS),
        },
    )


@app.route("/api/sighting/<int:sid>")
@cache.cached(timeout=600, query_string=False)
def api_sighting(sid):
    """Full detail for a single sighting.

    Cached for 10 minutes per-worker because individual record detail is
    immutable for the lifetime of a DB revision (the ETL pipeline only
    rebuilds the entire snapshot). Cold hits previously timed out with
    HTTP 504 on B1ms because the duplicate-candidate subquery used an OR
    across two columns with no supporting index — see idx_duplicate_a /
    idx_duplicate_b added in scripts/add_v07_indexes.sql, and the
    UNION ALL rewrite below.
    """
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT s.*, sd.name as source_name,
                   l.raw_text as loc_raw, l.city, l.county, l.state, l.country,
                   l.region, l.latitude, l.longitude
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN location l ON s.location_id = l.id
            WHERE s.id = %s
        """, (sid,))

        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404

        # Convert to dict, skipping None values for cleaner JSON
        keys = [desc[0] for desc in cur.description]
        record = {}
        for k, v in zip(keys, row, strict=False):
            if v is not None and v != "":
                record[k] = v

        # Parse raw_json for display
        if "raw_json" in record:
            try:
                record["raw_json"] = json.loads(record["raw_json"])
            except (json.JSONDecodeError, TypeError):
                record["raw_json"] = "(parse error)"

        # Check for duplicate candidates.
        #
        # v0.7 fix: the old query used WHERE sighting_id_a = %s OR
        # sighting_id_b = %s which can't use an index on either column.
        # Rewritten as a UNION ALL of two equality scans — the planner
        # now uses idx_duplicate_a for the first branch and idx_duplicate_b
        # for the second, both O(log n). This is the core 504 fix.
        cur.execute("""
            (
                SELECT dc.sighting_id_a, dc.sighting_id_b,
                       dc.similarity_score, dc.match_method, dc.status,
                       s2.date_event, sd2.name as other_source,
                       COALESCE(l2.city, '') as other_city,
                       COALESCE(l2.state, '') as other_state
                  FROM duplicate_candidate dc
                  JOIN sighting s2 ON s2.id = dc.sighting_id_b
                  JOIN source_database sd2 ON s2.source_db_id = sd2.id
                  LEFT JOIN location l2 ON s2.location_id = l2.id
                 WHERE dc.sighting_id_a = %s
            )
            UNION ALL
            (
                SELECT dc.sighting_id_a, dc.sighting_id_b,
                       dc.similarity_score, dc.match_method, dc.status,
                       s2.date_event, sd2.name as other_source,
                       COALESCE(l2.city, '') as other_city,
                       COALESCE(l2.state, '') as other_state
                  FROM duplicate_candidate dc
                  JOIN sighting s2 ON s2.id = dc.sighting_id_a
                  JOIN source_database sd2 ON s2.source_db_id = sd2.id
                  LEFT JOIN location l2 ON s2.location_id = l2.id
                 WHERE dc.sighting_id_b = %s
            )
            ORDER BY similarity_score DESC NULLS LAST
            LIMIT 10
        """, (sid, sid))

        duplicates = []
        for r in cur.fetchall():
            other_id = r[1] if r[0] == sid else r[0]
            duplicates.append({
                "id": other_id,
                "score": round(r[2], 3) if r[2] else None,
                "method": r[3],
                "status": r[4],
                "date": r[5],
                "source": r[6],
                "city": r[7],
                "state": r[8],
            })

        record["duplicates"] = duplicates

        # Get origin name if present
        if record.get("origin_id"):
            cur.execute("SELECT name FROM source_origin WHERE id = %s", (record["origin_id"],))
            origin = cur.fetchone()
            if origin:
                record["origin_name"] = origin[0]

        # Get collection name
        if record.get("source_db_id"):
            cur.execute("""
                SELECT sc.name FROM source_collection sc
                JOIN source_database sd ON sd.collection_id = sc.id
                WHERE sd.id = %s
            """, (record["source_db_id"],))
            coll = cur.fetchone()
            if coll:
                record["collection_name"] = coll[0]

        # Include sentiment data if available
        cur.execute("""
            SELECT vader_compound, vader_positive, vader_negative, vader_neutral,
                   emo_joy, emo_fear, emo_anger, emo_sadness,
                   emo_surprise, emo_disgust, emo_trust, emo_anticipation,
                   text_source, text_length
            FROM sentiment_analysis WHERE sighting_id = %s
        """, (sid,))
        sent_row = cur.fetchone()
        if sent_row:
            sent_keys = [desc[0] for desc in cur.description]
            record["sentiment"] = dict(zip(sent_keys, sent_row, strict=False))

        return jsonify(record)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sentiment / Emotion Endpoints
# ---------------------------------------------------------------------------

_SENTIMENT_OVERVIEW_COLS = (
    "total_analyzed", "avg_compound", "avg_positive", "avg_negative", "avg_neutral",
    "joy", "fear", "anger", "sadness", "surprise", "disgust", "trust", "anticipation",
)


@app.route("/api/sentiment/overview")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_overview():
    """Aggregate emotion distribution across filtered sightings.

    v0.7.5: when no common filters are set, reads the single-row
    mv_sentiment_overview materialized view (~2 ms). This was the
    23-second query from the v0.7.4 cold-start logs. Filtered requests
    still hit the live query path through Flask-Caching.
    """
    conn = get_db()
    cur = conn.cursor()

    # --- MV fast path: unfiltered overview ------------------------------
    if not _has_common_filters(request.args):
        try:
            cur.execute(f"""
                SELECT {", ".join(_SENTIMENT_OVERVIEW_COLS)}
                FROM mv_sentiment_overview
            """)
            row = cur.fetchone()
            conn.close()
            return jsonify(dict(zip(_SENTIMENT_OVERVIEW_COLS, row, strict=False)))
        except psycopg.errors.UndefinedTable:
            print("[api_sentiment_overview] mv_sentiment_overview missing, "
                  "falling back to live query")
            cur = conn.cursor()

    clauses = []
    args = []
    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses) if clauses else "TRUE"

    cur.execute(f"""
        SELECT
            COUNT(*) as total_analyzed,
            AVG(sa.vader_compound) as avg_compound,
            AVG(sa.vader_positive) as avg_positive,
            AVG(sa.vader_negative) as avg_negative,
            AVG(sa.vader_neutral)  as avg_neutral,
            SUM(sa.emo_joy) as joy,
            SUM(sa.emo_fear) as fear,
            SUM(sa.emo_anger) as anger,
            SUM(sa.emo_sadness) as sadness,
            SUM(sa.emo_surprise) as surprise,
            SUM(sa.emo_disgust) as disgust,
            SUM(sa.emo_trust) as trust,
            SUM(sa.emo_anticipation) as anticipation
        FROM sighting s
        JOIN sentiment_analysis sa ON s.id = sa.sighting_id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
    """, args)
    row = cur.fetchone()
    keys = [desc[0] for desc in cur.description]
    result = dict(zip(keys, row, strict=False))

    conn.close()
    return jsonify(result)


@app.route("/api/sentiment/timeline")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_timeline():
    """Average VADER compound score by year."""
    conn = get_db()
    cur = conn.cursor()

    clauses = ["s.date_event IS NOT NULL", "LENGTH(s.date_event) >= 4"]
    args = []
    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses)

    cur.execute(f"""
        SELECT SUBSTR(s.date_event, 1, 4) as year,
               COUNT(*) as count,
               AVG(sa.vader_compound) as avg_compound,
               AVG(sa.vader_positive) as avg_positive,
               AVG(sa.vader_negative) as avg_negative,
               SUM(sa.emo_joy) as joy,
               SUM(sa.emo_fear) as fear,
               SUM(sa.emo_anger) as anger,
               SUM(sa.emo_sadness) as sadness,
               SUM(sa.emo_surprise) as surprise,
               SUM(sa.emo_disgust) as disgust,
               SUM(sa.emo_trust) as trust,
               SUM(sa.emo_anticipation) as anticipation
        FROM sighting s
        JOIN sentiment_analysis sa ON s.id = sa.sighting_id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        GROUP BY year
        ORDER BY year
    """, args)
    rows = cur.fetchall()
    keys = [desc[0] for desc in cur.description]
    data = [dict(zip(keys, row, strict=False)) for row in rows]

    conn.close()
    return jsonify({"data": data})


@app.route("/api/sentiment/by-source")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_by_source():
    """Emotion breakdown per source database."""
    conn = get_db()
    cur = conn.cursor()

    clauses = []
    args = []
    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses) if clauses else "TRUE"

    cur.execute(f"""
        SELECT sd.name as source_name,
               COUNT(*) as count,
               AVG(sa.vader_compound) as avg_compound,
               SUM(sa.emo_joy) as joy,
               SUM(sa.emo_fear) as fear,
               SUM(sa.emo_anger) as anger,
               SUM(sa.emo_sadness) as sadness,
               SUM(sa.emo_surprise) as surprise,
               SUM(sa.emo_disgust) as disgust,
               SUM(sa.emo_trust) as trust,
               SUM(sa.emo_anticipation) as anticipation
        FROM sighting s
        JOIN sentiment_analysis sa ON s.id = sa.sighting_id
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        GROUP BY sd.name
        ORDER BY count DESC
    """, args)
    rows = cur.fetchall()
    keys = [desc[0] for desc in cur.description]
    data = [dict(zip(keys, row, strict=False)) for row in rows]

    conn.close()
    return jsonify({"data": data})


@app.route("/api/sentiment/by-shape")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_by_shape():
    """Emotion breakdown per top 10 shapes."""
    conn = get_db()
    cur = conn.cursor()

    clauses = ["s.shape IS NOT NULL", "s.shape != ''"]
    args = []
    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses)

    cur.execute(f"""
        SELECT s.shape,
               COUNT(*) as count,
               AVG(sa.vader_compound) as avg_compound,
               SUM(sa.emo_joy) as joy,
               SUM(sa.emo_fear) as fear,
               SUM(sa.emo_anger) as anger,
               SUM(sa.emo_sadness) as sadness,
               SUM(sa.emo_surprise) as surprise,
               SUM(sa.emo_disgust) as disgust,
               SUM(sa.emo_trust) as trust,
               SUM(sa.emo_anticipation) as anticipation
        FROM sighting s
        JOIN sentiment_analysis sa ON s.id = sa.sighting_id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        GROUP BY s.shape
        ORDER BY count DESC
        LIMIT 10
    """, args)
    rows = cur.fetchall()
    keys = [desc[0] for desc in cur.description]
    data = [dict(zip(keys, row, strict=False)) for row in rows]

    conn.close()
    return jsonify({"data": data})


@app.route("/api/duplicates")
@cache.cached(timeout=600, query_string=True)
def api_duplicates():
    """Browse duplicate candidate pairs with filtering."""
    conn = get_db()
    cur = conn.cursor()

    page = int(request.args.get("page", 0))
    per_page = 50
    offset = page * per_page

    clauses = []
    args = []

    # Score threshold filter
    min_score = request.args.get("min_score")
    if min_score:
        clauses.append("dc.similarity_score >= %s")
        args.append(float(min_score))

    max_score = request.args.get("max_score")
    if max_score:
        clauses.append("dc.similarity_score < %s")
        args.append(float(max_score))

    # Match method filter
    method = request.args.get("method")
    if method:
        clauses.append("dc.match_method = %s")
        args.append(method)

    # Source filter — either sighting must be from this source
    source = request.args.get("source")
    if source:
        clauses.append("(sa.source_db_id = %s OR sb.source_db_id = %s)")
        args.extend([int(source), int(source)])

    where = " AND ".join(clauses) if clauses else "TRUE"

    # Count total
    count_sql = f"""
        SELECT COUNT(*) FROM duplicate_candidate dc
        JOIN sighting sa ON dc.sighting_id_a = sa.id
        JOIN sighting sb ON dc.sighting_id_b = sb.id
        WHERE {where}
    """
    cur.execute(count_sql, args)
    total = cur.fetchone()[0]

    # Fetch page
    sql = f"""
        SELECT dc.id, dc.similarity_score, dc.match_method,
               dc.sighting_id_a, sda.name, sa.date_event,
               COALESCE(la.city, '') as city_a, COALESCE(la.state, '') as state_a,
               SUBSTR(COALESCE(sa.description, sa.summary, ''), 1, 120) as desc_a,
               sa.shape as shape_a,
               dc.sighting_id_b, sdb.name, sb.date_event,
               COALESCE(lb.city, '') as city_b, COALESCE(lb.state, '') as state_b,
               SUBSTR(COALESCE(sb.description, sb.summary, ''), 1, 120) as desc_b,
               sb.shape as shape_b
        FROM duplicate_candidate dc
        JOIN sighting sa ON dc.sighting_id_a = sa.id
        JOIN sighting sb ON dc.sighting_id_b = sb.id
        JOIN source_database sda ON sa.source_db_id = sda.id
        JOIN source_database sdb ON sb.source_db_id = sdb.id
        LEFT JOIN location la ON sa.location_id = la.id
        LEFT JOIN location lb ON sb.location_id = lb.id
        WHERE {where}
        ORDER BY dc.similarity_score DESC
        LIMIT %s OFFSET %s
    """
    cur.execute(sql, args + [per_page, offset])

    results = []
    for r in cur.fetchall():
        results.append({
            "pair_id": r[0],
            "score": round(r[1], 3) if r[1] is not None else None,
            "method": r[2],
            "a": {
                "id": r[3], "source": r[4], "date": r[5],
                "city": r[6], "state": r[7], "desc": r[8], "shape": r[9],
            },
            "b": {
                "id": r[10], "source": r[11], "date": r[12],
                "city": r[13], "state": r[14], "desc": r[15], "shape": r[16],
            },
        })

    conn.close()
    return jsonify({
        "results": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _init_app():
    """Initialize filter cache (called once on startup)."""
    print(f"DATABASE_URL host: {DATABASE_URL.split('@')[-1].split('/')[0]}")
    try:
        print("Loading filter values...")
        init_filters()
        print(f"  {len(FILTER_CACHE.get('shapes', []))} shapes, "
              f"{len(FILTER_CACHE.get('hynek', []))} hynek codes, "
              f"{len(FILTER_CACHE.get('sources', []))} sources")
    except Exception as e:
        print(f"WARNING: Could not load filters at startup: {e}")
        print("/health and routes will retry on first request.")


# Tables + indexes we want resident in PG's shared_buffers at startup.
# pg_prewarm loads pages into the buffer cache so the first request
# against a cold server doesn't eat disk I/O. Cheap (~1-3 seconds for
# the whole list on B1ms) and it runs in the background thread below,
# so we can afford to be generous.
_PREWARM_RELATIONS = [
    "sighting",
    "location",
    "idx_location_coords",
    "idx_sighting_date",
    "idx_sighting_location",
    "idx_sighting_source_date",
    "idx_sighting_shape",
    "idx_location_country",
    "idx_sighting_description_trgm",
    "idx_sighting_summary_trgm",
]


def _pg_prewarm_relations():
    """Call pg_prewarm() on each hot relation so shared_buffers is warm.

    Safe to call any time — pg_prewarm just reads pages into the cache and
    is a no-op if they're already resident. Silently skips the whole step
    if the extension isn't installed, so local dev / CI without superuser
    rights doesn't need any special setup.
    """
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            # Check extension presence first so we don't spam logs with
            # 'function pg_prewarm does not exist' on environments that
            # haven't enabled it.
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'pg_prewarm'"
            )
            if not cur.fetchone():
                print("[prewarm] pg_prewarm extension not installed, "
                      "skipping buffer prewarm (see scripts/pg_tuning.sql)")
                return
            for rel in _PREWARM_RELATIONS:
                try:
                    cur.execute("SELECT pg_prewarm(%s)", (rel,))
                    blocks = cur.fetchone()[0]
                    print(f"[prewarm] pg_prewarm {rel}: {blocks} blocks")
                except Exception as e:
                    # Missing relation (e.g. trgm indexes not built yet)
                    # is not fatal — keep going.
                    print(f"[prewarm] pg_prewarm {rel}: skipped ({e})")
                    conn.rollback() if hasattr(conn, "rollback") else None
        finally:
            conn.close()
    except Exception as e:
        print(f"[prewarm] pg_prewarm step failed: {e}")


def _prewarm_caches():
    """Pre-warm PG's buffer cache and the flask response cache.

    Runs in a background thread after gunicorn boots so the FIRST visitor
    to the site doesn't pay the cold-cache cost. Two stages:
      1) pg_prewarm() on hot tables + indexes → shared_buffers is warm.
      2) Hit the most common landing-page queries via the in-process
         Flask test client → the response cache + planner stats warm.

    ~30 seconds of background work that turns the cold-user experience
    from "20-50 seconds" into "200ms".
    """
    import threading

    def _warm():
        try:
            time.sleep(1)  # let the worker finish starting
            _pg_prewarm_relations()
            client = app.test_client()
            warm_paths = [
                "/api/stats",
                "/api/timeline",
                "/api/sentiment/overview",
                "/api/duplicates?page=0",
                # Whole-world map view (the landing-page default)
                "/api/map?south=-90&north=90&west=-180&east=180&zoom=2",
                # A continental and a city viewport so both sample modes warm
                "/api/map?south=25&north=50&west=-125&east=-65&zoom=4",
                "/api/map?south=33.5&north=34.5&west=-118.5&east=-117.5&zoom=10",
                "/api/heatmap?south=-90&north=90&west=-180&east=180",
            ]
            print(f"[prewarm] starting ({len(warm_paths)} queries)")
            for p in warm_paths:
                t0 = time.perf_counter()
                try:
                    r = client.get(p)
                    print(f"[prewarm] {r.status_code}  {(time.perf_counter()-t0)*1000:6.0f}ms  {p[:60]}")
                except Exception as e:
                    print(f"[prewarm] FAIL {p}: {e}")
            print("[prewarm] done")
        except Exception as e:
            print(f"[prewarm] thread crashed: {e}")

    t = threading.Thread(target=_warm, daemon=True, name="prewarm")
    t.start()


_init_app()
_prewarm_caches()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\nStarting server at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
