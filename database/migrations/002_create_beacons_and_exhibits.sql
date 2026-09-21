-- 002_create_beacons_and_exhibits.sql

CREATE OR REPLACE FUNCTION trigger_set_timestamp()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Exhibits (books, documents and monuments in our demo)
CREATE TABLE IF NOT EXISTS exhibits (
    id              TEXT NOT NULL,
    museum_id       TEXT NOT NULL REFERENCES museums(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    author          TEXT,                       -- attribution: author, architect, or body
    year_created    INTEGER,
    genre           TEXT,                       -- category, e.g. 'Monuments — Buddhist Stupa'
    location        TEXT,                       -- e.g. 'Gallery A, Bay 1'; NULL = not on the map
    tts_script      TEXT NOT NULL,              -- spoken on beacon trigger
    full_content    TEXT NOT NULL,              -- chunked into vector DB
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (id, museum_id)
);

CREATE TRIGGER set_updated_at
    BEFORE UPDATE ON exhibits
    FOR EACH ROW EXECUTE FUNCTION trigger_set_timestamp();

CREATE INDEX IF NOT EXISTS idx_exhibits_museum ON exhibits(museum_id);

-- Beacons (map UUID → exhibit)
CREATE TABLE IF NOT EXISTS beacons (
    uuid        TEXT NOT NULL,
    museum_id   TEXT NOT NULL REFERENCES museums(id) ON DELETE CASCADE,
    exhibit_id  TEXT NOT NULL,
    location    TEXT,
    PRIMARY KEY (uuid, museum_id)
);

CREATE INDEX IF NOT EXISTS idx_beacons_museum ON beacons(museum_id);

-- Device sessions
CREATE TABLE IF NOT EXISTS device_sessions (
    session_id       TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL,
    museum_id        TEXT NOT NULL,
    started_at       TIMESTAMPTZ DEFAULT NOW(),
    last_active      TIMESTAMPTZ DEFAULT NOW(),
    exhibits_visited TEXT[] DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_device ON device_sessions(device_id);
