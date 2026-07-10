# Deployment runbook — full bot stack

End-to-end operator procedure for the complete arby stack: infrastructure, the
GARCH / cross-platform latency dataset collection, the armed arbitrage bot, and the
read-only session viewer. Every command runs **from the operator's terminal** on the
bot host (`~/Documents/arby`).

> **⚠️ Launch long-running processes from the operator's terminal — never from an
> agent/assistant shell.** Daemons started inside an agent's bash session get reaped
> (observed 2026-06-24/25: bots died at 6 min / 44 min with no crash — external
> SIGKILL). `nohup … &` parented to your own shell survives indefinitely. Verify with
> `ps -o ppid= -p <pid>` — the PPID must be your shell, not an agent's.

## Dependency graph

```
 ┌─────────────────────────────────────────────────────────────────┐
 │  docker compose up -d        Postgres (TimescaleDB) + Redis      │  ← always first
 └─────────────────────────────────────────────────────────────────┘
          │
          ├──► COLLECT (≥7 days, one-time bootstrap)
          │    ┌─ RECORD_TO_PG=1 DISABLE_BETSSON=1 run_ingestion_daemon.py
          │    └─ RECORD_TICKS=1 run_hot_loop.py        (tees Betsson into Redis)
          │
          ├──► ANALYZE + ARTIFACT (after collection)
          │    └─ analyze_lag_structure.py --days 7 --garch --write-artifact
          │         → data/lag_model.json (garch_per_market_type + staleness_rank)
          │
          └──► ARMED BOT (daily operation)
               ┌─ run_hot_loop.py --arm --yes-real-money   (reads the artifact)
               └─ view_hot_sessions.py --keep-watching      (read-only observer)
```

The GARCH adaptive threshold and the lag-informed trigger/leg-ordering features are
**inert until the artifact exists**. Without `data/lag_model.json` the bot runs with
static thresholds and today's ordering — byte-identical to the pre-lag-model codebase.
Collection + analysis is a **one-time bootstrap**; re-run only to refresh the model.

---

## 0. Prerequisites (one-time)

```bash
cd ~/Documents/arby
cp .env.example .env          # then edit: set DATABASE_URL, REDIS_URL, ANTHROPIC_API_KEY
uv sync                       # install deps from uv.lock
```

Sportsbook login credentials live in the OS keychain (funded accounts → no plaintext
secrets on disk), not in `.env`:

```bash
uv run python -m src.credentials set betano
uv run python -m src.credentials set betsson
uv run python -m src.credentials set betwarrior
uv run python -m src.credentials status
```

---

## 1. Infrastructure — Postgres + Redis

**Start** (always first — the bot, the collector, and the recorder all need these):

```bash
docker compose up -d
```

Brings up `arby-postgres` (TimescaleDB on port 5433, runs `migrations/init.sql` on
first start) and `arby-redis` (port 6379, AOF-persisted, 256 MB LRU). Both restart
unless-stopped, so this is idempotent.

Verify:
```bash
docker compose ps                                # both healthy
docker compose exec postgres pg_isready -U arby  # OK
```

**Stop** (only when tearing down the whole stack — the bot/recorder need these up):

```bash
docker compose down          # stops containers, keeps named volumes (data survives)
docker compose down -v       # ALSO deletes postgres_data + redis_data (destructive)
```

---

## 2. GARCH + cross-platform latency dataset collection (one-time bootstrap, ≥7 days)

Collects the tick history the GARCH fit and the lag model need. Two processes run
**concurrently** so the dataset accrues without doubling traffic to Betsson (the
WAF-sensitive book):

```bash
docker compose up -d

# Terminal A — ingestion daemon WITHOUT its own Betsson scraper:
RECORD_TO_PG=1 DISABLE_BETSSON=1 uv run python scripts/run_ingestion_daemon.py

# Terminal B — hot loop tees every snapshot it already scrapes into Redis odds:raw
# (dry-run: no --arm; it detects but places through dry-run placers):
RECORD_TICKS=1 uv run python scripts/run_hot_loop.py
```

**Terminal B blocks at a login prompt** — the dry-run hot loop opens **two** warm
browser sessions (Betano + Betsson; BetWarrior's execution window opens only when
`--arm`). Log into both windows, confirm each shows a balance, then **press ENTER**
in Terminal B to start detection. (This is the same login gate as the armed bot — see
`docs/hot_loop_runbook.md`.) The tick tee only starts writing after the gate clears;
without this step the hot loop never runs and no Betsson rows are recorded.

**Why the split:** only the hot loop's overlap linker fetches Betsson
(`DISABLE_BETSSON=1` silences the daemon's Betsson poller), so Betsson stays at the
hot-loop-alone footprint — no extra WAF exposure. The daemon supplies Betano +
BetWarrior (list + depth) + Bplay, which the hot loop can't cover (no BW-depth
scraper; Betano's danae feed is 1X2-only).

Run continuously for **≥7 days spanning at least two weekend league windows**.
Sanity-check after 1 hour:

```sql
-- connect via the daemon's DATABASE_URL_SYNC, or:
docker compose exec postgres psql -U arby -d arby -c \
  "SELECT platform, count(*), count(*) FILTER (WHERE is_change) FROM odds_snapshots GROUP BY 1;"
```

**Daemon-only fallback** (no hot loop running — keep the daemon's Betsson scraper on):

```bash
docker compose up -d
RECORD_TO_PG=1 uv run python scripts/run_ingestion_daemon.py
```

### Generating the GARCH + lag artifact

After ≥7 days of collection, stop both processes (see termination below) and build the
artifact:

```bash
uv run python scripts/analyze_lag_structure.py --days 7 --garch --write-artifact --execution-audit
```

This writes `data/lag_model.json` containing:
- `garch_per_market_type` — median GARCH(1,1) params per market type (powers the
  adaptive margin threshold; empty `{}` if <3 accepted series per type → hot loop
  stays static, expected).
- `staleness_rank` — per-market-type laggard ranking (powers leg placement order).
- `burst_eligible_market_types` — market types eligible for trigger scanning.

Verify the GARCH section populated (if collection was long enough):
```bash
uv run python -c "import json; d=json.load(open('data/lag_model.json')); print(json.dumps(d.get('garch_per_market_type', {}), indent=2))"
```

### Terminating the collector

```bash
# Graceful (SIGTERM — the daemon drains its queue before exiting):
pkill -f 'run_ingestion_daemon.py'

# Hot-loop tick tee (same as the armed bot teardown, minus the browser/profile kills
# if it was a dry-run — but harmless to include):
pkill -9 -f 'run_hot_loop.py'
pkill -9 -f 'recon/profile/betano'
pkill -9 -f 'recon/profile/betsson'
pkill -9 -f 'recon/profile/betwarrior'
rm -f /tmp/arby_login_done
```

---

## 3. Armed bot + session viewer (daily operation)

> Detailed bot/viewer lifecycle, login-gate semantics, forced-relogin drills, and
> known gotchas live in **`docs/hot_loop_runbook.md`** — this section is the quick path.
>
> **Self-contained stack.** The hot-loop bot scrapes → detects → evaluates risk →
> places, all in-process. Do **not** start `run_arb_detector.py` or
> `run_risk_daemon.py` — those belong to the separate Redis-stream pipeline and are
> not part of the hot-loop path.

### Start

Two options. Both use the interactive **ENTER** login gate (not the file gate).

#### Option A — `tmux` (survives terminal close; recommended)

Prep (once):
```bash
cd ~/Documents/arby
: > /tmp/arby_session_viewer.log
rm -f /tmp/arby_login_done
```

Bot — this **attaches immediately** (you need the terminal to see the login prompt):
```bash
tmux new-session -s arby-bot \
  "GARCH_ADAPTIVE=1 CDP_PORT_BASE=9222 uv run python scripts/run_hot_loop.py --arm --yes-real-money"
```

Log into all three windows → confirm balances → **press ENTER**. Then detach (leave it
running): `Ctrl-B` then `D`. Re-attach any time: `tmux attach -t arby-bot`.

Viewer (in a **separate** terminal — it attaches to the bot's browsers over CDP,
independent of how the bot was launched):
```bash
nohup uv run python scripts/view_hot_sessions.py --keep-watching --log /tmp/arby_hot_loop.log --base-port 9222 >> /tmp/arby_session_viewer.log 2>&1 &
```

#### Option B — Foreground (simplest; two tabs; dies on tab close)

Tab 1 — armed bot:
```bash
cd ~/Documents/arby
GARCH_ADAPTIVE=1 CDP_PORT_BASE=9222 uv run python scripts/run_hot_loop.py --arm --yes-real-money
```
Tab 2 — session viewer (run AFTER the bot opens the windows):
```bash
uv run python scripts/view_hot_sessions.py --keep-watching --log /tmp/arby_hot_loop.log --base-port 9222
```
Optional Tab 3 — live JSON event tail:
```bash
tail -f /tmp/arby_hot_loop.log
```
Log into all three windows → confirm balances → **press ENTER in Tab 1**.

**GARCH adaptive threshold knobs** (env, read once at startup):
| Knob | Default | Effect |
|------|---------|--------|
| `GARCH_ADAPTIVE` | `1` | Uses GARCH-adaptive margins when `data/lag_model.json` has `garch_per_market_type`; `0` = static |
| `MIN_MARGIN_PCT_BASE` | `1.0` | Base detection margin (static value when GARCH off or no state) |
| `GARCH_SENSITIVITY` | `2.0` | Threshold response slope to relative excess volatility |

Startup confirms activation: look for `hot_loop.garch_adaptive` in the log (absent =
static mode, operator-visible):
```bash
grep 'hot_loop.garch_adaptive' /tmp/arby_hot_loop.log
```

**Dry run (no real money):** drop `--arm --yes-real-money`. Still opens windows and
detects; places through dry-run placers (no money moves).

### Health checks

```bash
# bot alive + parented to your shell?
ps -eo pid,ppid,etime,command | grep '[r]un_hot_loop.py'

# past the login gate? (hot_loop.start = gate cleared, live)
grep 'hot_loop.start' /tmp/arby_hot_loop.log | tail -1

# adaptive threshold active?
grep 'hot_loop.garch_adaptive' /tmp/arby_hot_loop.log | tail -1

# kill-switch state + arbs?
grep -E 'orchestrator\.arb_found|guardrails\.kill_switch' /tmp/arby_hot_loop.log | tail -8

# viewer attached?
tail -4 /tmp/arby_session_viewer.log
```

### Terminating the bot + viewer

```bash
pkill -9 -f 'run_hot_loop.py'                     # stop the bot
pkill -9 -f 'view_hot_sessions.py'                # stop the viewer
pkill -9 -f 'recon/profile/betano'                # release the three
pkill -9 -f 'recon/profile/betsson'               # persistent-profile
pkill -9 -f 'recon/profile/betwarrior'            # browser locks
rm -f /tmp/arby_login_done                        # reset the login gate
: > /tmp/arby_session_viewer.log                  # clear the observer tail
```

Killing the three profile browsers is **required** — without it their `SingletonLock`s
stay held and the next start can't launch on the same profiles.

Verify it's all down:
```bash
ps -eo command | grep -E 'run_hot_loop|view_hot_sessions|recon/profile/bet' | grep -v grep
# (no output = clean)
```

### Restart = terminate + start

Run the termination block, wait a couple seconds, run the start block. The bot
truncates its own log at startup; you must re-login + re-`touch` the gate each restart
(new browser instances open on the persistent profiles).

---

## 4. Full-stack teardown (everything)

```bash
# Bot + viewer + browser locks
pkill -9 -f 'run_hot_loop.py'
pkill -9 -f 'view_hot_sessions.py'
pkill -9 -f 'recon/profile/betano'
pkill -9 -f 'recon/profile/betsson'
pkill -9 -f 'recon/profile/betwarrior'
rm -f /tmp/arby_login_done

# Collector daemon (if running)
pkill -f 'run_ingestion_daemon.py'

# Infrastructure
docker compose down
```

`docker compose down` keeps the named volumes (your collected dataset + Postgres state
survive). Add `-v` only to wipe all data (destructive — loses the tick history and the
`opportunities` / `placements` audit rows).

---

## Logs & artifacts reference

| What | Where |
|------|-------|
| Bot JSON event log (in-process tee, truncated at startup) | `/tmp/arby_hot_loop.log` |
| Bot console (nohup redirect) | `/tmp/arby_hot_loop_console.log` |
| Session viewer stdout | `/tmp/arby_session_viewer.log` |
| Viewer dense-capture post-mortems | `recon/artifacts/session_viewer/<timestamp>/` |
| Betsson relogin failure evidence | `recon/artifacts/betsson_relogin/` |
| Lag + GARCH artifact | `data/lag_model.json` |
| Persistent browser profiles | `recon/profile/{betano,betsson,betwarrior}` |

Live tail (arb events + session health):
```bash
tail -f /tmp/arby_hot_loop.log | grep --line-buffered -E \
  'orchestrator\.arb_found|executor\.(aborted|completed|naked_exposure|leg_placement)|guardrails\.kill_switch|transport\.betsson_(context|relogin)'
```

## Further reading

- **`docs/hot_loop_runbook.md`** — full bot/viewer lifecycle, login-gate details,
  forced-relogin drills, Betsson evidence inspection, gotchas (minimized windows,
  timezones, Telegram blind spots).
- **`docs/cross_platform_lag_strategy.md`** — lag model estimator choices, the Phase
  C/D trigger gate, the Phase E GARCH-adaptive threshold mechanism, censoring caveats.
