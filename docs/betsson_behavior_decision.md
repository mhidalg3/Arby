# Betsson session behavior — detection & recovery decisions

**Status: DECISION PENDING (2026-06-29).** Two open decisions. ⚠️ **The repo source
currently contains both the skip-goto guard and the scan exclusivity change**
(`src/execution/session.py`) — any fresh `uv run` from HEAD picks them up (Option 3 state).
The working bot should stay running; to restart from the pre-change baseline, `git stash`
or revert those changes first. No decision has been made on which option to keep.

---

## Decision 1 — reality-check detection specificity

### Background

The reality-check popup (`fds-button[data-test-id="reality-check-btn-1"]` → "Cerrar") is
detected by `_BETSSON_REALITY_CHECK_SCAN_JS`, a bounded shadow-DOM walk that searches for
the grounded phrases ("¿sabés qué hora es?" / "el juego compulsivo es perjudicial para vos
y tu familia"). The scan result is consumed by `check_session_blocked()` →
`SessionBlock(kind="reality_check")` → the manager schedules
`attempt_reality_check_close()` to click Cerrar.

The **bounded-retry close** (`_REALITY_CHECK_MAX_CLOSE_ATTEMPTS = 3`, 10s delays, per-attempt
+ recheck timeouts) is the fix for "popup not closing." It runs as a background task holding
the page lock; the heartbeat `goto` is serialized behind it (also needs the lock), so the
goto cannot race or prevent the close. This is independent of the detection specificity
decision below.

### What changed this session (in source, NOT yet deployed)

Two changes were made to `src/execution/session.py`:

1. **Skip-goto guard** (`establish_betsson_context`): if `check_session_blocked()` returns a
   `reality_check` overlay, the method returns False immediately — skipping the
   `goto(_BETSSON_HOME, networkidle)` that normally runs on every heartbeat probe.

2. **Scan exclusivity** (`_BETSSON_REALITY_CHECK_SCAN_JS`): tightened to require BOTH a
   grounded phrase AND the popup's close button (`fds-button[data-test-id^="reality-check"]`),
   so responsible-gambling footer text on promo/API/502 pages does not false-positive.

### Why the skip-goto guard caused overreporting

Before any changes, the phrase-only scan was **tolerable** — not intrinsically exclusive —
because the heartbeat `goto` self-corrected non-sportsbook pages: a transient false
positive on `ofertas.pba.betsson.bet.ar/*` (footer phrase) was cleaned up by the next
heartbeat's `goto(_BETSSON_HOME)` navigating back to the sportsbook. The false positive was
transient, not persistent.

The skip-goto guard broke this self-correction: on a promo page, the scan false-positives
(footer phrase) → guard skips the goto → window stays on the promo page → scan keeps
false-positiving → persistent false positive + Betsson stuck cold. This is the overreporting
the operator observed.

The scan exclusivity change was a **compensating fix for the guard's side effect** — not a
fix the scan needed on its own.

### The three options

#### Option 1 — revert both, restore the working baseline

Revert the skip-goto guard AND the scan exclusivity change. Keep only the bounded-retry
close.

| | |
|---|---|
| **Detection** | Phrase-only scan (original). |
| **Goto** | Runs on every heartbeat (refreshes during popup — harmless, close is serialized behind the page lock). |
| **Self-correction** | Goto navigates away from non-sportsbook pages → transient false positives self-correct. |
| **Pros** | Known-working baseline. Detection is robust (phrase catches popup variants even if the button markup changes). Fewest moving parts. |
| **Cons** | Page refreshes during popup (cosmetic noise). Phrase-only scan has transient false positives on non-sportsbook pages (self-corrected, but each one triggers one close-recovery cycle that finds no button and returns False). |

#### Option 2 — revert skip-goto guard, keep scan exclusivity

Revert the skip-goto guard only. Keep the phrase + button scan.

| | |
|---|---|
| **Detection** | Requires phrase AND grounded `fds-button[data-test-id^="reality-check"]`. |
| **Goto** | Runs on every heartbeat. |
| **Self-correction** | Goto still navigates away from non-sportsbook pages; AND the scan won't false-positive even if the goto is delayed. |
| **Pros** | Defense-in-depth: no false positives even without goto self-correction. Eliminates wasted close-recovery cycles on non-sportsbook pages. |
| **Cons** | Depends on the `Cerrar` button being present for detection. If Betsson changes the button (different `data-test-id`, button absent during the ~5s cooldown before it's clickable), detection misses the popup until the button renders. More fragile for detection than phrase-only. |

#### Option 3 — keep both (skip-goto guard + scan exclusivity)

Keep both changes as currently in source.

| | |
|---|---|
| **Detection** | Requires phrase AND button. |
| **Goto** | Skipped when popup detected (no refresh during popup). |
| **Pros** | No page refresh during popup. Detection is exclusive. |
| **Cons** | If scan false-positives (button present without a real popup — unlikely but not impossible), skip-goto deadlock. Depends on button for detection. Most moving parts; the two changes are coupled (each depends on the other to avoid the deadlock). |

### Current-vs-proposed boundary

- **Live bot today:** whatever was last deployed (operator's terminal redeploy). The source
  has both changes (Option 3 state); a fresh `uv run` picks them up.
- **The bounded-retry close** is already deployed and is the actual fix for "popup not
  closing." It is orthogonal to all three options above.

---

## Decision 2 — cold-session recovery: goto wait-condition fallback on known-bad pages

### Background

When `establish_betsson_context()` returns False (Betsson cold, no block), the heartbeat
marks Betsson not-ready and suspends placement. The next heartbeat retries
`establish_betsson_context` (which does `goto(_BETSSON_HOME, networkidle, 60s)`). In most
cases this self-corrects within one or two heartbeats.

But the operator observed Betsson staying cold when the window lands on specific pages:

- `https://ofertas.pba.betsson.bet.ar/betsson-app` — app-download page (wrong subdomain).
- `https://ofertas.pba.betsson.bet.ar/ag/bono-de-bienvenida-deportes-out` — promo/bonus
  page.
- `⚠ User Context Retrieval Failed - 502 - ctx-...` — a server-side context error rendered
  on the page (the URL may still be `pba.betsson.bet.ar` but the SPA shows a 502).

On all three, a manual refresh (navigate to the sportsbook) resolves the cold state.

### Why the goto doesn't always self-correct these

`establish_betsson_context` does `goto(_BETSSON_HOME, wait_until="networkidle",
timeout=60000)`. On a broken/erroring page:

1. **`networkidle` may never fire** — the SPA's background polling or error-retry loop keeps
   the network busy, so Playwright's 60s `networkidle` timeout kills the goto before it
   completes.
2. **The ctx request 502s even after navigation** — the goto lands on the sportsbook but the
   context API still returns 502 on the first try; `prepare_betsson_context` fails.
3. **Skip-goto guard (if deployed)** — if the scan false-positives on the ofertas page
   (footer phrase), the goto is skipped entirely → window never leaves the bad page.

### Proposed change to the existing goto (NOT yet implemented)

`establish_betsson_context` already does `goto(_BETSSON_HOME)` on every heartbeat —
refresh-on-cold is not net-new. The proposal changes **how that goto runs** when the window
is detected on a known-bad page:

**Bad-page detection (two signals, checked before the goto):**
- **URL-based:** `page.url` starts with `ofertas.pba.betsson.bet.ar` (wrong subdomain — the
  sportsbook is `pba.betsson.bet.ar`).
- **Content-based:** the page text contains `"User Context Retrieval Failed"` or
  `"502"` + `"ctx-"` (the context-error overlay).

**Change to the goto:**
- When a bad-page signal is detected, use `wait_until="domcontentloaded"` instead of
  `"networkidle"`. The lighter-weight wait completes even when the SPA's background polling
  keeps the network busy (the `networkidle` timeout trap). Then proceed to
  `prepare_betsson_context` as normal.
- Log `transport.betsson_bad_page_recovery` with the detected signal + prior URL.

**Where it lives:** inside `establish_betsson_context`, as a condition on the existing
`goto` call (same lock, same probe — no new recovery path at the manager level).

**Boundary:** this fires ONLY when Betsson is cold (not blocked). A reality-check popup or
RG lockout takes priority. This never places a bet — it's pure navigation + context
verification.

### Pros/cons of the optimization

| | |
|---|---|
| **Pros** | Faster cold recovery on known-bad pages (one heartbeat instead of multiple). Avoids the `networkidle` timeout trap. Self-contained in establish (no new recovery wiring). |
| **Cons** | The `domcontentloaded` fallback is less strict — the SPA may not have fully hydrated when ctx-poll starts. The bad-page URL/error patterns are hardcoded and may need updating if Betsson changes them. |

### Current-vs-proposed boundary

- **Live bot today:** `establish_betsson_context` does `goto(_BETSSON_HOME, networkidle,
  60s)` on every heartbeat when Betsson is cold. This self-corrects in most cases but times
  out or fails on the bad pages above (networkidle never fires, or the ctx 502s).
- **Proposed:** when a bad-page signal is detected, switch the goto to `domcontentloaded`
  and retry ctx-poll. Not yet implemented — pending operator decision.
