-- 001_create_museums.sql
CREATE TABLE IF NOT EXISTS museums (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Seed the demo instance. The id stays 'demo-library' because it is embedded in
-- every MQTT topic and in device_config.yaml on each Pi; only the display name
-- describes the current content.
INSERT INTO museums (id, name) VALUES
    ('demo-library', 'Dr. B.R. Ambedkar Memorial Museum')
ON CONFLICT (id) DO NOTHING;
