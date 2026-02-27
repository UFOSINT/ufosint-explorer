"""
UFO Explorer — Lightweight web GUI for the unified UFO sightings database.
Run:  python app.py
Then: http://localhost:5000
"""
import sqlite3
import os
import json
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ufo_unified.db"),
)


def get_db():
    """Get a read-only database connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
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

    return clauses, args


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    """Health check for Railway."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sighting")
        count = cur.fetchone()[0]
        conn.close()
        return jsonify({"status": "ok", "sightings": count})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/stats")
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

    cur.execute("SELECT COUNT(*) FROM duplicate_candidate")
    dupes = cur.fetchone()[0]

    conn.close()

    return jsonify({
        "total_sightings": total,
        "by_source": by_source,
        "by_collection": by_collection,
        "date_range": {"min": date_min, "max": date_max},
        "geocoded_locations": geocoded,
        "duplicate_candidates": dupes,
    })


@app.route("/api/filters")
def api_filters():
    """Return cached filter options."""
    return jsonify(FILTER_CACHE)


@app.route("/api/map")
def api_map():
    """Map markers with bbox and filters. Default 5000, supports ?limit= for Load All."""
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

    # Total count in view (for "X of Y" display)
    cur.execute(f"""
        SELECT COUNT(*) FROM sighting s
        JOIN location l ON s.location_id = l.id
        WHERE {where}
    """, args)
    total_in_view = cur.fetchone()[0]

    # Configurable limit (default 5000, max 100000)
    req_limit = request.args.get("limit", 5000)
    try:
        req_limit = min(int(req_limit), 100000)
    except (ValueError, TypeError):
        req_limit = 5000

    sql = f"""
        SELECT s.id, l.latitude, l.longitude,
               s.date_event, s.shape, sd.name as source_name,
               COALESCE(l.city, '') as city,
               COALESCE(l.state, '') as state,
               COALESCE(l.country, '') as country,
               COALESCE(sc.name, '') as collection
        FROM sighting s
        JOIN location l ON s.location_id = l.id
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN source_collection sc ON sd.collection_id = sc.id
        WHERE {where}
        ORDER BY s.date_event DESC
        LIMIT {req_limit}
    """

    cur.execute(sql, args)
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

    conn.close()
    return jsonify({
        "markers": markers,
        "count": len(markers),
        "total_in_view": total_in_view,
    })


@app.route("/api/heatmap")
def api_heatmap():
    """Lightweight coordinate-only endpoint for heatmap rendering.

    Returns [lat, lng] pairs. Default 50k limit, supports ?limit= for Load All.
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

    # Total count in view
    cur.execute(f"""
        SELECT COUNT(*) FROM sighting s
        JOIN location l ON s.location_id = l.id
        WHERE {where}
    """, args)
    total_in_view = cur.fetchone()[0]

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

    conn.close()
    return jsonify({
        "points": points,
        "count": len(points),
        "total_in_view": total_in_view,
    })


@app.route("/api/timeline")
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
        # Yearly breakdown
        clauses.append("CAST(SUBSTR(s.date_event, 1, 4) AS INTEGER) >= 1900")
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

        return jsonify(record)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/duplicates")
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
