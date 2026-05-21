# Risk register

These are the failure modes that will actually determine whether this project succeeds or fails. The math is solved; these are the open problems.

## Operational risks

### Account longevity (severity: critical for real deployment)

Argentine bookmakers — like all bookmakers — actively detect and limit accounts that profit systematically. The mechanism is not always an outright ban; the more common pattern is asymmetric reduction of maximum stakes for winning accounts, sometimes within hours of detected patterns.

**Mitigations for credit deployment:** none required.

**Mitigations for real deployment:** account rotation, stake size randomization, bet timing randomization, dispersing bets across multiple accounts, avoiding the most-profitable markets where detection is easiest. None of these fully solve the problem.

### Execution latency vs. professional arb desks (severity: high)

On liquid Argentine soccer markets (Liga Profesional 1X2, major Copa Libertadores matches), professional operations close arb windows in seconds. Our polling cycle is 3-10 seconds per platform; agentic execution adds 5-15 seconds per leg.

**Implication:** our realistic edge is in prop bets (BTTS, first scorer, correct score, exact halftime score) where retail desks update more slowly and arb windows persist for tens of seconds to minutes.

**Document explicitly in class writeup:** opportunities found on 1X2 markets are likely false positives caused by latency in our data pipeline rather than real arbitrage. Filter these out or use them only for offline analysis.

### Platform anti-bot measures (severity: medium-high)

Argentine platforms run Cloudflare, DataDome, or similar bot detection. Pure `httpx` requests will be blocked on most platforms. Playwright with stealth plugins works on most but not all. Headless Chromium is detectable; headful is slower.

**Per-platform reconnaissance is required before scraping each platform.** Document what works for each platform in `docs/platform_notes/`.

## Architectural risks

### Partition validator false positives (severity: critical)

A false positive on partition validation means we place a bet pair that does not actually exhaust the outcome space, leaving directional exposure. The validator must achieve >99% precision on the "valid" label.

**Mitigation:** hardcoded rule-based pre-filter handles the highest-frequency trap (team wins / team loses without knockout context). LLM only runs on cases rules cannot classify. Every validation decision logged for audit.

**Open question:** how to detect that the validator is degrading. Periodic re-evaluation against the labeled test set is necessary but not sufficient — production data may exhibit failure modes absent from the test set.

### One-sided fills (severity: high)

Leg A fills, then odds on Leg B move adversely before we can place it. Now we hold directional exposure on Leg A with no hedge.

**Mitigation:** post-Leg-A odds verification re-runs the Dutch book check with the actual filled odds on Leg A. If the arb is no longer viable, we abort Leg B and log the naked exposure for manual review. The threshold for "no longer viable" is conservative — require the recomputed margin to exceed a buffer above the minimum, not just be positive.

**Open question:** when naked exposure occurs, what is the right next action? Holding to match completion is one option; placing a partially-hedged bet on a different platform is another. Defer this decision to manual review until we have data on how often it occurs.

### GARCH model degradation (severity: low-medium)

GARCH(1,1) fits assume the spread process is stationary within the rolling window. Around major events (lineup announcements, in-play goals, halftime), the spread regime shifts and the fit becomes unreliable.

**Mitigation:** fall back to sample variance when GARCH fails to converge. Cap the adaptive threshold multiplier at a reasonable upper bound so a pathological variance estimate cannot disable all trading. Refit every N observations rather than every observation to dampen noise.

## Class project risks

### Scope creep

The greatest threat to project completion is the temptation to implement every idea in the literature review. Specifically: Kalman filters, Hurst index analysis, neural network return prediction, DFS cycle detection, and permutation importance feature selection are all bad fits for this problem and will burn weeks if attempted.

**Mitigation:** the phased build plan in the project docs. Each phase has a validation gate. Do not advance until the prior gate is met.

### Demonstration without validation

A working demo on cherry-picked examples is worthless if the system fails on held-out data. The Chinese futures paper's discipline on in-sample vs. out-of-sample evaluation should be ported here: tune all parameters on a held-out historical odds dataset that is not touched until final evaluation.

### Overfit partition validator prompts

Iterating prompts against the labeled test set until the validator hits 99% precision is fine — that is the right way to tune the prompt. But the labeled test set must include genuine adversarial cases (the win/loss trap variations, knockout vs. group stage, push-able totals) and not just easy cases. If the test set is too easy, 99% on it doesn't generalize.
