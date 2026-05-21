---
name: phase-validation-gate
description: Runs the validation gate checks for the current arby development phase and reports pass/fail. Use when finishing work on a phase, before moving to the next phase, or when the user asks "am I ready for phase N+1?". Triggers on "validation gate", "phase complete", "ready for next phase".
---

# Phase validation gate

Check whether the current phase's validation criteria are met before advancing.

## Phase 1 (Dutch book engine)

Run:
- `uv run pytest tests/unit/test_dutch_book.py -v --cov=src/arbitrage --cov-report=term-missing`
- Verify coverage >= 95% on `src/arbitrage/dutch_book.py`
- Verify no skipped or xfailed tests
- Report PASS only if all three conditions hold

## Phase 2 (Partition validator)

Run:
- `uv run python scripts/eval_partition_validator.py --test-set tests/fixtures/partitions.yaml`
- Verify precision on the "valid" label >= 0.99
- Verify the test set contains at least 100 cases
- Verify at least 30% of cases are labeled "invalid"
- Report PASS only if all four conditions hold

## Phase 3 (Single platform scraper)

Run:
- `uv run python scripts/scrape_dry_run.py --platform <name> --duration 600`
- Verify success rate >= 99%
- Verify zero crashes
- Verify data completeness >= 95% vs. expected market count

## Output format

For each gate, report:
- ✅ or ❌ for each condition
- Overall PASS or FAIL
- If FAIL, what specifically needs to be fixed before advancing