"""MCP tool catalog invariants.

The Sprint 3 BYOK architecture has one shared source of truth:
`tools_catalog.TOOLS`. It's exposed three ways:

1. `/api/tools-catalog` — OpenAI function-calling format, used by the
   browser-side BYOK chat.
2. `/mcp` — MCP JSON-RPC over HTTP, used by Claude Desktop / Cursor /
   Cline / Windsurf / etc. running against this deployment.
3. `mcp_server.py` — stdio MCP via FastMCP, used by local CLI clients.

If any of these drift from the shared catalog, BYOK chat or external
MCP clients break silently. These tests lock the contract.
"""
from __future__ import annotations


def test_tools_catalog_module_loads():
    """Pure import check — no Flask needed."""
    import tools_catalog  # noqa: F401


def test_tools_catalog_has_six_tools():
    """Six tools shipped with Sprint 3 (search, get, stats, timeline,
    duplicates, count_by). If this count changes, review both consumers
    (BYOK + MCP) and bump the expected number here."""
    from tools_catalog import TOOLS
    assert len(TOOLS) == 6, f"expected 6 tools, got {len(TOOLS)}"


def test_every_tool_has_required_fields():
    from tools_catalog import TOOLS
    for tool in TOOLS:
        assert tool.get("name"), f"tool missing name: {tool!r}"
        assert tool.get("description"), f"tool {tool['name']} missing description"
        assert isinstance(tool.get("parameters"), dict), (
            f"tool {tool['name']} missing parameters dict"
        )
        params = tool["parameters"]
        assert params.get("type") == "object", (
            f"tool {tool['name']} parameters.type must be 'object'"
        )
        assert "properties" in params, (
            f"tool {tool['name']} parameters missing properties"
        )
        assert callable(tool.get("handler")), (
            f"tool {tool['name']} missing handler callable"
        )


def test_tools_catalog_names_are_unique():
    from tools_catalog import TOOLS
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"


def test_tools_catalog_endpoint_matches_source(client):
    """`/api/tools-catalog` returns OpenAI function format wrapping the
    same tool names as `tools_catalog.TOOLS`."""
    from tools_catalog import TOOLS

    resp = client.get("/api/tools-catalog")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "tools" in payload

    catalog_names = {t["name"] for t in TOOLS}
    endpoint_names = {t["function"]["name"] for t in payload["tools"]}
    assert catalog_names == endpoint_names, (
        f"drift between tools_catalog.TOOLS and /api/tools-catalog: "
        f"only-in-catalog={catalog_names - endpoint_names}, "
        f"only-in-endpoint={endpoint_names - catalog_names}"
    )


def test_list_tools_mcp_shape():
    """MCP format uses `inputSchema` (camelCase), unlike the OpenAI
    function format that uses `parameters`. Make sure the MCP adapter
    is emitting the right key."""
    from tools_catalog import list_tools_mcp
    tools = list_tools_mcp()
    assert tools, "list_tools_mcp returned nothing"
    for t in tools:
        assert "name" in t
        assert "description" in t
        assert "inputSchema" in t, (
            f"MCP tool {t.get('name')} missing inputSchema (MCP uses "
            f"camelCase, not OpenAI's 'parameters')"
        )


def test_mcp_blueprint_is_mounted(flask_app):
    rules = [r.rule for r in flask_app.url_map.iter_rules()]
    assert "/mcp" in rules, "mcp_http blueprint not mounted at /mcp"
