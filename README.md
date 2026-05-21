# Arby
#    ________  ________  ________  ________ 
#   /        \/        \/       / /    /   \
#  /         /         /        \/         /
# /         /        _/         /\__      / 
# \___/____/\____/___/\________/   \_____/  

Cross-platform sports betting arbitrage detection and execution for Argentine soccer markets.

## Status

Day 1 skeleton. Do not run against real money. Do not run against credit capital until Phase 5 validation gates pass.

## Architecture

Four layers, separated by purpose and trust level:

1. **Ingestion** (deterministic). Platform scrapers poll odds via JSON APIs where available, Playwright otherwise. No LLM in the runtime loop.
2. **Semantic** (LLM). Bet description normalization, cross-platform matching, partition validation.
3. **Arbitrage engine** (deterministic). Dutch book detection, stake allocation, GARCH-adaptive thresholds.
4. **Execution** (agentic with hardcoded guardrails). Agent navigates platform UIs to place bets; all financial decisions enforced by deterministic code.

See `docs/architecture.md` for details.

## Setup

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Pin Python version
uv python pin 3.11

# Install dependencies
uv sync

# Install Playwright browsers
uv run playwright install chromium

# Bring up infrastructure
docker compose up -d

# Verify Postgres and Redis are healthy
docker compose ps

# Copy and edit environment file
cp .env.example .env
# Edit .env to set ANTHROPIC_API_KEY

# Run the skeleton test to verify wiring
uv run pytest tests/unit/test_skeleton.py -v
```

## Development workflow

```bash
# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=src --cov-report=html

# Lint and format
uv run ruff check --fix src tests
uv run ruff format src tests

# Type-check
uv run mypy src
```

## Project structure

```
arby/
├── docker-compose.yml          # Postgres (TimescaleDB) + Redis
├── pyproject.toml              # uv-managed project config
├── .env.example                # Template for local environment
├── migrations/                 # Database schema
│   └── init.sql
├── src/
│   ├── config.py               # Pydantic settings loaded from .env
│   ├── ingestion/              # Scrapers and odds normalization
│   ├── semantic/               # LLM-backed bet matching and partition validation
│   ├── arbitrage/              # Dutch book math, stake allocation, GARCH thresholds
│   ├── risk/                   # Exposure limits and kill switch
│   ├── execution/              # Agentic placement with deterministic guardrails
│   └── storage/                # SQLAlchemy models and repository pattern
├── tests/
│   ├── unit/                   # Pure-function tests (run by default)
│   ├── integration/            # Tests requiring Postgres/Redis (marked `integration`)
│   └── fixtures/               # Recorded odds snapshots, labeled partition cases
├── scripts/                    # One-off utilities (backfill, evaluation)
└── docs/
    ├── architecture.md
    └── platform_notes/         # Per-platform reverse-engineering notes
```

## Phases

This codebase implements Phase 0 only. Subsequent phases per the build doc:

- Phase 1 — Dutch book engine in isolation (`src/arbitrage/dutch_book.py`)
- Phase 2 — Partition validator with 100-case labeled test set
- Phase 3 — Single-platform scraper end-to-end
- Phase 4 — GARCH-adaptive thresholds
- Phase 5 — Execution layer with hardcoded guardrails

Each phase has a validation gate. Do not advance until the prior gate is met.

## Risks

This project will likely fail at execution speed against professional arb desks on liquid markets. Edge, if any, exists in prop bets (BTTS, first scorer, correct score) where retail desks update more slowly. Account longevity is the dominant operational risk for any non-credit deployment. See `docs/risks.md`.
