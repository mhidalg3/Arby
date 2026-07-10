# Platform failure profiles

Operational runbook for platform-window failure states that suspend auto-placement (`kill_switch_tripped`), make a session not ready, or abort arbitrage execution. This is not a historical ledger; update it whenever a new reproducible failure mode is captured.

## Control-plane semantics

### Readiness heartbeat and kill switch

`HotSessionManager` periodically probes every wired platform. If any platform is not placeable, it trips the kill switch with:

```json
{"event":"guardrails.kill_switch_tripped","reason":"session not ready"}
```

Effect:

- Detection continues.
- Auto-placement is suspended.
- The status line reports `auto-placement: SUSPENDED`.
- When all platform probes become ready again, the heartbeat resets only this session-readiness trip and sends `✅ Sessions ready again — auto-placement resumed.`

Important distinction: `session not ready` is a generic guardrail reason. Read the preceding platform event(s) to identify the true source, for example `transport.betsson_context established=false`, missing BetWarrior bearer, Betano balance failure, or a blocking overlay.

### Execution outcomes

During execution, a failed leg has different consequences depending on state:

- `ABORTED`: no leg confirmed placed.
- `NAKED_EXPOSURE`: at least one earlier leg is live and a later leg failed or became unconfirmed.
- `PENDING_UNKNOWN`: a bet was submitted but final acceptance could not be confirmed; it may still be pending or placed on the book. Auto-placement halts until verified.

---

## Betsson profile

### B1. Betting context not established / expired

**How it presents**

API/logs:

```json
{"event":"transport.betsson_context","platform":"betsson","established":false}
{"event":"guardrails.kill_switch_tripped","reason":"session not ready"}
```

Often preceded by failed Betsson accordion calls:

```text
GET https://pba.betsson.bet.ar/api/sb/v1/widgets/accordion/v1 failed
```

UI:

- May still look logged in at a glance.
- Betting context in the SPA is gone; the betslip can behave as if the user must log in or cannot submit.
- Session-expired UI text may include `sesión cerrada por falta de actividad` or `volver a iniciar sesión`.

Recent observed episode:

- `transport.betsson_context established=true` every ~5 minutes from 15:52 to 16:39.
- Betsson accordion failures began around 16:43.
- `transport.betsson_context established=false` at 16:44:44.
- Kill switch tripped at 16:44:44 with `session not ready`.

**Cause**

Betsson's placeable betting context lives in SPA memory (`ctx-*`, `sessiontoken`, `x-sb-*`). A normal authenticated-looking page is not enough. The context can disappear after inactivity/session expiry or after the SPA is cold-loaded without the internal routing that creates the placeable state.

**Current autonomous recovery**

The heartbeat calls `establish_betsson_context()` every cycle. It:

1. loads the Betsson sportsbook home,
2. performs an in-app `Mi cuenta` / `My Account` navigation (SPA-router route, not a raw reload),
3. waits for the app to emit the authenticated context headers,
4. logs `transport.betsson_context established=true|false`.

When this returns true and all other platforms are ready, `_apply_health()` resets the kill switch and resumes auto-placement. This is why the Betsson `session not ready` trip can resolve without code changes: the bot keeps detecting, keeps probing, and resets itself once the context is re-established.

**Operator recovery**

If it does not auto-recover:

1. Re-login or refresh Betsson in its window.
2. Keep the window on PBA/sportsbook.
3. Let the bot perform the in-app My Account route on the next heartbeat.
4. Confirm `transport.betsson_context established=true` appears.
5. Confirm `guardrails.kill_switch_reset` appears.

Avoid hard reloads during placement. Reloading can cold-boot the SPA and lose the context that placement needs.

**Open gaps / improvements**

- Better classify accordion-fetch failures before the 5-minute context probe trips.
- Capture screenshot/DOM evidence on `established=false` episodes, not just responsible-gambling/session-expired overlays.
- Add an operator-visible status that names the platform (`betsson`) in the kill-switch reason, not only generic `session not ready`.

### B2. Coupon placement errors / unavailable selection / odds changed

**How it presents**

API/logs:

- `parse_betsson` rejects if `couponPlacementErrors` is non-empty.
- HTTP `>=400` in `BetssonLegPlacer.place()` returns `accepted=false`.
- Missing context returns detail: `betsson: authenticated context not resolved (login + in-app nav?)`.

UI examples seen in viewer screenshots:

- `Existen problemas con tu cupón...`
- `Selección no disponible`
- `Las cuotas han cambiado de 1.22 a 1.19`

**Cause**

- Odds drift between reverify and placement.
- Selection suspended/unavailable.
- Betslip stale.
- Authenticated context missing.

**Current handling**

- Betsson is exact-odds via the request body.
- A rejected first leg aborts cleanly.
- A rejected later leg becomes `NAKED_EXPOSURE` if prior legs were already live.
- Betsson responses do not echo stake/odds; if accepted, the executor records requested stake/odds.
- A FAVORABLE `E_BETTING_ODDS_INVALID` reject (Betsson returns the current `validOdds`
  for our selection) is a price-confirmation handshake: `BetssonLegPlacer.place()` re-submits
  ONCE at the exact returned odds when the move is strictly favorable
  (`validOdds > submitted`, same fixed stake → worst-case payout strictly non-decreasing,
  hedge can only improve) and within the `_BETSSON_RESUBMIT_MAX_UPLIFT_PCT` (20%) anomaly
  cap. It fails closed (keeps the reject) on: an unfavorable move, an over-cap "valid"
  price, a non-odds error, a selection-tag mismatch, or any reject carrying a non-empty
  `couponId` (a coupon may exist → never re-POST). The retry is bounded single-shot; a
  second price move on the retry returns the reject (no loop). This is hedge-safe, not a
  `src/risk/` decision: the stake is never resized.
- Odds-invalid rejects (unfavorable / over-cap / tag-mismatch / second-reject) now read
  "server repriced before acceptance" with the submitted→validOdds delta in the alert /
  audit `detail`; the per-leg telemetry event (`executor.leg_placement`) additionally
  records the server's corrected price as `server_valid_odds` and the price actually POSTed
  last as `odds_requested`.

**Operator recovery**

- Clear stale Betsson slip if the UI shows coupon problems.
- Re-login/re-establish context if the error is context/session related.
- Let the next detection/reverify cycle rebuild a fresh opportunity.

**Open gaps / improvements**

- Pre-place coupon cleanup / stale slip sanitizer.
- Better surface `couponPlacementErrors` values in viewer/postmortem.
- Residual hedge recomputation after a changed filled leg remains deferred.

### B3. Reality-check reminder popup

**How it presents**

Viewer/logs:

```text
c342 betsson: reality_check ⚠️
```

Shadow-DOM evidence:

- `h1.reality-check-question`: `¿Sabés qué hora es?`
- `p.reality-check-message`: `EL JUEGO COMPULSIVO ES PERJUDICIAL PARA VOS Y TU FAMILIA`

UI:

- Betsson overlays the sportsbook with a responsible-gaming reminder.
- The orange `Cerrar` button at the bottom closes the popup.

**Cause**

Betsson periodically shows a reality-check / responsible-gaming reminder. It is not a
hard responsible-gambling lockout and not a dead session; it is a dismissible modal that
occludes the betting UI.

**Current autonomous recovery**

The heartbeat detects this Betsson-only popup through a bounded open-shadow-DOM scan,
marks Betsson not-ready immediately, and schedules one background recovery click per
popup episode:

1. trip the normal `session not ready` kill switch before waiting through the cooldown,
2. confirm one of the grounded reality-check phrases is visible in Betsson's shadow DOM,
3. wait 5.25 seconds for the observed `Cerrar` cooldown to elapse,
4. click one visible `Cerrar` button from the same document/shadow root as that phrase,
5. re-probe until the shadow marker disappears,
6. reset auto-placement after all platforms are ready again.

Success logs `transport.reality_check_dismissed` and
`hot_sessions.reality_check_closed`, followed by the normal `Sessions ready again` reset.
If the click fails or the popup persists, the existing `session not ready` suspend path
remains active with a `REALITY CHECK` alert telling the operator to click `Cerrar`.

**Operator recovery**

If automation fails, manually click the orange `Cerrar` button. The heartbeat will resume
once the popup disappears.

**Open gaps / improvements**

- The selector is grounded on the current popup phrases, not class-only markers. Capture
  any future Betsson wording/DOM variant before broadening it.

---

## Betano profile

### A1. Session/auth readiness failure

**How it presents**

API/logs:

- `check_betano_ready()` calls Betano `/api/balance`.
- Ready only if HTTP 200 and `data.customerCode` exists.
- If false, the heartbeat includes `betano` in `not_ready` and can trip:

```json
{"event":"guardrails.kill_switch_tripped","reason":"session not ready"}
```

UI:

- Logged out / challenge / balance unavailable.
- May require completing a challenge before the balance endpoint becomes authenticated.

**Cause**

- Session expired.
- Challenge interrupted auth.
- Cookies invalidated.

**Current handling**

- Readiness fails closed before placement.
- Detection continues while auto-placement is suspended.
- Once `/api/balance` returns `data.customerCode`, heartbeat can reset the session-readiness kill switch if all platforms are ready.

**Operator recovery**

1. Re-login Betano.
2. Complete challenge if shown.
3. Confirm balance visible.
4. Wait for the next heartbeat to reset auto-placement.

### A2. Session timer warning / extension popup

**How it presents**

UI text:

- `Temporizador de sesión`
- `¿querés conservarlo?`
- `Sí, conservarlo`
- `No, quiero desconectarme`

Log/events:

- Detected as `SessionBlock(kind="session_timer_warning", is_overlay=true)`.
- If auto-click succeeds: `hot_sessions.session_extended`.
- If auto-click fails: warning and normal suspend/alert path.

**Cause**

Betano warns that the session is about to expire; the modal occludes the betting UI.

**Current autonomous recovery**

`attempt_session_extend()` knows Betano's grounded selector:

```text
#st-maintain-button
```

The heartbeat clicks the same `Sí, conservarlo` control the operator would click, once per popup episode, then re-probes. If the modal disappears, placement stays enabled and no operator intervention is needed.

**Operator recovery**

If auto-extend fails:

1. Click `Sí, conservarlo` manually, or re-login if it already expired.
2. Wait for heartbeat reset.

**Open gaps / improvements**

- Add selectors for Betsson/BetWarrior equivalents if captured.
- Persist screenshot evidence for failed extend attempts.

### A3. Responsible-gambling / mandatory-break overlays or banners

**How it presents**

Detected through platform text matching. Overlay vs body-only is important:

- Phrase inside visible overlay => true blocking lockout, suspend.
- Phrase only in body text => non-blocking banner, capture/log but do not suspend.

Examples include mandatory break wording such as `12h descanso` / `descanso de apostar y jugar`.

**Cause**

Responsible-gambling mandatory break, play-time limits, or curfew-like platform policy.

**Current handling**

- Captures evidence to `recon/artifacts/rg_blocks/` on new or escalated block episodes.
- Overlay blocks suspend auto-placement.
- Banner-only hits are logged/captured but treated as placeable.

**Operator recovery**

- Overlay lockout: wait out the mandatory break or use another platform/session only if policy permits.
- Banner-only: no action if placement remains possible.

**Open gaps / improvements**

- A real overlay whose selector is not in `_RG_BLOCK_DIALOG_SELECTOR` can be misclassified as a banner. Evidence capture is how we close that gap.
- No lockout-aware check at leg placement time yet; the server must reject for us to see it during execution.

### A4. Betano placement pipeline failures

**How it presents**

API/logs from `BetanoLegPlacer`:

- `HTTP <status> (plain-leg)`
- `betano: plain-leg added no bet`
- `HTTP <status> (updatebets)`
- `betano: updatebets returned no slip (errorCode=..., errors=...)`
- `HTTP <status> (place)`
- `betano: not accepted (errors=...; data_keys=...; top_keys=...)`

UI:

- Selection unavailable.
- Odds drift.
- Stake/cap rejection.
- Betslip not updated.

**Cause**

Betano placement is stateful: `plain-leg -> updatebets -> place`. Any stale selection, changed odds, stake limit, or invalid slip hash breaks the chain.

**Current handling**

- Fail-closed at every stage; no place call if `plain-leg`/`updatebets` returns no valid slip.
- `oddschanges` is `0`, so odds changes are not accepted silently.
- Betano cap refresh reads `POST /api/betslipcombo/limits` and uses `data.max` as the live max stake when available; fail-soft to fallback if unavailable.

**Operator recovery**

- Re-login if auth-related.
- Let the next cycle rebuild the slip from fresh `plain-leg/updatebets`.
- For repeated cap failures, lower stake or investigate the limits endpoint response.

**Open gaps / improvements**

- Richer capture of `data.errors` and `errorCode` in viewer/postmortem.
- More explicit distinction between odds drift vs stake cap vs closed selection.

---

## BetWarrior profile

### W1. Bearer not captured / session not ready

**How it presents**

API/logs:

- `prepare_betwarrior_auth()` returns `None` if no bearer has been captured or the bearer is stale.
- Placement fails closed with: `betwarrior: session bearer not captured (logged in?)`.
- Heartbeat readiness can trip `kill_switch_tripped {"reason":"session not ready"}` if BetWarrior is not ready.

UI:

- Window can look logged in and active.
- No obvious UI error.
- The missing piece is not the visual login; it is the Kambi player API bearer.

Recent observed condition:

- Operator logged in and bot detected for many cycles.
- `bw auth event count: 0`; no `kambicdn.com/player` bearer was captured.
- This means a BetWarrior placement would fail at auth-precheck and never reach `LIVE_DELAY_PENDING`.

**Cause**

BetWarrior/Kambi emits the bearer passively only when the SPA makes authenticated player-API calls. A passive login plus public offering fetches (`eu.offering-api.kambicdn.com/...`) is not enough; offering endpoints are public and do not carry the player bearer.

**Current handling**

- Fail-closed: no bearer, no placement.
- The readiness path treats this as not ready, suspending auto-placement rather than sending a stale/absent token.
- Bearer expiry is checked via JWT `exp` with skew.

**Operator recovery**

Trigger a Kambi player-API request:

1. In BetWarrior, click an odd, open betslip/account, or another UI action that causes `cf-al-auth-api.kambicdn.com/player/...` traffic.
2. Watch for heartbeat readiness to clear.
3. Once all sessions are ready, kill switch resets and auto-placement resumes.

This matches the earlier successful recovery pattern: operator clicked an odd; `coupon/validate.json` fired; bearer captured; bot resumed.

**Open gaps / improvements**

- Add an explicit, safe active bearer-capture probe after login instead of relying on passive SPA traffic.
- Surface `betwarrior: bearer missing` separately from generic `session not ready`.
- Add viewer/runbook cue when public offering fetches are active but player bearer is absent.

### W2. Session expired / inactivity logout overlay

**How it presents**

UI phrases:

- `Estabas desconectado`
- `se terminó por inactividad`
- `su sesión se terminó`
- `volver a iniciar sesión`

Detected by `check_session_blocked()` as `SessionBlock(kind="session_expired")` when the phrase is in visible overlay/body text.

**Cause**

Server-side inactivity logout or bearer refresh stopped. The JWT `exp` can lag behind actual server-side logout; the popup is the backstop signal.

**Current handling**

- Overlay blocks suspend auto-placement.
- Evidence captured once per episode.
- Keepalive does mouse move/scroll and occasional safe click, but prior lab evidence showed mouse/scroll alone did not defeat BetWarrior server-side inactivity; authenticated player-API activity is the stronger next step.

**Operator recovery**

1. Re-login BetWarrior.
2. Trigger player-API traffic (click odd / account / betslip) so bearer is captured.
3. Wait for heartbeat reset.

**Open gaps / improvements**

- Active authenticated Kambi keepalive via `page.evaluate` or transport-level player API call.
- Capture exact inactivity-popup DOM for any new variant.

### W3. Promotions page trap

**How it presents**

Viewer/capture:

```text
url: https://pba.betwarrior.bet.ar/es-ar/promotions
page title/body: PROMOCIONES
state: promotions_page
```

UI:

- The BetWarrior window shows `PROMOCIONES` with promo cards.
- Top navigation still shows `INICIO`.
- The page can remain stuck there for hours while bearer readiness still appears live.

Observed episode:

- BetWarrior first entered `/es-ar/promotions` at 18:07 in
  `recon/artifacts/session_viewer/20260623_125619/viewer.jsonl`.
- It stayed there through the current capture.
- Hot-loop logs did not show a promotions-specific event before this fix.

**Cause**

Likely SPA route drift from a user/promo navigation or from keepalive clicking a clickable
promo/banner implemented as a non-`button`/non-`a` element. The old keepalive only skipped
literal interactive tag names, so clickable `div` cards with `cursor:pointer` could still
be clicked.

**Current handling**

- `check_session_blocked()` treats BetWarrior `/promotions` + `PROMOCIONES` as
  `SessionBlock(kind=\"promotions_page\")`.
- The heartbeat marks BetWarrior not-ready and trips the normal `session not ready` kill
  switch before attempting recovery.
- A background task clicks the visible top-nav `INICIO`, polls until the promotions marker
  disappears, then re-probes and resets auto-placement only when all platforms are ready.
- Keepalive now skips pointer-cursor/onclick/interactive-ancestor targets, not just
  literal `button`/`a` tags, reducing the chance of navigating into promotions again.

**Operator recovery**

If automation fails, manually click the top-nav `INICIO` button. The heartbeat will resume
once the page returns to a sportsbook route and all sessions are ready.

**Open gaps / improvements**

- If future captures show a different route/title for non-sportsbook pages, add explicit
  route guards rather than relying on bearer liveness.
- Active authenticated Kambi keepalive is still safer than synthetic clicks long-term.

### W4. `LIVE_DELAY_PENDING` during execution

**How it presents**

API/logs:

```json
{"event":"leg_placer.betwarrior_non_success","status":"LIVE_DELAY_PENDING","coupon_ref":12796224824,"body":"..."}
```

Captured pending body fields included:

- `delayBeforeAcceptingBet: 3`
- `couponRef: 12796224824`
- `couponExternalRef: ae30704f-9235-40fe-a6fb-18211c7db050`
- `bets[0].betRef: 15918842962`
- `bets[0].betStatus: WAITING_FOR_APPROVAL`
- `bets[0].stake: 234720`
- `bets[0].betOdds: 25000`

UI:

- Bet held by Kambi live-delay approval window.
- May appear as pending/processing in betslip/history rather than rejected.

**Cause**

Kambi live-betting delay. The POST was received; final acceptance/rejection is asynchronous.

**Current handling**

Implemented poll v2:

1. Do **not** re-POST (`coupon.json`) — avoids double placement.
2. Poll `coupon/history.json` on `cf-al-auth-api.kambicdn.com` with the captured bearer.
3. Match by `couponRef`, fallback `betRef`.
4. Classify the matched bet's `betStatus`:
   - `OPEN` + echoed `stake` and `betOdds` => accepted.
   - `WAITING_FOR_APPROVAL` => keep polling.
   - known reject literal (`REFUSED`, `REJECTED`, etc.) => clean reject.
   - absent/unknown/error until deadline => `pending_unknown=True`.
5. Timeout or missing `couponRef` becomes `PENDING_UNKNOWN`, not a clean reject.

Effect:

- First-leg `PENDING_UNKNOWN` trips kill switch and alerts: the bet may be placed.
- Later-leg pending unknown is treated as naked exposure because earlier legs are live.

**Operator recovery**

- If `betwarrior_delay_resolved`: poll proven; continue normal operation.
- If `betwarrior_delay_rejected`: clean reject; no coupon exposure from that leg.
- If `betwarrior_delay_timeout` / `PENDING_UNKNOWN`: manually verify the coupon/betRef on BetWarrior immediately, because it may still settle.
- If `betwarrior_poll_http_error`: preserve logged body; endpoint/shape likely needs correction.

**Open gaps / improvements**

- Live proof passed on 2026-06-23: two BetWarrior `LIVE_DELAY_PENDING` coupons resolved
  via poll v2 to `betStatus=OPEN` during the first completed real arb.
- BetWarrior-first leg ordering. If any platform must bear asynchronous uncertainty, it should be first so no earlier confirmed legs are exposed.
- Residual hedge recomputation after a confirmed BetWarrior fill: **SHIPPED** (2026-07-02). An odds-change reject — or a mid-sequence pre-place drift beyond tolerance — now triggers ONE bounded executor-level recapture per leg: re-fetch fresh odds (the Tier-2 `LiveOddsReverifier` feed) + arb-layer re-price (`detect_arbitrage` full re-price when nothing is live yet; the residual solver `allocate_residual` around already-filled legs so the Dutch book still locks ≥ 0) + guardrail re-check + a single re-POST. No salvageable hedge → today's abort/naked. See W5.
- Keep `allowOddsChange* = NO`: the recapture path re-fetches from our own feed and re-prices through `src/arbitrage/`, so we never accept a worse filled price blind. Enabling YES would let Kambi fill at any (worse) price without recomputing the hedge.
- Potential active bearer-capture/keepalive so the bearer exists before an execution candidate appears.
- More structured `body` logging for pending responses instead of truncated stringified dicts.

### W5. Kambi HTTP errors / odds mismatch / suspended outcome

**How it presents**

API/logs:

```json
{"event":"leg_placer.http_error","status":400,"body":"..."}
```

or placement result detail:

```text
HTTP <status>: <body>
betwarrior: status=<literal>
betwarrior: SUCCESS without echoed coupon.bets (status ok, no bet)
```

**Cause**

- Odds changed after reverify; request sends exact `odds_x1000` and `allowOddsChange* = NO`.
- Outcome suspended/removed.
- Bearer stale or invalid.
- Kambi returned an unrecognized status shape.

**Current handling**

- Fail-closed; no loose success on `status=SUCCESS` unless `coupon.bets[0]` echoes `stake` and `betOdds`.
- HTTP error body is logged for diagnosis.
- Exact odds remain required (`allowOddsChange* = NO`).
- A Kambi 400 `"Invalid odds specified"` (price moved between our reverify and the POST) is classified by `betwarrior_odds_rejected` and flagged `odds_rejected` on the result. The executor then makes ONE bounded recapture per leg: re-fetch fresh odds, re-price via the arb layer (full `detect_arbitrage` re-price when nothing is live yet; the residual solver `allocate_residual` around already-filled legs so the book still locks ≥ 0), re-run guardrails, and re-POST once. No salvageable hedge (edge gone / budget exhausted / guardrail fail) → today's abort/naked. A `pending_unknown` (submitted) bet is never re-POSTed. Emitted log events: `executor.recaptured`, `executor.recapture_no_arb` (+ `…_unverifiable` / `…_guardrail` / `…_shape_mismatch` on the refuse paths).

**Operator recovery**

- Let next detection/reverify cycle build a fresh candidate.
- If repeated bearer/auth errors, trigger player-API traffic or re-login.
- If repeated odds mismatch despite the single recapture (the book moving faster than one re-fetch can catch), the market is too fast to hedge safely — leave `allowOddsChange* = NO` and let it abort/naked rather than accept a worse price blind.

---

## Current live blockers to a BetWarrior live-delay proof

As of the 2026-06-23 session:

1. No arb with a BetWarrior leg has executed since poll v2 deploy.
2. Betsson session expired after ~52 minutes and tripped `session not ready`.
3. BetWarrior bearer was never captured (`bw auth event count: 0`), so a BetWarrior placement would fail before reaching `LIVE_DELAY_PENDING`.

To resume proof hunting:

1. Re-establish Betsson context (re-login/refresh; let heartbeat log `transport.betsson_context established=true`).
2. Trigger BetWarrior player-API traffic (click odd / account / betslip) so bearer is captured.
3. Wait for `guardrails.kill_switch_reset`.
4. Keep the viewer running; watch for `leg_placer.betwarrior_delay_resolved`, `leg_placer.betwarrior_delay_timeout`, or `leg_placer.betwarrior_poll_http_error`.

---

## How to update this file

For every new failure state, add:

1. Platform and short state name.
2. API/log signature.
3. UI signature.
4. Cause.
5. Current automated handling.
6. Operator recovery.
7. Open gaps / desired automation.
8. Evidence artifact path if captured (`recon/artifacts/...`).
