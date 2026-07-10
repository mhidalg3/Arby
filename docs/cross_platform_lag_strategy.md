# Cross-platform update-lag strategy

Date: 2026-07-06

## Status

**Phase A/B landed; Phase C/D OFF until gate passes.**

The lag model is built and the analysis tool exists, but triggered scanning and
staleness-informed ordering are disabled by default (`TRIGGER_POLL=0`, no artifact file).
Live behavior is byte-identical to the pre-lag-model codebase until an operator runs the
bootstrap collection, the B report passes the gate, and the env knobs are set.

## Artifact key grammar

Artifact keys use the actual `CanonicalMarketCode` StrEnum values (`1x2`, `btts`,
`ou_goals`), not the plan's illustrative `h2h_3way`. Line markets carry the line suffix:
`ou_goals|2.5`. The `staleness_rank` object in `data/lag_model.json` is keyed by these
suffixes; `order_opportunity_for_execution` derives the same key via
`market_id.split("|", 1)[1]`.

## The lag-window model

Arbs appear when a price-leader platform updates and a laggard hasn't; the arb window IS
the lag. Modeling the cross-platform update-lag structure lets us:

1. **Trigger detection** on leader moves (concentrate requests on the laggard in the seconds
   after the leader moves — conditional arb probability is highest there).
2. **Order placement** so the laggard (perishable stale quote) goes first among legs.

## Estimator choices

- **Leader/lag attribution**: empirical quantiles with censoring bounds. Poll-observed
  changes are interval-censored: the true change happened in `(prev_observation, this_observation]`.
  Push feeds (`transport="push"`) are near-uncensored fine clocks. We report midpoint estimates
  plus pessimistic/optimistic bounds.
- **Discrepancy variance D(M)**: plain std of the time-aligned cross-platform implied-prob
  spread per market type. GARCH(1,1) on the spread is the one endorsed model
  (architecture.md:33,105-107) and now backs the live adaptive margin threshold (Phase E,
  below) via `--garch`.
- **No Hurst analysis** (AGENTS.md decided-against; architecture.md:102 — our mispricings are
  transient closes, not stationary mean-reversion).

## D/T/L/C disposition

| Term | Meaning | v1 status |
|------|---------|-----------|
| D(M) | Discrepancy variance | Spread std per market type (GARCH optional) |
| T | Trigger recency | Hot-set windows from `lag_p90_s` per market type |
| L(M) | Liquidity | Deferred to execution-time `cap_refresh` (public feeds emit `max_stake=None`) |
| C(M) | Capturability | Uniform in prematch-only universe; revisit if in-play is added |

v1 priority score = D×T only. L stays an execution-time concern; C is uniform prematch.

## Ingestion vs. placement: both, differently

**Ingestion** (the big win): a leader move is a detection trigger. Implemented as bulk-source
trigger mini-cycles + targeted surgical laggard refetch (Phase C). Concentrates Betsson
requests on the laggard in the seconds after the leader moves.

**Placement** (a smaller, real win): the arb exists because the laggard's stale quote hasn't
caught up; that leg is perishable and dies first. Among legs, place the laggard first. This
composes with — never replaces — the live fragile-auth rule: real-money naked incidents (BW
auth) trump latency theory. Fragile-auth stays key #1; staleness rank is key #2 (Phase D).

## Bootstrap runbook (Phase A5)

**Recommended — concurrent collection while the hot loop runs.** When arb detection is
live, run both processes so the dataset accrues without doubling traffic to Betsson (the
WAF-sensitive book):

```bash
docker compose up -d                    # Postgres (TimescaleDB) + Redis
# Terminal A — daemon WITHOUT its own Betsson scraper:
RECORD_TO_PG=1 DISABLE_BETSSON=1 uv run python scripts/run_ingestion_daemon.py
# Terminal B — hot loop tees every snapshot it already scrapes into Redis odds:raw:
RECORD_TICKS=1 uv run python scripts/run_hot_loop.py
```

Traffic accounting: **Betsson stays at the hot-loop-alone footprint** — only the hot loop's
overlap linker fetches Betsson (`DISABLE_BETSSON=1` silences the daemon's poller), so no
extra WAF exposure. The daemon keeps scraping Betano + BetWarrior-list + BetWarrior-depth +
Bplay; Betano/BW-list gain one cheap bulk call per ~45s hot-loop cycle on top of the daemon
cadence, and BW-depth/Bplay remain daemon-only (the hot loop has no BW-depth scraper and
Betano's danae feed is 1X2-only, so the daemon supplies the BTTS/OU depth coverage the
dataset needs).

Data correctness: interleaved same-platform observations (daemon + hot loop both scraping
Betano/BW) are not biased — `PgRecorder` keys state by `(platform, platform_outcome_id)` and
updates `prev_observed_at` on **every** observation across both producers, so interleaving
strictly *tightens* the analysis's censoring intervals.

Betsson rows now join cross-platform: `kickoff_utc` is stamped from each market's betting
`deadline` (for a prematch market the deadline IS kickoff), so `_stamp_canonical` derives a
date-bucketed `fixture_key` instead of leaving Betsson rows under session-scoped keys.
Minutes-level deadline-vs-kickoff skew is irrelevant — the `fixture_key` is date-bucketed.

**Daemon-only fallback** (no hot loop running): keep the daemon's Betsson scraper on.

```bash
docker compose up -d
RECORD_TO_PG=1 uv run python scripts/run_ingestion_daemon.py
```

Run continuously for ≥7 days spanning at least two weekend league windows. Sanity check
after 1h:
```sql
SELECT platform, count(*), count(*) FILTER (WHERE is_change)
FROM odds_snapshots GROUP BY 1;
```

Then analyze:
```bash
uv run python scripts/analyze_lag_structure.py --days 7 --write-artifact --execution-audit
```

## Phase C/D gate

**The gate is `TRIGGER_POLL` (env, default `0` = off).** With `TRIGGER_POLL` unset,
`run_forever` never calls `trigger_fetch` and behavior is byte-identical to today —
regardless of whether an artifact exists. The operator sets `TRIGGER_POLL>0` only after
the B report shows a genuine leader (see criteria below).

The artifact `data/lag_model.json` **tunes** triggered scanning, it does not gate it:

- **No artifact / malformed file** (`lag_model=None`): `trigger_fetch` still runs (once
  `TRIGGER_POLL>0`), using built-in defaults (120s hot windows) and **skipping** the
  `burst_eligible_market_types` filter — every moved market is eligible. Traffic stays
  bounded by `burst_budget=3` surgical laggard fetches per trigger tick.
- **Artifact present**: only market types listed in `burst_eligible_market_types` get hot
  windows, sized from each type's `lag_p90_s`. A market type is burst-eligible when
  `n_matched_moves ≥ 50` AND `lag_p90_s ≥ 60s`. An artifact whose `burst_eligible_market_types`
  is **empty** (the B-report gate failed for every type) hard-disables bursting — `trigger_fetch`
  returns `{}`.

**Operator gate criteria** (checked on the B report before setting `TRIGGER_POLL>0`): some
platform has `leader_share ≥ 0.6` on ≥1 market type with `n_matched_moves ≥ 50`, AND that
type's `lag_p90_s` (midpoint) ≥ 60s. Gate fails → leave `TRIGGER_POLL=0`; re-run collection
longer or accept uniform scanning. Malformed nested artifact shapes degrade to safe defaults
per field (never crash the hot loop).

## GARCH-adaptive threshold (Phase E)

The detection margin gate is static no more. `scripts/analyze_lag_structure.py --garch`
fits GARCH(1,1) per market type offline on the cross-platform implied-prob spread series
(median across ≥3 accepted per-(fixture, cell) fits; non-stationary fits discarded), using
the `PAIR_ORDER` first-two-present spread convention shared verbatim with the live
extractor (`src/arbitrage/garch.py`). The fit lands in the artifact under
`garch_per_market_type`.

The live hot loop (`ArbOrchestrator`) evolves σ²_t per (market, cell) from its own
per-cycle spread observations (`OverlapQuoteSource.cycle_spreads`, called once per FULL
cycle — never on the trigger path), and scales the margin threshold:

```
threshold = clamp(base · (1 + sensitivity · (ratio − 1)), floor, max(floor, 3·base))
```

where `ratio = max over the market's cells of √(σ²_t / σ²_uncond)`. A volatile market
demands a higher margin; a calm one accepts a lower margin down to the risk-policy floor
(0.5%). `base`, `sensitivity`, and `floor` come from `MIN_MARGIN_PCT_BASE`,
`GARCH_SENSITIVITY`, and the risk policy respectively.

Fail-open: missing artifact, empty section, no validated entry, or no per-market state →
the static base threshold (byte-identical to today). Regenerate with:

```bash
uv run python scripts/analyze_lag_structure.py --days 7 --garch --write-artifact
```

**Scope**: hot-loop only. The stream detector (`src/semantic/arb_detector.py`) and the
diagnostic `scripts/run_arb_loop.py` keep static thresholds — neither writes the
`opportunities` columns this feature populates. The pure helpers
(`spread_observations`, `AdaptiveThreshold`) take arbitrage-layer types only, so later
adoption is additive.

**Activation log**: when GARCH-adaptive thresholds are active, startup emits
`hot_loop.garch_adaptive` with the validated market types. Absence of that log = static
mode (operator-visible).

## Env knobs

| Knob | Default | Effect |
|------|---------|--------|
| `RECORD_TO_PG` | off | Enables Postgres tick recording in the ingestion daemon |
| `TRIGGER_POLL` | `0` | Seconds between trigger mini-cycles (0 = disabled) |
| `LAG_MODEL_PATH` | `data/lag_model.json` | Path to the lag model artifact |
| `RECORD_TICKS` | off | Hot loop tees every scraped snapshot to Redis `odds:raw` |
| `DISABLE_BETSSON` | off | Daemon omits its Betsson scraper (hot loop tee supplies Betsson) |
| `GARCH_ADAPTIVE` | `1` | Hot loop uses GARCH-adaptive margin thresholds when the artifact carries `garch_per_market_type` |
| `MIN_MARGIN_PCT_BASE` | `1.0` | Base detection margin threshold (static value when GARCH off) |
| `GARCH_SENSITIVITY` | `2.0` | Threshold response slope to relative excess volatility |

## Censoring caveats
- **Poll-observed changes are interval-censored.** The analysis carries pessimistic and
  optimistic bounds; the midpoint estimate is the headline number. If bounds are too wide
  to pick a leader, the contingency is raising the daemon's Betano/BW poll cadence during a
  dedicated fine-clock run — NOT polling Betsson faster (WAF).
- **Push feeds** (`betsson_ws`, `bplay_sse`) are near-uncensored but cover LIVE events, not
  the prematch hot-loop universe. Accept poll-censored estimates for prematch.
- **`fixture_key`** is date-bucketed (UTC date of kickoff), not the exact instant, because
  platforms report kickoff with minutes-level jitter. Rows without kickoff fall back to a
  session-scoped join key (`recorder_session_id` + `session_fixture_id`).

## Known future enhancement

Threading `kickoff_utc` through `FixtureResolver._register_new`/`_link` into
`CanonicalFixture.kickoff_utc` (backfill-only — don't overwrite on `_link` with later `None`
snapshots) is a clean future step that the current `RawOddsSnapshot.kickoff_utc` field
enables but the recorder doesn't yet require (it reads `snapshot.kickoff_utc` directly).
