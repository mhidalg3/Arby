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
import os

import httpx
import structlog

from src.execution.executor import DryRunPlacer, Executor
from src.execution.guardrails import Guardrails
from src.execution.hot_session import HotSessionManager
from src.execution.notify import build_notifier
from src.execution.orchestrator import ArbOrchestrator
from src.execution.quote_source import OverlapQuoteSource
from src.execution.recovery import HumanRecoveryHandler
from src.execution.reverify import BetanoCapRefresher, LiveOddsReverifier
from src.execution.session import InSessionTransport
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
        await loop.run_in_executor(
            None,
            input,
            f"\n  ▶ Log into {windows} (Betano: complete any challenge; Betsson: log in, "
            "stay on PBA — the manager will do the My-Account nav; BetWarrior: log in). "
            "When all show your balance, press ENTER… ",
        )

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
            executor = Executor(
                guardrails=guard,
                notifier=notifier,
                recovery=HumanRecoveryHandler(notifier),
                placers=placers,
                reverify=reverify,
                cap_refresh=BetanoCapRefresher(betano_t),
                dry_run=not live,
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
            await orch.run_forever(poll_interval_sec=poll)  # until Ctrl-C (never self-halts)
    log.info("hot_loop.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
