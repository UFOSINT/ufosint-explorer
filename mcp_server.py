"""
UFO Database MCP Server (stdio transport).

Local Model Context Protocol server that any MCP-aware AI client
(Claude Desktop, Cursor, Cline, Continue, Windsurf) can launch as a
subprocess. Use this if you want to run the MCP server on your own
machine — for offline access, lower latency, or to point at a local
copy of the database.

If you just want to connect to the hosted version, you don't need to
run this script. Use the HTTP MCP endpoint instead:

    https://ufosint-explorer.azurewebsites.net/mcp

Run directly:
    DATABASE_URL='postgresql://user:pass@host:5432/ufo_unified?sslmode=require' \\
    python mcp_server.py

Claude Desktop config (claude_desktop_config.json):
    {
      "mcpServers": {
        "ufosint-local": {
          "command": "python",
          "args": ["/absolute/path/to/mcp_server.py"],
          "env": {
            "DATABASE_URL": "postgresql://user:pass@host:5432/ufo_unified?sslmode=require"
          }
        }
      }
    }

The actual tool implementations live in tools_catalog.py — this file
is just a FastMCP wrapper that exposes them via stdio. The same
catalog is also exposed over HTTP by mcp_http.py for the hosted
production server.
"""
import os
import json

from fastmcp import FastMCP

# Importing tools_catalog also lazy-imports app.get_db() the first time
# a tool runs. We need DATABASE_URL set before that happens, otherwise
# the import of app.py will raise.
if not os.environ.get("DATABASE_URL"):
    raise RuntimeError(
        "DATABASE_URL env var is required.\n"
        "Example:\n"
        "  postgresql://user:pass@host.postgres.database.azure.com:5432/ufo_unified?sslmode=require"
    )

from tools_catalog import (
    search_sightings as _search_sightings,
    get_sighting as _get_sighting,
    get_stats as _get_stats,
    get_timeline as _get_timeline,
    find_duplicates_for as _find_duplicates_for,
    count_by as _count_by,
)


mcp = FastMCP(
    "ufosint",
    instructions=(
        "You are connected to the unified UFO sightings database "
        "(614,505 records from NUFORC, MUFON, UFOCAT, UPDB, UFO-search, "
        "deduplicated and cross-referenced). Use get_stats first to get the "
        "lay of the land, then search_sightings, get_sighting, get_timeline, "
        "find_duplicates_for, and count_by to dig in. All access is read-only."
    ),
)


# ---------------------------------------------------------------------------
# Tool wrappers — thin pass-throughs that JSON-stringify the result so MCP
# clients (which expect a string content payload) can display it.
# ---------------------------------------------------------------------------

def _as_text(result) -> str:
    return json.dumps(result, default=str, indent=2)


@mcp.tool()
def search_sightings(
    q: str = "",
    shape: str = "",
    source: str = "",
    state: str = "",
    country: str = "",
    date_from: str = "",
    date_to: str = "",
    hynek: str = "",
    limit: int = 25,
) -> str:
    """Free-text + filter search across the sighting table.

    Use this when the user asks 'find sightings matching X' or 'what reports
    came from California in 1973'. Returns up to 200 records (default 25).
    Date params accept either a year (1973) or an ISO date (1973-10-15).
    """
    return _as_text(_search_sightings(
        q=q or None, shape=shape or None, source=source or None,
        state=state or None, country=country or None,
        date_from=date_from or None, date_to=date_to or None,
        hynek=hynek or None, limit=limit,
    ))


@mcp.tool()
def get_sighting(sighting_id: int) -> str:
    """Fetch the full record for a single sighting by its database id.

    Use after search_sightings to get more detail on a specific result.
    """
    return _as_text(_get_sighting(sighting_id))


@mcp.tool()
def get_stats() -> str:
    """Top-level statistics: total sightings, counts by source, date range,
    geocoded location count, duplicate candidate pair count.
    """
    return _as_text(_get_stats())


@mcp.tool()
def get_timeline(year: int = 0, source: str = "", shape: str = "") -> str:
    """Sighting counts grouped by year (default) or by month (when a year
    is supplied). Optionally filter by source or shape.
    """
    return _as_text(_get_timeline(
        year=year if year else None,
        source=source or None,
        shape=shape or None,
    ))


@mcp.tool()
def find_duplicates_for(sighting_id: int, limit: int = 10) -> str:
    """Find duplicate-candidate pairs flagged for a given sighting.
    Each pair has a similarity score (0-1) and a match method.
    """
    return _as_text(_find_duplicates_for(sighting_id, limit=limit))


@mcp.tool()
def count_by(field: str, limit: int = 25, date_from: str = "", date_to: str = "") -> str:
    """Aggregate counts grouped by a categorical field.

    Allowed fields: shape, hynek, vallee, source, country, state.
    Use this for 'most common shapes', 'top 10 reporting states', etc.
    """
    return _as_text(_count_by(
        field=field, limit=limit,
        date_from=date_from or None, date_to=date_to or None,
    ))


if __name__ == "__main__":
    mcp.run()
