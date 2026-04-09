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
import time
import hashlib
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
from mcp_http import mcp_bp
app.register_blueprint(mcp_bp)

# In-process LRU cache for expensive query responses. Per-worker
# (not shared across gunicorn workers), keyed on the full query
# string. 5-minute default TTL.
cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_THRESHOLD": 500,  # max number of cached items per worker
})

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


@app.route("/api/stats")
@cache.cached(timeout=600, query_string=True)
def api_stats():
    """Dashboard statistics."""
    conn = get_db()
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

    conn.close()

    return jsonify({
        "total_sightings": total,
        "by_source": by_source,
        "by_collection": by_collection,
        "date_range": {"min": date_min, "max": date_max},
        "geocoded_locations": geocoded,
        "geocoded_original": geocoded_original,
        "geocoded_geonames": geocoded_geonames,
        "duplicate_candidates": dupes,
    })


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
                   city, state, country, collection
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
                   COALESCE(sc.name, '') AS collection
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


@app.route("/api/timeline")
@cache.cached(timeout=600, query_string=True)
def api_timeline():
    """Sighting counts grouped by year (or by month if year param given)."""
    conn = get_db()
    cur = conn.cursor()

    year = request.args.get("year")
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
        rows = [dict(zip(EXPORT_COLUMNS, r)) for r in cur.fetchall()]
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
def api_sighting(sid):
    """Full detail for a single sighting."""
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
        for k, v in zip(keys, row):
            if v is not None and v != "":
                record[k] = v

        # Parse raw_json for display
        if "raw_json" in record:
            try:
                record["raw_json"] = json.loads(record["raw_json"])
            except (json.JSONDecodeError, TypeError):
                record["raw_json"] = "(parse error)"

        # Check for duplicate candidates
        cur.execute("""
            SELECT dc.sighting_id_a, dc.sighting_id_b,
                   dc.similarity_score, dc.match_method, dc.status,
                   s2.date_event, sd2.name as other_source,
                   COALESCE(l2.city, '') as other_city,
                   COALESCE(l2.state, '') as other_state
            FROM duplicate_candidate dc
            JOIN sighting s2 ON s2.id = CASE
                WHEN dc.sighting_id_a = %s THEN dc.sighting_id_b
                ELSE dc.sighting_id_a END
            JOIN source_database sd2 ON s2.source_db_id = sd2.id
            LEFT JOIN location l2 ON s2.location_id = l2.id
            WHERE dc.sighting_id_a = %s OR dc.sighting_id_b = %s
            ORDER BY dc.similarity_score DESC
            LIMIT 10
        """, (sid, sid, sid))

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
            record["sentiment"] = dict(zip(sent_keys, sent_row))

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

@app.route("/api/sentiment/overview")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_overview():
    """Aggregate emotion distribution across filtered sightings."""
    conn = get_db()
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
    result = dict(zip(keys, row))

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
    data = [dict(zip(keys, row)) for row in rows]

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
    data = [dict(zip(keys, row)) for row in rows]

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
    data = [dict(zip(keys, row)) for row in rows]

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


def _prewarm_caches():
    """Pre-warm PG's buffer cache and the flask response cache.

    Runs in a background thread after gunicorn boots so the FIRST visitor
    to the site doesn't pay the cold-cache cost. Hits the most common
    landing-page queries via the in-process Flask test client (which
    reuses the same cache and the same connection pool).

    The map endpoint covers a couple of representative viewports so the
    grid-sample CTE plan is also primed. ~30 seconds of background work
    that turns the cold-user experience from "20-50 seconds" into "200ms".
    """
    import threading

    def _warm():
        try:
            time.sleep(1)  # let the worker finish starting
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
