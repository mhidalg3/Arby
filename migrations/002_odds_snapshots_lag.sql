-- Incremental migration: extend odds_snapshots for the cross-platform lag model.
-- Idempotent (ADD COLUMN IF NOT EXISTS) — safe to re-run on an existing live DB.
-- Fresh installs get these columns directly via migrations/init.sql.

ALTER TABLE odds_snapshots
    ADD COLUMN IF NOT EXISTS platform_market_id  TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS platform_outcome_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS raw_event_name      TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS raw_competition     TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS transport           TEXT NOT NULL DEFAULT 'poll',
    ADD COLUMN IF NOT EXISTS market_code         TEXT,
    ADD COLUMN IF NOT EXISTS line                DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cell                TEXT,
    ADD COLUMN IF NOT EXISTS fixture_key         TEXT,
    ADD COLUMN IF NOT EXISTS kickoff_utc         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS is_change           BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS prev_observed_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recorder_session_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS session_fixture_id  TEXT;

CREATE INDEX IF NOT EXISTS idx_odds_canonical_time
    ON odds_snapshots (fixture_key, market_code, line, cell, time DESC)
    WHERE fixture_key IS NOT NULL AND market_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_odds_session_time
    ON odds_snapshots (recorder_session_id, session_fixture_id, market_code, line, cell, time DESC)
    WHERE session_fixture_id IS NOT NULL;

SELECT add_retention_policy('odds_snapshots', INTERVAL '45 days', if_not_exists => TRUE);
