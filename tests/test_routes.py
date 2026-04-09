"""Route registration + HTML shell tests.

These don't hit the database — they just prove that:
- Every expected route is registered
- GET / returns versioned asset URLs in the HTML shell
- /health is a no-DB sanity endpoint that still has to exist
"""
from __future__ import annotations

import re


EXPECTED_ROUTES = {
    "/",
    "/health",
    "/api/stats",
    "/api/filters",
    "/api/tools-catalog",
    "/api/tool/<name>",
    "/api/map",
    "/api/heatmap",
    "/api/timeline",
    "/api/search",
    "/api/export.csv",
    "/api/export.json",
    "/api/sighting/<int:sid>",
    "/api/sentiment/overview",
    "/api/sentiment/timeline",
    "/api/sentiment/by-source",
    "/api/sentiment/by-shape",
    "/api/duplicates",
    "/mcp",
}


def test_every_expected_route_is_registered(flask_app):
    rules = {r.rule for r in flask_app.url_map.iter_rules()}
    missing = EXPECTED_ROUTES - rules
    assert not missing, f"missing routes: {sorted(missing)}"


def test_index_route_substitutes_asset_version(client, asset_version):
    """GET / should return HTML with the version string in place of the
    {{ASSET_VERSION}} placeholder."""
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "{{ASSET_VERSION}}" not in body, (
        "asset version placeholder leaked into rendered HTML"
    )
    assert f"/static/style.css?v={asset_version}" in body
    assert f"/static/app.js?v={asset_version}" in body


def test_index_html_content_type(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["Content-Type"].lower()


def test_index_response_has_short_cache(client):
    """The HTML shell stays on max-age=60 so the version string in
    <link>/<script> tags refreshes quickly after a deploy."""
    resp = client.get("/")
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=60" in cc, f"unexpected Cache-Control on /: {cc!r}"


def test_asset_version_is_nontrivial(asset_version):
    """Version must be a non-empty hex-ish string of reasonable length."""
    assert asset_version, "ASSET_VERSION is empty"
    assert len(asset_version) >= 8, (
        f"ASSET_VERSION too short: {asset_version!r}"
    )
    assert re.fullmatch(r"[A-Za-z0-9._-]+", asset_version), (
        f"ASSET_VERSION has weird characters: {asset_version!r}"
    )
