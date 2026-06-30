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

import httpx
import structlog

from src.execution.executor import DryRunPlacer, Executor, Leg
from src.execution.guardrails import Guardrails
from src.execution.hot_session import HotSessionManager
from src.execution.notify import build_notifier
from src.execution.orchestrator import ArbOrchestrator
from src.execution.quote_source import OverlapQuoteSource
from src.execution.recovery import HumanRecoveryHandler
from src.execution.reverify import BetanoCapRefresher, LiveOddsReverifier
from src.execution.session import _BW_SPORTSBOOK_HOME, InSessionTransport
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
from src.storage.audit_recorder import PostgresAuditRecorder

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BETANO_HOME = "https://www.betano.bet.ar/"
_BETSSON_HOME = "https://pba.betsson.bet.ar/apuestas-deportivas"
_BETWARRIOR_HOME = "https://pba.betwarrior.bet.ar/"


def _truncate_viewer_log() -> None:
    """Truncate the read-only session-viewer log (the operator's observation tool) so a
    fresh redeploy/restart starts clean. Called from the bot's shutdown path (incl. Ctrl+C);
    the END runbook block truncates too (pkill -9 can't run this handler)."""
    with contextlib.suppress(Exception):
        open("/tmp/arby_session_viewer.log", "w").close()


async def main() -> int:
    configure_logging()
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
            source = OverlapQuoteSource(
                bulk_sources=[
                    BetanoScraper(http_client=client, mode="prematch"),
                    BetWarriorPbaScraper(http_client=client),
                ],
                linkers=[BetssonScraper(http_client=client)],
                canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
                staleness_sec=float(os.environ.get("STALENESS_SEC", "45")),
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

            # Reactive re-auth: on a BetWarrior placement 401 (server-killed Kambi
            # session), drive a logout→login on the bot's OWN BW window (keyring creds)
            # so the executor's single retry completes the arb instead of aborting/going
            # naked. Gated to live + betwarrior-pba + a wired BW transport; dry-run, an
            # unwired transport, or a missing/challenged re-auth returns False → today's
            # abort/naked (the rescue never adds exposure).
            async def _reauth(leg: Leg) -> bool:
                if leg.platform != "betwarrior-pba" or betwarrior_t is None or not live:
                    return False
                return await betwarrior_t.attempt_betwarrior_relogin()

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
            orch = ArbOrchestrator(
                quote_source=source,
                risk_evaluator=risk,
                executor=executor,
                guardrails=guard,
                budget_ars=budget,
                dynamic_stake_cap_ars=betano_cap,
                notifier=notifier,
                recorder=PostgresAuditRecorder() if live else None,
            )
            log.warning("hot_loop.start", live=live, budget=budget, poll=poll)
            # Controlled re-auth drill trigger (operator-authorized; removable after the
            # live validation): touch /tmp/arby_force_bw_reauth to run
            # attempt_betwarrior_relogin() once on the bot's LIVE BetWarrior transport.
            # Log out BW via the window UI → touch the sentinel → read the log for
            # bw_reauth_trigger_result. Live-only (betwarrior_t is None in dry-run).
            reauth_trigger: asyncio.Task[None] | None = None
            if betwarrior_t is not None:

                async def _bw_reauth_trigger(bw: InSessionTransport) -> None:
                    sentinel = "/tmp/arby_force_bw_reauth"
                    while True:
                        await asyncio.sleep(2.0)
                        try:
                            if not await asyncio.to_thread(os.path.exists, sentinel):
                                continue
                            await asyncio.to_thread(os.unlink, sentinel)
                        except FileNotFoundError:
                            continue
                        except Exception as exc:  # noqa: BLE001
                            log.warning("hot_loop.bw_reauth_trigger_error", error=str(exc))
                            continue
                        log.warning("hot_loop.bw_reauth_trigger_fired")
                        await notifier.send(
                            "🔧 BW re-auth drill: running attempt_betwarrior_relogin…"
                        )
                        try:
                            ok = await bw.attempt_betwarrior_relogin()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            log.warning("hot_loop.bw_reauth_trigger_error", error=str(exc))
                            await notifier.send("🔧 BW re-auth drill: ERROR (see log)")
                            continue
                        log.warning("hot_loop.bw_reauth_trigger_result", ok=ok)
                        await notifier.send(
                            "🔧 BW re-auth drill: "
                            + (
                                "✅ OK — fresh bearer captured"
                                if ok
                                else "❌ FAILED/challenged — BW may now be logged out (sign in manually)"
                            )
                        )

                reauth_trigger = asyncio.create_task(_bw_reauth_trigger(betwarrior_t))
            try:
                await orch.run_forever(poll_interval_sec=poll)  # until Ctrl-C (never self-halts)
            finally:
                if reauth_trigger is not None:
                    reauth_trigger.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reauth_trigger
                # Clean the observer log on graceful shutdown (incl. Ctrl+C / SIGTERM) so the
                # operator's tail clears when the bot stops. (pkill -9 in END can't run this.)
                await asyncio.to_thread(_truncate_viewer_log)
    log.info("hot_loop.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
