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
import threading
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

# ---------------------------------------------------------------------------
# Rate limiting (v0.13 — audit finding MED-7)
# ---------------------------------------------------------------------------
# On 2026-04-18 at 22:00 UTC a single PowerShell client hit the MCP tool
# endpoint 7,208 times in one hour — no malice, just aggressive scripting.
# The defense stack absorbed it fine, but the incident made the case:
# we need rate limits so one client can't monopolize the PG pool or burn
# our B1ms compute budget. The /llms.txt and MCP tool descriptions now
# prominently point bulk-access clients at the SQLite download; this
# limiter is the enforcement layer when they ignore the hint.
#
# Defaults are conservative: we want interactive tools fast, bulk via
# the SQLite. Unlimited on /health, /api/stats, /api/filters (tiny and
# hot-cached). Moderate on /api/map (heavy payload) and /api/tool/*
# (the MCP function-call surface).
#
# Client-key strategy: use the X-Forwarded-For header if present (Azure
# App Service's load balancer passes through the real client IP there),
# else fall back to remote_addr. Without X-Forwarded-For every request
# would look like it came from Azure's internal LB at 169.254.130.1
# and they'd all share one bucket.
from flask_limiter import Limiter  # noqa: E402
from flask_limiter.util import get_remote_address  # noqa: E402


def _rate_limit_key():
    """Prefer the real client IP from X-Forwarded-For over Azure's LB IP.

    Azure App Service sets X-Forwarded-For to the chain of upstream
    proxies with the *leftmost* entry being the original client —
    AND ON AZURE the entry includes the ephemeral client port:
    ``X-Forwarded-For: 1.2.3.4:51234, 10.0.0.1``. A naive strip would
    use ``1.2.3.4:51234`` as the key, which changes every connection
    because the OS picks a new port each time — so the limiter would
    see every request as a new client and never rate-limit anyone.
    We strip the port so the key is just the IP.

    Falls back to remote_addr when the header isn't present (local
    dev, tests).
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        client = xff.split(",")[0].strip()
        # IPv6 literal with port: [::1]:51234 → [::1]
        if client.startswith("["):
            return client.split("]")[0] + "]"
        # IPv4 with port: 1.2.3.4:51234 → 1.2.3.4
        # (Bare IPv6 without brackets can't reliably coexist with a
        # port; Azure only uses bracketed form for IPv6.)
        if client.count(":") == 1:
            return client.split(":")[0]
        # Bare IPv4 or bracketless IPv6 (no port), use as-is.
        return client
    return get_remote_address()


# Storage backend: per-worker memory. Each gunicorn worker has its own
# counter, so a client that hits worker A 60 times may get another 60
# hits on worker B. Imperfect but acceptable — with 2 workers the real
# ceiling is 120/min per IP per minute, which is still tight enough to
# prevent the 2026-04-18 scripting spike (7,208 calls in one hour =
# ~120/min) from repeating. If abuse patterns emerge, wire this to the
# REDIS_URL backend (Flask-Limiter supports it; just change storage_uri).
limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=[],  # no global default — opt in per-route below
    storage_uri="memory://",
    headers_enabled=True,  # X-RateLimit-* headers in responses
)

# MCP-over-HTTP server, mounted at /mcp. Lets any MCP-aware AI client
# (Claude Desktop, Cursor, Cline, Continue, Windsurf, etc.) call the
# tool catalog directly using their own LLM, with no inference cost
# to us. See mcp_http.py for the JSON-RPC implementation.
# Deliberate late import: mcp_http imports from app, so we register
# the blueprint after the Flask instance exists. (ruff E402)
from mcp_http import mcp_bp  # noqa: E402
app.register_blueprint(mcp_bp)

# Apply the same 60/min rate limit to the MCP endpoint. Matches the
# /api/tool/<name> limit since both expose the same underlying tools.
# The limit is registered on the blueprint name; Flask-Limiter walks
# every view function in the blueprint and attaches the limit.
limiter.limit("60 per minute", key_func=_rate_limit_key)(mcp_bp)

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
#
# Resilience — prevents the 2026-04-16 incident from recurring.
# See docs/OPERATIONS.md for the full post-mortem.
#   - `check`: ping each connection before handing it out. Dead conns
#     get discarded and a replacement is opened. Kills the "wedged
#     pool" failure mode where a stale TCP connection sits in the pool
#     forever, timing out every request that tries to use it.
#   - `max_idle`: close connections idle >5 min. Azure's network path
#     silently drops long-idle PG connections; without this, a quiet
#     hour overnight leaves the pool full of half-dead sockets.
#   - `max_lifetime`: recycle every hour as defense in depth against
#     slow memory or handle leaks on either end of the connection.
#
# v0.12.2 hotfix — Layer 1 wasn't enough. When Azure NAT *silently
# drops* packets, the check=SELECT 1 probe hangs on the dead socket
# until the OS-level timeout fires, so getconn() still blocks for the
# full pool `timeout=` window. Belt-and-braces additions:
#   - `connect_timeout=5`: cap opening a fresh connection at 5s so a
#     pool refill after a mass eviction can't block forever.
#   - `statement_timeout=25000`: server-side kill switch for any stuck
#     query (25s — matches pool checkout + small margin).
#   - `timeout=8`: if all 8 slots are busy, return the 503 to the LB
#     in 8s instead of holding the request for 30s. Pairs with the
#     Azure Health Check probe on /health, which will rotate/restart
#     the instance after ~10 min of sustained 503s.
#
# v0.12.3 hotfix — Third wedge incident. Root cause: `alwaysOn` was
# false, so Azure put the container to sleep after ~20 min of no
# traffic. On wake-up, old TCP sockets are dead at the kernel level
# but the pool doesn't know — `check=SELECT 1` hangs on the dead FD
# because the default Linux TCP keepalive is 2 hours (way past Azure's
# NAT timeout). Fixes:
#   - `keepalives=1` + `keepalives_idle=60` + `keepalives_interval=10`
#     + `keepalives_count=5`: make the OS probe idle sockets every 60s.
#     A dead socket is detected in ~110s (60 + 10*5) instead of ~2h.
#     This means `check` runs on a socket the OS already knows is dead
#     → instant failure → pool replaces it → no hang.
#   - `alwaysOn=true` enabled on the App Service (ops change, not code)
#     so the container stays warm and connections don't go stale in the
#     first place. TCP keepalive is the belt; Always On is the braces.
#
# v0.12.4 hotfix — audit (FAILURE_MODES.md MED-9) found pool had no
# headroom above gunicorn's 2*4=8 concurrent slots. During the 20–40 s
# prewarm window the background thread also holds 1–2 connections, so
# a real request that lands mid-prewarm could starve. Bump to 12 slots
# to absorb prewarm + burst traffic without hitting PoolTimeout. PG
# B1ms max_connections is ~50, so there's plenty of headroom.
_pool = ConnectionPool(
    DATABASE_URL,
    min_size=2,         # v0.12.4: was 1 — keep 2 warm for fast first-request
    max_size=12,        # v0.12.4: was 8 — headroom above gunicorn 2x4 slots
    open=True,
    timeout=8,          # v0.12.2: was 30; fail fast to the LB
    max_idle=300,       # 5 min
    max_lifetime=3600,  # 1 hour
    check=ConnectionPool.check_connection,
    kwargs={
        # Read-only workload: autocommit avoids the BEGIN/COMMIT overhead
        # on every query, and default_transaction_read_only is a safety
        # net in case anything ever tries to write.
        "autocommit": True,
        "connect_timeout": 5,   # v0.12.2: cap fresh-connection dials
        # v0.12.3: TCP keepalive — detect dead sockets at the kernel
        # level in ~110s instead of the default ~2h. Azure's NAT drops
        # idle connections after a few minutes; without keepalive the
        # pool's `check` callback hangs on a dead FD for the full OS
        # TCP timeout, which is what caused the 3x wedge incidents.
        "keepalives": 1,
        "keepalives_idle": 60,      # start probing after 60s idle
        "keepalives_interval": 10,  # probe every 10s after that
        "keepalives_count": 5,      # give up after 5 failed probes
        "options": (
            "-c default_transaction_read_only=on "
            "-c statement_timeout=25000"  # v0.12.2: 25s server-side kill
        ),
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
    """Load distinct filter values once at startup.

    v0.8.7: pruned to the 4 filter types the Observatory client
    actually uses. The frontend now populates shape/color/emotion
    dropdowns from POINTS metadata (so they use the canonical
    standardized lists the bulk buffer was built with), so
    technically only `sources` is consumed client-side. We keep
    shapes/colors/emotions here as a defensive compatibility shim
    for dev tools hitting /api/filters directly — but they use
    the standardized_shape column, not the raw `shape` column,
    so they match what the Observatory actually filters on.

    Dropped in v0.8.7: hynek, vallee, collections, countries,
    states, match_methods. Each had no byte slot in the 32-byte
    bulk row and nothing in the v0.8.7 UI drives them.
    """
    # v0.12.4: try/finally — startup connection leak would block all
    # subsequent requests. See docs/FAILURE_MODES.md CRIT-1.
    conn = get_db()
    try:
        cur = conn.cursor()

        # Standardized shape (NOT raw shape — the raw column has
        # mixed-case duplicates like "Disk" vs "Disc" that the
        # v0.8.3b classifier collapses into the standardized list).
        cur.execute("""
            SELECT DISTINCT standardized_shape FROM sighting
            WHERE standardized_shape IS NOT NULL AND standardized_shape != ''
            ORDER BY standardized_shape
        """)
        FILTER_CACHE["shapes"] = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT id, name FROM source_database ORDER BY name")
        FILTER_CACHE["sources"] = [
            {"id": r[0], "name": r[1]} for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT DISTINCT primary_color FROM sighting
            WHERE primary_color IS NOT NULL AND primary_color != ''
            ORDER BY primary_color
        """)
        FILTER_CACHE["colors"] = [r[0] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT dominant_emotion FROM sighting
            WHERE dominant_emotion IS NOT NULL AND dominant_emotion != ''
            ORDER BY dominant_emotion
        """)
        FILTER_CACHE["emotions"] = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value, name, default=None):
    """Parse a query-param string to float, or raise 400 on bad input.

    v0.12.4 — added after audit found /api/map and /api/heatmap called
    `float(request.args.get(...))` with no validation, so a malformed
    URL like ?south=foo caused ValueError → 500 → connection leak
    (combined with the unsafe get_db pattern). See
    docs/FAILURE_MODES.md CRIT-3.

    Returns `default` if `value` is None (missing param). Raises
    werkzeug BadRequest → HTTP 400 with a JSON error body if the
    value is non-numeric.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError) as e:
        from werkzeug.exceptions import BadRequest
        raise BadRequest(f"{name} must be a number, got {value!r}") from e


def add_common_filters(params, clauses, args, table_prefix="s"):
    """Add common filter clauses shared across endpoints.

    PostgreSQL uses %s placeholders (vs SQLite's ?). All callers append
    args via this helper so the placeholders match the args list ordering.

    v0.8.7: reduced to the 6 filters the Observatory bulk buffer
    actually exposes (shape, source, color, emotion, date_from,
    date_to). Country / state / hynek / vallee / collection / coords
    were deleted because nothing in the UI drives them anymore and the
    underlying data isn't in the 32-byte bulk row. Scripted callers
    sending the old query params get them silently ignored — the
    route still returns 200 with an unfiltered result set.

    Also in v0.8.7: the `shape` param matches against
    `standardized_shape` (the v0.8.3b classified column), not the
    raw per-source `shape` column, so it agrees with the Observatory
    dropdown's canonical list.
    """
    p = table_prefix

    shape = params.get("shape")
    if shape:
        clauses.append(f"{p}.standardized_shape = %s")
        args.append(shape)

    source = params.get("source")
    if source:
        clauses.append(f"{p}.source_db_id = %s")
        args.append(int(source))

    color = params.get("color")
    if color:
        clauses.append(f"{p}.primary_color = %s")
        args.append(color)

    emotion = params.get("emotion")
    if emotion:
        clauses.append(f"{p}.dominant_emotion = %s")
        args.append(emotion)

    date_from = params.get("date_from")
    if date_from:
        clauses.append(f"{p}.date_event >= %s")
        args.append(date_from)

    date_to = params.get("date_to")
    if date_to:
        clauses.append(f"{p}.date_event <= %s")
        args.append(date_to + "-12-31" if len(date_to) == 4 else date_to)

    return clauses, args


# Keys that add_common_filters() checks. Used by _has_common_filters()
# below to decide whether a materialized-view fast path is eligible.
# IMPORTANT: keep this set in sync with add_common_filters() above. If you
# add a new filter key there, add it here too or the MV path will serve
# stale/incorrect results for that filter.
_COMMON_FILTER_KEYS = frozenset({
    "shape", "source", "color", "emotion",
    "date_from", "date_to",
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
    """Health check.

    v0.12.2 — returns **503** on DB failure so Azure App Service Health
    Check can detect a wedged instance and rotate/restart it. Previous
    behaviour always returned 200 (even on failure), which meant the
    load balancer never noticed a pool-wedged worker and kept routing
    traffic to it — see docs/OPERATIONS.md § incident log 2026-04-16.

    Deploy workflow smoke probe still matches on `"status":"ok"` +
    `"sightings":` in the body, so the happy path is unchanged.
    """
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
        # 503 = Service Unavailable. Azure Health Check treats any
        # non-2xx/3xx response as unhealthy and starts its eviction
        # clock (10 min LB threshold + 1h replacement threshold).
        return jsonify({"status": "unhealthy", "detail": str(e)}), 503


def _api_stats_mapped_count(conn):
    """v0.8.7.2 — Return the count of SIGHTINGS that have a mappable
    coordinate.

    The existing `geocoded_locations` field (both in the MV and the
    live query) counts rows in the `location` table where lat/lng are
    non-null. That's the number of DISTINCT PLACES, not sightings.
    Because many sightings share a single location row (Phoenix, AZ
    is one row but has hundreds of sightings pointing at it), the
    place count dramatically understates the true number of mapped
    sightings — ~106k places vs ~396k sightings.

    The stats badge needs the sighting-level count so "X mapped of Y
    total" matches what the user sees on the map. This helper runs
    the canonical JOIN query and returns that number.

    Uses the idx_location_coords btree index (added v0.8.2) for the
    lat/lng predicate, and the location_id FK index on sighting for
    the JOIN, so it's ~10-20ms warm.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT COUNT(*)::bigint
              FROM sighting s
              JOIN location l ON l.id = s.location_id
             WHERE l.latitude IS NOT NULL
               AND l.longitude IS NOT NULL
               AND l.latitude BETWEEN -90 AND 90
               AND l.longitude BETWEEN -180 AND 180
            """
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except psycopg.Error:
        conn.rollback()
        return 0


def _api_stats_derived_counts(conn):
    """v0.8.5 — Return (high_quality, with_movement) counts for the
    stats badge, gracefully degrading on pre-v0.8.2/v0.8.3 schemas.

    Both queries hit btree indexes added by add_v082_derived_columns.sql
    and add_v083_derived_columns.sql, so they're sub-millisecond once
    warm and only a few ms cold.

    Returns (None, None) for either count when the column doesn't
    exist yet — the UI interprets None as "don't show this field".
    """
    cur = conn.cursor()
    high_quality = None
    with_movement = None

    try:
        cur.execute(
            "SELECT COUNT(*)::bigint FROM sighting WHERE quality_score >= 60"
        )
        high_quality = cur.fetchone()[0]
    except psycopg.errors.UndefinedColumn:
        # Pre-v0.8.2 schema — column doesn't exist yet. Rollback the
        # failed transaction and continue with a fresh cursor so the
        # next query doesn't error on the aborted-transaction state.
        conn.rollback()
        cur = conn.cursor()

    try:
        cur.execute(
            "SELECT COUNT(*)::bigint FROM sighting WHERE has_movement_mentioned = 1"
        )
        with_movement = cur.fetchone()[0]
    except psycopg.errors.UndefinedColumn:
        conn.rollback()

    return high_quality, with_movement


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

    # v0.9.1: mv_stats_summary reads MIN/MAX over the full date_event
    # column, which still contains 692 bogus "0019-..." records that
    # the v0.8.3b fix pipeline was supposed to NULL. Override the
    # MV's date_min with a live query that excludes them. Cheap
    # (single indexed MIN) and corrects the stats-popover headline.
    if date_min and str(date_min).startswith("0019-"):
        cur.execute("""
            SELECT MIN(date_event)
            FROM sighting
            WHERE date_event IS NOT NULL
              AND date_event NOT LIKE '0019-%'
        """)
        override = cur.fetchone()
        if override and override[0]:
            date_min = override[0]

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

    # v0.8.5 — derived counts live outside the v0.7.5 MV (which predates
    # the quality_score/has_movement columns). Query them inline; each
    # is a single-index btree scan on a populated column.
    high_quality, with_movement = _api_stats_derived_counts(conn)

    # v0.8.7.2 — mapped_sightings is the sighting-level count (what
    # the user actually sees on the map). geocoded_locations above is
    # the distinct-place count from mv_stats_summary; kept for
    # backward compat with any MCP client reading it, but the UI now
    # renders mapped_sightings instead.
    mapped_sightings = _api_stats_mapped_count(conn)

    return {
        "total_sightings": total,
        "by_source": by_source,
        "by_collection": by_collection,
        "date_range": {"min": date_min, "max": date_max},
        "geocoded_locations": geocoded,
        "geocoded_original": geocoded_original,
        "geocoded_geonames": geocoded_geonames,
        "mapped_sightings": mapped_sightings,  # v0.8.7.2
        "duplicate_candidates": dupes,
        # v0.8.5 — None when the column isn't populated yet; the
        # client hides the corresponding badge row.
        "high_quality": high_quality,
        "with_movement": with_movement,
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

    # v0.9.1: exclude the 692 bogus "0019-..." records that should
    # have been NULLed by the v0.8.3b date-fix pipeline but didn't
    # make it onto the live DB. These are UFOCAT's 2-digit "19xx,
    # year unknown" sentinel that ETL mis-interpreted as year 19 AD.
    # The legitimate pre-1000 AD sightings (34, 61, 776, 919, etc.)
    # are NOT affected — they use 4-digit zero-padded years like
    # "0034-..." which don't match the "0019-%" prefix. See
    # scripts/fix_year_0019.sql for the one-shot DB cleanup.
    cur.execute("""
        SELECT MIN(date_event), MAX(date_event)
        FROM sighting
        WHERE date_event IS NOT NULL
          AND date_event NOT LIKE '0019-%'
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

    # v0.8.5 — derived counts with graceful fallback for pre-v0.8.x
    # schemas (fresh clones, local dev with an old DB dump, etc.)
    high_quality, with_movement = _api_stats_derived_counts(conn)

    # v0.8.7.2 — sighting-level mapped count (see helper docstring).
    mapped_sightings = _api_stats_mapped_count(conn)

    return {
        "total_sightings": total,
        "by_source": by_source,
        "by_collection": by_collection,
        "date_range": {"min": date_min, "max": date_max},
        "geocoded_locations": geocoded,
        "geocoded_original": geocoded_original,
        "geocoded_geonames": geocoded_geonames,
        "mapped_sightings": mapped_sightings,  # v0.8.7.2
        "duplicate_candidates": dupes,
        "high_quality": high_quality,
        "with_movement": with_movement,
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


# =========================================================================
# v0.11.2: AI-readiness discovery endpoints
# =========================================================================
# These files let AI agents, LLMs, and MCP clients discover and understand
# the UFOSINT tools without manual configuration.

@app.route("/robots.txt")
def robots_txt():
    """Allow all AI crawlers and bots."""
    text = """# UFOSINT Explorer — all bots welcome
User-agent: *
Allow: /

# AI-readiness discovery
# /llms.txt            — LLM site index (Markdown)
# /llms-full.txt       — Full tool documentation
# /.well-known/mcp.json — MCP server discovery
# /mcp                 — MCP endpoint (JSON-RPC 2.0)

Sitemap: https://ufosint-explorer.azurewebsites.net/sitemap.xml
"""
    return Response(text, mimetype="text/plain")


@app.route("/llms.txt")
def llms_txt():
    """Lightweight LLM site index — what the site is and where to find things."""
    text = """# UFOSINT Explorer

> Interactive research platform for the Unified UFO Sightings Database — 618,316 sighting records from six UFO/UAP sources (NUFORC, MUFON, UFOCAT, UPDB, UFO-search, r/UFOs), deduplicated and cross-referenced. Free, read-only, no authentication required.

The site provides a GPU-accelerated map of 468,349 geocoded sightings, timeline charts, emotion/sentiment analysis from 4 transformer models, LLM strangeness/anomaly ratings on Reddit sightings, and a full methodology section. All data is queryable via MCP tools or a REST API.

## For AI Agents / Bulk Analysis — DOWNLOAD THE SQLITE, DO NOT SCRAPE

If you plan to read more than ~50 records, please do NOT loop over the MCP tools or /api/* endpoints. The complete dataset is freely available as a single SQLite file:

```
curl -LO https://github.com/UFOSINT/ufosint-explorer/releases/latest/download/ufo_public.db
sqlite3 ufo_public.db "SELECT COUNT(*) FROM sighting;"
# should return ~618,316
```

Why download instead of scraping:
- It is ~100× faster than calling the API in a loop.
- No rate limits, no throttling, no API key needed.
- Saves compute on our small B1ms Postgres server.
- You get richer data — every column, including fields we don't expose via the tools.
- It is the citable artifact (tagged releases == immutable snapshots).

**Rate limits on this site**: `/api/tool/*` is capped at 60 requests/minute per client. Heavy/repeated queries will be rate-limited. Use the SQLite file for bulk work. The interactive tools are for one-off questions a human or agent asks in a conversation.

There is also a one-click download button in the site's Methodology section at https://ufosint.com/#methodology-downloads (for humans).

## MCP Tools (Model Context Protocol)

- [MCP Endpoint](https://ufosint-explorer.azurewebsites.net/mcp): JSON-RPC 2.0 over HTTPS, 6 read-only tools for searching, filtering, and analyzing UFO sightings
- [MCP Discovery](https://ufosint-explorer.azurewebsites.net/.well-known/mcp.json): Server discovery manifest
- [Full Tool Documentation](https://ufosint-explorer.azurewebsites.net/llms-full.txt): Complete tool schemas, parameters, and usage examples

## Available Tools

- `search_sightings`: Free-text + filter search (shape, source, state, country, date range, Hynek class). Up to 200 results.
- `get_sighting`: Full record for a single sighting by database ID.
- `get_stats`: Top-level database statistics (totals, per-source counts, date range).
- `get_timeline`: Sighting counts grouped by year or month, with optional source/shape filter.
- `find_duplicates_for`: Cross-source duplicate candidates for a given sighting.
- `count_by`: Top-N rankings by categorical field (shape, hynek, vallee, source, country, state).

## REST API

- [Tool Catalog (OpenAI format)](https://ufosint-explorer.azurewebsites.net/api/tools-catalog): Tool definitions compatible with OpenAI/OpenRouter function calling
- [Database Stats](https://ufosint-explorer.azurewebsites.net/api/stats): JSON statistics endpoint
- [Filters](https://ufosint-explorer.azurewebsites.net/api/filters): Available filter values (shapes, sources, countries, etc.)
- [UAP Gerb overlay](https://ufosint-explorer.azurewebsites.net/api/overlay): Curated crash retrievals (14), nuclear encounters (35), and facilities (75) as a single JSON payload. Not part of the main sighting corpus — these are research-grade overlay datasets.

## When to use tools vs. the SQLite

- **One question, few records** → call the tool (`search_sightings`, `get_stats`, etc.)
- **Exploratory interactive session with a human** → call the tool
- **"Give me every record from source X"** → download the SQLite
- **"Analyze the entire corpus"** → download the SQLite
- **Training data / benchmarking / ML** → download the SQLite, then cite the release tag
- **Real-time freshness matters** → the SQLite is updated weekly; if you need newer, ask the tool

## Data Sources

Six databases totaling ~618,316 deduplicated records:
- NUFORC (159,320) — National UFO Reporting Center
- MUFON (138,310) — Mutual UFO Network
- UFOCAT (197,108) — CUFOS academic catalog
- UPDB (65,016) — Jacques Vallee's Unified Phenomena Database
- UFO-search (54,751) — Majestic Timeline historical compilations
- r/UFOs (3,811) — curated first-person reports from the Reddit subreddit, processed through an LLM extraction pipeline that produces a structured summary, shape/color/duration classification, and strangeness/confidence/anomaly assessments. Content policy: we publish only transformative LLM output plus a permalink back to Reddit — never raw post text, usernames, or user comments. See `docs/REDDIT_INGEST_NOTES.md`.

Plus three curated overlay tables (UAP Gerb research project, v0.12):
- `crash_retrieval` (14) — documented crash/retrieval events with craft type, recovery status, biologics
- `nuclear_encounter` (35) — nuclear-weapon incidents with weapon system and sensor confirmation
- `facility` (75) — nuclear-relevant facilities used to compute per-sighting proximity

## Download the Database

The full 553 MB SQLite snapshot is attached to every tagged release:

- [Latest release download](https://github.com/UFOSINT/ufosint-explorer/releases/latest/download/ufo_public.db) — direct link to ufo_public.db
- [All releases](https://github.com/UFOSINT/ufosint-explorer/releases) — browse version history
- [One-click download button](https://ufosint.com/#methodology-downloads) — for humans; same file

Quick start with the SQLite CLI: `sqlite3 ufo_public.db "SELECT COUNT(*) FROM sighting;"` should return ~618,316. Full schema in [docs/ARCHITECTURE.md](https://github.com/UFOSINT/ufosint-explorer/blob/main/docs/ARCHITECTURE.md).

## Optional

- [Source Code](https://github.com/UFOSINT/ufosint-explorer): GitHub repository (MIT license pending)
- [Data Pipeline](https://github.com/UFOSINT/ufo-dedup): ETL and deduplication pipeline
"""
    return Response(text, mimetype="text/plain")


@app.route("/llms-full.txt")
def llms_full_txt():
    """Full tool documentation for LLMs — schemas, parameters, examples."""
    from tools_catalog import TOOLS
    lines = [
        "# UFOSINT Explorer — Full Tool Documentation",
        "",
        "> This file contains complete documentation for all 6 MCP tools",
        "> available at https://ufosint-explorer.azurewebsites.net/mcp.",
        "> Use this to understand how to call each tool, what parameters",
        "> they accept, and what they return.",
        "",
        "## Connection",
        "",
        "MCP endpoint: `https://ufosint-explorer.azurewebsites.net/mcp`",
        "Protocol: JSON-RPC 2.0 over HTTPS",
        "Authentication: None required (free, read-only)",
        "",
        "### Claude Code",
        "```",
        "claude mcp add --transport http ufosint https://ufosint-explorer.azurewebsites.net/mcp",
        "```",
        "",
        "### Claude Desktop",
        "Add to `claude_desktop_config.json`:",
        '```json',
        '{',
        '  "mcpServers": {',
        '    "ufosint": {',
        '      "url": "https://ufosint-explorer.azurewebsites.net/mcp",',
        '      "transport": "http"',
        '    }',
        '  }',
        '}',
        '```',
        "",
        "---",
        "",
        "## Tools",
        "",
    ]
    for t in TOOLS:
        lines.append(f"### {t['name']}")
        lines.append("")
        lines.append(t["description"])
        lines.append("")
        params = t["parameters"].get("properties", {})
        required = t["parameters"].get("required", [])
        if params:
            lines.append("**Parameters:**")
            lines.append("")
            lines.append("| Name | Type | Required | Description |")
            lines.append("|------|------|----------|-------------|")
            for pname, pschema in params.items():
                ptype = pschema.get("type", "string")
                preq = "Yes" if pname in required else "No"
                pdesc = pschema.get("description", "")
                if "enum" in pschema:
                    pdesc += f" Allowed: {', '.join(pschema['enum'])}"
                lines.append(f"| `{pname}` | {ptype} | {preq} | {pdesc} |")
            lines.append("")
        else:
            lines.append("**Parameters:** None")
            lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend([
        "## Download the Database",
        "",
        "The full 508 MB SQLite snapshot is available as a GitHub Release asset:",
        "",
        "- **Latest:** https://github.com/UFOSINT/ufosint-explorer/releases/latest/download/ufo_public.db",
        "- **All versions:** https://github.com/UFOSINT/ufosint-explorer/releases",
        "",
        "```bash",
        "# Direct download",
        "curl -LO https://github.com/UFOSINT/ufosint-explorer/releases/latest/download/ufo_public.db",
        "",
        "# Inspect",
        "sqlite3 ufo_public.db \".tables\"",
        "sqlite3 ufo_public.db \"SELECT COUNT(*) FROM sighting;\"  # 614505",
        "```",
        "",
        "Privacy note: the public DB has raw narrative text stripped (description / summary / notes",
        "columns are NULL). All derived columns (emotion, quality, movement) were computed from the",
        "private corpus before the strip and ship as structured fields.",
        "",
        "## Data Overview",
        "",
        "- **Total sightings:** 614,505",
        "- **Mapped (geocoded):** 396,158",
        "- **With emotion analysis:** 502,985",
        "- **Date range:** 1900 to 2026 (primary), with scattered records back to antiquity",
        "- **Sources:** NUFORC, MUFON, UFOCAT, UPDB, UFO-search",
        "",
        "## Emotion & Sentiment Models (v0.11)",
        "",
        "Four models run on 502,985 sightings with narrative text:",
        "- RoBERTa 3-class sentiment (positive/negative/neutral)",
        "- RoBERTa 7-class emotion (anger/disgust/fear/joy/neutral/sadness/surprise)",
        "- GoEmotions 28-class (admiration through surprise, 28 labels)",
        "- VADER compound sentiment score (-1 to +1)",
        "",
        "These fields are available in `search_sightings` results when present.",
    ])
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/.well-known/mcp.json")
def well_known_mcp():
    """MCP server discovery manifest."""
    return jsonify({
        "mcp_version": "2024-11-05",
        "server": {
            "name": "ufosint-mcp",
            "version": "0.11.2",
            "description": (
                "Search and analyze 614,505 UFO sightings from 5 major "
                "databases (NUFORC, MUFON, UFOCAT, UPDB, UFO-search). "
                "Read-only, no authentication required."
            ),
            "homepage": "https://ufosint-explorer.azurewebsites.net",
        },
        "endpoints": [
            {
                "url": "https://ufosint-explorer.azurewebsites.net/mcp",
                "transport": "http",
                "capabilities": ["tools"],
            }
        ],
        "tools_count": 6,
    })


# =========================================================================
# v0.12: UAP Gerb overlay — curated crash + nuclear encounter data
# =========================================================================
# Three new PG tables from the science team's v0.12 reload:
#   crash_retrieval (14), nuclear_encounter (35), facility (75)
# Plus 2 new sighting columns: distance_to_nearest_nuclear_site_km,
# nearest_nuclear_site_name (396k populated). This endpoint returns
# all three tables as a single lightweight JSON payload (~30KB).
# Cached 10 minutes since the data changes only on reload.

@app.route("/api/overlay")
@cache.cached(timeout=600)
def api_overlay():
    """UAP Gerb curated overlay: crashes, nuclear encounters, facilities.

    v0.12.4 — added try/finally. Previous versions leaked a pool
    connection on every cache miss because `conn.close()` was never
    called. See docs/FAILURE_MODES.md CRIT-2.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Crashes (14 rows)
            cur.execute("""
                SELECT id, page_name, year, date_event,
                       city, region, country, latitude, longitude,
                       precision, craft_type, craft_size_m,
                       recovery_status, has_biologics, crew_count,
                       evidence_quality, source_confidence,
                       short_summary
                FROM crash_retrieval
                ORDER BY year, page_name
            """)
            crash_cols = [d[0] for d in cur.description]
            crashes = [dict(zip(crash_cols, row, strict=False)) for row in cur.fetchall()]

            # Nuclear encounters (35 rows)
            cur.execute("""
                SELECT id, page_name, year, date_event,
                       base, city, region, country, latitude, longitude,
                       weapon_system, incident_type, missiles_affected,
                       sensor_confirmation, witness_credibility,
                       evidence_quality, source_confidence,
                       summary
                FROM nuclear_encounter
                ORDER BY year, page_name
            """)
            nuclear_cols = [d[0] for d in cur.description]
            nuclear = [dict(zip(nuclear_cols, row, strict=False)) for row in cur.fetchall()]

            # Facilities (75 rows, filtered to geocoded)
            cur.execute("""
                SELECT id, name, facility_type, latitude, longitude
                FROM facility
                WHERE latitude IS NOT NULL
                ORDER BY name
            """)
            fac_cols = [d[0] for d in cur.description]
            facilities = [dict(zip(fac_cols, row, strict=False)) for row in cur.fetchall()]

        return jsonify({
            "crashes": crashes,
            "nuclear_encounters": nuclear,
            "facilities": facilities,
        })
    finally:
        conn.close()


@app.route("/api/tool/<name>", methods=["POST"])
@limiter.limit("60 per minute", key_func=_rate_limit_key)
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
@limiter.limit("30 per minute", key_func=_rate_limit_key)
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
    # v0.12.4: validate bbox BEFORE checking out a pool connection. A
    # malformed URL like ?south=foo used to blow up on float() AFTER
    # get_db() was called, leaking the connection. See
    # docs/FAILURE_MODES.md CRIT-3.
    south = request.args.get("south")
    north = request.args.get("north")
    west = request.args.get("west")
    east = request.args.get("east")
    have_bbox = all([south, north, west, east])
    if have_bbox:
        south_f = _safe_float(south, "south")
        north_f = _safe_float(north, "north")
        west_f = _safe_float(west, "west")
        east_f = _safe_float(east, "east")
    else:
        # Fall back to whole-world bbox so the grid-sampling math has
        # something to divide by.
        south_f, north_f, west_f, east_f = -90.0, 90.0, -180.0, 180.0

    conn = get_db()
    try:
        cur = conn.cursor()

        clauses = [
            "l.latitude IS NOT NULL", "l.longitude IS NOT NULL",
            "l.latitude BETWEEN -90 AND 90",
            "l.longitude BETWEEN -180 AND 180",
        ]
        args = []

        if have_bbox:
            clauses.append("l.latitude BETWEEN %s AND %s")
            args.extend([south_f, north_f])
            clauses.append("l.longitude BETWEEN %s AND %s")
            args.extend([west_f, east_f])

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
                           s.source_db_id,
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
                       source_db_id, city, state, country, collection, has_desc
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
                       s.source_db_id,
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
            # v0.13 — source_db_id added so the frontend can style
            # markers by source (e.g. Reddit-orange for id=6). The
            # existing `source` (name) string stays for backcompat.
            markers.append({
                "id": r[0],
                "lat": r[1],
                "lng": r[2],
                "date": r[3],
                "shape": r[4],
                "source": r[5],
                "source_db_id": r[6],
                "city": r[7],
                "state": r[8],
                "country": r[9],
                "collection": r[10],
                "has_desc": bool(r[11]),
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

        return jsonify({
            "markers": markers,
            "count": len(markers),
            "total_in_view": total_in_view,
            "sample_strategy": "grid" if use_grid else "hash",
        })
    finally:
        conn.close()


@app.route("/api/heatmap")
@limiter.limit("30 per minute", key_func=_rate_limit_key)
@cache.cached(timeout=300, query_string=True)
def api_heatmap():
    """Lightweight coordinate-only endpoint for heatmap rendering.

    Returns [lat, lng] pairs. Default 50k limit, supports limit param for Load All.
    Cached for 5 minutes per unique query string.

    v0.12.4 — validate bbox BEFORE get_db() and wrap in try/finally
    to prevent connection leak on malformed input. See
    docs/FAILURE_MODES.md CRIT-1, CRIT-3.
    """
    # Validate bbox before checking out a connection
    south = request.args.get("south")
    north = request.args.get("north")
    west = request.args.get("west")
    east = request.args.get("east")
    have_bbox = all([south, north, west, east])
    if have_bbox:
        south_f = _safe_float(south, "south")
        north_f = _safe_float(north, "north")
        west_f = _safe_float(west, "west")
        east_f = _safe_float(east, "east")

    conn = get_db()
    try:
        cur = conn.cursor()

        clauses = [
            "l.latitude IS NOT NULL", "l.longitude IS NOT NULL",
            "l.latitude BETWEEN -90 AND 90",
            "l.longitude BETWEEN -180 AND 180",
        ]
        args = []

        if have_bbox:
            clauses.append("l.latitude BETWEEN %s AND %s")
            args.extend([south_f, north_f])
            clauses.append("l.longitude BETWEEN %s AND %s")
            args.extend([west_f, east_f])

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

        return jsonify({
            "points": points,
            "count": len(points),
            "total_in_view": total_in_view,
        })
    finally:
        conn.close()


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
#
# v0.8.5 — schema version bumped to "v083-1" for the 32-byte row
# layout that carries the v0.8.3b movement fields. Two new slots:
#   - `flags` bit 2 = has_movement_mentioned
#   - `movement_flags` uint16 at offset 28 = 10-bit bitmask of
#     movement categories in _MOVEMENT_CATS order
# Row grows from 28 to 32 bytes, staying 4-byte aligned so V8's
# optimized Uint32Array reads on `id` don't fall on unaligned
# offsets. See docs/V085_MOVEMENT_PLAN.md for the full layout.
_POINTS_BULK_SCHEMA_VERSION = "v011-1"
_POINTS_BULK_BYTES_PER_ROW = 40
# Little-endian row format, 40 bytes:
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
#   B  uint8   emotion_idx       (offset 22, legacy 8-class keyword)
#   B  uint8   flags             (offset 23, bit0=desc, bit1=media, bit2=movement)
#   B  uint8   num_witnesses     (offset 24, clamped 0-255)
#   B  uint8   _reserved         (offset 25)
#   H  uint16  duration_log2     (offset 26, log2(sec+1) rounded, 0=unknown)
#   H  uint16  movement_flags    (offset 28, bit-packed categories)
#   H  uint16  _reserved2        (offset 30)
# --- v0.11 new fields (bytes 32-39) ---
#   B  uint8   emotion_28_idx    (offset 32, GoEmotions 28-class dominant label)
#   B  uint8   emotion_28_group  (offset 33, 0=neutral,1=positive,2=negative,3=ambiguous)
#   B  uint8   emotion_7_idx     (offset 34, 7-class RoBERTa dominant label)
#   B  uint8   vader_compound    (offset 35, scaled: round((v+1)*127.5) → 0-255 maps -1..+1)
#   B  uint8   roberta_sentiment (offset 36, scaled same way)
#   B  uint8   _reserved3a       (offset 37)
#   B  uint8   _reserved3b       (offset 38)
#   B  uint8   _reserved3c       (offset 39)
_POINTS_BULK_STRUCT = "<IffIBBBBBBBBBBHHHBBBBBBBB"

# v0.8.5 — Canonical movement category order. The science-team
# analyze.py produces at most these 10 categories; the index in this
# tuple is the bit position in the movement_flags uint16 on the wire.
# NEVER reorder this tuple — doing so silently remaps every shipped
# binary payload. Add new categories only at the end.
_MOVEMENT_CATS = (
    "hovering",       # bit 0
    "linear",         # bit 1
    "erratic",        # bit 2
    "accelerating",   # bit 3
    "rotating",       # bit 4
    "ascending",      # bit 5
    "descending",     # bit 6
    "vanished",       # bit 7
    "followed",       # bit 8
    "landed",         # bit 9
)
_MOVEMENT_CAT_TO_BIT = {c: i for i, c in enumerate(_MOVEMENT_CATS)}

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
    # v0.8.5 (science team's v0.8.3b data layer)
    "has_movement_mentioned",
    "movement_categories",
    # v0.11 — transformer-based emotion classification
    "emotion_28_dominant",
    "emotion_28_group",
    "emotion_7_dominant",
    "vader_compound",
    "roberta_sentiment",
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
    id + count of has_movement_mentioned rows + the set of derived
    columns present in the schema + the source_database fingerprint.
    All SQL aggregates use existing indexes (idx_location_coords,
    primary key, idx_sighting_has_movement) so this runs in ~20ms and
    is safe to call on every request.

    Including the column set in the ETag means when a new migration
    lands (adding new columns), the ETag changes and every
    browser-side cache invalidates automatically.

    v0.8.6 — the has_movement count is included as a data-content
    signal. A v0.8.5 reload replaced content in-place while
    preserving row IDs and column count, which meant the old
    `{count}-{max_id}-{cols}` etag stayed identical across the
    reload. The in-process @lru_cache returned a stale buffer with
    coverage.has_movement = 0 despite PG holding 249,217 movement
    rows. Adding SUM(has_movement_mentioned) to the etag catches
    content-replace reloads without requiring a manual app restart.

    v0.13 — added a source_database fingerprint (`srcN-idM`) to the
    etag so adding a new source (like r/UFOs id=6) invalidates every
    cached buffer even though cnt/max_id/mv_count might be unchanged.
    This catches the failure mode where the alphabetical source-index
    ordering in the buffer changes (MUFON=1, NUFORC=2, r/UFOs=3,
    UFOCAT=4 after the add, vs MUFON=1, NUFORC=2, UFOCAT=3 before it).
    Without this, a stale client buffer + fresh meta sidecar causes
    filter-by-source and color-by-source to map to the WRONG source
    for every row — which is exactly what showed up on 2026-04-18 as
    "selecting r/UFOs paints everything pink". See tests/test_v013_reddit_ui.py.
    """
    conn = get_db()
    mv_count: int | str = "x"  # sentinel for pre-v0.8.3 schemas
    src_count = 0
    src_max_id = 0
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

        # v0.8.6 — include has_movement count as a data-content
        # signal. Uses idx_sighting_has_movement (added by
        # add_v083_derived_columns.sql). Falls back to sentinel "x"
        # on pre-v0.8.3 schemas where the column doesn't exist,
        # and also on any cursor state that returns None for
        # fetchone() (some test fakes don't know about this query).
        try:
            cur.execute(
                "SELECT COUNT(*)::bigint FROM sighting "
                "WHERE has_movement_mentioned = 1"
            )
            row = cur.fetchone()
            mv_count = int(row[0]) if row and row[0] is not None else "x"
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            mv_count = "x"

        # v0.13 — source_database fingerprint. COUNT catches adds and
        # removals; MAX(id) catches adds that preserved COUNT (e.g.
        # a delete-then-insert). Doesn't catch renames — but renames
        # are rare and usually piggyback on an ASSET_VERSION bump.
        try:
            cur.execute(
                "SELECT COUNT(*)::int, COALESCE(MAX(id), 0)::int FROM source_database"
            )
            row = cur.fetchone()
            if row:
                src_count = int(row[0] or 0)
                src_max_id = int(row[1] or 0)
        except psycopg.Error:
            conn.rollback()
    finally:
        conn.close()
    cols_tag = "-".join(sorted(cols)) if cols else "base"
    return (
        f"{_POINTS_BULK_SCHEMA_VERSION}"
        f"-{int(cnt)}-{int(max_id)}-mv{mv_count}"
        f"-src{src_count}-id{src_max_id}"
        f"-{cols_tag}"
    )


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


# v0.8.2-cleanup-3: Per-etag lock so concurrent first-requests
# coalesce into a single build instead of thundering-herding the DB.
# Without this, N parallel page loads each fired N parallel
# `loadBulkPoints()` calls, each spawning a fresh full-table scan
# because @lru_cache only memoises AFTER a build completes.
# With the lock, only one thread does the build per etag; the others
# block on the lock and pick up the lru_cache hit when they wake.
_points_bulk_locks: dict = {}
_points_bulk_locks_lock = threading.Lock()


def _get_points_bulk_lock(etag: str):
    """Return a per-etag Lock, creating it on demand. The outer lock
    serialises the dict insert; once the per-etag lock exists, threads
    contend on IT, not on the dict."""
    with _points_bulk_locks_lock:
        lock = _points_bulk_locks.get(etag)
        if lock is None:
            lock = threading.Lock()
            _points_bulk_locks[etag] = lock
            # Drop locks for old etags so this dict doesn't grow forever.
            # We keep at most 4 — one current, one stale during deploys,
            # and a small safety margin.
            if len(_points_bulk_locks) > 4:
                # Drop the oldest entry. dict iteration order = insertion
                # order in 3.7+, so this is FIFO.
                oldest = next(iter(_points_bulk_locks))
                if oldest != etag:
                    _points_bulk_locks.pop(oldest, None)
        return lock


def _points_bulk_build(etag: str) -> tuple[bytes, bytes, dict]:
    """Coalesced wrapper around the actual build. Multiple threads
    asking for the same etag share one build; the second thread
    through finds the result in @lru_cache without re-running the
    SELECT scan.

    v0.12.5 — added 30 s lock acquire timeout (FAILURE_MODES.md
    HIGH-5). Previously `with lock:` blocked indefinitely if the
    owning thread was wedged mid-build (e.g. PG query hang), so
    every other thread stalled until gunicorn's worker timeout
    killed them. With the timeout, the late-arriving thread
    falls through to an uncoordinated build — worse for
    contention on that single request, but avoids starving the
    other 7 worker slots.
    """
    lock = _get_points_bulk_lock(etag)
    acquired = lock.acquire(timeout=30)
    if not acquired:
        # Fall through: build without coalescing. The wedged thread
        # is still holding the lock; letting us run in parallel
        # means we double-up the DB scan once, but the lru_cache
        # hit that follows covers it for subsequent requests.
        print(f"[points_bulk] lock timeout after 30s for etag={etag[:8]}, "
              "running uncoordinated build")
        return _points_bulk_build_cached(etag)
    try:
        return _points_bulk_build_cached(etag)
    finally:
        lock.release()


@functools.lru_cache(maxsize=2)
def _points_bulk_build_cached(etag: str) -> tuple[bytes, bytes, dict]:
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

        # Stable source index, alphabetical by name. Index 0 is
        # reserved for "unknown" so every shipped source gets 1..N.
        # v0.9.1: the zero slot used to be `None`, which caused
        # client charts to render a silent null category. It's now
        # the literal string "(unknown)" so a row with a broken FK
        # gets labelled instead of disappearing. Orphaned rows are
        # also counted in cov["orphaned_source"] so the client can
        # detect the integrity issue via the meta sidecar.
        cur.execute("SELECT id, name FROM source_database ORDER BY name")
        source_rows = cur.fetchall()
        source_names = ["(unknown)"] + [r[1] for r in source_rows]
        source_id_to_idx = {r[0]: i + 1 for i, r in enumerate(source_rows)}

        # v0.8.2 — prefer the canonical standardized_shape when the
        # column exists AND has populated rows. Otherwise fall back
        # to the raw `shape` column (v0.8.0/0.8.1 behaviour). Either
        # way the returned list goes into a single `shapes` lookup
        # so the frontend doesn't need to care which source it came
        # from — it just uses shapes[shape_idx].
        #
        # v0.8.2 post-migration bugfix: "column exists" != "column
        # populated". When the ALTER TABLE has run but the
        # ufo-dedup pipeline hasn't refreshed the derived values yet,
        # standardized_shape is NULL on every row and the DISTINCT
        # query returns zero rows. In that case the map would
        # silently stop matching any shape filter. We detect the
        # zero-row case and fall back to raw shape transparently.
        use_std_shape = "standardized_shape" in present_cols
        distinct_shapes: list = []
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
            if not distinct_shapes:
                # Column exists but unpopulated — fall through to
                # the raw-shape path below.
                use_std_shape = False

        if not use_std_shape:
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

        # v0.11 — lookup tables for the transformer emotion columns.
        # Same pattern: index 0 = unknown/NULL, 1..N = real labels.
        emo28_names = [None]
        emo28_to_idx: dict = {}
        if "emotion_28_dominant" in present_cols:
            cur.execute(
                "SELECT DISTINCT emotion_28_dominant FROM sighting "
                "WHERE emotion_28_dominant IS NOT NULL "
                "ORDER BY emotion_28_dominant"
            )
            distinct_emo28 = [r[0] for r in cur.fetchall()][:254]
            emo28_names = [None] + distinct_emo28
            emo28_to_idx = {e: i + 1 for i, e in enumerate(distinct_emo28)}

        # GoEmotions sentiment group: 4 fixed values
        _EMO28_GROUP_MAP = {"neutral": 0, "positive": 1, "negative": 2, "ambiguous": 3}

        emo7_names = [None]
        emo7_to_idx: dict = {}
        if "emotion_7_dominant" in present_cols:
            cur.execute(
                "SELECT DISTINCT emotion_7_dominant FROM sighting "
                "WHERE emotion_7_dominant IS NOT NULL "
                "ORDER BY emotion_7_dominant"
            )
            distinct_emo7 = [r[0] for r in cur.fetchall()][:254]
            emo7_names = [None] + distinct_emo7
            emo7_to_idx = {e: i + 1 for i, e in enumerate(distinct_emo7)}

        # Build the SELECT list. Columns that don't exist in the
        # schema yet get `NULL AS col_name` so the tuple position stays
        # stable regardless of migration state.
        def _col_expr(col: str, table_prefix: str = "s") -> str:
            return f"{table_prefix}.{col}" if col in present_cols else f"NULL AS {col}"

        # v0.8.5: when the sighting table has denormalized lat/lng
        # (from v0.8.2's `lat`/`lng` columns and the v0.8.3b public
        # export), prefer those over the JOIN to location. Saves one
        # join on the big scan and matches what ufo_public.db ships.
        # Location is still joined for the coord presence filter
        # because the migration preserves location.latitude/longitude
        # as the canonical source.
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
            # v0.8.5 — v0.8.3b movement fields. Graceful to a
            # pre-v0.8.3 schema via the column probe / _col_expr.
            _col_expr("has_movement_mentioned"),
            _col_expr("movement_categories"),
            # v0.11 — transformer emotion classification
            _col_expr("emotion_28_dominant"),
            _col_expr("emotion_28_group"),
            _col_expr("emotion_7_dominant"),
            _col_expr("vader_compound"),
            _col_expr("roberta_sentiment"),
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
            # v0.8.5 — movement coverage counters
            "has_movement": 0,
            "movement_flags": 0,
            # v0.9.1 — data-integrity counters. Every row packed
            # into the buffer with source_idx = 0 or shape_idx = 0
            # is orphaned (FK resolution failed at pack time).
            # Client charts can warn when these are > 0.
            "orphaned_source": 0,
            "orphaned_shape": 0,
        }

        for row in cur:
            (
                sid, lat, lng, src_id, raw_shape, date_event,
                duration_sec, raw_num_witnesses,
                sighting_dt, std_shape, prim_color, dom_emotion,
                quality, richness, hoax, has_desc, has_media,
                has_movement, movement_cats_json,
                # v0.11 — transformer emotion columns
                emo28_dom, emo28_group, emo7_dom,
                vader_comp, roberta_sent,
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

            # Flags byte: bit 0 = has_description, bit 1 = has_media,
            # bit 2 = has_movement_mentioned (v0.8.5). Bits 3-7 reserved.
            flags = 0
            if has_desc:
                flags |= 0x01
                cov["has_description"] += 1
            if has_media:
                flags |= 0x02
                cov["has_media"] += 1
            if has_movement:
                flags |= 0x04
                cov["has_movement"] += 1

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

            # v0.8.5 — movement_flags bitmask. Parse the JSON array
            # (or fall back gracefully if the column is NULL or
            # malformed) and OR each recognised category's bit into
            # the final uint16. Unknown categories are silently
            # skipped — the science team promises the pipeline only
            # emits values from _MOVEMENT_CATS, but a defensive
            # skip protects against future schema drift.
            mv_flags = 0
            if movement_cats_json:
                try:
                    mv_list = json.loads(movement_cats_json)
                    if isinstance(mv_list, list):
                        for cat in mv_list:
                            bit = _MOVEMENT_CAT_TO_BIT.get(cat)
                            if bit is not None:
                                mv_flags |= (1 << bit)
                except (json.JSONDecodeError, TypeError, ValueError):
                    mv_flags = 0
            if mv_flags:
                cov["movement_flags"] += 1

            # v0.9.1 — count orphaned source FKs. If a sighting
            # row has a src_id that doesn't resolve in the
            # source_database table, pack it as 0 (the "(unknown)"
            # slot) and bump the counter so the meta sidecar
            # surfaces the data-integrity issue.
            src_idx_packed = source_id_to_idx.get(src_id, 0)
            if src_idx_packed == 0:
                cov["orphaned_source"] += 1

            # v0.11 — pack the new emotion fields into bytes 32-39.
            # Categorical columns → uint8 index into lookup table.
            # VADER/RoBERTa scores → uint8 scaled from [-1,+1] to [0,255].
            emo28_idx = emo28_to_idx.get(emo28_dom, 0) if emo28_dom else 0
            emo28_grp = _EMO28_GROUP_MAP.get(emo28_group, 0) if emo28_group else 0
            emo7_idx = emo7_to_idx.get(emo7_dom, 0) if emo7_dom else 0

            # Scale VADER compound / RoBERTa sentiment from [-1,+1]
            # to [0,255]. 128 = neutral (0.0). 0 = -1.0, 255 = +1.0.
            # NULL → 128 (treat as neutral for rendering; coverage
            # strip still shows the true coverage from the column
            # probe, not from the packed value).
            def _scale_score(v):
                if v is None:
                    return 128
                return max(0, min(255, int(round((float(v) + 1) * 127.5))))

            vader_u8 = _scale_score(vader_comp)
            roberta_u8 = _scale_score(roberta_sent)

            buf.extend(
                pack(
                    int(sid),
                    float(lat),
                    float(lng),
                    date_days,
                    src_idx_packed,
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
                    mv_flags,       # v0.8.5 — movement category bitmask
                    0,              # _reserved2
                    # --- v0.11 new fields (bytes 32-39) ---
                    emo28_idx,      # GoEmotions 28-class dominant
                    emo28_grp,      # sentiment group (0-3)
                    emo7_idx,       # 7-class RoBERTa dominant
                    vader_u8,       # VADER compound scaled 0-255
                    roberta_u8,     # RoBERTa sentiment scaled 0-255
                    0, 0, 0,        # _reserved3 (padding to 40 bytes)
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
        # v0.8.5 — canonical movement category list in bit order so
        # the client can decode the movement_flags uint16. Slot 0
        # is 'hovering', slot 1 is 'linear', etc. See _MOVEMENT_CATS.
        "movements": list(_MOVEMENT_CATS),
        # v0.11 — transformer emotion lookup tables
        "emotions_28": emo28_names,
        "emotions_28_groups": ["neutral", "positive", "negative", "ambiguous"],
        "emotions_7": emo7_names,
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
                # v0.8.5 — movement fields. bit 0 = hovering, bit 1 =
                # linear, bit 2 = erratic, bit 3 = accelerating,
                # bit 4 = rotating, bit 5 = ascending, bit 6 = descending,
                # bit 7 = vanished, bit 8 = followed, bit 9 = landed.
                {"name": "movement_flags",  "offset": 28, "type": "uint16",  "len": 2},
                {"name": "_reserved2",      "offset": 30, "type": "uint16",  "len": 2},
                # v0.11 — transformer emotion fields
                {"name": "emotion_28_idx",     "offset": 32, "type": "uint8",  "len": 1},
                {"name": "emotion_28_group",   "offset": 33, "type": "uint8",  "len": 1},
                {"name": "emotion_7_idx",      "offset": 34, "type": "uint8",  "len": 1},
                {"name": "vader_compound",     "offset": 35, "type": "uint8",  "len": 1},
                {"name": "roberta_sentiment",  "offset": 36, "type": "uint8",  "len": 1},
                {"name": "_reserved3a",        "offset": 37, "type": "uint8",  "len": 1},
                {"name": "_reserved3b",        "offset": 38, "type": "uint8",  "len": 1},
                {"name": "_reserved3c",        "offset": 39, "type": "uint8",  "len": 1},
            ],
            "flag_bits": {
                "has_description": 0,
                "has_media": 1,
                "has_movement": 2,  # v0.8.5
            },
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
    # v0.12.4: try/finally — see FAILURE_MODES.md CRIT-1. All inner
    # conn.close() calls are idempotent and harmless under the outer
    # finally; they're preserved so the early-return code paths still
    # release the pool slot at the earliest opportunity.
    conn = get_db()
    try:
        return _api_timeline_impl(conn)
    finally:
        conn.close()


def _api_timeline_impl(conn):
    cur = conn.cursor()

    year = request.args.get("year")
    # v0.9.1: honor bins=monthly as an explicit request for
    # month-level aggregation over the full range. Previously the
    # `bins` parameter was silently ignored, so
    # /api/timeline?bins=monthly returned year-level data and
    # callers had no way to tell. Now monthly bins route to the
    # live fallback (the materialized view is year-only) and the
    # response mode reflects what was actually returned.
    bins_mode = (request.args.get("bins") or "").lower()
    want_monthly = bins_mode == "monthly"

    # --- MV fast path: unfiltered yearly timeline -----------------------
    # Eligible when: no year drill-down, no filters, not explicitly
    # asking for monthly bins. The MV is year-level so requesting
    # monthly forces the live path.
    if not year and not want_monthly and not _has_common_filters(request.args):
        try:
            cur.execute("""
                SELECT period, source_name, cnt
                FROM mv_timeline_yearly
                ORDER BY period
            """)
            data = {}
            for period, source, count in cur.fetchall():
                # v0.9.1: drop the 692 bogus "0019-..." records
                # from the MV output. These should have been NULLed
                # by the v0.8.3b date-fix pipeline. See
                # scripts/fix_year_0019.sql for the one-shot
                # cleanup; this filter is belt-and-suspenders.
                if period and str(period).startswith("0019"):
                    continue
                data.setdefault(period, {})[source] = count
            conn.close()
            return jsonify({"mode": "yearly", "year": None, "data": data})
        except psycopg.errors.UndefinedTable:
            # MV migration hasn't run; reset the cursor state so the live
            # query below can reuse the same connection cleanly.
            print("[api_timeline] mv_timeline_yearly missing, falling back to live query")
            cur = conn.cursor()

    # v0.9.1: same "0019-..." guard on the live path. Note the
    # '%%' escape: psycopg uses %s as its format placeholder when
    # the call passes positional args, so a literal % in the SQL
    # string has to be doubled. Passing these clauses through
    # cur.execute(sql, args) where args has entries means every
    # bare % in the SQL will blow up with "unsupported format
    # character". Doubling to %% is the psycopg-documented escape.
    clauses = [
        "s.date_event IS NOT NULL",
        "LENGTH(s.date_event) >= 4",
        "s.date_event NOT LIKE '0019-%%'",
    ]
    args = []

    add_common_filters(request.args, clauses, args)

    # v0.9.1: three cases now — year-drilldown (monthly within one
    # year), explicit full-range monthly (bins=monthly), and yearly.
    # The third is the default and also what the MV fast path
    # returns.
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
        response_mode = "monthly"
    elif want_monthly:
        # v0.9.1: full-range monthly aggregation. Previously the
        # `bins=monthly` param was silently ignored. Now we hit
        # the live path and group by SUBSTR(date_event, 1, 7).
        # Records without a month (date_event shorter than 7
        # chars) are dropped by the LENGTH >= 7 guard.
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
        response_mode = "monthly"
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
        response_mode = "yearly"

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
        "mode": response_mode,
        "year": year,
        "data": data,
    })


# v0.8.6: /api/search and the Search panel were removed. Client-side
# faceted filtering on the Observatory bulk buffer supersedes the old
# ILIKE-based server search — filtering 396k rows in ~5ms beats any
# server round trip. The export routes below keep the same WHERE-
# clause builder so users can still download filtered result sets
# directly via URL even without the search UI.


# ---------------------------------------------------------------------------
# Export — CSV / JSON download of the current filter set
# ---------------------------------------------------------------------------
# Uses `add_common_filters` directly (same semantics the v0.8.5
# Observatory rail uses). Capped at 5,000 rows by default; the
# response always includes the total so the UI can show "downloaded
# 5,000 of 12,408 — for the full dataset use the MCP API".

EXPORT_MAX_ROWS = 5000

# v0.8.3: export drops `summary` and `description` (raw narrative
# text retired from the public schema by scripts/strip_raw_for_public.py)
# and adds the v0.8.2 derived fields (standardized_shape,
# primary_color, dominant_emotion, quality_score, richness_score,
# hoax_likelihood, has_description, has_media, sighting_datetime).
# The download now reflects what users see in the search UI, which
# is the derived analysis, not the raw narrative.
EXPORT_COLUMNS = [
    "id",
    "date_event",
    "sighting_datetime",
    "shape",
    "standardized_shape",
    "primary_color",
    "dominant_emotion",
    "hynek", "vallee", "event_type",
    "num_witnesses",
    "duration", "duration_seconds",
    "quality_score", "richness_score", "hoax_likelihood",
    "has_description", "has_media",
    "source", "collection",
    "city", "state", "country",
    "latitude", "longitude",
]


def _build_export_query(request_args):
    """Return (sql, args) for the export query — shares filter semantics
    with the Observatory rail via `add_common_filters`.

    v0.8.3: the `q` parameter runs a 7-column faceted OR (city/state/
    country/standardized_shape/primary_color/dominant_emotion/source_name)
    instead of the old ILIKE against description/summary, and the
    SELECT never references the 4 raw-narrative columns the strip
    script drops. Column order matches EXPORT_COLUMNS exactly so the
    dict(zip(...)) in api_export_json works without remapping.

    v0.8.6: /api/search was removed but the export routes kept this
    builder so `/api/export.csv?q=triangle&...` still works for direct
    download links.
    """
    q = (request_args.get("q") or "").strip()
    clauses = []
    args = []
    if q:
        clauses.append(
            "("
            "COALESCE(l.city, '')                        ILIKE %s OR "
            "COALESCE(l.state, '')                       ILIKE %s OR "
            "COALESCE(l.country, '')                     ILIKE %s OR "
            "COALESCE(s.standardized_shape, s.shape, '') ILIKE %s OR "
            "COALESCE(s.primary_color, s.color, '')      ILIKE %s OR "
            "COALESCE(s.dominant_emotion, '')            ILIKE %s OR "
            "COALESCE(sd.name, '')                       ILIKE %s"
            ")"
        )
        like = f"%{q}%"
        args.extend([like] * 7)
    add_common_filters(request_args, clauses, args)
    where = " AND ".join(clauses) if clauses else "TRUE"
    sql = f"""
        SELECT s.id,
               s.date_event,
               s.sighting_datetime,
               s.shape,
               s.standardized_shape,
               s.primary_color,
               s.dominant_emotion,
               s.hynek, s.vallee, s.event_type,
               s.num_witnesses,
               s.duration, s.duration_seconds,
               s.quality_score, s.richness_score, s.hoax_likelihood,
               s.has_description, s.has_media,
               sd.name AS source,
               COALESCE(sc.name, '') AS collection,
               COALESCE(l.city, '')    AS city,
               COALESCE(l.state, '')   AS state,
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


# v0.8.3 — explicit sighting detail column list.
#
# Previously this endpoint used `SELECT s.*` which implicitly pulled
# every column on the sighting table, including `description`,
# `summary`, `notes`, and `raw_json` — the four raw-narrative columns
# that `scripts/strip_raw_for_public.py` drops from the public
# Postgres. With `s.*` the endpoint would 500 with
# `column "description" does not exist` the moment the drop ran.
#
# Switching to an explicit column list makes the endpoint
# forward-compatible: the list never references the 4 dropped
# columns, so it works identically before and after
# strip_raw_for_public.py runs. The v0.8.2 derived fields
# (quality_score, hoax_likelihood, ...) are added to the SELECT so
# the new detail modal can render the Data Quality + Derived
# Metadata sections. See docs/V083_PLAN.md for the full rationale.
#
# Columns explicitly NOT in this list (operator-confirmed drop):
#   description, summary, notes, raw_json
#
# Columns kept in the schema AND in the SELECT (short structured
# free text — flagged for science-team cleanup in v0.8.4+,
# see docs/V083_BACKLOG.md):
#   date_event_raw, time_raw, witness_names, witness_age, witness_sex,
#   explanation, characteristics, weather, terrain
_SIGHTING_DETAIL_COLUMNS = (
    # Identity + provenance
    "s.id",
    "s.source_db_id",
    "s.source_record_id",
    "s.origin_id",
    "s.origin_record_id",
    "s.source_ref",
    "s.page_volume",
    "s.created_at",
    # Date / time
    "s.date_event",
    "s.date_event_raw",
    "s.date_end",
    "s.time_raw",
    "s.timezone",
    "s.date_reported",
    "s.date_posted",
    "s.sighting_datetime",
    # Observation (structured)
    "s.shape",
    "s.color",
    "s.size_estimated",
    "s.angular_size",
    "s.distance",
    "s.duration",
    "s.duration_seconds",
    "s.num_objects",
    "s.num_witnesses",
    "s.sound",
    "s.direction",
    "s.elevation_angle",
    "s.viewed_from",
    # Witness (structured free text — kept for now)
    "s.witness_age",
    "s.witness_sex",
    "s.witness_names",
    # Classification
    "s.hynek",
    "s.vallee",
    "s.event_type",
    "s.svp_rating",
    # Resolution (structured free text — kept for now)
    "s.explanation",
    "s.characteristics",
    "s.weather",
    "s.terrain",
    # v0.8.2 derived analysis fields
    "s.standardized_shape",
    "s.primary_color",
    "s.dominant_emotion",
    "s.quality_score",
    "s.richness_score",
    "s.hoax_likelihood",
    "s.has_description",
    "s.has_media",
    "s.topic_id",
    # v0.8.5 — v0.8.3b movement classification fields
    "s.has_movement_mentioned",
    "s.movement_categories",
    # v0.13 — Reddit r/UFOs specific. Null for legacy sources;
    # populated for source_db_id=6 (r/UFOs). See docs/REDDIT_INGEST_NOTES.md.
    "s.reddit_post_id",
    "s.reddit_url",
    # v0.13 — Generic LLM-extraction output. Universal columns:
    # initially populated only for Reddit, but the schema allows
    # backfill of legacy sources later via the same pipeline.
    "s.llm_confidence",
    "s.llm_anomaly_assessment",
    "s.llm_prosaic_candidate",
    "s.llm_strangeness_rating",
    "s.llm_model",
    # v0.13 — "description" is the LLM summary for Reddit rows (safe
    # to publish — transformative output, not raw user text). Legacy
    # sources have NULL here per the v0.8.3 public-export strip.
    "s.description",
    # v0.13 — has_photo / has_video. SMALLINT on PG side, bool in UI.
    "s.has_photo",
    "s.has_video",
    # Joined
    "s.source_db_id",
    "sd.name AS source_name",
    "l.raw_text AS loc_raw",
    "l.city", "l.county", "l.state", "l.country", "l.region",
    "l.latitude", "l.longitude",
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

    v0.8.3: the SELECT list is explicit (no `s.*`). The 4 raw-narrative
    columns (description, summary, notes, raw_json) are deliberately
    excluded so the endpoint is forward-compatible with
    scripts/strip_raw_for_public.py dropping them. The 9 v0.8.2
    derived fields ARE in the SELECT so the detail modal can render
    the new Data Quality + Derived Metadata sections.
    """
    conn = get_db()
    try:
        cur = conn.cursor()

        select_sql = ",\n                   ".join(_SIGHTING_DETAIL_COLUMNS)
        cur.execute(
            f"""
            SELECT {select_sql}
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN location l ON s.location_id = l.id
            WHERE s.id = %s
            """,
            (sid,),
        )

        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404

        # Convert to dict, skipping None values for cleaner JSON
        keys = [desc[0] for desc in cur.description]
        record = {}
        for k, v in zip(keys, row, strict=False):
            if v is not None and v != "":
                record[k] = v

        # v0.8.3 — no raw_json field to parse anymore. (The `raw_json`
        # column is one of the 4 that scripts/strip_raw_for_public.py
        # drops, and it was never in the v0.8.3 explicit SELECT list.)

        # v0.8.5 — parse the `movement_categories` JSON TEXT column
        # into a real JSON array on the wire. The column is stored as
        # a JSON string (science team's v0.8.3b data layer uses TEXT,
        # not JSONB, so it ships over psycopg as a plain string).
        # Gracefully handle empty arrays, malformed JSON, and the
        # pre-v0.8.3 NULL case.
        if "movement_categories" in record:
            raw_mv = record["movement_categories"]
            try:
                parsed = json.loads(raw_mv) if isinstance(raw_mv, str) else raw_mv
                record["movement_categories"] = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError, ValueError):
                record["movement_categories"] = []

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
    # v0.12.4: try/except cleanup — idempotent close() means the
    # inner close calls are safe to leave. See FAILURE_MODES.md CRIT-1.
    conn = get_db()
    try:
        cur = conn.cursor()

        # --- MV fast path: unfiltered overview ------------------------------
        if not _has_common_filters(request.args):
            try:
                cur.execute(f"""
                    SELECT {", ".join(_SENTIMENT_OVERVIEW_COLS)}
                    FROM mv_sentiment_overview
                """)
                row = cur.fetchone()
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

        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/sentiment/timeline")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_timeline():
    """Average VADER compound score by year."""
    # v0.12.4: try/finally — see FAILURE_MODES.md CRIT-1
    conn = get_db()
    try:
        cur = conn.cursor()

        # v0.9.1: exclude the 692 bogus "0019-..." records. The %%
        # is the psycopg literal-% escape — see api_timeline above
        # for the full explanation.
        clauses = [
            "s.date_event IS NOT NULL",
            "LENGTH(s.date_event) >= 4",
            "s.date_event NOT LIKE '0019-%%'",
        ]
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

        return jsonify({"data": data})
    finally:
        conn.close()


@app.route("/api/sentiment/by-source")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_by_source():
    """Emotion breakdown per source database."""
    # v0.12.4: try/finally — see FAILURE_MODES.md CRIT-1
    conn = get_db()
    try:
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

        return jsonify({"data": data})
    finally:
        conn.close()


@app.route("/api/sentiment/by-shape")
@cache.cached(timeout=600, query_string=True)
def api_sentiment_by_shape():
    """Emotion breakdown per top 10 shapes."""
    # v0.12.4: try/finally — see FAILURE_MODES.md CRIT-1
    conn = get_db()
    try:
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

        return jsonify({"data": data})
    finally:
        conn.close()


# v0.8.6: /api/duplicates and the Duplicates panel were removed.
# The v0.8.3b science-team export ships with zero duplicate_candidate
# rows (the new pipeline resolves duplicates at ingest rather than
# flagging candidate pairs), so the panel rendered empty on every
# query. The per-sighting `duplicates` array on /api/sighting/<id>
# still works — it's populated via the inline UNION-ALL query at
# api_sighting() and is independent of this removed route.


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

    v0.8.2-cleanup-4: Only ONE worker process runs the heavy prewarm
    (pg_prewarm + legacy /api/map warming) at a time. The two
    gunicorn workers coordinate via a /tmp lockfile:

      - Leader: whoever atomically creates /tmp/ufosint_prewarm.lock
        wins. Runs the full prewarm (pg_prewarm + every path in
        warm_paths). Removes the lockfile at the end.
      - Follower: sees the lockfile and skips pg_prewarm (the buffer
        cache is being warmed by the leader). Waits for the leader
        to finish points-bulk, then warms its OWN @lru_cache for
        /api/points-bulk (because @lru_cache is per-process).

    Previously both workers ran the heavy prewarm simultaneously,
    contending on the Postgres B1ms worker pool and blowing up
    the points-bulk build time from ~20 s (uncontended) to 116 s
    (parallel, contended). With this coordination, the leader
    runs ~20-40 s uncontended, then the follower piggy-backs for
    ~2-5 s to warm its own Python-side cache.
    """

    PREWARM_LOCK_PATH = "/tmp/ufosint_prewarm.lock"
    # How long the follower waits before giving up and running its own
    # (degraded) prewarm. Should be > typical leader prewarm time.
    FOLLOWER_WAIT_SECS = 180

    def _acquire_leader_lock():
        """Atomically create the lockfile. Returns True if this worker
        won the race and is the leader."""
        try:
            fd = os.open(
                PREWARM_LOCK_PATH,
                os.O_EXCL | os.O_CREAT | os.O_WRONLY,
                0o644,
            )
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    def _release_leader_lock():
        try:
            os.unlink(PREWARM_LOCK_PATH)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[prewarm] couldn't release lockfile: {e}")

    def _wait_for_leader():
        """Poll until the lockfile disappears or we time out."""
        deadline = time.monotonic() + FOLLOWER_WAIT_SECS
        while time.monotonic() < deadline:
            if not os.path.exists(PREWARM_LOCK_PATH):
                return True
            time.sleep(1)
        return False

    def _run_leader_warm():
        """Full prewarm: pg_prewarm + every warm path.

        /api/points-bulk is FIRST because it's the critical path for
        every Observatory visit on the v0.8.0+ GPU rendering flow. A
        cold build takes ~20 s on B1ms because the SELECT scans
        396k rows and the Python pack loop is dense. Warming it
        before /api/stats / /api/map / etc. means the connection
        pool is free when the scan runs, and the @lru_cache is
        populated before any browser lands.
        """
        _pg_prewarm_relations()
        client = app.test_client()
        warm_paths = [
            "/api/points-bulk?meta=1",
            "/api/points-bulk",
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
        print(f"[prewarm leader] starting ({len(warm_paths)} queries)")
        for p in warm_paths:
            t0 = time.perf_counter()
            try:
                r = client.get(p)
                print(f"[prewarm leader] {r.status_code}  {(time.perf_counter()-t0)*1000:6.0f}ms  {p[:60]}")
            except Exception as e:
                print(f"[prewarm leader] FAIL {p}: {e}")
        print("[prewarm leader] done")

    def _run_follower_warm():
        """Light prewarm for the non-leader worker.

        Skips pg_prewarm entirely (the leader already warmed the
        shared buffer cache on the Postgres side). Hits only the
        paths whose results are cached PER-PROCESS in this Python
        worker's memory — mainly /api/points-bulk, whose 4 MB
        gzipped buffer lives in this worker's @lru_cache and is
        not shared with the leader worker. Fast because the DB
        is already hot from the leader's pass.
        """
        client = app.test_client()
        warm_paths = [
            "/api/points-bulk?meta=1",
            "/api/points-bulk",
            "/api/stats",
            "/api/timeline",
            "/api/sentiment/overview",
        ]
        print(f"[prewarm follower] starting ({len(warm_paths)} queries)")
        for p in warm_paths:
            t0 = time.perf_counter()
            try:
                r = client.get(p)
                print(f"[prewarm follower] {r.status_code}  {(time.perf_counter()-t0)*1000:6.0f}ms  {p[:60]}")
            except Exception as e:
                print(f"[prewarm follower] FAIL {p}: {e}")
        print("[prewarm follower] done")

    def _warm():
        try:
            time.sleep(1)  # let the worker finish starting
            is_leader = _acquire_leader_lock()
            if is_leader:
                print(f"[prewarm] this worker ({os.getpid()}) is LEADER")
                try:
                    _run_leader_warm()
                finally:
                    _release_leader_lock()
            else:
                print(f"[prewarm] this worker ({os.getpid()}) is FOLLOWER; waiting for leader")
                if _wait_for_leader():
                    print("[prewarm] leader finished; follower starting")
                    _run_follower_warm()
                else:
                    print(f"[prewarm] follower gave up after {FOLLOWER_WAIT_SECS}s; running fallback leader prewarm")
                    _run_leader_warm()
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
