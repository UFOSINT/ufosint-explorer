"""v0.17 — the reload must put narrative text back.

ufo_public.db strips description/summary on purpose: it is the artifact
we publish and the text is licensed. But the live site shows a
description on the detail page, so Postgres carries text the published
SQLite does not.

Nothing in the reload path knew that. It loads the stripped export, so
every reload emptied sighting.description across the whole corpus and
the detail page went back to advertising narratives it could not show —
has_description 1, description null. The v0.17 reload wiped 468,251 of
them and reintroduced a bug that had already been reported and fixed
once, because restoring them was a manual step that lived nowhere.

These tests pin the step and, more importantly, the things that make it
fail loudly. A restore that silently no-ops is indistinguishable from
the bug it exists to prevent.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELOAD = ROOT / "scripts" / "reload_from_public_db.py"


def _src() -> str:
    return RELOAD.read_text(encoding="utf-8")


def test_reload_has_a_description_restore_step():
    src = _src()
    assert "def step7_restore_descriptions" in src
    assert "UNIFIED_SQLITE" in src, (
        "the step needs the unpublished working DB — the public export "
        "has no narrative text to restore from"
    )


def test_restore_actually_runs_in_main():
    """A step nobody calls is the state we were already in."""
    src = _src()
    assert "desc_ok = step7_restore_descriptions(url)" in src
    verify_at = src.find("ok_status = step6_verify(url)")
    restore_at = src.find("desc_ok = step7_restore_descriptions(url)")
    assert verify_at != -1 and restore_at != -1
    assert verify_at < restore_at, "restore runs after the data is loaded"


def test_missing_text_is_not_reported_as_success():
    """Exit 0 on a corpus with no narrative text is how this got missed."""
    src = _src()
    assert "if ok_status and desc_ok:" in src, (
        "the success banner must require the restore to have worked"
    )
    assert "return 6" in src, (
        "a reload that loaded data but no text needs its own exit status"
    )


def test_restore_refuses_mismatched_builds():
    """Ids are copied verbatim across the boundary.

    Orphans mean ufo_unified.db and ufo_public.db came from different
    builds, and writing text keyed on those ids would attach narratives
    to the wrong sightings — worse than having none.
    """
    src = _src()
    start = src.find("def step7_restore_descriptions")
    body = src[start:start + 6000]
    assert "orphans" in body
    assert "Refusing to write text onto mismatched ids" in body


def test_restore_is_chunked():
    """The single-statement version 503s the site.

    A 593k-row UPDATE of wide text rows runs 10+ minutes in IO wait and
    leaves enough dead tuples that /health's count query blows its 25s
    timeout.
    """
    src = _src()
    start = src.find("def step7_restore_descriptions")
    body = src[start:start + 6000]
    assert "DESC_CHUNK" in body
    assert "VACUUM (ANALYZE) sighting" in body


def test_restore_is_idempotent():
    """Re-running against correct data must update nothing.

    That property is what makes the step safe to re-run after a partial
    failure, and it is how the step was validated against production.
    """
    src = _src()
    start = src.find("def step7_restore_descriptions")
    body = src[start:start + 6000]
    assert "IS DISTINCT FROM" in body


def test_elapsed_uses_the_same_clock_as_the_helper():
    """elapsed() subtracts from perf_counter; time.time() prints nonsense."""
    src = _src()
    start = src.find("def step7_restore_descriptions")
    body = src[start:start + 1200]
    assert "time.perf_counter()" in body
    assert re.search(r"t0 = time\.time\(\)", body) is None
