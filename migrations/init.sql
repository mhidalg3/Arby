-- Enable TimescaleDB
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ===== Canonical event registry =====
-- A "match" is the underlying sporting event. Multiple platforms may name
-- the same match differently; the event_registry holds the canonical ID.
CREATE TABLE IF NOT EXISTS matches (
    id              BIGSERIAL PRIMARY KEY,
    canonical_key   TEXT NOT NULL UNIQUE,  -- e.g., "ARG-LPF-2026-03-15-RIVER-BOCA"
    competition     TEXT NOT NULL,
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL,
    kickoff_utc     TIMESTAMPTZ NOT NULL,
    is_knockout     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_matches_kickoff ON matches (kickoff_utc);

-- ===== Platform-specific event identifiers =====
-- Maps each platform's internal event ID to our canonical match.
CREATE TABLE IF NOT EXISTS platform_events (
    id              BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    platform_event_id TEXT NOT NULL,
    raw_event_name  TEXT NOT NULL,
    UNIQUE (platform, platform_event_id)
);

CREATE INDEX idx_platform_events_match ON platform_events (match_id);

-- ===== Canonical bet outcomes =====
-- "Team1 wins", "Team1 does not win", "BTTS Yes", "BTTS No", etc.
-- We define outcomes at the canonical level so cross-platform matching
-- reduces to comparing canonical_outcome_id values.
CREATE TABLE IF NOT EXISTS canonical_outcomes (
    id              BIGSERIAL PRIMARY KEY,
    match_id        BIGINT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    market_type     TEXT NOT NULL,   -- e.g., "match_result_binary", "btts", "first_scorer"
    outcome_key     TEXT NOT NULL,   -- e.g., "home_wins", "home_not_wins", "btts_yes"
    description     TEXT NOT NULL,   -- human-readable
    UNIQUE (match_id, market_type, outcome_key)
);

CREATE INDEX idx_canonical_outcomes_match ON canonical_outcomes (match_id);

-- ===== Partition pairs =====
-- Pre-validated pairs of canonical outcomes where P(A∪B)=1 and P(¬A∧¬B)=0.
-- The arbitrage engine only operates on entries in this table.
CREATE TABLE IF NOT EXISTS partition_pairs (
    id              BIGSERIAL PRIMARY KEY,
    outcome_a_id    BIGINT NOT NULL REFERENCES canonical_outcomes(id) ON DELETE CASCADE,
    outcome_b_id    BIGINT NOT NULL REFERENCES canonical_outcomes(id) ON DELETE CASCADE,
    validation_method TEXT NOT NULL, -- "rule_based" or "llm"
    validation_confidence DOUBLE PRECISION,  -- NULL for rule-based
    validated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (outcome_a_id < outcome_b_id),  -- canonical ordering to prevent duplicates
    UNIQUE (outcome_a_id, outcome_b_id)
);

-- ===== Raw odds snapshots (hypertable) =====
-- High-volume time-series data. Every poll appends a row per outcome per platform.
-- TimescaleDB hypertable for efficient time-range queries.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    time            TIMESTAMPTZ NOT NULL,
    platform        TEXT NOT NULL,
    platform_event_id TEXT NOT NULL,
    canonical_outcome_id BIGINT,  -- NULL until semantic layer resolves the match
    raw_market_name TEXT NOT NULL,
    raw_outcome_name TEXT NOT NULL,
    decimal_odds    DOUBLE PRECISION NOT NULL,
    max_stake       DOUBLE PRECISION,
    -- Cross-platform lag model columns (additive, all have defaults).
    platform_market_id  TEXT NOT NULL DEFAULT '',
    platform_outcome_id TEXT NOT NULL DEFAULT '',
    raw_event_name      TEXT NOT NULL DEFAULT '',
    raw_competition     TEXT NOT NULL DEFAULT '',
    transport           TEXT NOT NULL DEFAULT 'poll',
    market_code         TEXT,             -- canonical: h2h_3way|btts|ou
    line                DOUBLE PRECISION, -- OU line, else NULL
    cell                TEXT,             -- HOME|DRAW|AWAY|YES|NO|OVER|UNDER
    fixture_key         TEXT,             -- "home_n|away_n|YYYY-MM-DD" (kickoff UTC date); NULL when kickoff unknown
    kickoff_utc         TIMESTAMPTZ,
    is_change           BOOLEAN NOT NULL DEFAULT TRUE,
    prev_observed_at    TIMESTAMPTZ,      -- last observation of this (platform, outcome) BEFORE this row — censoring lower bound
    recorder_session_id TEXT NOT NULL DEFAULT '',  -- uuid minted once per pg_recorder process start
    session_fixture_id  TEXT,             -- the canonicalizer's in-process fx-<uuid> — stable WITHIN one recorder session only
    CHECK (decimal_odds > 1.0)
);

SELECT create_hypertable('odds_snapshots', 'time', if_not_exists => TRUE);

CREATE INDEX idx_odds_outcome_time
    ON odds_snapshots (canonical_outcome_id, time DESC)
    WHERE canonical_outcome_id IS NOT NULL;

CREATE INDEX idx_odds_platform_event
    ON odds_snapshots (platform, platform_event_id, time DESC);

-- Canonical time-series index for the lag analysis (cross-platform joins).
CREATE INDEX IF NOT EXISTS idx_odds_canonical_time
    ON odds_snapshots (fixture_key, market_code, line, cell, time DESC)
    WHERE fixture_key IS NOT NULL AND market_code IS NOT NULL;

-- Session-scoped fallback join for rows without kickoff (no fixture_key).
CREATE INDEX IF NOT EXISTS idx_odds_session_time
    ON odds_snapshots (recorder_session_id, session_fixture_id, market_code, line, cell, time DESC)
    WHERE session_fixture_id IS NOT NULL;

-- Bound storage: 45-day rolling window on the hypertable.
SELECT add_retention_policy('odds_snapshots', INTERVAL '45 days', if_not_exists => TRUE);

-- ===== Detected opportunities =====
-- One row per arbitrage opportunity surfaced to the engine.
-- Status tracks the lifecycle: detected → approved → executing → filled/aborted.
CREATE TYPE opportunity_status AS ENUM (
    'detected',
    'approved',
    'leg_a_pending',
    'leg_a_filled',
    'leg_b_pending',
    'completed',
    'aborted_pre_execution',
    'aborted_post_leg_a',  -- naked exposure incident
    'pending_unknown',     -- bet submitted, acceptance unconfirmed; halt + verify
    'expired',
    'frozen'               -- recovery failed / unexpected state — halt
);

-- The hot path is N-leg (2-outcome O/U and 3-outcome 1X2) with no partition-pair
-- provenance, so the legs live in a JSONB array and partition_pair_id is nullable.
-- A future semantic pipeline can still populate partition_pair_id.
CREATE TABLE IF NOT EXISTS opportunities (
    id              BIGSERIAL PRIMARY KEY,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    market_id       TEXT NOT NULL,   -- orchestrator's canonical market key (dedup id)
    partition_pair_id BIGINT REFERENCES partition_pairs(id),  -- NULL from the hot path
    legs            JSONB NOT NULL,  -- [{"platform","outcome","decimal_odds","target_stake"}, ...]
    expected_margin_pct DOUBLE PRECISION NOT NULL,  -- realized ROI shown in the alert
    expected_profit DOUBLE PRECISION NOT NULL,
    risk_confidence DOUBLE PRECISION,               -- RiskDecision.confidence (nullable)
    high_margin_warning BOOLEAN NOT NULL DEFAULT FALSE,  -- RiskDecision.high_margin_warning
    adaptive_threshold_pct DOUBLE PRECISION,        -- NULL from the hot path
    garch_variance  DOUBLE PRECISION,               -- NULL from the hot path
    status          opportunity_status NOT NULL DEFAULT 'detected',
    status_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    execution_reason TEXT                           -- ExecutionResult.reason (aborted/naked/frozen)
);

CREATE INDEX idx_opportunities_status ON opportunities (status, detected_at DESC);
CREATE INDEX idx_opportunities_market ON opportunities (market_id, detected_at DESC);

-- ===== Placements =====
-- One row per actual bet placement attempt. Linked to an opportunity.
-- `leg` is 'a','b','c',... — one per leg of an N-leg arb.
CREATE TABLE IF NOT EXISTS placements (
    id              BIGSERIAL PRIMARY KEY,
    opportunity_id  BIGINT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    leg             CHAR(1) NOT NULL CHECK (leg ~ '^[a-z]$'),
    platform        TEXT NOT NULL,
    submitted_at    TIMESTAMPTZ NOT NULL,
    confirmed_at    TIMESTAMPTZ,
    target_odds     DOUBLE PRECISION NOT NULL,
    actual_filled_odds DOUBLE PRECISION,
    target_stake    DOUBLE PRECISION NOT NULL,
    actual_stake    DOUBLE PRECISION,
    platform_bet_id TEXT,
    success         BOOLEAN,
    error_message   TEXT,
    raw_response    JSONB
);

CREATE INDEX idx_placements_opportunity ON placements (opportunity_id);

-- ===== Audit log for LLM validations =====
-- Every partition validation decision is logged for later evaluation.
CREATE TABLE IF NOT EXISTS partition_validations (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    desc_a          TEXT NOT NULL,
    desc_b          TEXT NOT NULL,
    match_context   JSONB NOT NULL,
    method          TEXT NOT NULL,  -- "rule_based" or "llm"
    model           TEXT,           -- LLM model name, NULL for rule-based
    result          TEXT NOT NULL,  -- "valid", "invalid", "needs_review"
    confidence      DOUBLE PRECISION,
    reasoning       TEXT,
    edge_cases      JSONB
);

CREATE INDEX idx_validations_created ON partition_validations (created_at DESC);
