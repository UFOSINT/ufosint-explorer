#!/usr/bin/env python3
"""Run the v0.11 PG migration (add emotion columns + indexes).

Usage:
    $env:DATABASE_URL = 'postgresql://...'
    python scripts/run_v011_migration.py
"""
import os
import sys

try:
    import psycopg
except ImportError:
    print("ERROR: psycopg not installed. Run: pip install psycopg[binary]")
    sys.exit(1)

url = os.environ.get("DATABASE_URL")
if not url:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)

print("Connecting to:", url.split("@")[1].split("/")[0])
conn = psycopg.connect(url, autocommit=True)
cur = conn.cursor()

# Step 1: Add columns
alters = [
    (
        "ALTER TABLE sighting "
        "ADD COLUMN IF NOT EXISTS emotion_28_dominant TEXT, "
        "ADD COLUMN IF NOT EXISTS emotion_28_group TEXT"
    ),
    "ALTER TABLE sighting ADD COLUMN IF NOT EXISTS emotion_7_dominant TEXT",
    (
        "ALTER TABLE sighting "
        "ADD COLUMN IF NOT EXISTS vader_compound REAL, "
        "ADD COLUMN IF NOT EXISTS roberta_sentiment REAL"
    ),
    (
        "ALTER TABLE sighting "
        "ADD COLUMN IF NOT EXISTS emotion_7_surprise REAL, "
        "ADD COLUMN IF NOT EXISTS emotion_7_fear REAL, "
        "ADD COLUMN IF NOT EXISTS emotion_7_neutral REAL, "
        "ADD COLUMN IF NOT EXISTS emotion_7_anger REAL, "
        "ADD COLUMN IF NOT EXISTS emotion_7_disgust REAL, "
        "ADD COLUMN IF NOT EXISTS emotion_7_sadness REAL, "
        "ADD COLUMN IF NOT EXISTS emotion_7_joy REAL"
    ),
]

for i, sql in enumerate(alters):
    print(f"  ALTER [{i+1}/{len(alters)}]...", end=" ")
    cur.execute(sql)
    print("OK")

# Step 2: Create indexes
indexes = [
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_emo28 ON sighting(emotion_28_dominant)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_emo28g ON sighting(emotion_28_group)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sighting_emo7 ON sighting(emotion_7_dominant)",
]

for i, sql in enumerate(indexes):
    print(f"  INDEX [{i+1}/{len(indexes)}]...", end=" ")
    try:
        cur.execute(sql)
        print("OK")
    except Exception as e:
        print(f"({e})")

# Step 3: Verify
cur.execute(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'sighting' "
    "AND (column_name LIKE 'emotion%' "
    "     OR column_name IN ('vader_compound', 'roberta_sentiment')) "
    "ORDER BY column_name"
)
cols = [r[0] for r in cur.fetchall()]
print(f"\nNew columns on sighting: {len(cols)}")
for c in cols:
    print(f"  - {c}")

conn.close()
print("\nMigration complete! Now run:")
print("  python scripts/reload_from_public_db.py")
