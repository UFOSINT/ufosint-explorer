"""v0.17 — the corpus totals must agree with each other.

The v0.17 rebuild changed the headline number in roughly twenty places
across app.py and index.html: page title, meta/OG/Twitter tags, the
llms.txt masthead, the MCP catalog, the methodology tables, the rail and
the credits line. Updating that by hand is exactly the kind of edit that
leaves one straggler behind, and a site quoting two different corpus
sizes on the same page reads as broken data rather than a typo.

These tests do not assert any particular number is *correct* — only that
the surfaces agree, and that the source list matches what the pipeline
actually imports. Getting the number itself right is the reload
tripwire's job (scripts/reload_from_public_db.py).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
INDEX = ROOT / "static" / "index.html"

# The four source databases the pipeline imports as of v0.17. NUFORC is
# deliberately absent: it is no longer imported directly and reaches the
# corpus through both aggregators, labelled as an origin.
SOURCES = {"UFOCAT", "UPDB", "UFO-search", "Capella"}


def _totals(text):
    """Every 6-digit comma-grouped number that looks like a corpus total."""
    return set(re.findall(r"\b[5-9]\d{2},\d{3}\b", text))


def test_headline_total_is_consistent_across_surfaces():
    """One corpus size, everywhere a visitor can see one."""
    html = INDEX.read_text(encoding="utf-8")

    # The title tag is the canonical statement of the total.
    m = re.search(r"<title>[^<]*?([0-9]{3},[0-9]{3})[^<]*</title>", html)
    assert m, "the page title should quote the corpus total"
    headline = m.group(1)

    surfaces = {
        "meta description": r'<meta name="description" content="([^"]*)"',
        "og:description": r'<meta property="og:description" content="([^"]*)"',
        "twitter:description": r'<meta name="twitter:description" content="([^"]*)"',
    }
    for label, pattern in surfaces.items():
        found = re.search(pattern, html)
        assert found, f"{label} is missing"
        nums = _totals(found.group(1))
        assert not nums or headline in nums, (
            f"{label} quotes {nums} but the title says {headline}"
        )


def test_app_py_and_index_agree_on_the_total():
    html = INDEX.read_text(encoding="utf-8")
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r"<title>[^<]*?([0-9]{3},[0-9]{3})[^<]*</title>", html)
    headline = m.group(1)
    assert headline in src, (
        f"index.html advertises {headline} but app.py never mentions it — "
        "the llms.txt masthead and MCP catalog are probably stale"
    )
    # The pre-v0.17 figure must be gone from both.
    for name, text in (("app.py", src), ("index.html", html)):
        assert "573,210" not in text, f"{name} still quotes the pre-v0.17 total"


def test_nuforc_is_not_listed_as_a_source_database():
    """It is an origin now, not a source we import.

    Listing it as a source would promise a NUFORC filter that returns
    nothing, since no sighting carries that source_db_id any more.
    """
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r'"- \*\*Sources:\*\* ([^"]+)"', src)
    assert m, "the MCP catalog should list the source databases"
    listed = {s.strip() for s in m.group(1).split(",")}
    assert listed == SOURCES, f"expected {SOURCES}, got {listed}"


def test_capella_is_credited_to_phenomainon():
    """Their licence turns on "All sources credited"; the short internal
    label alone would read as obscuring provenance."""
    html = INDEX.read_text(encoding="utf-8")
    assert "Capella" in html
    assert "PhenomAInon" in html and "0toAI" in html, (
        "the Capella row must name the originator, not just the label"
    )


def test_points_bulk_schema_version_was_bumped():
    """Source indices are alphabetical, so the v0.17 source change shifts
    the source_idx byte of nearly every row in the packed buffer."""
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r'_POINTS_BULK_SCHEMA_VERSION = "([^"]+)"', src)
    assert m, "the buffer schema version constant is missing"
    assert m.group(1) == "v017-1", (
        f"expected v017-1, found {m.group(1)} — a stale client buffer would "
        "colour and filter by the wrong source"
    )
