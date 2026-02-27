"""
UFO Database MCP Server — AI connector for the unified UFO sightings database.

Exposes read-only tools so Claude Desktop (or any MCP client) can query,
search, and analyze 614k+ UFO sighting records across 5 source databases.

Run directly:    python mcp_server.py
Claude Desktop:  Add to claude_desktop_config.json (see --help)
"""

import sqlite3
import os
import json
import re
from typing import Optional
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ufo_unified.db")
MAX_ROWS = 500          # Hard cap on rows returned per query
DEFAULT_ROWS = 100      # Default row limit
DESC_TRUNCATE = 500     # Max chars for description fields in list results

# SQL keywords that indicate write operations — blocked for safety
WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|REINDEX|VACUUM|PRAGMA)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Database connection (read-only, same pattern as app.py)
# ---------------------------------------------------------------------------

def get_db():
    """Get a read-only database connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def rows_to_dicts(cursor, rows):
    """Convert sqlite3.Row results to a list of dicts."""
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "ufo-database",
    instructions=(
        "You are connected to a unified UFO sighting database containing 614,505 "
        "records from 5 sources (NUFORC, MUFON, UFOCAT, UPDB, UFO-search). "
        "Use get_schema first to understand the tables, then run_query for analysis. "
        "The database also contains 126,730 duplicate candidate pairs with similarity scores. "
        "All access is read-only."
    ),
)


# ---------------------------------------------------------------------------
# Tool 1: get_schema
# ---------------------------------------------------------------------------

@mcp.tool()
def get_schema() -> str:
    """Get the complete database schema — all tables, their columns, types, and row counts.

    Call this first to understand what data is available before writing queries.
    The main table is 'sighting' with 614k+ records. Key related tables include
    'location' (geocoded places), 'source_database' (the 5 data sources),
    'source_origin' (sub-sources within aggregators), and 'duplicate_candidate'
    (flagged duplicate pairs with similarity scores).
    """
    conn = get_db()
    cur = conn.cursor()

    # Get all tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]

    schema = {}
    for table in tables:
        # Column info
        cur.execute(f"PRAGMA table_info({table})")
        columns = []
        for col in cur.fetchall():
            columns.append({
                "name": col[1],
                "type": col[2],
                "nullable": not col[3],
                "primary_key": bool(col[5]),
            })

        # Row count
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]

        schema[table] = {"columns": columns, "row_count": count}

    # Also include indexes
    cur.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
    indexes = [{"name": r[0], "table": r[1], "sql": r[2]} for r in cur.fetchall()]

    conn.close()
    return json.dumps({"tables": schema, "indexes": indexes}, indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_stats
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> str:
    """Get a quick overview of the database — total sightings, breakdown by source,
    date range, geocoded count, duplicate count, top shapes, and top countries.

    Good for orienting yourself before diving into specific queries.
    """
    conn = get_db()
    cur = conn.cursor()

    result = {}

    # Total sightings
    cur.execute("SELECT COUNT(*) FROM sighting")
    result["total_sightings"] = cur.fetchone()[0]

    # By source
    cur.execute("""
        SELECT sd.name, COUNT(s.id) as cnt
        FROM source_database sd
        LEFT JOIN sighting s ON s.source_db_id = sd.id
        GROUP BY sd.id ORDER BY cnt DESC
    """)
    result["by_source"] = {r[0]: r[1] for r in cur.fetchall()}

    # Date range
    cur.execute("""
        SELECT MIN(date_event), MAX(date_event)
        FROM sighting WHERE date_event IS NOT NULL
    """)
    row = cur.fetchone()
    result["date_range"] = {"earliest": row[0], "latest": row[1]}

    # Geocoded locations
    cur.execute("""
        SELECT COUNT(*) FROM location
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    result["geocoded_locations"] = cur.fetchone()[0]

    # Duplicates
    cur.execute("SELECT COUNT(*) FROM duplicate_candidate")
    result["duplicate_pairs"] = cur.fetchone()[0]

    # Top 15 shapes
    cur.execute("""
        SELECT shape, COUNT(*) as cnt FROM sighting
        WHERE shape IS NOT NULL AND shape != ''
        GROUP BY shape ORDER BY cnt DESC LIMIT 15
    """)
    result["top_shapes"] = {r[0]: r[1] for r in cur.fetchall()}

    # Top 15 countries
    cur.execute("""
        SELECT l.country, COUNT(*) as cnt
        FROM sighting s
        JOIN location l ON s.location_id = l.id
        WHERE l.country IS NOT NULL AND l.country != ''
        GROUP BY l.country ORDER BY cnt DESC LIMIT 15
    """)
    result["top_countries"] = {r[0]: r[1] for r in cur.fetchall()}

    # Hynek classification distribution
    cur.execute("""
        SELECT hynek, COUNT(*) as cnt FROM sighting
        WHERE hynek IS NOT NULL AND hynek != ''
        GROUP BY hynek ORDER BY cnt DESC
    """)
    result["hynek_distribution"] = {r[0]: r[1] for r in cur.fetchall()}

    # Vallee classification distribution
    cur.execute("""
        SELECT vallee, COUNT(*) as cnt FROM sighting
        WHERE vallee IS NOT NULL AND vallee != ''
        GROUP BY vallee ORDER BY cnt DESC LIMIT 20
    """)
    result["vallee_distribution"] = {r[0]: r[1] for r in cur.fetchall()}

    conn.close()
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool 3: run_query
# ---------------------------------------------------------------------------

@mcp.tool()
def run_query(sql: str, limit: int = DEFAULT_ROWS) -> str:
    """Execute a read-only SQL query against the UFO database.

    This is the main analytical tool. You can write any SELECT query to explore
    the data. The database uses SQLite syntax.

    Args:
        sql: A SELECT query to execute. Must be read-only (no INSERT/UPDATE/DELETE).
             Tip: JOIN sighting s with location l ON s.location_id = l.id for geo data,
             and source_database sd ON s.source_db_id = sd.id for source names.
        limit: Max rows to return (default 100, max 500). Applied automatically
               if your query has no LIMIT clause.

    Returns:
        JSON with 'columns', 'rows' (list of lists), 'row_count', and 'truncated' flag.

    Example queries:
        - "SELECT COUNT(*) as cnt, shape FROM sighting GROUP BY shape ORDER BY cnt DESC LIMIT 20"
        - "SELECT s.date_event, s.shape, l.city, l.state, l.country FROM sighting s JOIN location l ON s.location_id = l.id WHERE s.hynek = 'CE3' ORDER BY s.date_event DESC LIMIT 50"
        - "SELECT SUBSTR(s.date_event,1,4) as year, COUNT(*) as cnt FROM sighting s GROUP BY year ORDER BY year"
    """
    # Safety: block write operations
    if WRITE_KEYWORDS.search(sql):
        return json.dumps({
            "error": "Write operations are not allowed. This is a read-only database. "
                     "Only SELECT queries are permitted."
        })

    # Enforce row limit
    effective_limit = min(limit, MAX_ROWS)
    # If query doesn't already have a LIMIT, add one
    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql = sql.rstrip().rstrip(";") + f" LIMIT {effective_limit}"

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql)

        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchmany(effective_limit + 1)  # Fetch one extra to detect truncation

        truncated = len(rows) > effective_limit
        if truncated:
            rows = rows[:effective_limit]

        # Convert Row objects to plain lists
        data = [list(row) for row in rows]

        return json.dumps({
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
            "limit_applied": effective_limit,
        }, default=str)

    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 4: search_sightings
# ---------------------------------------------------------------------------

@mcp.tool()
def search_sightings(
    text: Optional[str] = None,
    shape: Optional[str] = None,
    source: Optional[str] = None,
    hynek: Optional[str] = None,
    vallee: Optional[str] = None,
    country: Optional[str] = None,
    state: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Search UFO sightings with text and/or filters.

    A convenience wrapper so you don't need to write SQL for common searches.
    All parameters are optional — combine as needed.

    Args:
        text: Free-text search in description and summary fields.
        shape: Filter by shape (e.g. 'circle', 'triangle', 'light', 'disk').
        source: Filter by source database name (NUFORC, MUFON, UFOCAT, UPDB, UFO-search).
        hynek: Filter by Hynek classification (CE1, CE2, CE3, DD, NL, etc.).
        vallee: Filter by Vallee classification (MA1, CE1, FB1, etc.).
        country: Filter by country (US, GB, CA, AU, etc.).
        state: Filter by state/province.
        date_from: Start date (ISO format, e.g. '1990-01-01' or just '1990').
        date_to: End date (ISO format, e.g. '2020-12-31' or just '2020').
        limit: Max results (default 50, max 500).

    Returns:
        JSON with matching sightings and total count.
    """
    conn = get_db()
    cur = conn.cursor()

    clauses = []
    args = []

    if text:
        clauses.append("(s.description LIKE ? OR s.summary LIKE ?)")
        like = f"%{text}%"
        args.extend([like, like])

    if shape:
        clauses.append("s.shape = ?")
        args.append(shape)

    if source:
        clauses.append("sd.name = ?")
        args.append(source)

    if hynek:
        clauses.append("s.hynek = ?")
        args.append(hynek)

    if vallee:
        clauses.append("s.vallee = ?")
        args.append(vallee)

    if country:
        clauses.append("l.country = ?")
        args.append(country)

    if state:
        clauses.append("l.state = ?")
        args.append(state)

    if date_from:
        clauses.append("s.date_event >= ?")
        args.append(date_from)

    if date_to:
        end = date_to + "-12-31" if len(date_to) == 4 else date_to
        clauses.append("s.date_event <= ?")
        args.append(end)

    where = " AND ".join(clauses) if clauses else "1=1"
    effective_limit = min(limit, MAX_ROWS)

    # Count
    count_sql = f"""
        SELECT COUNT(*) FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
    """
    cur.execute(count_sql, args)
    total = cur.fetchone()[0]

    # Fetch
    sql = f"""
        SELECT s.id, s.date_event, s.shape, sd.name as source,
               COALESCE(l.city, '') as city,
               COALESCE(l.state, '') as state,
               COALESCE(l.country, '') as country,
               l.latitude, l.longitude,
               s.hynek, s.vallee, s.num_witnesses, s.duration,
               SUBSTR(COALESCE(s.description, s.summary, ''), 1, {DESC_TRUNCATE}) as description
        FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        ORDER BY s.date_event DESC
        LIMIT ?
    """
    args.append(effective_limit)
    cur.execute(sql, args)
    results = rows_to_dicts(cur, cur.fetchall())

    conn.close()
    return json.dumps({
        "results": results,
        "total": total,
        "returned": len(results),
    }, default=str, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: get_sighting
# ---------------------------------------------------------------------------

@mcp.tool()
def get_sighting(sighting_id: int) -> str:
    """Get the full detail for a single UFO sighting by its database ID.

    Returns all available fields including the complete description, location,
    classification, witness info, and any duplicate candidate matches.

    Args:
        sighting_id: The sighting ID (integer primary key).
    """
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT s.*, sd.name as source_name,
                   l.raw_text as location_raw, l.city, l.county, l.state,
                   l.country, l.region, l.latitude, l.longitude
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN location l ON s.location_id = l.id
            WHERE s.id = ?
        """, (sighting_id,))

        row = cur.fetchone()
        if not row:
            return json.dumps({"error": f"Sighting {sighting_id} not found"})

        # Convert to dict, keeping non-empty values
        keys = [desc[0] for desc in cur.description]
        record = {}
        for k, v in zip(keys, row):
            if v is not None and v != "":
                record[k] = v

        # Parse raw_json
        if "raw_json" in record:
            try:
                record["raw_json"] = json.loads(record["raw_json"])
            except (json.JSONDecodeError, TypeError):
                record["raw_json"] = "(parse error)"

        # Get origin name
        if record.get("origin_id"):
            cur.execute("SELECT name FROM source_origin WHERE id = ?", (record["origin_id"],))
            origin = cur.fetchone()
            if origin:
                record["origin_name"] = origin[0]

        # Get duplicate candidates
        cur.execute("""
            SELECT dc.sighting_id_a, dc.sighting_id_b,
                   dc.similarity_score, dc.match_method, dc.status,
                   s2.date_event, sd2.name as other_source,
                   COALESCE(l2.city, '') as other_city,
                   COALESCE(l2.state, '') as other_state,
                   SUBSTR(COALESCE(s2.description, s2.summary, ''), 1, 200) as other_desc
            FROM duplicate_candidate dc
            JOIN sighting s2 ON s2.id = CASE
                WHEN dc.sighting_id_a = ? THEN dc.sighting_id_b
                ELSE dc.sighting_id_a END
            JOIN source_database sd2 ON s2.source_db_id = sd2.id
            LEFT JOIN location l2 ON s2.location_id = l2.id
            WHERE dc.sighting_id_a = ? OR dc.sighting_id_b = ?
            ORDER BY dc.similarity_score DESC
            LIMIT 10
        """, (sighting_id, sighting_id, sighting_id))

        duplicates = []
        for r in cur.fetchall():
            other_id = r[1] if r[0] == sighting_id else r[0]
            duplicates.append({
                "other_sighting_id": other_id,
                "score": round(r[2], 3) if r[2] else None,
                "method": r[3],
                "status": r[4],
                "date": r[5],
                "source": r[6],
                "city": r[7],
                "state": r[8],
                "description_preview": r[9],
            })

        if duplicates:
            record["duplicate_candidates"] = duplicates

        return json.dumps(record, default=str, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 6: get_duplicates
# ---------------------------------------------------------------------------

@mcp.tool()
def get_duplicates(
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    match_method: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Query duplicate candidate pairs in the database.

    The deduplication pipeline flagged 126,730 cross-source duplicate pairs with
    similarity scores. Use this to analyze dedup quality or find specific matches.

    Args:
        min_score: Minimum similarity score (0.0-1.0). E.g., 0.9 for high-confidence.
        max_score: Maximum similarity score. E.g., 0.5 for low-confidence pairs.
        match_method: Filter by match method. Common values:
            tier1_mufon_nuforc, tier2a_date_city_state,
            tier2b_nuforc_ufocat, tier2b_mufon_ufocat,
            tier2c_updb_ufocat, tier2c_updb_nuforc, tier2c_updb_mufon,
            tier2d_ufosearch, tier3_desc_similarity
        source: Filter by source database name — either sighting in the pair
                must be from this source.
        limit: Max pairs to return (default 50, max 500).

    Returns:
        JSON with duplicate pairs showing both sightings side-by-side.
    """
    conn = get_db()
    cur = conn.cursor()

    clauses = []
    args = []

    if min_score is not None:
        clauses.append("dc.similarity_score >= ?")
        args.append(min_score)

    if max_score is not None:
        clauses.append("dc.similarity_score < ?")
        args.append(max_score)

    if match_method:
        clauses.append("dc.match_method = ?")
        args.append(match_method)

    if source:
        clauses.append("(sda.name = ? OR sdb.name = ?)")
        args.extend([source, source])

    where = " AND ".join(clauses) if clauses else "1=1"
    effective_limit = min(limit, MAX_ROWS)

    # Count
    count_sql = f"""
        SELECT COUNT(*) FROM duplicate_candidate dc
        JOIN sighting sa ON dc.sighting_id_a = sa.id
        JOIN sighting sb ON dc.sighting_id_b = sb.id
        JOIN source_database sda ON sa.source_db_id = sda.id
        JOIN source_database sdb ON sb.source_db_id = sdb.id
        WHERE {where}
    """
    cur.execute(count_sql, args)
    total = cur.fetchone()[0]

    # Fetch
    sql = f"""
        SELECT dc.id, dc.similarity_score, dc.match_method,
               dc.sighting_id_a, sda.name as source_a, sa.date_event as date_a,
               COALESCE(la.city, '') as city_a, COALESCE(la.state, '') as state_a,
               COALESCE(la.country, '') as country_a,
               SUBSTR(COALESCE(sa.description, sa.summary, ''), 1, 200) as desc_a,
               sa.shape as shape_a,
               dc.sighting_id_b, sdb.name as source_b, sb.date_event as date_b,
               COALESCE(lb.city, '') as city_b, COALESCE(lb.state, '') as state_b,
               COALESCE(lb.country, '') as country_b,
               SUBSTR(COALESCE(sb.description, sb.summary, ''), 1, 200) as desc_b,
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
        LIMIT ?
    """
    args.append(effective_limit)
    cur.execute(sql, args)

    results = []
    for r in cur.fetchall():
        results.append({
            "pair_id": r[0],
            "score": round(r[1], 3) if r[1] is not None else None,
            "method": r[2],
            "sighting_a": {
                "id": r[3], "source": r[4], "date": r[5],
                "city": r[6], "state": r[7], "country": r[8],
                "description": r[9], "shape": r[10],
            },
            "sighting_b": {
                "id": r[11], "source": r[12], "date": r[13],
                "city": r[14], "state": r[15], "country": r[16],
                "description": r[17], "shape": r[18],
            },
        })

    conn.close()
    return json.dumps({
        "results": results,
        "total_matching": total,
        "returned": len(results),
    }, default=str, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Verify database exists before starting
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Make sure ufo_unified.db is in the ufo-explorer directory.")
        exit(1)

    print(f"UFO Database MCP Server")
    print(f"Database: {DB_PATH}")
    print(f"Size: {os.path.getsize(DB_PATH) / (1024*1024):.1f} MB")
    print(f"Tools: get_schema, get_stats, run_query, search_sightings, get_sighting, get_duplicates")
    print(f"Starting on stdio transport...")

    mcp.run()
