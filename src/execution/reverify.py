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
from src.execution import placers
from src.execution.executor import Leg
from src.execution.session import Transport, TransportError
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


# Betano's dynamic per-bet cap lives on a dedicated endpoint (NOT the public
# odds feed, NOT updatebets — that one echoes maxAmount: 0). The cap refresher
# probes it over the AUTHENTICATED transport so the executor can size a Betano
# leg to the REAL book ceiling instead of the static fallback.
_BETANO_BASE = "https://www.betano.bet.ar/api/betslip/v3"
_BETANO_LIMITS_URL = "https://www.betano.bet.ar/api/betslipcombo/limits"


def _data_envelope(resp: dict[str, object]) -> dict[str, object]:
    """Betano wraps its slip payload in ``{"data": {...}}``."""
    inner = resp.get("data")
    return inner if isinstance(inner, dict) else {}


@dataclass
class BetanoCapRefresher:
    """Callable ``(leg) -> live per-bet stake cap (ARS) | None`` for Betano.

    Betano's per-bet cap is dynamic (computed server-side); the public feed and
    ``updatebets`` carry no usable ``maxAmount``. This probes the dedicated
    ``POST /api/betslipcombo/limits`` over the AUTHENTICATED betano transport:
    plain-leg (build a one-selection slip) → limits (read ``data.max``).

    Fail-SOFT: any error, non-200, or non-positive cap → ``None`` (the caller
    falls back to the leg's static cap). Never raises into execution — a missing
    cap just sizes conservatively. Non-betano legs → ``None`` (nothing to probe).

    Slip-pollution note: plain-leg builds a fresh slip (empty hash); the placer
    rebuilds its own slip from plain-leg on every placement, so a probe slip is
    not the one placed. If a future incident shows Betano accumulating selections
    across slips, clear the slip after probing here.
    """

    transport: Transport

    async def __call__(self, leg: Leg) -> float | None:
        if leg.platform.split("-", 1)[0].lower() != "betano":
            return None  # only Betano's cap is dynamic; others use static caps
        event_id = leg.platform_event_ref or leg.match_id
        try:
            status, resp = await self.transport.fetch(
                "POST",
                f"{_BETANO_BASE}/plain-leg/",
                json_body=placers.build_betano_plain_leg(
                    leg.platform_outcome_id, event_id
                ),
            )
            if status >= 400:
                log.warning("cap_refresh.plain_leg_failed", status=status)
                return None
            data = _data_envelope(resp)
            slip = placers.betano_slip_from_response(data)
            if not slip.get("bets"):
                return None
            tag = placers.betano_leg_tag(data)
            if tag is None:
                return None
            status, resp = await self.transport.fetch(
                "POST",
                _BETANO_LIMITS_URL,
                json_body=placers.build_betano_limits(slip, tag),
            )
            if status >= 400:
                log.warning("cap_refresh.limits_failed", status=status)
                return None
            cap = placers.betano_limits_max(resp)
        except TransportError as exc:
            log.warning("cap_refresh.transport_error", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — any fault → fail-soft, never raise
            log.warning("cap_refresh.error", error=str(exc))
            return None
        if cap is None or cap <= 0:
            return None
        log.info("cap_refresh.fetched", platform=leg.platform, cap=cap)
        return cap
