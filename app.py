"""
UFO Explorer — Lightweight web GUI for the unified UFO sightings database.
Run:  python app.py
Then: http://localhost:5000
"""
import sqlite3
import os
import json
import shutil
import urllib.request
from flask import Flask, request, jsonify, send_from_directory
from flask_caching import Cache

app = Flask(__name__, static_folder="static")

# In-process LRU cache for expensive query responses. Per-worker
# (not shared across gunicorn workers), keyed on the full query
# string. 5-minute default TTL.
cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_THRESHOLD": 500,  # max number of cached items per worker
})

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ufo_unified.db"),
)


def get_db():
    """Get a read-only database connection.

    PRAGMA tuning is critical on Azure App Service Linux where the
    DB lives on the /home Azure Files mount — without mmap + a real
    page cache, every query pays SMB round-trip latency.
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    # 64 MB SQLite page cache (negative value = KB; this is per connection
    # but the underlying OS page cache is shared, so warm-up persists).
    conn.execute("PRAGMA cache_size = -65536")
    # 256 MB memory-mapped I/O. Once the DB pages are mmap'd, reads
    # bypass the slow Azure Files mount and hit RAM directly.
    conn.execute("PRAGMA mmap_size = 268435456")
    # Keep ORDER BY / GROUP BY scratch in memory, not in temp files.
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


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

    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_where(params, args, filters):
    """Build WHERE clauses from common filters.

    filters is a dict of param_name -> (sql_fragment, value_transform)
    """
    clauses = []
    for param_name, (sql, transform) in filters.items():
        val = params.get(param_name)
        if val:
            clauses.append(sql)
            args.append(transform(val) if transform else val)
    return clauses


def add_common_filters(params, clauses, args, table_prefix="s"):
    """Add common filter clauses shared across endpoints."""
    p = table_prefix

    shape = params.get("shape")
    if shape:
        clauses.append(f"{p}.shape = ?")
        args.append(shape)

    source = params.get("source")
    if source:
        clauses.append(f"{p}.source_db_id = ?")
        args.append(int(source))

    collection = params.get("collection")
    if collection:
        clauses.append(f"{p}.source_db_id IN (SELECT id FROM source_database WHERE collection_id = ?)")
        args.append(int(collection))

    hynek = params.get("hynek")
    if hynek:
        clauses.append(f"{p}.hynek = ?")
        args.append(hynek)

    vallee = params.get("vallee")
    if vallee:
        clauses.append(f"{p}.vallee = ?")
        args.append(vallee)

    date_from = params.get("date_from")
    if date_from:
        clauses.append(f"{p}.date_event >= ?")
        args.append(date_from)

    date_to = params.get("date_to")
    if date_to:
        clauses.append(f"{p}.date_event <= ?")
        args.append(date_to + "-12-31" if len(date_to) == 4 else date_to)

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
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    """Health check for Railway. Returns 200 even without DB so deploys succeed."""
    if not os.path.exists(DB_PATH):
        return jsonify({"status": "waiting", "detail": "Database not yet uploaded to volume"})
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sighting")
        count = cur.fetchone()[0]
        conn.close()
        return jsonify({"status": "ok", "sightings": count})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/setup-db")
def setup_db():
    """TEMPORARY: Download rebuilt DB from Google Drive to volume. Remove after use."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        # Also remove WAL/SHM files
        for ext in ("-wal", "-shm"):
            p = DB_PATH + ext
            if os.path.exists(p):
                os.remove(p)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    gdrive_id = "11pOBDZIyl-aXB7BEX_Pn4F1ObE_1pj5_"
    url = f"https://drive.usercontent.google.com/download?id={gdrive_id}&export=download&confirm=t"
    tmp = DB_PATH + ".tmp"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        shutil.move(tmp, DB_PATH)
        size = os.path.getsize(DB_PATH) / (1024 * 1024)
        init_filters()
        return jsonify({"status": "downloaded", "size_mb": round(size, 1)})
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return jsonify({"status": "error", "detail": str(e)}), 500


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
        GROUP BY sd.id ORDER BY COUNT(s.id) DESC
    """)
    by_source = [{"name": r[0], "count": r[1], "collection": r[2]} for r in cur.fetchall()]

    cur.execute("""
        SELECT sc.name, COUNT(s.id)
        FROM source_collection sc
        JOIN source_database sd ON sd.collection_id = sc.id
        LEFT JOIN sighting s ON s.source_db_id = sd.id
        GROUP BY sc.id ORDER BY COUNT(s.id) DESC
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

    Both modes are deterministic (same input → same output) so the
    flask-caching layer above gets clean cache hits. Default cap is
    25,000 markers; ?limit= overrides up to 100,000 for "Load All".
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
        clauses.append("l.latitude BETWEEN ? AND ?")
        args.extend([south_f, north_f])
        clauses.append("l.longitude BETWEEN ? AND ?")
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

    # Decide sampling strategy from explicit zoom (preferred) or
    # bbox area as a fallback. Zoom 0 = whole world, ~7 = continent,
    # 10+ = city. Threshold: zoom <= 7 → hash sample; >= 8 → grid.
    zoom_param = request.args.get("zoom")
    if zoom_param is not None:
        try:
            use_grid = int(zoom_param) >= 8
        except (ValueError, TypeError):
            use_grid = False
    else:
        bbox_area = max(0.0001, (north_f - south_f) * (east_f - west_f))
        use_grid = bbox_area < 100.0  # < ~10° on a side

    if use_grid:
        # 50x50 grid of the visible bbox, up to 10 most-recent samples per
        # cell. Worst case 25k markers; usually far fewer because most
        # cells are empty. Window function is fine on SQLite >= 3.25.
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
                               CAST((l.latitude  - ?) / ? AS INTEGER),
                               CAST((l.longitude - ?) / ? AS INTEGER)
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
            WHERE rn <= ?
            LIMIT ?
        """
        cur.execute(
            sql,
            [south_f, lat_step, west_f, lng_step] + args + [K_PER_CELL, req_limit],
        )
    else:
        # Hash-based pseudo-random sample. ORDER BY ((id * large prime) % M)
        # gives a deterministic shuffle over the bbox-filtered set; LIMIT
        # then takes a representative slice. Same input = same output, so
        # the response cache works perfectly.
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
            ORDER BY ((s.id * 2654435761) % 1000000)
            LIMIT ?
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

    Returns [lat, lng] pairs. Default 50k limit, supports ?limit= for Load All.
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
        clauses.append("l.latitude BETWEEN ? AND ?")
        args.extend([float(south), float(north)])
        clauses.append("l.longitude BETWEEN ? AND ?")
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
        LIMIT {req_limit}
    """

    cur.execute(sql, args)
    points = [[r[0], r[1]] for r in cur.fetchall()]

    # Only run the COUNT(*) when we hit the limit; otherwise the total
    # in view IS the number of points we just fetched. Saves a full
    # scan on the common case.
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
        clauses.append("SUBSTR(s.date_event, 1, 4) = ?")
        args.append(year)
        where = " AND ".join(clauses)

        sql = f"""
            SELECT SUBSTR(s.date_event, 1, 7) as period,
                   sd.name as source_name,
                   COUNT(*) as cnt
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
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
def api_search():
    """Search sightings by text and filters."""
    conn = get_db()
    cur = conn.cursor()

    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 0))
    per_page = 50
    offset = page * per_page

    clauses = []
    args = []

    if q:
        # Search in description and summary
        clauses.append("(s.description LIKE ? OR s.summary LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like])

    add_common_filters(request.args, clauses, args)

    where = " AND ".join(clauses) if clauses else "1=1"

    # Count total
    count_sql = f"""
        SELECT COUNT(*) FROM sighting s WHERE {where}
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
        LIMIT ? OFFSET ?
    """
    args.extend([per_page, offset])
    cur.execute(sql, args)

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
            WHERE s.id = ?
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
                WHEN dc.sighting_id_a = ? THEN dc.sighting_id_b
                ELSE dc.sighting_id_a END
            JOIN source_database sd2 ON s2.source_db_id = sd2.id
            LEFT JOIN location l2 ON s2.location_id = l2.id
            WHERE dc.sighting_id_a = ? OR dc.sighting_id_b = ?
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
            cur.execute("SELECT name FROM source_origin WHERE id = ?", (record["origin_id"],))
            origin = cur.fetchone()
            if origin:
                record["origin_name"] = origin[0]

        # Get collection name
        if record.get("source_db_id"):
            cur.execute("""
                SELECT sc.name FROM source_collection sc
                JOIN source_database sd ON sd.collection_id = sc.id
                WHERE sd.id = ?
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
            FROM sentiment_analysis WHERE sighting_id = ?
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

    where = " AND ".join(clauses) if clauses else "1=1"

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

    where = " AND ".join(clauses) if clauses else "1=1"

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
        clauses.append("dc.similarity_score >= ?")
        args.append(float(min_score))

    max_score = request.args.get("max_score")
    if max_score:
        clauses.append("dc.similarity_score < ?")
        args.append(float(max_score))

    # Match method filter
    method = request.args.get("method")
    if method:
        clauses.append("dc.match_method = ?")
        args.append(method)

    # Source filter — either sighting must be from this source
    source = request.args.get("source")
    if source:
        clauses.append("(sa.source_db_id = ? OR sb.source_db_id = ?)")
        args.extend([int(source), int(source)])

    where = " AND ".join(clauses) if clauses else "1=1"

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
        LIMIT ? OFFSET ?
    """
    args.extend([per_page, offset])
    cur.execute(sql, args)

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
    print(f"Database: {DB_PATH}")
    if os.path.exists(DB_PATH):
        print(f"Size: {os.path.getsize(DB_PATH) / (1024*1024):.1f} MB")
    else:
        print("WARNING: Database file not found — /health will fail until DB is available")
        return
    print("Loading filter values...")
    init_filters()
    print(f"  {len(FILTER_CACHE.get('shapes', []))} shapes, "
          f"{len(FILTER_CACHE.get('hynek', []))} hynek codes, "
          f"{len(FILTER_CACHE.get('sources', []))} sources")


_init_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\nStarting server at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
