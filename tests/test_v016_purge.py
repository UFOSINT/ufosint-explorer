"""v0.16 — mufon.csv + r/UFOs purge.

Regression tests that lock in the one thing the purge got wrong on the
first attempt: a schema migration that seeds *content*.

`add_v013_reddit_columns.sql` created source_collection('Reddit') and
source_database('r/UFOs') behind an `IF NOT EXISTS` guard. That guard is
idempotent only while the rows exist. After the v0.16 purge deleted them
it inverted into a resurrection — the deploy that shipped the purge
re-inserted both rows, and r/UFOs was back in /api/filters and
/api/stats with a count of 0 within minutes of the delete committing.

Every migration in the deploy workflow's list runs on *every* deploy, so
any one of them that writes rows can undo a data change. These tests pin
that down for the whole migration set, not just the Reddit one.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "azure-deploy.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _deployed_migrations() -> list[Path]:
    """The migration files azure-deploy.yml applies on every deploy."""
    wf = _read(WORKFLOW)
    names = sorted(set(re.findall(r"scripts/add_v[0-9a-z_]+\.sql", wf)))
    assert names, "no migrations found in the deploy workflow"
    return [ROOT / n for n in names]


def test_deployed_migrations_exist():
    for p in _deployed_migrations():
        assert p.exists(), f"deploy workflow references missing migration {p.name}"


def test_no_deployed_migration_seeds_source_rows():
    """Schema migrations create structure, not content.

    A source_collection / source_database row inserted by a migration is a
    row that comes back on the next deploy, however carefully the INSERT is
    guarded.
    """
    offenders = []
    for p in _deployed_migrations():
        sql = _read(p)
        # Strip comments — the retired seed is documented in prose and that
        # documentation should stay readable without tripping the test.
        live = "\n".join(
            line for line in sql.splitlines() if not line.lstrip().startswith("--")
        )
        if re.search(r"INSERT\s+INTO\s+source_(collection|database)", live, re.I):
            offenders.append(p.name)

    assert not offenders, (
        "these deployed migrations insert rows into the source_* tables, so a "
        f"deploy will resurrect them after a purge: {offenders}"
    )


def test_v013_migration_still_defines_reddit_columns():
    """The purge removed the rows, not the schema.

    app.py still SELECTs reddit_post_id / reddit_url, so dropping the column
    definitions along with the seed would 500 the sighting-detail endpoint.
    """
    sql = _read(SCRIPTS / "add_v013_reddit_columns.sql")
    for col in ("reddit_post_id", "reddit_url", "llm_confidence"):
        assert col in sql, f"v0.13 migration lost the {col} column definition"


def test_purge_script_targets_source_db_id_not_origin():
    """MUFON reaching the corpus via UFOCAT must survive.

    `sighting` records provenance twice: source_db_id (which import made the
    row) and origin_id / source_ref (who originally reported it). The purge
    must key on the former only.
    """
    sql = _read(SCRIPTS / "purge_mufon_csv_and_reddit.sql")
    live = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "source_db_id" in live
    assert "origin_id" not in live, (
        "purge must never filter on origin_id — that would delete "
        "MUFON-originated records carried by UFOCAT and UFO-search"
    )
