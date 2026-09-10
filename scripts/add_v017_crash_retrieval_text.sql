-- v0.17 — crash_retrieval.craft_size_m is prose, not a number.
--
-- The column was declared `real`, but the source values are things like
-- '~10', '~30 (99 ft, per Scully)' and '' — hedged estimates with units
-- and citations, which is what a crash-retrieval catalogue actually
-- records. SQLite stores them happily; Postgres rejects them, so every
-- reload died with:
--
--   invalid input syntax for type real: ""
--   CONTEXT: COPY crash_retrieval, line 1, column craft_size_m: ""
--
-- That aborted the migrator after the real work was done. It went
-- unnoticed for several releases because crash_retrieval is copied last
-- and the reload script read the non-zero exit as a known false-positive.
--
-- crew_count in the same table is already `text` for the same reason, so
-- this brings craft_size_m into line rather than inventing a convention.
-- Coercing the values to a number instead would discard the hedge and the
-- citation, which are the informative parts.
--
-- Idempotent: re-running against an already-text column is a no-op.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'crash_retrieval'
          AND column_name = 'craft_size_m'
          AND data_type <> 'text'
    ) THEN
        ALTER TABLE crash_retrieval
            ALTER COLUMN craft_size_m TYPE text
            USING craft_size_m::text;
    END IF;
END $$;
