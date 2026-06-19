# Arby — Agent Instructions

## Project
Sports betting arbitrage bot for Argentine soccer. Cross-platform Dutch book detection
with LLM partition validation and agentic execution. See `docs/architecture.md` for the
full design.

## Stack & code style
- Python 3.13 (pinned in `.python-version`). Type hints everywhere; mypy runs strict.
- ruff for lint + format (configured in pyproject.toml). Don't argue with ruff's choices.
- Prefer pure functions and dataclasses over classes with mutable state.
- Async for I/O (httpx, sqlalchemy, redis); sync for pure computation.
- Docstrings on public functions/classes; skip them on obvious one-liners.
- Deps and venv via `uv` (not pip/poetry).

## Correctness-critical modules (keep on the frontier tier, review every change)
- `src/arbitrage/` — Dutch book detection. Deterministic math; bugs here lose money.
- `src/risk/` — all financial decisions flow through here.
Do not let the value/execute model own these. Plan and review changes here on the
frontier tier, and run the review pass without exception.

## Architectural rules (non-negotiable — about the product runtime, not the coding model)
- No LLM calls in `src/arbitrage/`. That layer is deterministic math.
- No LLM calls in `src/ingestion/scrapers/` at runtime. Scrapers are dumb and fast.
- All financial decisions go through `src/risk/`. The execution agent does not decide profitability.
- Every partition validation decision is logged to `partition_validations` for audit.

## Conventions
- Logging: `structlog.get_logger(__name__)`, never stdlib `logging`. JSON output by default;
  call `configure_logging()` once at process startup, use `.bind(...)` for context elsewhere.
- Config via `src.config.get_settings()`, never `os.environ` directly.
- DB access via `src.storage.db.get_session()` context manager.
- Money values: float for now; revisit if precision becomes a concern.

## When making changes
- Run `uv run pytest tests/unit/` (or `-m "not integration"`) before calling anything "done."
- New tests go in `tests/unit/` unless they need running infra → `tests/integration/` with
  the `integration` marker. Add the marker rather than skipping.
- Schema changes touch both `migrations/init.sql` and `src/storage/models.py` — keep them in sync.
- pytest runs `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed) and `--strict-markers`
  (`integration`, `slow` registered). New code must type-check clean under mypy strict.

## Do NOT
- Don't implement Kalman filters, Hurst index analysis, or NN return prediction — decided against.
- Don't add dependencies without explaining why. Every new dependency is a new failure mode.
- Don't call real bookmakers without explicit confirmation.

## Commands
```bash
uv sync                                       # install / sync deps from uv.lock
docker compose up -d                          # Postgres (TimescaleDB) + Redis
uv run pytest                                 # all tests (coverage on via addopts)
uv run pytest -m "not integration"            # unit-only
uv run pytest tests/unit/test_dutch_book.py -v
uv run pytest tests/unit/test_dutch_book.py::test_name -v
uv run ruff check --fix src tests && uv run ruff format src tests
uv run mypy src
```