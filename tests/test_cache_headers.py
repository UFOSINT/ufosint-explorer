"""Cache-Control policy tests.

The three-tier cache strategy is:
1. /static/*?v=<anything>  → public, max-age=31536000, immutable
2. /static/* (no ?v=)      → public, max-age=3600, must-revalidate
3. /                       → public, max-age=60
4. /api/* cacheable        → public, max-age=300
5. /health, /api/sighting  → no Cache-Control header (framework default)

If any of these drift, the Sprint 4 stale-cache class of bug can recur.
"""
from __future__ import annotations


def test_versioned_static_is_immutable(client):
    """?v=<anything> marks a URL as content-addressed — cache forever."""
    resp = client.get("/static/style.css?v=anything")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=31536000" in cc, cc
    assert "immutable" in cc, cc


def test_unversioned_static_must_revalidate(client):
    """No version → short cache + must-revalidate so the browser can
    never be more than an hour behind on a new deploy."""
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=3600" in cc, cc
    assert "must-revalidate" in cc, cc


def test_html_shell_has_short_cache(client):
    resp = client.get("/")
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=60" in cc, cc


def test_unversioned_app_js_must_revalidate(client):
    """Same policy applies to the JS bundle."""
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=3600" in cc, cc
    assert "must-revalidate" in cc, cc


def test_versioned_app_js_is_immutable(client):
    resp = client.get("/static/app.js?v=abcdef")
    assert resp.status_code == 200
    cc = resp.headers.get("Cache-Control", "")
    assert "max-age=31536000" in cc, cc
    assert "immutable" in cc, cc
