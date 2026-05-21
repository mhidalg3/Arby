# Arby — Agent Instructions

## Project context

Sports betting arbitrage bot for Argentine soccer. Cross-platform Dutch book detection with LLM partition validation and agentic execution. See `docs/architecture.md` for the full design.

## Code style

- Python 3.11+. Use type hints everywhere; we run mypy in strict mode.
- Format with ruff (configured in pyproject.toml). Don't argue with ruff's choices.
- Prefer pure functions and dataclasses over classes with mutable state.
- Async for I/O (httpx, sqlalchemy, redis). Sync for pure computation.
- Docstrings on public functions and classes. Skip them on obvious one-liners.

## Architectural rules (non-negotiable)

- **No LLM calls in `src/arbitrage/`.** That layer is deterministic math.
- **No LLM calls in `src/ingestion/scrapers/`** at runtime. Scrapers are dumb and fast.
- **All financial decisions go through `src/risk/`.** The execution agent does not decide profitability.
- **Every partition validation decision is logged** to `partition_validations` for audit.

## Conventions

- Use `structlog.get_logger(__name__)`, not stdlib logging.
- Config via `src.config.get_settings()`, never `os.environ` directly.
- DB access via `src.storage.db.get_session()` context manager.
- Money values: float for now; revisit if precision becomes a concern.

## When making changes

- Run `uv run pytest tests/unit/` before suggesting anything is "done."
- New tests go in `tests/unit/` unless they need running infra (then `tests/integration/` with the `integration` marker).
- Schema changes touch both `migrations/init.sql` and `src/storage/models.py`. They must stay in sync.

## Tone preferences

- Write with unwavering commitment to truth and accuracy as your primary objective. 
- Prioritize factual precision over palatability, and don't shy away from critical analysis when warranted. 
- Deliver constructive criticism directly and clearly, focusing on substantive issues rather than softening language. Use evidence-based reasoning and acknowledge complexity where it exists. 
- Maintain a respectful but unflinching tone that values intellectual honesty, like a colleague. 
- Avoid euphemisms, hedging language, or false reassurance when the situation calls for candor. 
- Structure your responses to be both rigorous and helpful, ensuring that directness serves the goal of genuine improvement rather than mere negativity.
- Be direct and concise if possible. 

## What NOT to do

- Don't suggest implementing Kalman filters, Hurst index analysis, or NN return prediction. We decided against these.
- Don't add dependencies without explaining why. Every new dependency is a new failure mode.
- Don't write code that calls real bookmakers without explicit confirmation. The risk surface is too high.

## Commands

Dependency and venv management is via `uv` (not pip/poetry). Python is pinned to 3.11 in `.python-version`.

```bash
# Install / sync deps from uv.lock
uv sync

# Bring up Postgres (TimescaleDB) + Redis
docker compose up -d

# Run all tests (coverage is on by default via addopts in pyproject.toml)
uv run pytest

# Unit-only (skip integration tests that need postgres/redis)
uv run pytest -m "not integration"

# Single test file / single test
uv run pytest tests/unit/test_dutch_book.py -v
uv run pytest tests/unit/test_dutch_book.py::test_name -v

# Lint + format + types
uv run ruff check --fix src tests
uv run ruff format src tests
uv run mypy src
```

`pytest` runs in `asyncio_mode = "auto"`, so async tests don't need `@pytest.mark.asyncio`. Two markers are registered and enforced via `--strict-markers`: `integration` (needs running infra) and `slow`. Add the marker rather than skipping.

mypy is configured `strict = true`. New code should type-check clean.

## Logging

`structlog` with JSON output by default. Call `configure_logging()` once at process startup; everywhere else use `structlog.get_logger(__name__)` and `.bind(...)` for context. Don't use stdlib `logging` directly.