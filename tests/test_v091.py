"""v0.9.1 — Correctness hotfix + Insights coverage panels.

Two threads of work, both informed by the Science-reviewer findings:

**Wave 1 — Correctness hotfix.** The v0.8.8 release carried several
bugs where the app was silently wrong, not just suboptimal:

1. Methodology page lede claimed "126,730 duplicate candidate pairs
   are flagged for review" but /api/stats.duplicate_candidates = 0
   (duplicate_candidate table ships empty in v0.8.3b).
2. /api/stats.date_range.min returned "0019-01-01" because 692
   UFOCAT "19xx, year unknown" records still had date_event set to
   "0019-..." instead of NULL. The v0.8.3b fix pipeline was
   documented but never landed on Azure PG. v0.9.1 adds query-time
   guards in /api/stats, /api/timeline, /api/sentiment/timeline +
   a one-shot scripts/fix_year_0019.sql cleanup.
3. /api/timeline silently ignored the `bins=monthly` parameter and
   returned yearly data regardless. v0.9.1 honors it by routing to
   the live path.
4. /api/points-bulk?meta=1 `sources[0]` was literal `None`, causing
   downstream client charts to render a silent null category. v0.9.1
   replaces it with the string "(unknown)" so orphaned-FK rows get
   a labeled bucket. Also counts orphans in coverage.orphaned_source.
5. "Hide likely hoaxes" / "Hoax likelihood" UI labels implied the
   column is a calibrated probability. It's a keyword-match
   heuristic. v0.9.1 renames all user-facing strings to "narrative
   red flags" and adds tooltip disclaimers.
6. The Data Quality rail's "High quality only" toggle silently
   filtered to a subset that's structurally biased toward modern
   MUFON-investigated reports. v0.9.1 adds a bias warning banner
   that appears inside the rail whenever the toggle is active.
7. has_description UI label implied the narrative is readable.
   In the public DB raw text is stripped. Renamed to "Had
   description (in source)" with a "classifier ran; text not
   retained" sub-label.

**Wave 2 — Insights coverage panels.** Every Insights card now
surfaces a coverage strip at the bottom: "N = x / y visible (z%)"
plus a green/yellow/orange/red pill. Cards with <50% coverage get
dimmed to 0.72 opacity; <30% coverage gets a big red "INSUFFICIENT
DATA" banner. Prevents users from looking at a radar chart drawn
from 200 labeled rows and walking away with conclusions.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"
STYLE_CSS = ROOT / "static" / "style.css"
FIX_0019_SQL = ROOT / "scripts" / "fix_year_0019.sql"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    pat = re.compile(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(")
    m = pat.search(src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i == -1:
        return ""
    depth = 0
    start = i
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    return src[start:]


# =============================================================================
# Wave 1a — Year-0019 guard
# =============================================================================

def test_api_stats_live_path_excludes_0019():
    """The live fallback /api/stats query must exclude the 692
    bogus 'UFOCAT 2-digit 19 = 0019' records from MIN/MAX."""
    src = _read(APP_PY)
    m = re.search(
        r"SELECT MIN\(date_event\), MAX\(date_event\).*?\"\"\"",
        src,
        re.DOTALL,
    )
    assert m, "couldn't locate the live date_min/max query"
    body = m.group(0)
    assert "NOT LIKE '0019-%'" in body or "NOT LIKE '0019%'" in body, (
        "live /api/stats date_min/max query must exclude 0019-* "
        "records — v0.9.1 correctness fix"
    )


def test_api_stats_mv_path_overrides_0019():
    """The MV fast path must detect and override a stale
    mv_stats_summary.date_min that starts with '0019-'."""
    src = _read(APP_PY)
    body = _extract_js_function_like(
        src, "_api_stats_from_mv", end_pat=r"\n\ndef "
    )
    assert body, "couldn't locate _api_stats_from_mv body"
    assert 'startswith("0019-")' in body or '"0019-"' in body, (
        "_api_stats_from_mv must check if date_min starts with "
        "'0019-' and re-query the live table to correct it"
    )


def test_api_timeline_excludes_0019():
    """Both the live fallback and the MV post-filter must drop
    the '0019' key from the timeline response.

    v0.9.1 hotfix note: the live path uses '%%' (psycopg literal-%
    escape) because clauses are passed through cur.execute(sql,
    args). Without the escape psycopg raises 'unsupported format
    character' at runtime — which caused a 500 regression that
    I caught in post-deploy smoke, hence the %% in all
    parameterized-execute branches."""
    src = _read(APP_PY)
    # v0.12.4: api_timeline was split — the body that holds the SQL
    # moved to _api_timeline_impl so we could wrap the connection in
    # a try/finally. Prefer the impl; fall back to the route for
    # older revs of this test running against a pre-v0.12.4 checkout.
    body = _extract_js_function_like(
        src, "_api_timeline_impl", end_pat=r"\n\ndef |\n\n@app\."
    ) or _extract_js_function_like(
        src, "api_timeline", end_pat=r"\n\ndef |\n\n@app\."
    )
    assert body, "couldn't locate api_timeline body"
    assert "NOT LIKE '0019-%%'" in body, (
        "live timeline query must exclude 0019-* records using "
        "the %% escape (psycopg treats bare % as a format "
        "placeholder when args is passed)"
    )
    # MV path also drops "0019" keys from the response pivot
    assert '"0019"' in body or 'startswith("0019")' in body, (
        "MV timeline path must drop the '0019' bucket from its "
        "response pivot"
    )


def test_api_sentiment_timeline_excludes_0019():
    """/api/sentiment/timeline uses the same date_event LIKE
    guards as /api/timeline. Both clauses-list paths use the
    %% escape because they're parameterized execute calls."""
    src = _read(APP_PY)
    not_null_count = src.count("s.date_event IS NOT NULL")
    # Escaped form appears in parameterized execute paths.
    not_like_count_escaped = src.count("s.date_event NOT LIKE '0019-%%'")
    # api_timeline + api_sentiment_timeline both add the guard
    # to their clauses lists → expect >= 2 escaped occurrences.
    assert not_null_count >= 2
    assert not_like_count_escaped >= 2, (
        f"expected at least 2 `s.date_event NOT LIKE '0019-%%'` "
        f"guards (api_timeline + api_sentiment_timeline), found "
        f"{not_like_count_escaped}"
    )


def test_fix_0019_sql_exists():
    """One-shot cleanup script must exist and be idempotent."""
    assert FIX_0019_SQL.exists(), (
        "scripts/fix_year_0019.sql must exist for the one-shot DB "
        "cleanup"
    )
    sql = _read(FIX_0019_SQL)
    assert "UPDATE sighting" in sql
    assert "date_event LIKE '0019-%'" in sql
    assert "REFRESH MATERIALIZED VIEW" in sql
    # Idempotency check — should log to date_correction only if
    # not already logged
    assert "NOT EXISTS" in sql or "fix_year_0019" in sql


# =============================================================================
# Wave 1b — Methodology lede fix
# =============================================================================

def test_methodology_lede_drops_duplicate_claim():
    """The methodology <p class="meth-intro"> should no longer
    claim 126,730 flagged duplicate pairs — that was a
    pre-v0.8.3b number."""
    html = _read(INDEX_HTML)
    m = re.search(r'<p class="meth-intro">([\s\S]*?)</p>', html)
    assert m, "couldn't find .meth-intro"
    lede = m.group(1)
    assert "126,730 duplicate" not in lede, (
        "methodology lede still claims 126,730 duplicate pairs "
        "(which is the pre-v0.8.3b number; current build has 0)"
    )


def test_methodology_has_current_build_banner():
    """A correction banner should explicitly state the current
    build ships 0 duplicate candidate pairs."""
    html = _read(INDEX_HTML)
    assert "meth-banner" in html, (
        "methodology should have a .meth-banner element calling "
        "out the current-build correction"
    )
    assert ("ships 0 duplicate" in html
            or "empty in this release" in html
            or "Reproducibility statement" in html)


# =============================================================================
# Wave 1c — /api/timeline?bins=monthly honored
# =============================================================================

def test_api_timeline_reads_bins_param():
    src = _read(APP_PY)
    # v0.12.4: see test_api_timeline_excludes_0019 above — body moved
    # to _api_timeline_impl.
    body = _extract_js_function_like(
        src, "_api_timeline_impl", end_pat=r"\n\ndef |\n\n@app\."
    ) or _extract_js_function_like(
        src, "api_timeline", end_pat=r"\n\ndef |\n\n@app\."
    )
    assert body
    assert 'request.args.get("bins")' in body, (
        "api_timeline must read the bins query parameter"
    )
    assert "want_monthly" in body or 'bins_mode == "monthly"' in body, (
        "api_timeline must branch on the bins value"
    )
    # The monthly path must group by SUBSTR(date_event, 1, 7) and
    # filter to LENGTH >= 7 (which the month-specific path already
    # does, but the full-range monthly path needs it too)
    assert "LENGTH(s.date_event) >= 7" in body


# =============================================================================
# Wave 1d — source_idx=0 audit + meta flag
# =============================================================================

def test_source_names_first_slot_is_unknown_string():
    """source_names[0] should be the literal string '(unknown)'
    instead of None. Client charts that render a per-source
    category would silently skip null entries otherwise."""
    src = _read(APP_PY)
    assert 'source_names = ["(unknown)"]' in src, (
        "source_names[0] must be the string '(unknown)' in v0.9.1 "
        "so client charts render orphaned-FK rows in a labeled "
        "category"
    )


def test_coverage_tracks_orphaned_source():
    """The packer's coverage dict should include orphaned_source
    and orphaned_shape counters."""
    src = _read(APP_PY)
    assert '"orphaned_source": 0' in src, (
        "coverage dict must initialise orphaned_source counter"
    )
    # Packer loop must increment it
    m = re.search(
        r'src_idx_packed = source_id_to_idx\.get\(src_id, 0\)',
        src,
    )
    assert m, "packer must compute src_idx_packed before the struct.pack call"
    assert 'cov["orphaned_source"] += 1' in src


# =============================================================================
# Wave 1e — has_description rename
# =============================================================================

def test_has_description_rail_label_clarified():
    src = _read(APP_JS)
    body = _extract_js_function(src, "mountQualityRail")
    assert body
    assert "Had description (in source)" in body, (
        "hasDescription rail toggle should be renamed to make "
        "clear the text itself isn't retained in the public DB"
    )


# =============================================================================
# Wave 1f — High quality bias warning banner
# =============================================================================

def test_update_quality_bias_banner_exists():
    src = _read(APP_JS)
    assert "function updateQualityBiasBanner(" in src, (
        "updateQualityBiasBanner helper must exist"
    )


def test_mount_quality_rail_creates_bias_banner():
    src = _read(APP_JS)
    body = _extract_js_function(src, "mountQualityRail")
    assert body
    assert "quality-bias-banner" in body, (
        "mountQualityRail must create the .quality-bias-banner "
        "element when the rail is first rendered"
    )
    assert "biased" in body.lower(), (
        "bias banner text should explain the subset is biased"
    )


def test_css_has_bias_banner_rule():
    css = _read(STYLE_CSS)
    assert ".quality-bias-banner" in css, (
        "style.css must have .quality-bias-banner rules"
    )


# =============================================================================
# Wave 1g — hoax_likelihood rename
# =============================================================================

def test_hoax_renamed_in_detail_modal():
    src = _read(APP_JS)
    body = _extract_js_function(src, "openDetail")
    assert body
    assert "Narrative red flags" in body, (
        "openDetail must display the hoax column as "
        "'Narrative red flags', not 'Hoax likelihood'"
    )
    # Tooltip should explain it's a heuristic
    assert "heuristic" in body.lower() or "keyword" in body.lower()


def test_hoax_renamed_in_rail_toggle():
    src = _read(APP_JS)
    body = _extract_js_function(src, "mountQualityRail")
    assert body
    assert "Hide narrative red flags" in body, (
        "rail toggle label must be 'Hide narrative red flags' "
        "not 'Hide likely hoaxes'"
    )


def test_hoax_card_title_renamed():
    html = _read(INDEX_HTML)
    assert "Narrative Red Flags" in html, (
        "the Insights card title must be 'Narrative Red Flags', "
        "not 'Hoax Likelihood Curve'"
    )


# =============================================================================
# Wave 2 — Coverage panels
# =============================================================================

def test_compute_insights_coverage_exists():
    src = _read(APP_JS)
    assert "function _computeInsightsCoverage(" in src


def test_render_coverage_strip_exists():
    src = _read(APP_JS)
    assert "function _renderCoverageStrip(" in src


def test_mount_all_coverage_strips_exists():
    src = _read(APP_JS)
    assert "function _mountAllCoverageStrips(" in src


def test_refresh_insights_computes_coverage():
    src = _read(APP_JS)
    body = _extract_js_function(src, "refreshInsightsClientCards")
    assert body
    assert "_computeInsightsCoverage" in body, (
        "refreshInsightsClientCards must compute coverage before "
        "rendering cards"
    )
    assert "_mountAllCoverageStrips" in body, (
        "refreshInsightsClientCards must call _mountAllCoverageStrips "
        "after rendering"
    )


def test_coverage_covers_all_nine_cards():
    """v0.11: _mountAllCoverageStrips must mount a strip on each of
    the 9 client-side cards (5 emotion + 2 quality + 2 movement)."""
    src = _read(APP_JS)
    body = _extract_js_function(src, "_mountAllCoverageStrips")
    assert body
    for canvas_id in (
        # v0.11 emotion cards
        "sentiment-group-chart",
        "emotion-7-chart",
        "emotion-28-chart",
        "sentiment-scores-chart",
        "emotion-source-chart",
        # Quality + movement (unchanged)
        "quality-distribution-chart",
        "hoax-curve-chart",
        "movement-taxonomy-chart",
        "shape-movement-chart",
    ):
        assert canvas_id in body, (
            f"_mountAllCoverageStrips must call _renderCoverageStrip "
            f"for {canvas_id!r}"
        )


def test_coverage_computes_all_relevant_columns():
    src = _read(APP_JS)
    body = _extract_js_function(src, "_computeInsightsCoverage")
    assert body
    # The seven fields the insight cards read
    for key in ("quality", "hoax", "shape", "color", "emotion",
                "hasDescription", "movementFlags"):
        assert key in body, (
            f"_computeInsightsCoverage must track {key!r}"
        )


def test_css_has_coverage_strip_rules():
    css = _read(STYLE_CSS)
    for cls in (
        ".insight-coverage-strip",
        ".cov-pill",
        ".cov-pill.cov-hi",
        ".cov-pill.cov-low",
    ):
        assert cls in css, f"style.css missing {cls!r}"


def test_css_has_low_coverage_dim_rule():
    css = _read(STYLE_CSS)
    assert ".insight-card.is-low-coverage" in css, (
        "low-coverage cards need a dimming rule so the visual "
        "hierarchy signals 'don't over-interpret this'"
    )
    assert ".insight-card.is-critical-coverage" in css, (
        "critical-coverage (<30%) cards need stronger dimming + "
        "an INSUFFICIENT DATA banner"
    )


# =============================================================================
# Helper — extract a Python function body by walking to the next
# top-level def. Not perfect but good enough for these contracts.
# =============================================================================

def _extract_js_function_like(src: str, name: str, end_pat: str) -> str:
    """Python-function body extractor by regex. Returns everything
    from `def name(` up to the first match of end_pat."""
    start_re = re.compile(r"def " + re.escape(name) + r"\(")
    m = start_re.search(src)
    if not m:
        return ""
    rest = src[m.start():]
    end_re = re.compile(end_pat)
    em = end_re.search(rest, 1)
    if em:
        return rest[:em.start()]
    return rest
