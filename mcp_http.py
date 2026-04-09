"""
MCP-over-HTTP Flask blueprint, mounted at /mcp.

Implements the Model Context Protocol's "Streamable HTTP" transport
just enough to expose our tool catalog (tools_catalog.TOOLS) to any
MCP-compatible client (Claude Desktop, Cursor, Cline, Continue,
Windsurf, ChatGPT custom GPTs that consume MCP, etc).

Spec reference:
    https://modelcontextprotocol.io/specification/server/transports/streamable-http

We implement the minimum non-streaming subset:
    initialize         — handshake
    initialized        — client-side notification (no response needed)
    notifications/*    — discarded
    ping               — health check
    tools/list         — list of tool definitions
    tools/call         — execute a tool

All requests are JSON-RPC 2.0. The response is JSON (no SSE) because
none of our tools stream output — they all return complete results.

The endpoint also serves a GET request returning a friendly HTML page
explaining how to connect, so visiting the URL in a browser is helpful
instead of confusing.
"""
from flask import Blueprint, request, jsonify, Response

from tools_catalog import TOOLS_BY_NAME, list_tools_mcp, call_tool


PROTOCOL_VERSION = "2024-11-05"   # MCP spec version we implement
SERVER_NAME = "ufosint-mcp"
SERVER_VERSION = "0.1.0"

mcp_bp = Blueprint("mcp", __name__)


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# Standard JSON-RPC error codes
PARSE_ERROR      = -32700
INVALID_REQUEST  = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS   = -32602
INTERNAL_ERROR   = -32603


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------

def handle_initialize(req_id, params):
    return _result(req_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
        "capabilities": {
            # We expose tools, no resources/prompts/sampling.
            "tools": {"listChanged": False},
        },
        "instructions": (
            "You are connected to the unified UFO sightings database "
            "(614,505 records from NUFORC, MUFON, UFOCAT, UPDB, UFO-search). "
            "Use get_stats first to get the lay of the land, then "
            "search_sightings for free-text and filter searches, "
            "get_sighting for full record details, get_timeline for "
            "temporal aggregates, count_by for top-N rankings, and "
            "find_duplicates_for to see cross-source duplicate candidates. "
            "All access is read-only."
        ),
    })


def handle_tools_list(req_id, params):
    return _result(req_id, {"tools": list_tools_mcp()})


def handle_tools_call(req_id, params):
    if not isinstance(params, dict):
        return _error(req_id, INVALID_PARAMS, "params must be an object")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not name or name not in TOOLS_BY_NAME:
        return _error(req_id, INVALID_PARAMS, f"unknown tool: {name!r}")

    result = call_tool(name, arguments)

    # MCP tool results use a content array with typed parts. We return
    # one text part containing the JSON-encoded result; clients can
    # parse it and present it however they like.
    import json as _json
    is_error = isinstance(result, dict) and "error" in result and len(result) == 1
    return _result(req_id, {
        "content": [
            {
                "type": "text",
                "text": _json.dumps(result, default=str, indent=2),
            }
        ],
        "isError": is_error,
    })


def handle_ping(req_id, params):
    return _result(req_id, {})


METHOD_HANDLERS = {
    "initialize":  handle_initialize,
    "tools/list":  handle_tools_list,
    "tools/call":  handle_tools_call,
    "ping":        handle_ping,
}

# Notification methods we silently accept (no response per JSON-RPC spec).
NOTIFICATION_METHODS = {
    "initialized",
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
}


# ---------------------------------------------------------------------------
# CORS — needed so browser-based MCP clients can connect
# ---------------------------------------------------------------------------

def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id, Mcp-Protocol-Version",
        "Access-Control-Expose-Headers": "Mcp-Session-Id",
        "Access-Control-Max-Age": "86400",
    }


@mcp_bp.route("/mcp", methods=["OPTIONS"])
def mcp_preflight():
    return ("", 204, _cors_headers())


# ---------------------------------------------------------------------------
# Main POST handler
# ---------------------------------------------------------------------------

@mcp_bp.route("/mcp", methods=["POST"])
def mcp_post():
    """Handle a JSON-RPC request (or batch). Returns a JSON-RPC response (or batch)."""
    try:
        body = request.get_json(force=True, silent=False)
    except Exception as e:
        resp = jsonify(_error(None, PARSE_ERROR, f"invalid JSON: {e}"))
        resp.headers.update(_cors_headers())
        return resp, 400

    # Batch?
    if isinstance(body, list):
        responses = [_handle_one(req) for req in body]
        responses = [r for r in responses if r is not None]
        resp = jsonify(responses) if responses else Response(status=204)
    else:
        result = _handle_one(body)
        if result is None:
            resp = Response(status=204)
        else:
            resp = jsonify(result)

    if isinstance(resp, Response):
        for k, v in _cors_headers().items():
            resp.headers[k] = v
    return resp


def _handle_one(req):
    """Handle one JSON-RPC request object. Returns a response dict, or None for notifications."""
    if not isinstance(req, dict):
        return _error(None, INVALID_REQUEST, "request must be an object")

    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params", {})

    # Notification (no id) — process side effects but return nothing
    if req_id is None:
        if method in NOTIFICATION_METHODS:
            return None
        # Unknown notification — silently ignore (per JSON-RPC spec)
        return None

    if not method:
        return _error(req_id, INVALID_REQUEST, "missing 'method'")

    handler = METHOD_HANDLERS.get(method)
    if not handler:
        return _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    try:
        return handler(req_id, params)
    except Exception as e:
        return _error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# GET / browser-friendly landing page
# ---------------------------------------------------------------------------

LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UFOSINT MCP Server</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body { font-family: Inter, system-ui, sans-serif; max-width: 720px; margin: 4em auto;
         padding: 0 1.5em; color: #e8edf5; background: #0b0f17; line-height: 1.55; }
  h1 { color: #6ea8ff; font-weight: 700; }
  code, pre { background: #1a2130; padding: 2px 6px; border-radius: 4px;
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  pre { padding: 1em; overflow-x: auto; }
  a { color: #6ea8ff; }
  .muted { color: #8a96ad; font-size: 13px; }
</style>
</head>
<body>
<h1>UFOSINT MCP Server</h1>
<p>This is a <a href="https://modelcontextprotocol.io">Model Context Protocol</a>
server exposing read-only tools against the unified UFO sightings database
(614,505 records, 5 sources, deduplicated).</p>

<h3>Connect from Claude Desktop</h3>
<p>Add this to your <code>claude_desktop_config.json</code>:</p>
<pre>{
  "mcpServers": {
    "ufosint": {
      "url": "https://ufosint-explorer.azurewebsites.net/mcp",
      "transport": "http"
    }
  }
}</pre>

<h3>Test it from the command line</h3>
<pre>curl -s https://ufosint-explorer.azurewebsites.net/mcp \\
  -H 'Content-Type: application/json' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'</pre>

<p class="muted">All access is read-only. Source data licensed by UFOSINT;
the deduplicated database is the work of the
<a href="https://github.com/UFOSINT/ufo-dedup">ufo-dedup pipeline</a>.</p>
</body>
</html>"""


@mcp_bp.route("/mcp", methods=["GET"])
def mcp_landing():
    """Browser-visiting users get a help page instead of a 405."""
    accept = request.headers.get("Accept", "")
    if "text/html" in accept or "*/*" in accept and request.headers.get("User-Agent", "").startswith("Mozilla"):
        return Response(LANDING_HTML, mimetype="text/html")
    # MCP clients sending GET (some do for SSE handshake) — give them a server info JSON
    info = {
        "protocol": "MCP",
        "version": PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "transport": "http",
        "endpoint": "/mcp",
        "methods": list(METHOD_HANDLERS.keys()),
    }
    resp = jsonify(info)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp
