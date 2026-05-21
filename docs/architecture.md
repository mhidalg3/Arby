# Architecture

## Layer separation

The system is divided into four layers, each with a distinct trust level and a distinct technology choice.

### Layer 1: Ingestion (deterministic)

Polls platform odds via HTTP. Prefers JSON API endpoints discovered during reconnaissance; falls back to Playwright HTML scraping where APIs are unavailable.

**No LLM in this loop.** Polling runs every 3-10 seconds per platform, and an LLM call would add hundreds of milliseconds and tens of dollars per day to the operational cost. The scrapers are dumb, fast, and deterministic. When a platform changes its DOM or API contract, the scraper fails with a clear exception; we fix it manually rather than asking an agent to reason about the change in real time.

**One-time agentic use:** during initial scraper construction, Claude (via Claude in Chrome or a Playwright harness) is used to identify API endpoints, parse the page structure, and generate the scraper code. The output is deterministic Python.

### Layer 2: Semantic (LLM)

Resolves three linguistic problems that deterministic code cannot:

1. **Bet description parsing.** Maps platform-specific strings like "Resultado Final: Boca Juniors" to canonical market and outcome identifiers.
2. **Cross-platform matching.** Determines whether "BTTS: Yes" on Codere and "Ambos Marcan: Si" on Betsson refer to the same underlying market.
3. **Partition validation.** Verifies that two candidate bets form a valid partition (P(A∪B) = 1, P(¬A ∧ ¬B) = 0) given the sport-specific rules of the match.

A rule-based pre-filter handles the obvious cases (clear patterns, known traps like the "team wins / team loses" partition failure in non-knockout matches). The LLM only runs on cases the rules cannot classify. This keeps cost and latency manageable while preserving precision where it matters.

**Critical requirement:** the partition validator must achieve >99% precision on the "valid" label. A false positive means we place an unhedged bet.

### Layer 3: Arbitrage engine (deterministic)

Pure-function math:

- Dutch book detection: `1/o_a + 1/o_b < 1` and margin above threshold.
- Optimal stake allocation: `s_a = K/o_a, s_b = K/o_b` with proportional scaling under liquidity caps.
- GARCH-adaptive thresholds: adjust the minimum margin requirement based on recent cross-platform spread volatility.

No I/O. No LLM. No async. Fully testable as pure functions.

### Layer 4: Execution (agentic with hardcoded guardrails)

Bet placement requires navigating real platform UIs that vary in structure, occasionally pop unexpected dialogs (responsible gambling prompts, 2FA challenges, session expiries), and may return ambiguous confirmation states ("pending" vs "accepted"). A deterministic Playwright script fails on the first edge case it wasn't programmed for. An agent can reason about the current page and decide what to do next.

The agent's authority is narrow: given a target bet (platform, market, outcome, stake), navigate the UI, place the bet, parse the confirmation, return structured results. The agent does not decide whether the bet is profitable. All financial decisions are enforced by `src/execution/guardrails.py`:

- Maximum exposure per match
- Total portfolio exposure cap
- Daily loss limit with automatic kill switch
- Post-Leg-A odds verification before Leg-B placement
- Naked-exposure logging when Leg A fills but Leg B becomes unviable

## Data flow

```
Scrapers → Redis queue → Normalizer → Postgres odds_snapshots
                                          ↓
                            Semantic layer (LLM) resolves
                            platform → canonical IDs
                                          ↓
                            For each canonical outcome,
                            fetch latest odds per platform
                                          ↓
                            Arbitrage engine evaluates
                            all platform pairs for partition pairs
                                          ↓
                            Opportunities written to DB,
                            approved opportunities published
                            to execution queue
                                          ↓
                            Execution agent places Leg A,
                            verifies fill, places Leg B
                                          ↓
                            P&L tracker reconciles fills
                            against expected
```

## Why this architecture rather than alternatives

**Why not a single agent end-to-end?** Latency, cost, and reliability. Scraping is high-volume and time-sensitive; a single-agent loop would be both too slow for the arbitrage window and prohibitively expensive in tokens.

**Why not pure deterministic code throughout?** The semantic problems (bet matching, partition validation) require natural-language reasoning that rule-based code cannot handle robustly. Hardcoding every possible bet-naming variation across Argentine platforms is a losing battle. The execution problem is similar but in a different mode — real UI variation that brittle scripts cannot survive.

**Why credit capital first?** Account bans on the first detection of systematic arbitrage are the dominant operational risk in real deployments. Credit capital sidesteps this entirely while letting us validate the system end-to-end.

## What we explicitly chose not to build

These ideas appeared in adjacent literature but were rejected as poor fits for the problem:

- **Kalman filter dynamic hedge ratios** (from Chinese futures pairs trading). Our bet pairs are binary; there is no continuous spread to track.
- **Hurst index mean-reversion filtering.** Same reason — the framing assumes a stationary spread that reverts to a mean, not a transient mispricing that closes when one platform updates.
- **DFS positive-cycle detection** (from currency arbitrage). Our cycles are length 2 by construction; cycle enumeration is not the bottleneck.
- **Permutation importance feature selection** (from XAI StatArb). Applies to high-dimensional return prediction; our LLM uses prompts, not features.
- **Neural-GARCH joint estimation.** Plain GARCH(1,1) on the cross-platform spread captures the operationally relevant signal; the NN component would overfit without orders of magnitude more data.

The one borrowed idea is GARCH(1,1) for adaptive thresholding, which addresses a real and specific risk in the execution layer.
