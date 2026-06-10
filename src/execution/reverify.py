"""Live odds re-verify — the production `reverify` hook for the Executor.

Before placing each leg the Executor calls ``reverify(leg) -> current_odds`` and
checks ``Guardrails.odds_still_acceptable``; it then places AT the re-verified
odds. The default (`_no_reverify`) trusts the stated odds — fine for tests, unsafe
live. `LiveOddsReverifier` wires a LIVE per-platform refetch (the Tier-2
`QuoteRefresher`s) so a leg places only if its odds still hold within tolerance,
at the book's current price.

Fail-CLOSED: if the odds can't be confirmed — no refresher for the platform, a
refetch error, or the market is gone — it returns ``0.0``, which fails
``odds_still_acceptable`` so the Executor aborts (or flags naked exposure) rather
than place on unverified odds.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.arbitrage.quotes import OddsQuote
from src.execution.executor import Leg
from src.risk.refreshers import QuoteRefresher

log = structlog.get_logger(__name__)

_UNVERIFIED = 0.0  # sentinel that fails odds_still_acceptable → executor aborts


@dataclass
class LiveOddsReverifier:
    """Callable ``(leg) -> current decimal odds`` backed by per-platform refreshers.

    Keyed by ``leg.platform``. Carries the executor `Leg`'s identifiers into the
    refresher's `OddsQuote` shape (``platform_event_ref`` → ``platform_event_id``)."""

    refreshers: dict[str, QuoteRefresher]

    async def __call__(self, leg: Leg) -> float:
        refresher = self.refreshers.get(leg.platform)
        if refresher is None:
            log.warning("reverify.no_refresher", platform=leg.platform)
            return _UNVERIFIED
        quote = OddsQuote(
            platform=leg.platform,
            market_id=leg.market,
            outcome=leg.outcome,
            decimal_odds=leg.odds,
            max_stake=None,
            timestamp=0.0,
            platform_outcome_id=leg.platform_outcome_id,
            platform_event_id=leg.platform_event_ref,
        )
        try:
            fresh = await refresher.refresh(quote)
        except Exception as exc:  # noqa: BLE001 — any refetch fault → fail-closed
            log.warning("reverify.refresh_error", platform=leg.platform, error=str(exc))
            return _UNVERIFIED
        if fresh.decimal_odds is None:
            log.warning(
                "reverify.market_unavailable",
                platform=leg.platform,
                outcome_id=leg.platform_outcome_id,
            )
            return _UNVERIFIED
        return fresh.decimal_odds
