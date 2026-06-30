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
| Bot | `run_hot_loop.py --arm --yes-real-money` | stdout → `/tmp/arby_hot_loop.log` |
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
: > /tmp/arby_hot_loop.log            # clean slate — the viewer replays this from offset 0 on start
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

Run **from the operator's terminal**. Two options — both redirect the bot's stdout to
`/tmp/arby_hot_loop.log` so the viewer can detect arbs and capture them (see "First-arb
capture" below). **Terminal.app mangles `\`-continued multiline pastes** (exit 2); paste
each command as a single line.

### Option A — backgrounded, survives terminal close (nohup, canonical)

The durable form: survives terminal close. Paste each command as **one line** — Terminal.app
mangles `\`-continued multiline pastes (exit 2).

Prep (once):
```bash
cd ~/Documents/arby
: > /tmp/arby_hot_loop.log
: > /tmp/arby_session_viewer.log
rm -f /tmp/arby_login_done
```

Bot:
```bash
nohup bash -c 'CDP_PORT_BASE=9222 uv run python scripts/run_hot_loop.py --arm --yes-real-money < <(while [ ! -f /tmp/arby_login_done ]; do sleep 2; done) >> /tmp/arby_hot_loop.log 2>&1' &
```

Viewer:
```bash
nohup uv run python scripts/view_hot_sessions.py --keep-watching --log /tmp/arby_hot_loop.log --base-port 9222 >> /tmp/arby_session_viewer.log 2>&1 &
```

Then: log into all three windows → confirm balances → `touch /tmp/arby_login_done`.

### Option B — foreground with redirect (paste-safe troubleshooting)

Two terminal tabs. Simpler to paste, but the bot dies if you close its tab — use for
testing/inspection, not a long unattended session.

Prep (once):
```bash
cd ~/Documents/arby
: > /tmp/arby_hot_loop.log
: > /tmp/arby_session_viewer.log
rm -f /tmp/arby_login_done
```

Tab 1 — armed bot (stdout → log so the viewer can see it):
```bash
CDP_PORT_BASE=9222 uv run python scripts/run_hot_loop.py --arm --yes-real-money >> /tmp/arby_hot_loop.log 2>&1
```

Tab 2 — read-only viewer (run AFTER the bot opens the windows):
```bash
uv run python scripts/view_hot_sessions.py --keep-watching --log /tmp/arby_hot_loop.log --base-port 9222
```

Log into all three windows → confirm balances → **press ENTER** in Tab 1. Tab 1 looks
blank — stdout is redirected to the log so the `input()` login prompt is hidden there.
Press ENTER anyway once all three windows show a balance, or watch Tab 3 for the prompt.

Tab 3 — live bot log (shows the hidden login prompt + all structlog output):
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
   - **Bot events** (both options — the bot's stdout is redirected to this file):
     ```bash
     tail -f /tmp/arby_hot_loop.log | grep --line-buffered -E 'transport\.betsson_context|guardrails\.kill_switch|orchestrator\.arb_found|executor\.(aborted|completed|naked_exposure)|leg_placer\.(http_error|betsson_odds_resubmit|betwarrior_(non_success|delay_resolved))|transport\.(betsson_stale_betslip_cleared|betsson_context_skipped_reality_check|reality_check_dismissed)'
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

**Critical dependency:** this only works if the bot writes JSON logs to
`/tmp/arby_hot_loop.log`. A foreground bot run without `>> /tmp/arby_hot_loop.log 2>&1`
writes to the terminal only — the viewer can't see arb events and won't trigger dense
capture. **Always redirect the bot's stdout to the log** (both options above do this).

Artifacts location:
```bash
ls -lt recon/artifacts/session_viewer/ | head    # newest capture first
```

---

## RESTART = END + START

Run the END block, wait a couple seconds, run the START block. The truncate + `rm gate` in
START handles the redeploy clean-slate. You must re-login + re-`touch` the gate each restart
(new browser instances open on the persistent profiles).

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

- **Why truncate `arby_hot_loop.log` on every redeploy:** the viewer tails it from offset 0 on
  startup and replays every `kill_switch` event. A stale trip from a previous run makes the
  viewer print a false `🛑 auto-placement OFF` forever (the new bot never emits a reset on a
  clean armed start). A clean log = an honest flag. (Permanent fix, not yet applied: make the
  viewer seek to the log's end on startup — `scripts/view_hot_sessions.py` `log_offset`.)
- **Viewer must be restarted whenever the bot is.** It attaches to the bot's browsers over
  CDP; replacing the browsers drops the attach, and `--keep-watching` does not recover a full
  browser swap (it reads `attach_failed` until relaunched).
- **The bot truncates `arby_session_viewer.log` on graceful shutdown** (Ctrl+C / SIGTERM /
  an exception inside the run loop) from inside `run_hot_loop.py`, so your `tail -f` clears
  the moment the bot stops. Scope is narrow: it does NOT fire on a crash *before* the run
  loop or on `pkill -9` (SIGKILL is uncatchable) — those still rely on the END block's
  manual `: >` truncates. (`arby_hot_loop.log` is truncated by the START block on redeploy.)
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
