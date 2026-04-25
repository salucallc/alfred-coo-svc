CREATE SCHEMA IF NOT EXISTS infisical;

-- Example table for storing secrets
CREATE TABLE IF NOT EXISTS infisical.secrets (
    id SERIAL PRIMARY KEY,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Additional tables would be created by Infisical migrations; this ensures the schema exists.
