"""Live hot-session arbitrage loop: warm sessions → detect → (place).

Wires the whole bot together: a fast `OverlapQuoteSource` for detection, a
`HotSessionManager` keeping both platforms' browser sessions open + authenticated,
and the `ArbOrchestrator` driving detect → risk → execute through those warm
sessions. On startup the operator logs into BOTH windows once; the manager then
establishes the Betsson betting context and heartbeats to keep both alive.

SAFETY: dry-run by default — it opens the real sessions and detects live, but
places through `DryRunPlacer`s (nothing sent). `--arm --yes-real-money` switches to
real placement through the warm transports, capped per leg.

    uv run python scripts/run_hot_loop.py                       # dry-run
    uv run python scripts/run_hot_loop.py --arm --yes-real-money  # LIVE money
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable

import httpx
import structlog
from redis.asyncio import Redis, from_url

from src.arbitrage.garch import AdaptiveThreshold, GarchParams
from src.config import get_settings
from src.execution.executor import DryRunPlacer, Executor, Leg
from src.execution.guardrails import Guardrails
from src.execution.hot_session import HotSessionManager
from src.execution.notify import build_notifier
from src.execution.orchestrator import ArbOrchestrator
from src.execution.quote_source import OverlapQuoteSource
from src.execution.recovery import HumanRecoveryHandler
from src.execution.reverify import BetanoCapRefresher, LiveOddsReverifier
from src.execution.session import _BW_SPORTSBOOK_HOME, InSessionTransport
from src.ingestion.redis_sink import RedisSnapshotSink
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.ingestion.scrapers.betano import BetanoScraper
from src.ingestion.scrapers.betsson import BetssonScraper
from src.ingestion.scrapers.betwarrior import BetWarriorPbaDepthScraper, BetWarriorPbaScraper
from src.logging_setup import configure_logging
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy
from src.risk.refreshers import (
    BetanoQuoteRefresher,
    BetssonQuoteRefresher,
    BetWarriorQuoteRefresher,
)
from src.semantic.canonicalizer import Canonicalizer
from src.semantic.fixture_resolver import FixtureResolver
from src.storage.audit_recorder import PostgresAuditRecorder, audit_watchdog

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BETANO_HOME = "https://www.betano.bet.ar/"
_BETSSON_HOME = "https://pba.betsson.bet.ar/apuestas-deportivas"
_BETWARRIOR_HOME = "https://pba.betwarrior.bet.ar/"
# Bot log — the session viewer tails this file for arb events (dense capture +
# postmortem triggers). Written IN-PROCESS via the configure_logging tee so the
# viewer contract holds no matter how the operator launches the bot; truncated
# at startup (clean slate for the viewer's offset-0 replay).
_HOT_LOOP_LOG = "/tmp/arby_hot_loop.log"


def _truncate_viewer_log() -> None:
    """Truncate the read-only session-viewer log (the operator's observation tool) so a
    fresh redeploy/restart starts clean. Called from the bot's shutdown path (incl. Ctrl+C);
    the END runbook block truncates too (pkill -9 can't run this handler)."""
    with contextlib.suppress(Exception):
        open("/tmp/arby_session_viewer.log", "w").close()


def _load_lag_model() -> dict | None:
    """Load the cross-platform lag model artifact for lag-informed features.

    Reads env ``LAG_MODEL_PATH`` (default ``data/lag_model.json``). Returns the
    parsed dict, or None when the file is missing/malformed (all lag-informed
    features then use built-in defaults / today's behavior). One warning on
    malformed; silent on missing (expected before the first analysis run).
    """
    import json

    path = os.environ.get("LAG_MODEL_PATH", "data/lag_model.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        structlog.get_logger("hot_loop").warning("hot_loop.lag_model_malformed", error=str(exc))
        return None
    if not isinstance(data, dict):
        structlog.get_logger("hot_loop").warning(
            "hot_loop.lag_model_malformed", error=f"expected object, got {type(data).__name__}"
        )
        return None
    return data


def _load_garch_thresholds(lag_model: dict | None, floor_pct: float) -> AdaptiveThreshold | None:
    """AdaptiveThreshold from the artifact's ``garch_per_market_type`` section.

    None (static thresholds) when: ``GARCH_ADAPTIVE`` is off, the section is
    absent/empty, or no entry validates. Env-read convention matches the rest of
    this script (BUDGET, POLL, ...): direct ``os.environ``, NOT ``get_settings`` —
    the money path must not require a full validated .env. Env names match the
    config-field/.env.example names ``MIN_MARGIN_PCT_BASE`` / ``GARCH_SENSITIVITY``.
    """
    if os.environ.get("GARCH_ADAPTIVE", "1").strip() not in ("1", "true", "yes"):
        return None
    raw = (lag_model or {}).get("garch_per_market_type")
    if not isinstance(raw, dict) or not raw:
        return None
    params = {mt: p for mt, d in raw.items() if (p := GarchParams.from_artifact(d)) is not None}
    if not params:
        return None
    adaptive = AdaptiveThreshold(
        base_pct=float(os.environ.get("MIN_MARGIN_PCT_BASE", "1.0")),
        sensitivity=float(os.environ.get("GARCH_SENSITIVITY", "2.0")),
        floor_pct=floor_pct,
        params_by_market_type=params,
    )
    structlog.get_logger("hot_loop").info("hot_loop.garch_adaptive", market_types=sorted(params))
    return adaptive


async def main() -> int:
    configure_logging(tee_path=_HOT_LOOP_LOG)
    log = structlog.get_logger("hot_loop")
    p = argparse.ArgumentParser()
    p.add_argument("--arm", action="store_true", help="place real bets through the warm sessions")
    p.add_argument("--yes-real-money", action="store_true")
    args = p.parse_args()
    live = args.arm and args.yes_real_money
    if args.arm and not args.yes_real_money:
        raise SystemExit("REFUSED: --arm requires --yes-real-money")

    budget = float(os.environ.get("BUDGET", "5000"))
    # 45s default: with the multi-book overlap, a faster poll bursts the linker (Betsson)
    # into a WAF 403. Gives the per-event fetches room + keeps odds within staleness.
    poll = float(os.environ.get("POLL", "45"))
    betano_cap = float(os.environ.get("BETANO_CAP_ARS", "300"))
    trigger_poll = float(os.environ.get("TRIGGER_POLL", "0"))
    record_ticks = os.environ.get("RECORD_TICKS", "").strip() in ("1", "true", "yes")
    lag_model = _load_lag_model()
    # staleness_rank feeds order_opportunity_for_execution's nested .get() chain;
    # accept it only when it's a mapping, else None (today's ordering).
    _sr = (lag_model or {}).get("staleness_rank")
    staleness_rank = _sr if isinstance(_sr, dict) else None

    guard = Guardrails(
        max_position_per_match_ars=5000.0,
        max_total_exposure_ars=15000.0,
        max_daily_loss_ars=1000.0,
        odds_tolerance_pct=1.0,
    )
    risk = RiskEvaluator(
        policy=RiskPolicy(
            platform_reliability={"betano": 1.0, "betsson-pba": 1.0, "betwarrior-pba": 1.0}
        )
    )
    adaptive = _load_garch_thresholds(lag_model, floor_pct=risk.policy.min_margin_pct)
    headers = {"User-Agent": _UA, "Accept-Language": "es-AR,es;q=0.9"}

    # Real sessions either way (so a dry-run validates the warm-session lifecycle);
    # placement is what differs. In dry-run the transports are NOT armed (fetch
    # refuses) AND the placers are DryRunPlacers — two independent guards. BetWarrior's
    # execution session (a login window) is opened only when live; its odds are scraped
    # for detection in BOTH modes (public Kambi API, no login).
    betano_t = InSessionTransport("betano", dry_run=False)
    betsson_t = InSessionTransport("betsson", dry_run=False, restore_session=True)
    betwarrior_t = InSessionTransport("betwarrior", dry_run=False) if live else None

    async def _operator_login() -> None:
        # The manager has just opened the (blank) windows — point each at its site
        # so the operator has a login page, then wait for them to finish.
        await betano_t.goto(_BETANO_HOME)
        await betsson_t.goto(_BETSSON_HOME)
        windows = "BOTH windows"
        if betwarrior_t is not None:
            await betwarrior_t.goto(_BETWARRIOR_HOME)
            windows = "ALL THREE windows"
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                input,
                f"\n  ▶ Log into {windows} (Betano: complete any challenge; Betsson: log "
                "in, stay on PBA — the manager will do the My-Account nav; BetWarrior: "
                "log in — the bot opens its sportsbook after). When all show your "
                "balance, press ENTER… ",
            )
        except EOFError:
            # Background launch: stdin is the gate feeder
            # (`while [ ! -f /tmp/arby_login_done ]; do sleep 2; done`), which EOFs when
            # the operator touches the gate file. Proceed ONLY if the file actually exists
            # — a feeder death before the operator is ready must NOT silently trade past
            # the gate (would place through un-logged-in sessions).
            if not await asyncio.to_thread(os.path.exists, "/tmp/arby_login_done"):
                raise SystemExit(
                    "login gate: stdin EOF but /tmp/arby_login_done absent — feeder died "
                    "before the operator signaled ready; aborting (sessions not logged in)"
                ) from None
        # BetWarrior emits its Kambi placement bearer ONLY when the sportsbook widget
        # loads (validated live during the reauth work — auth alone on the root page
        # does NOT surface it). After the operator logs in, navigate BW to the
        # sportsbook home so the first readiness probe captures the bearer; otherwise
        # BW reads cold and disables auto-placement until a manual force-reauth.
        if betwarrior_t is not None:
            await betwarrior_t.goto(_BW_SPORTSBOOK_HOME)

    async with httpx.AsyncClient(headers=headers, timeout=25.0) as client:
        # Real notifier if Telegram creds are in the keychain, else a NullNotifier.
        # Shared by the manager (cold-session alerts), executor (naked leg / freeze)
        # and orchestrator (arb found / errors). See src/execution/notify.py.
        notifier = build_notifier(client)
        manager = HotSessionManager(
            betano=betano_t,
            betsson=betsson_t,
            betwarrior=betwarrior_t,
            guardrails=guard,
            login_gate=_operator_login,
            arm=live,
            notifier=notifier,
        )
        async with manager:
            # Detection over all three books: Betano + BetWarrior are bulk anchors
            # (one cheap call each, register fixtures); Betsson is the overlap linker.
            tick_queue: asyncio.Queue[RawOddsSnapshot] | None = None
            tick_stop: asyncio.Event | None = None
            tick_sink_task: asyncio.Task[None] | None = None
            tick_redis: Redis | None = None
            if record_ticks:
                try:
                    tick_redis = from_url(get_settings().redis_url, decode_responses=True)
                    await tick_redis.ping()  # type: ignore[misc]
                    tick_queue = asyncio.Queue(maxsize=10_000)
                    tick_stop = asyncio.Event()
                    tick_sink_task = asyncio.create_task(
                        RedisSnapshotSink(redis_client=tick_redis).run(tick_queue, tick_stop),
                        name="tick-sink",
                    )
                    log.info("hot_loop.tick_recording_enabled")
                except Exception as exc:  # noqa: BLE001 — recording is best-effort
                    log.error("hot_loop.tick_recording_disabled", error=str(exc))
                    if tick_redis is not None:
                        with contextlib.suppress(Exception):
                            await tick_redis.aclose()
                    tick_redis = None

            source = OverlapQuoteSource(
                bulk_sources=[
                    BetanoScraper(http_client=client, mode="prematch"),
                    BetWarriorPbaScraper(http_client=client),
                ],
                linkers=[BetssonScraper(http_client=client)],
                canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
                staleness_sec=float(os.environ.get("STALENESS_SEC", "45")),
                lag_model=lag_model,
                snapshot_sink=tick_queue,
            )
            placers = (
                manager.placers()
                if live
                else {
                    "betano": DryRunPlacer(),
                    "betsson-pba": DryRunPlacer(),
                    "betwarrior-pba": DryRunPlacer(),
                }
            )
            # Live re-verify: before placing each leg, re-fetch its current odds and
            # place only if the arb still holds within tolerance, AT the current odds.
            # Fail-closed if a leg's odds can't be confirmed.
            reverify = LiveOddsReverifier(
                refreshers={
                    "betano": BetanoQuoteRefresher(
                        BetanoScraper(http_client=client, mode="prematch")
                    ),
                    "betsson-pba": BetssonQuoteRefresher(BetssonScraper(http_client=client)),
                    "betwarrior-pba": BetWarriorQuoteRefresher(
                        BetWarriorPbaDepthScraper(http_client=client)
                    ),
                }
            )

            # Reactive re-auth: on a placement 401 (server-killed session), drive a
            # logout→login on the bot's OWN window (keyring creds) so the executor's
            # single retry completes the arb instead of aborting/going naked. Gated to
            # live + a wired transport for the leg's platform; dry-run, an unwired
            # transport, or a missing/challenged re-auth returns False → today's
            # abort/naked (the rescue never adds exposure). Betsson's relogin is scan-
            # first, so a healthy-but-ctx-lost session is re-established, not logged out.
            async def _reauth(leg: Leg) -> bool:
                if not live:
                    return False
                if leg.platform == "betwarrior-pba" and betwarrior_t is not None:
                    return await betwarrior_t.attempt_betwarrior_relogin()
                if leg.platform == "betsson-pba":
                    return await betsson_t.attempt_betsson_relogin()
                return False

            executor = Executor(
                guardrails=guard,
                notifier=notifier,
                recovery=HumanRecoveryHandler(notifier),
                placers=placers,
                reverify=reverify,
                cap_refresh=BetanoCapRefresher(betano_t),
                dry_run=not live,
                reauth=_reauth,
            )
            # Hoisted so the audit watchdog can share it: same recorder drives the
            # write paths (down/recovered alerts on each audit attempt) and the
            # periodic liveness probe (alert even when no arbs occur).
            recorder = PostgresAuditRecorder(notifier=notifier) if live else None

            orch = ArbOrchestrator(
                quote_source=source,
                risk_evaluator=risk,
                executor=executor,
                guardrails=guard,
                budget_ars=budget,
                min_margin_pct=float(os.environ.get("MIN_MARGIN_PCT_BASE", "1.0")),
                dynamic_stake_cap_ars=betano_cap,
                notifier=notifier,
                recorder=recorder,
                staleness_rank=staleness_rank,
                adaptive_threshold=adaptive,
            )
            log.warning("hot_loop.start", live=live, budget=budget, poll=poll)
            # Controlled re-auth drill triggers (operator-authorized; removable after live
            # validation): manually log out the target bookmaker window, then touch its
            # sentinel to run the relogin method once on the bot's transport:
            #   /tmp/arby_force_bw_reauth       -> attempt_betwarrior_relogin()
            #   /tmp/arby_force_betsson_reauth  -> attempt_betsson_relogin()
            # Betsson is scan-first: if still logged in, the drill only re-establishes ctx;
            # if logged out, it drives the same relogin path the heartbeat/executor use.
            background_tasks: list[asyncio.Task[None]] = []

            async def _reauth_trigger(
                *,
                sentinel: str,
                platform: str,
                label: str,
                action: str,
                attempt: Callable[[], Awaitable[bool]],
                ok_message: str,
                fail_message: str,
            ) -> None:
                while True:
                    await asyncio.sleep(2.0)
                    try:
                        if not await asyncio.to_thread(os.path.exists, sentinel):
                            continue
                        await asyncio.to_thread(os.unlink, sentinel)
                    except FileNotFoundError:
                        continue
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "hot_loop.reauth_trigger_error", platform=platform, error=str(exc)
                        )
                        continue
                    log.warning(
                        "hot_loop.reauth_trigger_fired", platform=platform, sentinel=sentinel
                    )
                    await notifier.send(f"🔧 {label} re-auth drill: running {action}…")
                    try:
                        ok = await attempt()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "hot_loop.reauth_trigger_error", platform=platform, error=str(exc)
                        )
                        await notifier.send(f"🔧 {label} re-auth drill: ERROR (see log)")
                        continue
                    log.warning("hot_loop.reauth_trigger_result", platform=platform, ok=ok)
                    await notifier.send(
                        f"🔧 {label} re-auth drill: " + (ok_message if ok else fail_message)
                    )

            if betwarrior_t is not None:
                background_tasks.append(
                    asyncio.create_task(
                        _reauth_trigger(
                            sentinel="/tmp/arby_force_bw_reauth",
                            platform="betwarrior",
                            label="BW",
                            action="attempt_betwarrior_relogin",
                            attempt=betwarrior_t.attempt_betwarrior_relogin,
                            ok_message="✅ OK — fresh bearer captured",
                            fail_message=(
                                "❌ FAILED/challenged — BW may now be logged out (sign in manually)"
                            ),
                        )
                    )
                )
            background_tasks.append(
                asyncio.create_task(
                    _reauth_trigger(
                        sentinel="/tmp/arby_force_betsson_reauth",
                        platform="betsson",
                        label="Betsson",
                        action="attempt_betsson_relogin",
                        attempt=betsson_t.attempt_betsson_relogin,
                        ok_message="✅ OK — logged-in UI + ctx captured",
                        fail_message=(
                            "❌ NOT READY/FAILED — Betsson relogin did not reach "
                            "logged-in UI + ctx; check transport.betsson_relogin_* logs, "
                            "then sign in manually if needed"
                        ),
                    )
                )
            )
            if recorder is not None:
                background_tasks.append(asyncio.create_task(audit_watchdog(recorder)))
            try:
                await orch.run_forever(
                    poll_interval_sec=poll, trigger_interval_sec=trigger_poll
                )  # until Ctrl-C (never self-halts)
            finally:
                for trigger in background_tasks:
                    trigger.cancel()
                for trigger in background_tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await trigger
                if tick_stop is not None and tick_sink_task is not None:
                    tick_stop.set()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(tick_sink_task, timeout=10)
                    if not tick_sink_task.done():
                        tick_sink_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await tick_sink_task
                if tick_redis is not None:
                    await tick_redis.aclose()
                # Clean the observer log on graceful shutdown (incl. Ctrl+C / SIGTERM) so the
                # operator's tail clears when the bot stops. (pkill -9 in END can't run this.)
                await asyncio.to_thread(_truncate_viewer_log)
    log.info("hot_loop.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
