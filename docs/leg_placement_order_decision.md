# Leg placement order decision — current risk assessment

Date: 2026-06-27

## Status

Current execution order is deterministic, but not risk-aware:

1. `semantic/arb_detector.py` picks the best quote per outcome cell.
2. It emits legs by sorted cell/outcome name.
3. `arb_executor.py` preserves that order.
4. `Executor` places sequentially.

So a Betsson-first or BetWarrior-first run is currently an artifact of which platform won the earliest sorted outcome cell, not a deliberate safety policy.

**Current-vs-proposed boundary:** every ordering heuristic below is analysis-only. The live
bot does **not** currently do BetWarrior-first, Betsson-first, single-leg-first, stake-first,
or residual-risk ordering. It still executes the detector's outcome-cell order until a
future implementation changes that policy.

## Current abort / exposure model

Before any placement, the executor can cleanly abort on:

- kill switch already tripped;
- missing placer for any leg;
- live odds cannot be re-verified;
- repriced arb is no longer profitable;
- guardrail rejection;
- pre-place auth gate returning false.

Important gap: the `Executor` supports an `auth_precheck` seam, but `scripts/run_hot_loop.py` does not currently pass one. BetWarrior protection is therefore reactive placement-time reauth today, not a zero-exposure pre-place abort for a server-dead BetWarrior session.

After one or more legs are accepted, any later leg reject/drift/auth failure/unconfirmed state becomes naked exposure or pending unknown. A first-leg `pending_unknown` is also exposure-ish, not a clean abort: the executor returns `PENDING_UNKNOWN` because the submitted coupon may already be placed even though no other hedge leg is live.

## Fixes in place

### BetWarrior 401 / no-bearer reactive reauth

- Covered today: no bearer and HTTP 401 from BetWarrior placement.
- Behavior: one logout→login→sportsbook navigation→fresh bearer, then reverify odds and retry once.
- Relogin drill is live-validated.
- Full real-arb 401→reauth→retry→place is still awaiting natural live validation.
- HTTP 409 `USER_NOT_AUTHENTICATED` is not covered today. It is a strong follow-up candidate, but should be matched on explicit body code/message, not every 409.

### BetWarrior `LIVE_DELAY_PENDING`

- Covered today via bounded coupon-history polling.
- Live-validated: one `LIVE_DELAY_PENDING` resolved to `OPEN` on poll attempt 1 and the arb completed.
- If unresolved, it becomes `PENDING_UNKNOWN` because the coupon may already be placed. This is not a clean/no-exposure outcome, even if BW is leg A.

### Betsson `E_BETTING_ODDS_INVALID`

- Covered only for favorable corrections: same selection, parseable `validOdds`, higher than submitted odds, within the 20% anomaly cap.
- Behavior: one resubmit at exact `validOdds`.
- Unfavorable, ambiguous tag, over-cap, non-odds errors, or second reject keep the normal abort/naked path.
- Unit-tested; not yet live-validated.

### Betano contribution

- Low Betano arb contribution should be treated first as detection topology/feed coverage, not execution quality.
- The live hot loop wires `BetanoScraper(mode="prematch")` only. It does not use Betano live.
- The prematch feed is `top-events-v2`, not a full catalog; it may simply expose fewer overlapable fixtures than BetWarrior + Betsson.
- Betano dynamic caps/readiness affect execution, not whether detection finds a Betano leg.

## Incident evidence: BW-last drift after two Betsson fills

Real failure observed on `fx-366d0143f674|1x2`:

- Fixture: Aguia de Maraba PA vs Parnahyba PI.
- Detected arb: ROI 1.24%, stake 5,000 ARS.
- Execution order: Betsson AWAY @ 6.8 for 744 ARS, Betsson DRAW @ 3.1 for 1,633 ARS,
  then BetWarrior HOME.
- First two Betsson legs filled.
- BetWarrior HOME reverify moved from 1.94 to 1.82 before placement.
- Result: `NAKED_EXPOSURE` with two Betsson legs live.

The BW drift objectively destroyed the arb:

- At HOME 1.94: `1/1.94 + 1/3.1 + 1/6.8 = 0.98510` → still profitable.
- At HOME 1.82: `1/1.82 + 1/3.1 + 1/6.8 = 1.01909` → no true arb remains.

This is a sequencing failure, not a missing initial validation gate. The executor already
does a fresh-odds profitability check before placement and re-verifies each leg immediately
before placing it. The exposed gap is between-leg drift after earlier legs have filled.

Ordering lesson:

- This is evidence against same-platform pair first / multi-leg-platform-first as a
  safety rule. It placed both Betsson legs before proving the single remaining BW hedge
  leg was still executable.
- It supports placing the single remaining platform early in 3-way 2+1 splits, especially
  when that platform is also operationally fragile or historically drift-prone.
- It broadens the BW-first argument: the problem was not BW auth here; it was BW being the
  last and sole remaining hedge leg after two fills.


## Placement order candidates

### 1. BetWarrior first

Benefits:

- Converts BW auth failures into clean first-leg aborts when reauth cannot rescue.
- Best short-term hedge against the known historical BW 401 naked: previous naked exposure came from accepted Betsson legs followed by BW HTTP 401.
- Gives reactive reauth a chance before other legs are live.

Risks:

- `LIVE_DELAY_PENDING` as leg A can still create `PENDING_UNKNOWN` single-leg risk. The coupon may already be placed.
- BW reauth takes time; if it succeeds, later legs may drift while rescue runs.
- If BW is a low-odds/high-stake leg, placing it first may commit a larger absolute stake before confirming the higher-odds legs.

Prognosis:

- Best near-term default for BW-including arbs until 401/409 rescue is real-arb validated.
- Does not eliminate exposure; it shifts the most dangerous historical failure mode earlier.

### 2. Betsson first

Benefits:

- Betsson placement path is mature and historically proven.
- If Betsson rejects immediately, no money moves.
- Favorable odds correction can improve payout if the returned `validOdds` is higher and accepted.

Risks:

- Existing naked exposure came from accepted Betsson legs followed by BW auth failure.
- Betsson-first is not currently a policy; it happens because sorted outcome order puts Betsson-winning outcomes first.
- The favorable odds correction is not live-validated and is not a general drift cure.

Prognosis:

- Reasonable only when no BW leg exists, or after BW auth rescue is validated and the specific BW leg looks lower-risk than Betsson.

### 3. Multi-leg platform first

Definition: when one platform supplies multiple outcomes in a 3-way arb, place that platform's legs first or together in sequence.

Benefits:

- Reduces number of platform/session transitions early.
- If both same-platform legs are accepted, one book-side session is known healthy for its part of the arb.
- Could reduce auth/context churn where a platform has a fragile auth model.

Risks:

- Can concentrate exposure on one book before the hedge book is touched.
- If the single remaining platform fails, exposure may be larger and less diversified.
- Does not address market drift on the remaining platform.

Prognosis:

- Not a standalone safety rule. Use only as a tie-breaker after platform failure risk and stake/payout tail are considered.

### 4. Single-leg platform first

Definition: in a 2+1 platform split, place the platform that supplies only one leg before placing the two-leg platform.

Benefits:

- Often smaller stake on the long-odds outcome; if it becomes single-leg exposure, absolute stake may be lower.
- Flushes the unique platform/session risk early.
- Prevents placing two legs on one platform before discovering the only hedge platform is broken.

Risks:

- Not always smaller stake; depends on odds and Dutch sizing.
- A low-odds single-leg can be the largest stake.
- If the single-leg platform has `pending_unknown` semantics, risk remains.

Prognosis:

- Strong candidate heuristic for 3-way arbs, especially if the single-leg platform is BW. Needs stake-tail calculation rather than assuming smaller stake.

### 5. Low-odds leg first

Benefits:

- Lower odds usually means higher win probability and larger stake in Dutch sizing.
- If accepted first, later high-odds reject may leave a large stake exposed but on a more probable outcome; this can reduce variance but not necessarily expected loss.
- Low-odds markets may be more liquid and less likely to disappear.

Risks:

- Usually commits the largest stake first.
- If later legs fail, absolute money exposed can be high.
- "Safer bet" is not the same as safer execution sequence; the goal is hedge completion, not picking winners.

Prognosis:

- Weak as a primary rule. Useful only inside a tail-loss optimizer that computes worst-case residual P/L after each partial fill.

### 6. High-odds leg first

Benefits:

- Usually smaller stake first, limiting absolute exposure if subsequent legs fail.
- Flushes fragile longshot/outcome availability early; long odds often move faster.

Risks:

- If it wins and hedge fails, payout liability shape can be severe despite smaller stake.
- High-odds quotes may be stale/volatile and more likely to reject.
- Can leave a low-odds large stake to place last, creating drift and cap risk late.

Prognosis:

- Better than low-odds-first when minimizing initial stake exposure, but still incomplete without residual-tail calculation.

### 7. Lowest expected failure probability first

Benefits:

- Places the most reliable leg before fragile legs.
- May maximize completion probability if reliable legs rarely fail.

Risks:

- Bad for exposure safety: reliable legs accepted first make later fragile failures naked.
- This is the pattern that hurt when Betsson legs filled before BW 401.

Prognosis:

- Optimizes completion rate, not exposure minimization. Avoid as primary policy for armed sequential execution.

### 8. Highest expected failure probability first

Benefits:

- A reject-prone leg failing first is a clean abort.
- Directly minimizes naked exposure from known fragile auth/odds paths.

Risks:

- If the failure mode is `pending_unknown`, first does not mean clean.
- May lower completion rate by front-loading flaky platforms.
- Requires continuously maintained failure profiles.

Prognosis:

- Best conceptual safety policy for sequential betting, with explicit exception for pending/unknown submission states.

## Recommended near-term policy

Use a combined heuristic, not one simple platform rule:

1. If any leg has a known auth/session fragility that can hard-reject before submission, place it first.
   - Today this means BetWarrior before Betsson when BW is present.
   - Extend to BW 409 `USER_NOT_AUTHENTICATED` after implementation.
2. If one platform supplies only one leg in a 3-way arb, prefer that single-leg platform early, especially if it is also the fragile platform.
3. Among remaining candidates, prefer the order that minimizes worst-case residual loss after each prefix of accepted legs.
   - This should be computed from stakes and odds, not inferred from platform or odds alone.
   - Latency constraint: this scoring must be local arithmetic over the already-known
     legs, stakes, odds, and platform risk metadata. It must not add bookmaker calls,
     sleeps, browser probes, or extra between-leg revalidation. For normal 3-way arbs the
     full search is only six permutations, so computation is negligible; if inputs are
     missing, fall back to the hand-coded heuristic rather than delaying placement.
4. Treat `pending_unknown` platforms specially.
   - Putting BW first reduces multi-leg naked exposure from BW auth rejects, but BW first can still create single-leg unknown exposure on unresolved `LIVE_DELAY_PENDING`.
5. Do not use low-odds-first or high-odds-first as a standalone rule.
   - They are proxies for stake and tail shape; compute the actual tail instead.

## Proposed future decision algorithm

For each possible leg order, simulate prefixes after each accepted leg and score:

- clean reject probability for the next leg;
- pending/unknown probability for the next leg;
- worst-case residual P/L if execution stops after that prefix;
- total stake already committed;
- whether the next leg has reactive rescue available and live-validated;
- expected time cost before remaining legs can be placed.

Then choose the order with the lowest safety score, not necessarily the highest completion probability.

Implementation constraint: order scoring is allowed to spend CPU, not time on the wire.
The live risk is odds drift between submitted legs, so a clever scorer that performs extra
network I/O would be counterproductive. Compute the order once immediately before execution
from the current quote/stake snapshot, then place legs without adding new pauses.

Initial hand-coded approximation until enough data exists:

1. BW first when present, unless BW leg has a high chance of `LIVE_DELAY_PENDING` and another leg has worse auth/session risk.
2. Single-leg platform before same-platform pair when failure risks are comparable.
3. Smaller stake before larger stake when failure risks are comparable.
4. Lower worst-case residual loss before lower variance / lower odds.
5. Keep deterministic tie-breaker by outcome cell only as the final fallback.

## Validation remaining

- Observe a real BW 401 → reauth → retry → complete path.
- Add/validate BW 409 `USER_NOT_AUTHENTICATED` as `auth_failed` if it appears live.
- Observe Betsson `leg_placer.betsson_odds_resubmit` accepted live.
- Collect enough executed/naked/aborted examples to calibrate failure probabilities by platform and error type.
- Add detection metrics separating Betano feed contribution from Betano execution outcomes.
