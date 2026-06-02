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

### Layer 4: Execution (deterministic, in-session, with hardcoded guardrails)

> **Design revised 2026-06-02 (supersedes the original "agentic UI navigation" plan).** The original design had an LLM agent navigate each platform's UI to place bets, justified by UI variation (popups, 2FA, ambiguous confirmations). Two things changed that calculus: (1) the recon phase reverse-engineered each platform's **authenticated bet-slip API** (Betano `/api/betslip/v3/*`, Bplay `/bettingslip/*`, Kambi `coupon/*`, Betsson betslip) plus the logged-in session contracts — so we no longer need to drive a DOM at all; and (2) the live in-play tests showed cross-book quote **staleness** produces phantom arbs, which makes an LLM's per-action latency (seconds) actively dangerous: it widens the detect→place gap and risks filling Leg A on stale odds while still "reasoning" about Leg B → naked exposure. For a time-critical, real-money, repeatable operation we want byte-exact, auditable requests, not a reasoning step in the hot path.

**Mechanism — deterministic placement inside the live session.** Bets are placed by replaying each platform's bet-slip API call **from within the logged-in browser context** (an in-page `fetch`, reusing the session's cookies, CSRF/`hash` tokens, TLS, and fingerprint). This inherits the real browser's legitimacy against anti-bot (raw `httpx` was WAF-blocked during recon) while staying fully deterministic — no LLM, no scripted mouse-clicking. A recorded Playwright interaction is a per-platform fallback **only** if a place endpoint proves to require real UI interaction (to be determined when we capture the first placement). No LLM sits in the detect→verify→place path.

**Authority is narrow; financial decisions stay in `src/risk/`.** Execution consumes a `RiskDecision` + sized stakes; it never decides profitability. `src/execution/guardrails.py` enforces, in code:

- Maximum exposure per match; total portfolio exposure cap
- Daily loss limit with automatic **kill switch**
- **Post-Leg-A odds re-verification** before Leg-B placement (abort if drift > `ODDS_TOLERANCE_PCT`) — the deterministic answer to the phantom-arb risk
- **Naked-exposure** logging + alert when Leg A fills but Leg B becomes unviable
- **Per-platform stake limits** (from the 2026-06-01 logged-in recon): Betsson flat `min(20,000,000, 100,000,000/odds)`; Betano dynamic — query `/api/betslipcombo/limits` per bet; Bplay payout-cap `999,999,999/odds` (effectively non-binding); BetWarrior none exposed (conservative prior). Fail closed if the live limit can't be read.

**Two-leg state machine:** re-verify odds → place Leg A → confirm fill → re-verify Leg B within tolerance → place Leg B → on Leg-B-unviable: log naked exposure + alert. Fail-closed on any unexpected response (abort + notify a human) rather than improvise on real money.

**Cold path = human-in-the-loop (via Telegram), not an LLM.** Session expiry, 2FA re-challenge, or a genuinely novel state aborts the hot path and alerts the operator over Telegram. Telegram also carries routine notifications (arb found, placing, placed/fill, naked exposure, kill-switch tripped, errors). A browser-agent (e.g. openclaw) may later be wired into the cold path for *automated re-auth/login only* — never the time-critical placement loop.

**Rollout is dry-run-first.** Prerequisite: capture one real placement to record the place/confirm contract (recon stopped at the slip). Then: Phase 0 dry-run (build the request, diff vs. the captured real one, never send) → Phase 1 one tiny real bet, one platform, manual confirm + kill switch → Phase 2 single-leg per platform → Phase 3 supervised min-stake cross-platform arb. Never autonomous before Phase 3 is clean; every real-money step needs explicit operator confirmation.

**Why not raw `httpx`?** WAF-blocked in recon; the in-session fetch inherits the browser's accepted fingerprint. **Why not pure scripted Playwright clicking?** Slower and more brittle than the API for a *known, fixed* bet-slip; kept only as a per-platform fallback. **Why not an LLM agent?** Latency (widens the staleness gap), non-determinism (real money), cost — and the API path removes the UI-variation problem it was meant to solve. Its only safe home is the non-time-critical cold path.

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
