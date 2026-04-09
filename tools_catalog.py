"""
Tool catalog for AI assistants — single source of truth.

Defines the read-only tools that AI clients can call against the
unified UFO sightings database. Three callers consume this:

1. The MCP-over-HTTP Flask blueprint at /mcp (any MCP client:
   Claude Desktop, Cursor, Cline, Continue, Windsurf, etc).
2. The /api/tools-catalog endpoint that returns OpenAI-compatible
   tool definitions, consumed by the browser-side BYOK chat UI.
3. The stdio mcp_server.py for local Claude-Desktop-style configs.

Each tool is a dict with:
    name        — snake_case identifier
    description — what it does (the LLM reads this to decide when to call)
    parameters  — JSON Schema for the arguments (OpenAI / MCP compatible)
    handler     — Python function that does the actual work; receives
                  validated kwargs and returns a JSON-serializable result

The handlers connect to PostgreSQL via the same psycopg connection
pool the Flask app uses (imported lazily to avoid circular imports).
"""
from typing import Any


# ---------------------------------------------------------------------------
# Database access — borrows the pool from app.py to keep one connection
# pool process-wide. Imported lazily so this module can be loaded by tools
# that don't need the database (e.g. tests).
# ---------------------------------------------------------------------------

def _get_db():
    """Lazily import the pooled connection from app.py."""
    from app import get_db  # noqa: WPS433 — intentional lazy import
    return get_db()


def _rows_to_dicts(cur, rows):
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Hard cap on rows we'll ever return to a tool caller, regardless of what
# the LLM asks for. Protects PG and the network.
ABS_MAX_ROWS = 200
DEFAULT_ROWS = 25
DESC_TRUNCATE = 600


def _trunc(s, n=DESC_TRUNCATE):
    if s is None:
        return None
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def _clamp(n, lo, hi, default):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# ---------------------------------------------------------------------------
# Tool 1: search_sightings
# ---------------------------------------------------------------------------

def search_sightings(
    q: str | None = None,
    shape: str | None = None,
    source: str | None = None,
    state: str | None = None,
    country: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    hynek: str | None = None,
    limit: int = DEFAULT_ROWS,
):
    """Free-text + filter search across the sighting table."""
    limit = _clamp(limit, 1, ABS_MAX_ROWS, DEFAULT_ROWS)
    clauses = []
    args: list[Any] = []

    if q:
        clauses.append("(s.description ILIKE %s OR s.summary ILIKE %s)")
        args.extend([f"%{q}%", f"%{q}%"])
    if shape:
        clauses.append("s.shape ILIKE %s")
        args.append(shape)
    if source:
        clauses.append("sd.name ILIKE %s")
        args.append(source)
    if state:
        clauses.append("l.state ILIKE %s")
        args.append(state)
    if country:
        clauses.append("l.country ILIKE %s")
        args.append(country)
    if hynek:
        clauses.append("s.hynek = %s")
        args.append(hynek)
    if date_from:
        clauses.append("s.date_event >= %s")
        args.append(date_from)
    if date_to:
        clauses.append("s.date_event <= %s")
        args.append(date_to + "-12-31" if len(date_to) == 4 else date_to)

    where = " AND ".join(clauses) if clauses else "TRUE"

    sql = f"""
        SELECT s.id, s.date_event, s.shape, s.hynek, s.num_witnesses,
               s.duration,
               COALESCE(LEFT(s.description, {DESC_TRUNCATE}), '') AS description,
               sd.name AS source,
               COALESCE(l.city, '') AS city,
               COALESCE(l.state, '') AS state,
               COALESCE(l.country, '') AS country,
               l.latitude, l.longitude
        FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        ORDER BY s.date_event DESC NULLS LAST
        LIMIT %s
    """
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args + [limit])
        results = _rows_to_dicts(cur, cur.fetchall())

        # Cheap total — only run when the page is full (same trick app.py uses)
        if len(results) < limit:
            total = len(results)
        else:
            cur.execute(
                f"SELECT COUNT(*) FROM sighting s "
                f"JOIN source_database sd ON s.source_db_id = sd.id "
                f"LEFT JOIN location l ON s.location_id = l.id WHERE {where}",
                args,
            )
            total = cur.fetchone()[0]
    finally:
        conn.close()

    return {"total": total, "returned": len(results), "results": results}


# ---------------------------------------------------------------------------
# Tool 2: get_sighting
# ---------------------------------------------------------------------------

def get_sighting(sighting_id: int):
    """Full record for a single sighting by id."""
    sighting_id = int(sighting_id)
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, sd.name AS source_name,
                   l.raw_text AS loc_raw, l.city, l.county, l.state, l.country,
                   l.region, l.latitude, l.longitude
            FROM sighting s
            JOIN source_database sd ON s.source_db_id = sd.id
            LEFT JOIN location l ON s.location_id = l.id
            WHERE s.id = %s
        """, (sighting_id,))
        row = cur.fetchone()
        if not row:
            return {"error": f"sighting {sighting_id} not found"}
        cols = [d[0] for d in cur.description]
        record = {k: v for k, v in zip(cols, row, strict=False) if v is not None and v != ""}
        # Truncate the long fields so we don't blow the LLM's context
        record["description"] = _trunc(record.get("description"))
        record["summary"] = _trunc(record.get("summary"))
        record.pop("raw_json", None)  # huge, low-signal
    finally:
        conn.close()
    return record


# ---------------------------------------------------------------------------
# Tool 3: get_stats
# ---------------------------------------------------------------------------

def get_stats():
    """Top-level database stats: row counts by source, by collection, date range."""
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sighting")
        total = cur.fetchone()[0]
        cur.execute("""
            SELECT sd.name, COUNT(s.id), COALESCE(sc.name, 'Unknown') AS collection
            FROM source_database sd
            LEFT JOIN source_collection sc ON sd.collection_id = sc.id
            LEFT JOIN sighting s ON s.source_db_id = sd.id
            GROUP BY sd.id, sd.name, sc.name
            ORDER BY COUNT(s.id) DESC
        """)
        by_source = [{"source": r[0], "count": r[1], "collection": r[2]} for r in cur.fetchall()]
        cur.execute("""
            SELECT MIN(date_event), MAX(date_event)
            FROM sighting WHERE date_event IS NOT NULL
        """)
        dmin, dmax = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM location WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
        geocoded = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM duplicate_candidate")
        dupes = cur.fetchone()[0]
    finally:
        conn.close()
    return {
        "total_sightings": total,
        "by_source": by_source,
        "date_range": {"min": dmin, "max": dmax},
        "geocoded_locations": geocoded,
        "duplicate_pairs": dupes,
    }


# ---------------------------------------------------------------------------
# Tool 4: get_timeline
# ---------------------------------------------------------------------------

def get_timeline(year: int | None = None,
                 source: str | None = None,
                 shape: str | None = None):
    """Sighting counts by year (default) or by month if a year is given."""
    clauses = ["s.date_event IS NOT NULL", "LENGTH(s.date_event) >= 4"]
    args: list[Any] = []
    if source:
        clauses.append("sd.name ILIKE %s")
        args.append(source)
    if shape:
        clauses.append("s.shape ILIKE %s")
        args.append(shape)
    if year:
        clauses.append("SUBSTR(s.date_event, 1, 4) = %s")
        args.append(str(int(year)))
        clauses.append("LENGTH(s.date_event) >= 7")
        period_expr = "SUBSTR(s.date_event, 1, 7)"
    else:
        period_expr = "SUBSTR(s.date_event, 1, 4)"

    where = " AND ".join(clauses)
    sql = f"""
        SELECT {period_expr} AS period, COUNT(*) AS count
        FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        WHERE {where}
        GROUP BY period
        ORDER BY period
    """
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = [{"period": r[0], "count": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()
    return {
        "mode": "monthly" if year else "yearly",
        "year": year,
        "filters": {k: v for k, v in {"source": source, "shape": shape}.items() if v},
        "buckets": rows,
    }


# ---------------------------------------------------------------------------
# Tool 5: find_duplicates_for
# ---------------------------------------------------------------------------

def find_duplicates_for(sighting_id: int, limit: int = 10):
    """Duplicate-candidate pairs flagged for a given sighting."""
    sighting_id = int(sighting_id)
    limit = _clamp(limit, 1, 50, 10)
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT dc.sighting_id_a, dc.sighting_id_b,
                   dc.similarity_score, dc.match_method, dc.status,
                   s2.date_event, sd2.name AS other_source,
                   COALESCE(l2.city, '') AS other_city,
                   COALESCE(l2.state, '') AS other_state
            FROM duplicate_candidate dc
            JOIN sighting s2 ON s2.id = CASE
                WHEN dc.sighting_id_a = %s THEN dc.sighting_id_b
                ELSE dc.sighting_id_a END
            JOIN source_database sd2 ON s2.source_db_id = sd2.id
            LEFT JOIN location l2 ON s2.location_id = l2.id
            WHERE dc.sighting_id_a = %s OR dc.sighting_id_b = %s
            ORDER BY dc.similarity_score DESC
            LIMIT %s
        """, (sighting_id, sighting_id, sighting_id, limit))
        rows = []
        for r in cur.fetchall():
            other_id = r[1] if r[0] == sighting_id else r[0]
            rows.append({
                "id": other_id,
                "score": round(r[2], 3) if r[2] is not None else None,
                "method": r[3],
                "status": r[4],
                "date": r[5],
                "source": r[6],
                "city": r[7],
                "state": r[8],
            })
    finally:
        conn.close()
    return {"sighting_id": sighting_id, "duplicates": rows}


# ---------------------------------------------------------------------------
# Tool 6: count_by
# ---------------------------------------------------------------------------

def count_by(field: str, limit: int = 25,
             date_from: str | None = None,
             date_to: str | None = None):
    """Aggregate counts grouped by a categorical field.

    Allowed fields: shape, hynek, vallee, source, country, state.
    Use this when the user asks 'what are the most common shapes' etc.
    """
    allowed = {
        "shape":   "s.shape",
        "hynek":   "s.hynek",
        "vallee":  "s.vallee",
        "source":  "sd.name",
        "country": "l.country",
        "state":   "l.state",
    }
    if field not in allowed:
        return {"error": f"field must be one of {sorted(allowed)}"}
    expr = allowed[field]
    limit = _clamp(limit, 1, 100, 25)

    clauses = [f"{expr} IS NOT NULL", f"{expr} != ''"]
    args: list[Any] = []
    if date_from:
        clauses.append("s.date_event >= %s")
        args.append(date_from)
    if date_to:
        clauses.append("s.date_event <= %s")
        args.append(date_to + "-12-31" if len(date_to) == 4 else date_to)
    where = " AND ".join(clauses)

    sql = f"""
        SELECT {expr} AS value, COUNT(*) AS count
        FROM sighting s
        JOIN source_database sd ON s.source_db_id = sd.id
        LEFT JOIN location l ON s.location_id = l.id
        WHERE {where}
        GROUP BY value
        ORDER BY count DESC
        LIMIT %s
    """
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, args + [limit])
        rows = [{"value": r[0], "count": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()
    return {"field": field, "rows": rows}


# ---------------------------------------------------------------------------
# Tool catalog — single source of truth
# ---------------------------------------------------------------------------
# Each entry has the JSON Schema (OpenAI / MCP compatible), a description
# the LLM reads to decide when to call, and the Python handler.

TOOLS = [
    {
        "name": "search_sightings",
        "description": (
            "Search the unified UFO sightings database by free text and/or "
            "structured filters. Use this when the user asks 'find sightings "
            "matching X' or 'what reports came from California in 1973'. "
            "Returns up to 200 records (default 25). "
            "Date params accept either a year (1973) or an ISO date "
            "(1973-10-15)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "q":         {"type": "string", "description": "Free-text substring (case-insensitive) matched against description and summary"},
                "shape":     {"type": "string", "description": "Object shape, e.g. triangle, disk, cigar, sphere"},
                "source":    {"type": "string", "description": "Source database name: NUFORC, MUFON, UFOCAT, UPDB, UFO-search"},
                "state":     {"type": "string", "description": "US state name or 2-letter code (matched ILIKE)"},
                "country":   {"type": "string", "description": "Country name"},
                "date_from": {"type": "string", "description": "Start date (YYYY or YYYY-MM-DD)"},
                "date_to":   {"type": "string", "description": "End date (YYYY or YYYY-MM-DD)"},
                "hynek":     {"type": "string", "description": "Hynek classification, e.g. NL (nocturnal light), CE-I, CE-II, CE-III, DD (daylight disk), RV (radar/visual)"},
                "limit":     {"type": "integer", "description": "Max records (1-200, default 25)", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "handler": search_sightings,
    },
    {
        "name": "get_sighting",
        "description": (
            "Fetch the full record for a single sighting by its database id. "
            "Use this after search_sightings to get more detail on a specific result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sighting_id": {"type": "integer", "description": "The database row id of the sighting"},
            },
            "required": ["sighting_id"],
            "additionalProperties": False,
        },
        "handler": get_sighting,
    },
    {
        "name": "get_stats",
        "description": (
            "Top-level statistics about the database: total sightings, "
            "row counts by source, date range covered, count of geocoded "
            "locations, count of duplicate candidate pairs."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": get_stats,
    },
    {
        "name": "get_timeline",
        "description": (
            "Sighting counts grouped by year (default) or by month (when "
            "a year is supplied). Use this to answer 'how many sightings "
            "in the 1970s' or 'show me the seasonal pattern in 1973'. "
            "Optionally filter by source or shape."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "year":   {"type": "integer", "description": "If set, return monthly counts for this year instead of yearly counts"},
                "source": {"type": "string",  "description": "Restrict to one source database"},
                "shape":  {"type": "string",  "description": "Restrict to one object shape"},
            },
            "additionalProperties": False,
        },
        "handler": get_timeline,
    },
    {
        "name": "find_duplicates_for",
        "description": (
            "Find duplicate-candidate pairs flagged for a given sighting. "
            "Each pair has a similarity score (0-1) and a match method. "
            "Use this when the user wants to know if a specific report "
            "was also captured by another source."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sighting_id": {"type": "integer", "description": "The database row id"},
                "limit":       {"type": "integer", "description": "Max duplicates to return (1-50, default 10)", "minimum": 1, "maximum": 50},
            },
            "required": ["sighting_id"],
            "additionalProperties": False,
        },
        "handler": find_duplicates_for,
    },
    {
        "name": "count_by",
        "description": (
            "Count sightings grouped by a categorical field. Use this for "
            "questions like 'most common shapes', 'top 10 reporting states', "
            "'how many reports per Hynek class'. Allowed fields: shape, "
            "hynek, vallee, source, country, state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field":     {"type": "string", "enum": ["shape", "hynek", "vallee", "source", "country", "state"]},
                "limit":     {"type": "integer", "description": "Max groups to return (1-100, default 25)", "minimum": 1, "maximum": 100},
                "date_from": {"type": "string", "description": "Start date (YYYY or YYYY-MM-DD)"},
                "date_to":   {"type": "string", "description": "End date (YYYY or YYYY-MM-DD)"},
            },
            "required": ["field"],
            "additionalProperties": False,
        },
        "handler": count_by,
    },
]

# Quick lookup by name
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def list_tools_openai():
    """Return tools in OpenAI / OpenRouter function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOLS
    ]


def list_tools_mcp():
    """Return tools in MCP tools/list format."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": t["parameters"],
        }
        for t in TOOLS
    ]


def call_tool(name: str, arguments: dict):
    """Run a tool by name with validated kwargs. Returns the JSON-serializable result."""
    if name not in TOOLS_BY_NAME:
        return {"error": f"unknown tool: {name}"}
    handler = TOOLS_BY_NAME[name]["handler"]
    try:
        return handler(**(arguments or {}))
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}
