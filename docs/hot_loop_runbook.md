# Hot-loop + viewer runbook (end / start / restart)

Operational procedure for the armed hot-loop bot (`scripts/run_hot_loop.py`) and its
read-only CDP observer (`scripts/view_hot_sessions.py`). All commands run from the
**operator's terminal** on the bot host.

## ⚠️ The one rule that matters most

**Launch the bot and viewer from the operator's terminal — never from an agent/assistant
shell.** Long-running daemons started inside an agent's bash session get reaped (observed
2026-06-24/25: bots died at 6 min and 44 min with no crash — external SIGKILL). Processes
parented to the operator's own terminal (with `nohup … &`) survive indefinitely. Verify with
`ps -o ppid= -p <pid>` — the PPID must be the operator's shell, not an agent's.

## Pieces & files

| Piece | What | Logs / state |
|---|---|---|
| Bot | `run_hot_loop.py --arm --yes-real-money` | writes `/tmp/arby_hot_loop.log` itself (in-process tee, truncated at startup); console → `/tmp/arby_hot_loop_console.log` |
| Viewer | `view_hot_sessions.py --keep-watching` | stdout → `/tmp/arby_session_viewer.log` |
| Login gate | `/tmp/arby_login_done` | bot blocks at login until this file exists |
| Browsers | persistent profiles `recon/profile/{betano,betsson,betwarrior}` | CDP 9222/9223/9224 |
| Viewer artifacts | dense screenshots + `events.jsonl` + `postmortem.md` | `recon/artifacts/session_viewer/<ts>/` |

CDP ports = `CDP_PORT_BASE` (default 9222) + offset: betano +0, betsson +1, betwarrior +2.

---

## END a session

```bash
pkill -9 -f 'run_hot_loop.py'                                  # stop the bot
pkill -9 -f 'view_hot_sessions.py'                             # stop the viewer
pkill -9 -f 'recon/profile/betano'                             # release the three
pkill -9 -f 'recon/profile/betsson'                            # persistent-profile
pkill -9 -f 'recon/profile/betwarrior'                         # browser locks
rm -f /tmp/arby_login_done                                     # reset the login gate
: > /tmp/arby_session_viewer.log      # clear the observer tail on termination (fresh on next redeploy)
```

Killing the three profile browsers is required — without it their `SingletonLock`s stay held
and the next start can't launch on the same profiles.

Verify it's all down:
```bash
ps -eo command | grep -E 'run_hot_loop|view_hot_sessions|recon/profile/bet' | grep -v grep
# (no output = clean)
```

---

## START a session (armed, real money)

Run **from the operator's terminal**. Two options — the bot writes
`/tmp/arby_hot_loop.log` itself now (in-process tee), so the viewer can detect arbs
and capture them no matter how you launch (see "First-arb capture" below). **Do NOT
redirect the bot's stdout to `/tmp/arby_hot_loop.log`** — that double-writes every
line; redirect to `/tmp/arby_hot_loop_console.log` instead (Option A) or nowhere
(Option B). **Terminal.app mangles `\`-continued multiline pastes** (exit 2); paste
each command as a single line.

### Option A — backgrounded, survives terminal close (nohup, canonical)

The durable form: survives terminal close. Paste each command as **one line** — Terminal.app
mangles `\`-continued multiline pastes (exit 2).

Prep (once):
```bash
cd ~/Documents/arby
: > /tmp/arby_session_viewer.log
rm -f /tmp/arby_login_done
```

Bot:
```bash
nohup bash -c 'CDP_PORT_BASE=9222 uv run python scripts/run_hot_loop.py --arm --yes-real-money < <(while [ ! -f /tmp/arby_login_done ]; do sleep 2; done) >> /tmp/arby_hot_loop_console.log 2>&1' &
```

Viewer:
```bash
nohup uv run python scripts/view_hot_sessions.py --keep-watching --log /tmp/arby_hot_loop.log --base-port 9222 >> /tmp/arby_session_viewer.log 2>&1 &
```

Then: log into all three windows → confirm balances → `touch /tmp/arby_login_done`.

### Option B — foreground, stdout visible (paste-safe troubleshooting)

Two terminal tabs. Simpler to paste, but the bot dies if you close its tab — use for
testing/inspection, not a long unattended session.

Prep (once):
```bash
cd ~/Documents/arby
: > /tmp/arby_session_viewer.log
rm -f /tmp/arby_login_done
```

Tab 1 — armed bot (writes its own log file; stdout stays in the terminal):
```bash
CDP_PORT_BASE=9222 uv run python scripts/run_hot_loop.py --arm --yes-real-money
```

Tab 2 — read-only viewer (run AFTER the bot opens the windows):
```bash
uv run python scripts/view_hot_sessions.py --keep-watching --log /tmp/arby_hot_loop.log --base-port 9222
```

Log into all three windows → confirm balances → **press ENTER in Tab 1** when each
shows a balance. The login `input()` prompt is visible in Tab 1 (the bot writes its
own log file now, so stdout is no longer redirected away from the terminal).

Tab 3 — live bot log (still works — the in-process tee writes the same JSON here):
```bash
tail -f /tmp/arby_hot_loop.log
```

### After login

1. Log into **all three** windows (Betano — clear any challenge; Betsson — log in, stay on
   PBA; BetWarrior — log in). Confirm each shows a balance.
2. Clear the gate: `touch /tmp/arby_login_done` (Option A) or press ENTER (Option B).
3. Watch:
   - **Viewer health:** Option A → `tail -f /tmp/arby_session_viewer.log`. Option B →
     already visible in the viewer terminal tab (it writes to stdout, not the file).
   - **Bot events** (both options — the bot writes this file itself):
     ```bash
     tail -f /tmp/arby_hot_loop.log | grep --line-buffered -E 'transport\.betsson_(context|relogin)|guardrails\.kill_switch|orchestrator\.arb_found|executor\.(aborted|completed|naked_exposure|leg_placement)|leg_placer\.(http_error|betsson_odds_resubmit|betwarrior_(non_success|delay_resolved))|transport\.(betsson_stale_betslip_cleared|betsson_context_skipped_reality_check|reality_check_dismissed)'
     ```

### Betsson forced re-login drill / failure evidence

With the bot running and Betsson manually logged out:

```bash
touch /tmp/arby_force_betsson_reauth
```

Watch `/tmp/arby_hot_loop.log` for `transport.betsson_relogin_*`. The bot writes local
evidence under `recon/artifacts/betsson_relogin/` and attaches the JSON path as
`evidence=` on the relevant event. During the password step it also logs
`transport.betsson_relogin_password_trace`; later failure evidence embeds the same
sanitized `passwordTrace` array.

- `transport.betsson_relogin_open_miss` — first popup-open attempt missed before reload;
  this can still be followed by a successful second attempt.
- `transport.betsson_relogin_no_form` — second attempt still could not see the password input.
- `transport.betsson_relogin_form_incomplete` — password or submit selector missing.
- `transport.betsson_relogin_timeout` — submit path ran, but the session never became logged-in.

The JSON keeps only auth booleans, selector metadata for trigger/email/password/submit/
geolocation, and password-step focus metadata. Inspect `selectors.password.descendantInput`
and `selectors.password.container` to distinguish a real input from a wrapper fallback;
inspect `passwordTrace[*].hitPath`, `activePath`, and `eventSeen` to see whether the
password click landed on the input, whether focus entered nested shadow roots, and whether
`keydown`/`beforeinput`/`input` events fired. It intentionally does **not** store full HTML,
input values, or typed keys. For the click-site selectors (`trigger`/`submit`/`geolocation`),
`center` and `pointTarget` reflect the resolved inner shadow `<button>` (expect
`pointTarget.testId` like `btn-1-button` on a healthy read, not the host tag); the login
trigger falls back to the header "Iniciar sesión" `fdsp-button` when the `login-button`
wrapper testId is absent. Screenshots are saved only for pre-credential open-form misses
(`open_miss_before_reload`, `no_form_after_reload`). Offline repro tools require the
bot/viewer stopped first:
The Betsson persistent profile (`recon/profile/betsson`) is SingletonLocked while the bot is
running.

```bash
uv run python scripts/probe_betsson_auth.py
uv run python scripts/drill_betsson_relogin.py full
```

**Dry run (no real money):** drop `--arm --yes-real-money` from the bot command. It still
opens the windows and detects, but places through dry-run placers (no money moves, no
betslip/cleanup side effects).

---

## First-arb capture (already on by default)

The viewer captures dense screenshots around each arb execution and writes a post-mortem.
**No flag needed — it's the default.** It triggers automatically when the viewer sees
`orchestrator.arb_found` / `orchestrator.executing` in the bot log.

- **Normal cadence:** screenshot every `--interval-sec` (default 60s).
- **Dense cadence:** on arb detection → every `--dense-sec` (default 10s) for at least
  `--dense-window` (default 180s). Captures the placement sequence in detail.
- **Post-mortem:** on the first `orchestrator.executed` → writes `postmortem.md` + all
  screenshots + `events.jsonl` to `recon/artifacts/session_viewer/<timestamp>/`.
- **Stop behavior:** WITHOUT `--keep-watching` the viewer **stops** after the first arb
  execution. WITH `--keep-watching` (both options above) it keeps running and captures
  every subsequent arb.

**Critical dependency:** this is now automatic — the bot writes JSON to
`/tmp/arby_hot_loop.log` itself via the in-process tee (`configure_logging(tee_path=…)`),
and `log_format` defaults to `json` (`src/config.py`), so the viewer sees parseable
arb events without any redirection on your part. **Never redirect the bot's stdout to
`/tmp/arby_hot_loop.log`** — every line would be written twice (once by the tee, once
by the shell).

Artifacts location:
```bash
ls -lt recon/artifacts/session_viewer/ | head    # newest capture first
```

---

## RESTART = END + START
Run the END block, wait a couple seconds, run the START block. The bot truncates its
own log at startup; only `rm -f /tmp/arby_login_done` is manual. You must re-login +
re-`touch` the gate each restart (new browser instances open on the persistent profiles).

---

## Health checks

```bash
# bot alive + how long + parented to your shell?
ps -eo pid,ppid,etime,command | grep '[r]un_hot_loop.py'

# past the login gate? (a hot_loop.start line = gate cleared, live)
grep 'hot_loop.start' /tmp/arby_hot_loop.log | tail -1
ls /tmp/arby_login_done            # present = gate has been tripped

# context established + kill-switch state + cleanup firing?
grep -E 'transport\.betsson_context|guardrails\.kill_switch|transport\.betsson_stale_betslip_cleared' /tmp/arby_hot_loop.log | tail -8

# viewer attached?  (cN betsson: ok  /  betwarrior: ok)
tail -4 /tmp/arby_session_viewer.log
```

A `transport.betsson_context established=true` line every ~5 min = the heartbeat is healthy
(not wedged). A `transport.betsson_stale_betslip_cleared removed=N` = the betslip cleanup
fired. A `guardrails.kill_switch_tripped`/`_reset` pair = a session blip that auto-recovered.

---

## Gotchas & known behaviors

- **Why `arby_hot_loop.log` starts empty on every redeploy:** the bot truncates it
  itself at startup now (the in-process tee opens it with mode `"w"`). The viewer tails
  it from offset 0 on startup and replays every `kill_switch` event — a stale trip from
  a previous run would make the viewer print a false `🛑 auto-placement OFF` forever
  (the new bot never emits a reset on a clean armed start). A clean log = an honest flag.
- **Viewer must be restarted whenever the bot is.** It attaches to the bot's browsers over
  CDP; replacing the browsers drops the attach, and `--keep-watching` does not recover a full
  browser swap (it reads `attach_failed` until relaunched).
- **The bot truncates `arby_session_viewer.log` on graceful shutdown** (Ctrl+C / SIGTERM /
  an exception inside the run loop) from inside `run_hot_loop.py`, so your `tail -f` clears
  the moment the bot stops. Scope is narrow: it does NOT fire on a crash *before* the run
  loop or on `pkill -9` (SIGKILL is uncatchable) — those still rely on the END block's
  manual `: >` truncates. (`arby_hot_loop.log` is truncated by the bot itself at startup.)
- **Don't minimize the windows** (or ⌘H-hide them). Chromium throttles minimized/occluded
  windows, which starves `establish_betsson_context`'s `networkidle` goto and can wedge the
  heartbeat. Visible on any Space/desktop is fine; dock-minimized is not. Window size and
  fullscreen are irrelevant — Playwright pins each page's viewport to 1280×720 regardless.
- **Timezones:** bot structlog timestamps are **UTC** (trailing `Z`); the viewer lines and
  your clock are **America/Argentina (UTC−3)**. Subtract 3 h from a bot timestamp to compare
  with the viewer/local.
- **Telegram** reflects the bot's *in-process* kill-switch state directly (correct, never
  stale like the viewer's log-derived flag), but it inherits the bot's BetWarrior blind spot
  (bearer JWT exp outlives a server-side kill) — "ready/ON" from Telegram can coexist with a
  server-dead BetWarrior until a placement actually tests it.
- **`disown` under zsh:** `disown -a` is bash syntax; zsh reports `job not found: -a`.
  Harmless — `nohup` already makes the jobs ignore SIGHUP, so they survive terminal close
  without disown.
