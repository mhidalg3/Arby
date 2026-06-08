"""Dry-run diagnostic for the live arbitrage loop (NO bets placed).

Runs the real pipeline on live data — scrape Betsson + Betano → canonicalize
(cross-platform fixture/market/outcome alignment) → assemble complete partitions
→ `detect_arbitrage` → `RiskEvaluator` — and LOGS what it finds. It never places:
this validates #1 (the live QuoteSource + canonicalization) end-to-end and answers
the key question — do the two books align and do any arbs appear — without risk.

Betano is scraped first (it's the fixture anchor: "{home} vs {away}"); Betsson
(non-anchor) links to Betano-registered fixtures by outcome-team label.

Usage:
    uv run python scripts/run_arb_loop.py                 # 3 cycles, ~5s apart
    CYCLES=10 POLL=8 BUDGET=2000 uv run python scripts/run_arb_loop.py
"""

from __future__ import annotations

import asyncio
import os

import httpx
import structlog

from src.arbitrage.dutch_book import detect_arbitrage
from src.execution.quote_source import CanonicalizingQuoteSource
from src.ingestion.scrapers.betano import BetanoScraper
from src.ingestion.scrapers.betsson import BetssonScraper
from src.logging_setup import configure_logging
from src.risk.decision import Verdict
from src.risk.evaluator import RiskEvaluator
from src.risk.policy import RiskPolicy
from src.semantic.canonicalizer import Canonicalizer
from src.semantic.fixture_resolver import FixtureResolver

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("arb_loop")
    cycles = int(os.environ.get("CYCLES", "3"))
    poll = float(os.environ.get("POLL", "5"))
    budget = float(os.environ.get("BUDGET", "1000"))
    min_margin = float(os.environ.get("MIN_MARGIN_PCT", "1.0"))

    headers = {"User-Agent": _UA, "Accept-Language": "es-AR,es;q=0.9"}
    risk = RiskEvaluator(
        policy=RiskPolicy(platform_reliability={"betano": 1.0, "betsson-pba": 1.0})
    )
    async with httpx.AsyncClient(headers=headers, timeout=25.0) as client:
        # Order matters: the anchor (Betano) must be scraped before the non-anchor
        # (Betsson) so fixtures exist for Betsson to link to.
        # The synchronous full scrape is slow (~100s for Betsson's per-event
        # accordion fetch), so quotes from one fetch span a wide time window. A
        # generous staleness keeps them in the partition for this dry-run; real
        # low-latency detection wants the streaming ingestion daemon, not a poll.
        source = CanonicalizingQuoteSource(
            scrapers=[
                BetanoScraper(http_client=client, mode="prematch"),
                BetssonScraper(http_client=client),
            ],
            canonicalizer=Canonicalizer(fixture_resolver=FixtureResolver()),
            staleness_sec=float(os.environ.get("STALENESS_SEC", "180")),
        )
        for cycle in range(1, cycles + 1):
            partitions = await source.fetch()
            cross = {
                m: q for m, q in partitions.items() if len({leg.platform for leg in q}) >= 2
            }
            log.info(
                "loop.cycle",
                cycle=cycle,
                complete_partitions=len(partitions),
                cross_platform=len(cross),
            )
            for market_id, quotes in cross.items():
                platforms = sorted({leg.platform for leg in quotes})
                opp = detect_arbitrage(quotes, budget, min_margin)
                if opp is None:
                    log.info("loop.no_arb", market_id=market_id, platforms=platforms)
                    continue
                decision = risk.evaluate(opp)
                log.warning(
                    "loop.ARB_FOUND",
                    market_id=market_id,
                    platforms=platforms,
                    roi_pct=round(opp.realized_roi_pct, 3),
                    stakes=[round(s, 2) for s in opp.stakes],
                    verdict=decision.verdict.value,
                    reason=decision.reason,
                    would_place=decision.verdict is Verdict.APPROVED,
                )
            if cycle < cycles:
                await asyncio.sleep(poll)
    log.info("loop.done", cycles=cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
