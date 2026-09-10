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


# ---------------------------------------------------------------------------
# crash_retrieval — the failure the reload script kept calling a false-positive
# ---------------------------------------------------------------------------

MIGRATION = ROOT / "scripts" / "add_v017_crash_retrieval_text.sql"
DEPLOY_YML = ROOT / ".github" / "workflows" / "azure-deploy.yml"
MIGRATOR = ROOT / "scripts" / "migrate_sqlite_to_pg.py"
RELOAD = ROOT / "scripts" / "reload_from_public_db.py"


def test_crash_retrieval_migration_exists_and_is_idempotent():
    assert MIGRATION.exists()
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "craft_size_m" in sql and "TYPE text" in sql
    assert "data_type <> 'text'" in sql, (
        "the ALTER must be guarded so re-running on every deploy is a no-op"
    )


def test_crash_retrieval_migration_is_deployed():
    yml = DEPLOY_YML.read_text(encoding="utf-8")
    assert yml.count("add_v017_crash_retrieval_text.sql") >= 2, (
        "migration must be in both the sparse-checkout list and the psql loop"
    )


def test_migrator_nulls_empty_strings_for_non_text_columns():
    """SQLite writes "" where the schema says numeric; Postgres rejects it."""
    src = MIGRATOR.read_text(encoding="utf-8")
    assert "def pg_non_text_columns" in src
    assert 'row[i] = None' in src


def test_reload_does_not_treat_a_crash_as_the_known_false_positive():
    """A migrator that raises emits no MISMATCH lines.

    Checking only for mismatches made an abort indistinguishable from a
    clean run, so crash_retrieval's COPY failure was reported as expected
    on every reload for several releases. It was harmless only because
    that table is copied last.
    """
    src = RELOAD.read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in src, (
        "the reload must detect a crashed migrator, not just mismatches"
    )
    crash_at = src.find("crashed = [")
    false_pos_at = src.find("expected date_correction false-positive")
    assert crash_at != -1 and crash_at < false_pos_at, (
        "the crash check must run before the false-positive branch"
    )


# ---------------------------------------------------------------------------
# Source filter list vs the packed buffer's source list
# ---------------------------------------------------------------------------

def test_filter_list_excludes_sources_with_no_sightings():
    """source_database keeps rows for retired imports.

    MUFON and r/UFOs since the v0.16 purge, NUFORC since v0.17 moved it to
    origin-only. Offering them as filters promises results that cannot
    exist, and selecting NUFORC and getting nothing back reads as a broken
    site rather than an empty set.
    """
    src = APP_PY.read_text(encoding="utf-8")
    start = src.find('FILTER_CACHE["sources"]')
    assert start != -1
    block = src[max(0, start - 1200):start]
    assert "EXISTS" in block and "FROM sighting s" in block, (
        "the sources filter must be restricted to sources that have rows"
    )


def test_packed_buffer_still_lists_every_source():
    """source_idx is a position in this list, so it must not be filtered.

    Making it "consistent" with /api/filters would renumber every source
    after the dropped one, and cached client buffers would colour and
    filter by the wrong source — the v0.13 "selecting r/UFOs paints
    everything pink" failure. The client looks names up via
    POINTS.sources.indexOf(), so the two lists are free to differ.
    """
    src = APP_PY.read_text(encoding="utf-8")
    start = src.find("source_rows = cur.fetchall()")
    assert start != -1
    block = src[max(0, start - 900):start]
    assert "EXISTS" not in block, (
        "the buffer's source list must stay unfiltered or source_idx shifts"
    )


# ---------------------------------------------------------------------------
# Timeline legend
# ---------------------------------------------------------------------------

APP_JS = ROOT / "static" / "app.js"
DECK_JS = ROOT / "static" / "deck.js"


def test_deck_exposes_corpus_wide_source_totals():
    """Anything rendering one entry per source needs to know which are real.

    POINTS.sources keeps a slot for every source_database row so source_idx
    stays a stable position — retired imports included.
    """
    js = DECK_JS.read_text(encoding="utf-8")
    assert "function getSourceTotals()" in js
    start = js.find("function getSourceTotals()")
    body = js[start:start + 700]
    assert "POINTS.sourceIdx" in body
    assert "visibleIdx" not in body, (
        "source totals must cover the whole corpus, not the visible set — a "
        "legend that reshuffles while the user drags a brush is worse than "
        "one carrying a dead entry"
    )
    # And it must actually be reachable from app.js.
    assert "getSourceTotals," in js, "getSourceTotals must be exported"


def test_timeline_skips_sources_with_no_rows():
    """MUFON, NUFORC and r/UFOs would otherwise appear as zero-height bands.

    The stacked timeline built one dataset per source index, so every
    retired source became a legend entry that is zero at every year.
    """
    js = APP_JS.read_text(encoding="utf-8")
    anchor = js.find('for (let s = 1; s < sourceCount; s++)')
    assert anchor != -1, "the stacked-timeline dataset loop moved"
    block = js[anchor:anchor + 400]
    assert "sourceTotals" in block and "continue" in block, (
        "the timeline must skip sources with a corpus-wide count of zero"
    )
