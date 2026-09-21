-- 003_session_persistence_and_fk.sql
--
-- 1. Adds referential integrity so a beacon cannot point at a missing exhibit.
-- 2. Prepares device_sessions for durable, queryable tour history
--    (the table existed since 002 but nothing wrote to it).
--
-- NOTE: migrations under /docker-entrypoint-initdb.d only auto-run on the FIRST
-- Postgres container start. To apply this to an existing volume, run it manually:
--   psql "$POSTGRES_DSN" -f database/migrations/003_session_persistence_and_fk.sql

-- ── 1. Referential integrity: beacons → exhibits ───────────────────────────────
-- exhibits PK is composite (id, museum_id), so the FK must be composite too.
-- Guarded in a DO block because Postgres has no ADD CONSTRAINT IF NOT EXISTS.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_beacons_exhibit'
    ) THEN
        ALTER TABLE beacons
            ADD CONSTRAINT fk_beacons_exhibit
            FOREIGN KEY (exhibit_id, museum_id)
            REFERENCES exhibits (id, museum_id)
            ON DELETE CASCADE;
    END IF;
END$$;

-- ── 2. Session tracking support ────────────────────────────────────────────────
-- Index to support "recently active sessions" / pruning queries.
CREATE INDEX IF NOT EXISTS idx_sessions_last_active
    ON device_sessions (museum_id, last_active DESC);
