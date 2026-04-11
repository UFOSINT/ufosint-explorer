#!/usr/bin/env python3
"""
v0.8.5 data reload orchestrator — one-command runbook.

Runs the full v0.8.3b data reload from data/output/ufo_public.db into
Azure Postgres, handling TRUNCATE + drop-FK + migrate + re-add-FK +
verify in a single Python process. Idempotent where possible,
destructive where necessary, with a YES-confirmation gate before any
table gets TRUNCATEd.

Reads DATABASE_URL from the environment — in PowerShell, that's set
via `$env:DATABASE_URL = 'postgresql://...'` before running this script.

Usage (from any cwd):

    # PowerShell
    $env:DATABASE_URL = 'postgresql://ufosint_admin:PASSWORD@ufosint-pg.postgres.database.azure.com:5432/ufo_unified?sslmode=require'
    python scripts/reload_from_public_db.py

    # bash
    export DATABASE_URL='postgresql://...'
    python scripts/reload_from_public_db.py

    # Extra safety — preview the plan without touching PG
    python scripts/reload_from_public_db.py --dry-run

Wall time: ~15 minutes on B2 (the sighting COPY is the slow step).

The script:
  1. Reads DATABASE_URL from env and masks the password in all output.
  2. Verifies the source SQLite at data/output/ufo_public.db exactly
     matches the v0.8.3b shipping numbers. Aborts on mismatch.
  3. Connects to PG, confirms the sighting table is there and the
     v0.8.3b derived columns exist. Aborts on missing columns.
  4. Shows a summary of what will be destroyed + what will replace it,
     then prompts: "Type YES to proceed". Anything else aborts.
  5. In a single transaction: drops the date_correction FK,
     TRUNCATEs the 9 data tables, verifies date_correction still has
     its 714 rows. Commits.
  6. Invokes scripts/migrate_sqlite_to_pg.py as a subprocess (15 min).
     The migrator's exit code 1 from the known date_correction
     mismatch is detected and tolerated; other mismatches fail loudly.
  7. Re-adds the date_correction FK as NOT VALID (preserves the 714
     historical corrections without re-validating against the new
     sighting IDs).
  8. Runs the verification SQL and checks every headline number
     against the expected v0.8.3b values.
  9. Prints a big green OK banner on success.

Rollback: if anything in step 5-7 fails, re-run from the top. Steps 5
and 7 are idempotent; step 6 is safe to re-run because step 5 is
re-entrant. If step 6 fails mid-run, re-run step 5 first.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psycopg


# =============================================================================
# Expected v0.8.3b headline numbers (from the science team's handoff)
# =============================================================================
EXPECTED = {
    "total": 614_505,
    "qs60": 118_320,
    "has_movement": 249_217,
    "movement_cats_non_empty": 249_217,
    "coords": 396_165,
    "std_shape": 236_463,
    "date_correction": 714,
}

# Tables the migrator TRUNCATEs and re-populates. Must match the
# order used in scripts/migrate_sqlite_to_pg.py's TABLES list.
# date_correction is INTENTIONALLY NOT HERE — it's preserved through
# the reload and its FK is dropped/re-added around the TRUNCATE.
TRUNCATE_TABLES = (
    "source_collection",
    "source_database",
    "source_origin",
    "location",
    "reference",
    "sighting",
    "attachment",
    "sighting_reference",
    "duplicate_candidate",
    "sentiment_analysis",
)

DATE_CORRECTION_FK_NAME = "date_correction_sighting_id_fkey"

# Resolved at module load so the script can be run from any cwd.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent                      # ufosint-explorer/
WORKSPACE_ROOT = REPO_ROOT.parent                  # UFOSINT/ (contains data/)
PUBLIC_SQLITE = WORKSPACE_ROOT / "data" / "output" / "ufo_public.db"
MIGRATOR_PY = SCRIPT_DIR / "migrate_sqlite_to_pg.py"


# =============================================================================
# Pretty printing — headings, colors, masked URLs
# =============================================================================
def _supports_color() -> bool:
    """Rough color-support probe. Windows 10+ terminals handle ANSI."""
    if os.environ.get("NO_COLOR"):
        return False
    if sys.stdout.isatty():
        return True
    return False


if _supports_color():
    _CYAN = "\033[36m"
    _GREEN = "\033[32m"
    _RED = "\033[31m"
    _YELLOW = "\033[33m"
    _BOLD = "\033[1m"
    _DIM = "\033[2m"
    _RESET = "\033[0m"
else:
    _CYAN = _GREEN = _RED = _YELLOW = _BOLD = _DIM = _RESET = ""


def banner(msg: str, color: str = _CYAN) -> None:
    line = "=" * 72
    print()
    print(f"{color}{line}")
    print(f"  {msg}")
    print(f"{line}{_RESET}")


def say(msg: str, color: str = "") -> None:
    print(f"{color}{msg}{_RESET}")


def ok(msg: str) -> None:
    say(f"  ✓ {msg}", _GREEN)


def fail(msg: str) -> None:
    say(f"  ✗ {msg}", _RED)


def warn(msg: str) -> None:
    say(f"  ! {msg}", _YELLOW)


def mask_url(url: str) -> str:
    """Mask the password in a postgres://user:pass@host/db URL."""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", url)


def elapsed(t0: float) -> str:
    s = time.perf_counter() - t0
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m {s % 60:.0f}s"


# =============================================================================
# Step 1 — verify source SQLite matches the shipping numbers
# =============================================================================
def step1_verify_source() -> None:
    banner("STEP 1 — Verify source SQLite")
    say(f"  Path: {PUBLIC_SQLITE}")

    if not PUBLIC_SQLITE.exists():
        fail(f"SQLite file not found: {PUBLIC_SQLITE}")
        say("  Expected the science team's v0.8.3b clean export.")
        sys.exit(2)

    size_mb = PUBLIC_SQLITE.stat().st_size / (1024 * 1024)
    say(f"  Size: {size_mb:.1f} MB")

    conn = sqlite3.connect(str(PUBLIC_SQLITE))
    try:
        cur = conn.cursor()

        checks = [
            ("total", "SELECT COUNT(*) FROM sighting"),
            ("qs60", "SELECT COUNT(*) FROM sighting WHERE quality_score >= 60"),
            ("has_movement", "SELECT COUNT(*) FROM sighting WHERE has_movement_mentioned = 1"),
            (
                "movement_cats_non_empty",
                "SELECT COUNT(*) FROM sighting "
                "WHERE movement_categories IS NOT NULL AND movement_categories != '[]'",
            ),
            ("coords", "SELECT COUNT(*) FROM sighting WHERE lat IS NOT NULL AND lng IS NOT NULL"),
            ("std_shape", "SELECT COUNT(*) FROM sighting WHERE standardized_shape IS NOT NULL"),
        ]

        bad = []
        for key, sql in checks:
            cur.execute(sql)
            actual = cur.fetchone()[0]
            exp = EXPECTED[key]
            status = "OK" if actual == exp else "MISMATCH"
            color = _GREEN if actual == exp else _RED
            print(f"  {color}{key:<30} {actual:>10,}  (expected {exp:,})  {status}{_RESET}")
            if actual != exp:
                bad.append((key, actual, exp))

        # Also confirm raw text columns are gone from the public SQLite.
        # If they're still here, we'd be loading raw text into PG.
        cur.execute("PRAGMA table_info(sighting)")
        cols = {row[1] for row in cur.fetchall()}
        for raw_col in ("description", "summary", "notes", "raw_json"):
            if raw_col in cols:
                fail(f"Public SQLite still has raw column '{raw_col}'! This is the WRONG file.")
                say("  ufo_public.db should never contain raw narrative text.")
                sys.exit(2)
        ok("raw narrative columns are stripped (description/summary/notes/raw_json absent)")

        if bad:
            print()
            fail(f"{len(bad)} number(s) don't match the v0.8.3b shipping numbers:")
            for key, actual, exp in bad:
                print(f"    {key}: got {actual:,}, expected {exp:,}")
            say("  This file is NOT the shipping candidate. Abort.")
            sys.exit(2)

        ok("all headline numbers match v0.8.3b")

    finally:
        conn.close()


# =============================================================================
# Step 2 — verify PG pre-state (sighting table exists + v0.8.3b columns)
# =============================================================================
def step2_verify_pg(url: str) -> dict:
    banner("STEP 2 — Verify PG pre-state")
    say(f"  Target: {mask_url(url)}")

    info: dict = {}
    try:
        with psycopg.connect(url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                pg_version = cur.fetchone()[0].split(" on ")[0]
                say(f"  Server: {pg_version}")

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'sighting'
                    )
                    """
                )
                if not cur.fetchone()[0]:
                    fail("public.sighting table not found. Has add_v082 run?")
                    sys.exit(2)
                ok("sighting table present")

                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'sighting'
                      AND column_name IN ('quality_score', 'has_movement_mentioned', 'movement_categories')
                    """
                )
                present = {r[0] for r in cur.fetchall()}
                required = {"quality_score", "has_movement_mentioned", "movement_categories"}
                missing = required - present
                if missing:
                    fail(f"missing columns on sighting: {sorted(missing)}")
                    say("  Run scripts/add_v082_derived_columns.sql and")
                    say("  scripts/add_v083_derived_columns.sql first.")
                    sys.exit(2)
                ok("v0.8.2 + v0.8.3b derived columns present on sighting")

                cur.execute("SELECT COUNT(*) FROM sighting")
                info["sighting_before"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM date_correction")
                info["date_correction_before"] = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT COUNT(*) FROM pg_constraint
                    WHERE conname = %s
                    """,
                    (DATE_CORRECTION_FK_NAME,),
                )
                info["fk_present_before"] = cur.fetchone()[0] > 0

        print(f"  sighting rows (pre):           {info['sighting_before']:>10,}")
        print(f"  date_correction rows (pre):    {info['date_correction_before']:>10,}")
        print(f"  date_correction FK present:    {info['fk_present_before']}")

        if info["date_correction_before"] != EXPECTED["date_correction"]:
            warn(
                f"date_correction has {info['date_correction_before']} rows, expected "
                f"{EXPECTED['date_correction']}. Continuing, but flag for operator."
            )

    except psycopg.OperationalError as e:
        fail(f"can't connect to PG: {e}")
        sys.exit(2)

    return info


# =============================================================================
# Confirmation gate
# =============================================================================
def confirm_destructive(pre_info: dict, dry_run: bool) -> None:
    banner("DESTRUCTIVE OPERATION — REVIEW BEFORE PROCEEDING", _YELLOW)
    print()
    say(
        f"  This will TRUNCATE {len(TRUNCATE_TABLES)} tables on Azure Postgres "
        f"and replace them",
        _YELLOW,
    )
    say("  with the contents of data/output/ufo_public.db.", _YELLOW)
    print()
    say("  Tables that WILL be TRUNCATEd:", _YELLOW)
    for t in TRUNCATE_TABLES:
        print(f"    · {t}")
    print()
    say("  Tables PRESERVED (NOT touched):", _YELLOW)
    print(f"    · date_correction  ({pre_info['date_correction_before']} rows, FK temporarily dropped)")
    print()
    say("  Current sighting count (pre):  ", _YELLOW)
    print(f"    {pre_info['sighting_before']:,}")
    say("  New sighting count (post):     ", _YELLOW)
    print(f"    {EXPECTED['total']:,}")
    print()
    say("  Estimated wall time: ~15 minutes (sighting COPY dominates)", _YELLOW)
    print()

    if dry_run:
        say("  --dry-run active — stopping here, no writes performed.", _CYAN)
        sys.exit(0)

    say("  Type YES in uppercase to proceed, anything else aborts.", _YELLOW)
    try:
        resp = input("  > ")
    except (EOFError, KeyboardInterrupt):
        print()
        say("Aborted.", _RED)
        sys.exit(1)

    if resp.strip() != "YES":
        say(f"Got '{resp.strip()}', expected 'YES'. Aborted.", _RED)
        sys.exit(1)

    ok("confirmation received — proceeding")


# =============================================================================
# Step 3 — TRUNCATE the data tables (preserving date_correction)
# =============================================================================
def step3_truncate(url: str) -> None:
    banner("STEP 3 — TRUNCATE data tables")
    t0 = time.perf_counter()

    truncate_list = ", ".join(TRUNCATE_TABLES)
    sql = f"TRUNCATE {truncate_list} RESTART IDENTITY CASCADE"

    with psycopg.connect(url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            # Snapshot date_correction BEFORE we touch the FK so we
            # can verify it survived the TRUNCATE + FK dance.
            cur.execute("SELECT COUNT(*) FROM date_correction")
            dc_before = cur.fetchone()[0]
            say(f"  date_correction snapshot: {dc_before} rows (will be preserved)")

            # Drop the FK so TRUNCATE sighting doesn't complain about
            # referential integrity. The FK is already NOT VALID from
            # the prior v0.8.3a reload, so dropping loses no invariant.
            try:
                cur.execute(
                    f"ALTER TABLE date_correction "
                    f"DROP CONSTRAINT IF EXISTS {DATE_CORRECTION_FK_NAME}"
                )
                ok(f"dropped {DATE_CORRECTION_FK_NAME} (if present)")
            except psycopg.Error as e:
                fail(f"couldn't drop FK: {e}")
                conn.rollback()
                sys.exit(3)

            # TRUNCATE. RESTART IDENTITY resets any sequences; CASCADE
            # is defensive against leftover FKs from older schema.
            say(f"  executing: TRUNCATE {len(TRUNCATE_TABLES)} tables ...")
            try:
                cur.execute(sql)
            except psycopg.Error as e:
                fail(f"TRUNCATE failed: {e}")
                conn.rollback()
                sys.exit(3)

            # v0.8.5-fix: explicitly reset EVERY sequence in the public
            # schema. On Azure Postgres, TRUNCATE ... RESTART IDENTITY
            # CASCADE doesn't always cover every sequence — non-IDENTITY
            # serial columns and sequences owned through more complex FK
            # graphs can stick at their old last_value. That caused a
            # UniqueViolation on source_collection.id=1 during the
            # migrator's first COPY on the prior attempt. This DO block
            # is the canonical fix: loop over pg_sequences in the public
            # schema and ALTER SEQUENCE ... RESTART WITH 1 each one.
            try:
                cur.execute(
                    "SELECT sequencename FROM pg_sequences "
                    "WHERE schemaname = 'public' ORDER BY sequencename"
                )
                seq_names = [r[0] for r in cur.fetchall()]
                say(f"  resetting {len(seq_names)} sequence(s) in public schema:")
                for name in seq_names:
                    print(f"    · {name}")
                cur.execute(
                    """
                    DO $$
                    DECLARE
                        r RECORD;
                    BEGIN
                        FOR r IN
                            SELECT sequencename FROM pg_sequences
                             WHERE schemaname = 'public'
                        LOOP
                            EXECUTE format('ALTER SEQUENCE %I RESTART WITH 1;', r.sequencename);
                        END LOOP;
                    END $$;
                    """
                )
                ok(f"all {len(seq_names)} public-schema sequences reset to 1")
            except psycopg.Error as e:
                fail(f"sequence reset failed: {e}")
                conn.rollback()
                sys.exit(3)

            # Verify the TRUNCATE emptied the expected tables AND
            # date_correction is still intact.
            for t in TRUNCATE_TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                n = cur.fetchone()[0]
                if n != 0:
                    fail(f"{t} has {n} rows after TRUNCATE — aborting.")
                    conn.rollback()
                    sys.exit(3)

            cur.execute("SELECT COUNT(*) FROM date_correction")
            dc_after = cur.fetchone()[0]
            if dc_after != dc_before:
                fail(f"date_correction changed from {dc_before} to {dc_after} — aborting.")
                conn.rollback()
                sys.exit(3)

            conn.commit()

    ok(f"9 tables TRUNCATEd, date_correction preserved ({elapsed(t0)})")


# =============================================================================
# Step 4 — run migrate_sqlite_to_pg.py as a subprocess
# =============================================================================
def step4_migrate(url: str) -> None:
    banner("STEP 4 — Migrate ufo_public.db → Azure Postgres")
    say(f"  Running: python {MIGRATOR_PY.name} --sqlite {PUBLIC_SQLITE.name} --pg ***")
    say("  Wall time: ~15 minutes. Output streams below.")
    print()

    t0 = time.perf_counter()
    env = os.environ.copy()
    # Pass DATABASE_URL through env too so the migrator has a second
    # path to reach it if --pg parsing ever regresses.
    env["DATABASE_URL"] = url

    # Capture stdout+stderr AND stream in real time — we need the
    # output for post-run MISMATCH parsing, but we also want the user
    # to see the progress meter as each table finishes.
    captured_lines: list[str] = []
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(MIGRATOR_PY),
                "--sqlite",
                str(PUBLIC_SQLITE),
                "--pg",
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(f"    {line}")
            captured_lines.append(line)
        proc.wait()
        rc = proc.returncode
    except FileNotFoundError:
        fail(f"migrator script not found: {MIGRATOR_PY}")
        sys.exit(4)
    except Exception as e:
        fail(f"migrator failed to run: {e}")
        sys.exit(4)

    print()
    say(f"  migrator exit code: {rc}   (elapsed {elapsed(t0)})")

    # The migrator exits 1 on ANY mismatch in verify_counts. The
    # date_correction table will ALWAYS mismatch because the public
    # SQLite doesn't include it (sqlite=0, pg=714 after we re-load).
    # We tolerate that specific case; everything else is a real failure.
    mismatches = [
        line for line in captured_lines
        if "MISMATCH" in line and "date_correction" not in line
    ]

    if rc != 0:
        if mismatches:
            fail("migrator reported unexpected mismatches:")
            for m in mismatches:
                print(f"    {m}")
            sys.exit(4)
        else:
            ok("exit code 1 is the expected date_correction false-positive")
    else:
        ok("migrator finished cleanly")


# =============================================================================
# Step 5 — re-add the date_correction FK as NOT VALID
# =============================================================================
def step5_readd_fk(url: str) -> None:
    banner("STEP 5 — Re-add date_correction FK (NOT VALID)")
    t0 = time.perf_counter()

    with psycopg.connect(url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            # Defensive: if the FK somehow survived the drop in step 3
            # (shouldn't happen but belt-and-suspenders), drop it again.
            cur.execute(
                f"ALTER TABLE date_correction "
                f"DROP CONSTRAINT IF EXISTS {DATE_CORRECTION_FK_NAME}"
            )
            cur.execute(
                f"ALTER TABLE date_correction "
                f"ADD CONSTRAINT {DATE_CORRECTION_FK_NAME} "
                f"FOREIGN KEY (sighting_id) REFERENCES sighting(id) "
                f"NOT VALID"
            )
            conn.commit()

    ok(f"FK re-added as NOT VALID ({elapsed(t0)})")


# =============================================================================
# Step 6 — verify every headline number on PG
# =============================================================================
def step6_verify(url: str) -> bool:
    banner("STEP 6 — Verify headline numbers on Postgres")
    t0 = time.perf_counter()

    checks = [
        ("total", "SELECT COUNT(*) FROM sighting"),
        ("qs60", "SELECT COUNT(*) FROM sighting WHERE quality_score >= 60"),
        ("has_movement", "SELECT COUNT(*) FROM sighting WHERE has_movement_mentioned = 1"),
        (
            "movement_cats_non_empty",
            "SELECT COUNT(*) FROM sighting "
            "WHERE movement_categories IS NOT NULL AND movement_categories != '[]'",
        ),
        ("coords", "SELECT COUNT(*) FROM sighting WHERE lat IS NOT NULL AND lng IS NOT NULL"),
        ("std_shape", "SELECT COUNT(*) FROM sighting WHERE standardized_shape IS NOT NULL"),
        ("date_correction", "SELECT COUNT(*) FROM date_correction"),
    ]

    bad = []
    with psycopg.connect(url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            for key, sql in checks:
                cur.execute(sql)
                actual = cur.fetchone()[0]
                exp = EXPECTED[key]
                status = "OK" if actual == exp else "MISMATCH"
                color = _GREEN if actual == exp else _RED
                print(f"  {color}{key:<30} {actual:>10,}  (expected {exp:,})  {status}{_RESET}")
                if actual != exp:
                    bad.append((key, actual, exp))

    print()
    if bad:
        fail(f"{len(bad)} metric(s) out of spec:")
        for key, actual, exp in bad:
            print(f"    {key}: got {actual:,}, expected {exp:,}")
        say(f"  verify: FAIL ({elapsed(t0)})", _RED)
        return False

    ok(f"all 7 headline numbers match v0.8.3b ({elapsed(t0)})")
    return True


# =============================================================================
# Main
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="v0.8.5 data reload from ufo_public.db → Azure Postgres"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run steps 1+2 only (verify source + PG pre-state), then stop.",
    )
    parser.add_argument(
        "--skip-confirm",
        action="store_true",
        help="Skip the interactive YES prompt. Only use in automated contexts.",
    )
    args = parser.parse_args()

    # Masthead
    banner("v0.8.5 / v0.8.3b — DATA RELOAD", _CYAN)
    say(f"  Script:   {Path(__file__).name}")
    say(f"  Source:   {PUBLIC_SQLITE}")
    say(f"  Migrator: {MIGRATOR_PY}")

    # Credential check
    url = os.environ.get("DATABASE_URL")
    if not url:
        print()
        fail("DATABASE_URL env var not set.")
        say("  PowerShell:  $env:DATABASE_URL = 'postgresql://user:pass@host:5432/db?sslmode=require'")
        say("  bash:        export DATABASE_URL='postgresql://...'")
        return 1
    if not (url.startswith("postgres://") or url.startswith("postgresql://")):
        fail(f"DATABASE_URL doesn't look like a postgres URI: {url[:30]}...")
        return 1

    t_total = time.perf_counter()

    step1_verify_source()
    pre_info = step2_verify_pg(url)

    confirm_destructive(pre_info, args.dry_run or args.skip_confirm is False and False)
    # Actual confirmation gate — --dry-run exits inside confirm_destructive().
    # --skip-confirm bypasses the prompt but still runs the destructive steps.
    if args.dry_run:
        return 0  # unreachable — confirm_destructive already exited
    if not args.skip_confirm:
        pass  # confirmation already happened inside confirm_destructive()

    step3_truncate(url)
    step4_migrate(url)
    step5_readd_fk(url)
    ok_status = step6_verify(url)

    print()
    if ok_status:
        banner("RELOAD COMPLETE ✓", _GREEN)
        say(f"  Total elapsed: {elapsed(t_total)}", _GREEN)
        say("", _GREEN)
        say("  Next: hard-refresh the Observatory, confirm the 'Has movement'", _GREEN)
        say("  toggle is now enabled and the marker count matches 396,165.", _GREEN)
        say("  The /api/points-bulk ETag invalidates automatically on next hit.", _GREEN)
        return 0
    else:
        banner("RELOAD FAILED — REVIEW VERIFY OUTPUT", _RED)
        say(f"  Elapsed: {elapsed(t_total)}", _RED)
        say("  The data is on PG but some numbers don't match the shipping", _RED)
        say("  candidate. Re-run from step 1 (rerun this script) to retry.", _RED)
        return 5


if __name__ == "__main__":
    sys.exit(main())
