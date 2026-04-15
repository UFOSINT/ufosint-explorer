"""Static-asset invariants.

These tests are pure file-read checks — no Flask, no database. They catch
the class of bug that hit us in Sprint 4: HTML that references inline
SVG icons without explicit width/height, emoji that should have been
replaced, asset URLs without a version query string.

If any of these ever fail, the deploy is almost certainly broken for
users with stale browser caches.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SVG sizing
# ---------------------------------------------------------------------------
_SVG_OPEN_RE = re.compile(r"<svg\s[^>]*>")


@pytest.mark.parametrize("path", [INDEX_HTML, APP_JS])
def test_every_inline_svg_has_explicit_width_and_height(path: Path):
    """Defensive sizing — see commit 7377087.

    Without width/height attributes, SVGs fall back to their default
    intrinsic size (300x150) when CSS fails to apply. That's what broke
    the Sprint 4 deploy for users holding stale CSS.

    v0.8.6: app.js no longer contains any inline SVGs (the CSV/JSON/
    copy-link buttons that embedded them were deleted with the Search
    panel). If app.js has zero SVGs that's now fine — the check only
    applies when there ARE SVGs to size.
    """
    content = _read(path)
    svgs = _SVG_OPEN_RE.findall(content)
    if not svgs:
        if path == APP_JS:
            return  # v0.8.6: app.js has no inline SVGs anymore
        raise AssertionError(f"no <svg> tags found in {path.name}")
    for svg in svgs:
        assert "width=" in svg, (
            f"{path.name}: <svg> without explicit width — "
            f"this will blow up when CSS fails to load.\n  {svg[:160]}"
        )
        assert "height=" in svg, (
            f"{path.name}: <svg> without explicit height.\n  {svg[:160]}"
        )


def test_index_html_has_expected_svg_count():
    """Lock the SVG count so accidental duplication is obvious in diffs.

    v0.8.6: dropped from 10 → 7 because the Search panel's CSV /
    JSON download buttons and the Copy-link button were deleted.
    v0.9.0: jumped 7 → 12 because the Observatory rail gained 5
    new SVG chevrons (one per .rail-section) for the mobile
    accordion affordance.
    Remaining: settings gear, Ask AI, Connect, Near me, AI empty UFO,
    chat fab, chat popover UFO, 5× rail-chevron.
    """
    content = _read(INDEX_HTML)
    assert len(_SVG_OPEN_RE.findall(content)) == 18, (
        "Expected 18 inline SVGs in index.html (settings gear, Ask AI, "
        "Connect, Credits, Near me, AI empty UFO, chat fab, chat popover "
        "UFO, 5x rail-chevron, 2x DQ gear icons, 1x help tour icon, "
        "1x REGION draw button, and 1x region draw overlay SVG added "
        "in v0.11.5). "
        "If this changed intentionally, update the expected count."
    )


# ---------------------------------------------------------------------------
# Cache-bust version placeholder
# ---------------------------------------------------------------------------
def test_index_html_references_versioned_assets():
    """CSS and JS must load through a ?v=<version> query string.

    The placeholder {{ASSET_VERSION}} is replaced by app.py on startup.
    If index.html loses this pattern, a new deploy won't bust the
    browser cache and users on old CSS will see broken layouts.
    """
    content = _read(INDEX_HTML)
    assert 'href="/static/style.css?v={{ASSET_VERSION}}"' in content, (
        "style.css link must use ?v={{ASSET_VERSION}} cache-bust pattern"
    )
    assert 'src="/static/app.js?v={{ASSET_VERSION}}"' in content, (
        "app.js script must use ?v={{ASSET_VERSION}} cache-bust pattern"
    )


# ---------------------------------------------------------------------------
# Emoji
# ---------------------------------------------------------------------------
_EMOJI_RE = re.compile(
    r"[\u2300-\u23FF\u2600-\u27BF\U0001F300-\U0001F9FF\U0001FA70-\U0001FAFF]"
)
# ✓/✗ are allowed as inline text feedback (copy button); everything else
# in those Unicode ranges should be an SVG icon now.
_EMOJI_ALLOWLIST = {"\u2713", "\u2717"}


@pytest.mark.parametrize("path", [INDEX_HTML, APP_JS])
def test_no_emoji_in_static_assets(path: Path):
    """Sprint 4 replaced every emoji with an SVG icon — keep it that way."""
    content = _read(path)
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for m in _EMOJI_RE.finditer(line):
            if m.group(0) in _EMOJI_ALLOWLIST:
                continue
            hits.append((lineno, m.group(0)))
            break
    assert not hits, (
        f"{path.name} contains emoji that should be SVG icons: "
        + ", ".join(f"line {n}: U+{ord(c):04X}" for n, c in hits)
    )


# ---------------------------------------------------------------------------
# CSS tokens — guards against a regression that would re-introduce the
# Sprint 4 brittleness (no .icon rule, no category palette, etc.).
# ---------------------------------------------------------------------------
_REQUIRED_CSS_TOKENS = [
    "--text-strong",
    "--font-mono",
    "--font-sans",
    "--cat-1",
    "--success",
    "--warning",
    "--danger",
    "--shadow-sm",
]


@pytest.mark.parametrize("token", _REQUIRED_CSS_TOKENS)
def test_style_css_defines_required_design_tokens(token: str):
    content = _read(STYLE_CSS)
    assert f"{token}:" in content, (
        f"style.css is missing required design token {token!r}. "
        "These are referenced by other rules — dropping one is a silent break."
    )


def test_style_css_defines_icon_sizing_rule():
    """The .icon base class sizes inline SVGs. Must always exist."""
    content = _read(STYLE_CSS)
    assert ".icon {" in content, "missing .icon base rule"
    assert "width: 1em" in content, ".icon must size itself relative to font"
    assert "height: 1em" in content


def test_style_css_has_no_rogue_purple():
    """The Sprint 4 chip unification killed the #6c5ce7 purple.

    If this ever comes back, someone added a new chip rule without
    reusing the shared tokens.
    """
    content = _read(STYLE_CSS)
    assert "#6c5ce7" not in content, (
        "rogue Sprint 3-era purple is back — use the shared chip tokens"
    )


# ---------------------------------------------------------------------------
# JS syntax — delegate to `node -c static/app.js` when node is on PATH.
# Skipped on machines without node (lightweight dev envs). The CI
# workflow installs node and hard-fails the job if this check fails.
# ---------------------------------------------------------------------------
def test_app_js_parses_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH — skipping JS syntax check")
    result = subprocess.run(
        [node, "-c", str(APP_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"node -c static/app.js failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
