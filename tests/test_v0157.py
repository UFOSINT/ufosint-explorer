"""v0.15.7 — SEO / sharing / security-header polish.

Static source inspection + endpoint smoke tests, matching the
pattern of the other per-version test files.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "static" / "index.html"
APP_PY = ROOT / "app.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_index_has_opengraph_tags():
    html = _read(INDEX_HTML)
    for tag in (
        'property="og:title"',
        'property="og:description"',
        'property="og:image"',
        'name="twitter:card"',
    ):
        assert tag in html, f"index.html must carry {tag}"


def test_index_has_canonical_link():
    html = _read(INDEX_HTML)
    assert '<link rel="canonical" href="https://ufosint.com/"' in html


def test_index_has_favicon():
    html = _read(INDEX_HTML)
    assert 'rel="icon"' in html
    assert (ROOT / "static" / "favicon.svg").exists()


def test_og_card_asset_exists():
    assert (ROOT / "static" / "og-card.png").exists()


def test_no_stale_counts_in_index():
    """v0.16.4 raised the corpus to 573,210 (rebuild + MUFON via UPDB).

    618,316 is still allowed to appear once, in the download card that
    warns the published SQLite snapshot predates the purge — but it must
    never reach the head, where the title/meta/JSON-LD totals live.
    """
    html = _read(INDEX_HTML)
    assert "614,505" not in html
    assert "614505" not in html
    assert "573,210" in html

    head = html[: html.find("</head>")]
    for stale in ("476,195", "476195", "618,316", "618316", "614,505"):
        assert stale not in head, (
            f"stale total {stale!r} leaked back into the document head"
        )


def test_no_azurewebsites_urls_in_public_metadata():
    """SEO identity must consolidate on ufosint.com — the JSON-LD
    block and llms.txt previously pointed at the azurewebsites.net
    origin."""
    html = _read(INDEX_HTML)
    assert "ufosint-explorer.azurewebsites.net" not in html
    src = _read(APP_PY)
    assert "ufosint-explorer.azurewebsites.net" not in src


def test_sitemap_route_exists(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert b"<urlset" in resp.data
    assert b"https://ufosint.com/" in resp.data


def test_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers.get("Strict-Transport-Security")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy")
    assert "frame-ancestors" in (resp.headers.get("Content-Security-Policy") or "")
