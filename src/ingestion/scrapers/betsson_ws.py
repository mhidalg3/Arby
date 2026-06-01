"""Betsson PBA live (in-play) scraper — Diffusion WebSocket subscriber.

Companion to ``BetssonScraper`` (the HTTP prematch ``accordion/v1``, which
returns ``{"data": {}}`` once a match goes live). This subscribes to
Betsson's Diffusion feed at ``wss://pba.betsson.bet.ar/diffusion`` and
decodes live odds via :mod:`betsson_diffusion`. Same
``platform_name = "betsson-pba"`` as the HTTP scraper, so downstream
treats the two feeds as one book.

Protocol (see ``scripts/recon/RECON_LOG.md`` 2026-05-30 + the encode/decode
helpers in :mod:`betsson_diffusion`):

1. Connect with the WB-transport params in the URL query; the server
   replies with a connect/session frame (type ``0x23``) and then accepts
   messaging sends.
2. Subscribe by sending ``obg/gossip/subscribe`` frames
   (``encode_subscribe_frame``) carrying the markets/events selectors per
   event.
3. Server pushes value frames (``0x04``/``0x84``) that decode to CBOR;
   market messages (``t == 27``, ``mti == "MW3W"``) carry the live 1X2 odds.

NETWORK CAVEAT: the connect handshake + keepalive were derived from a
PASSIVE capture and have not been validated against a live connection —
the first live run may need a round of iteration (e.g. responding to
server pings). To sidestep long-lived keepalive, each cycle opens a fresh
connection, subscribes, streams for a bounded window, then closes — the
same per-cycle streaming pattern as ``bplay_sse.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

import httpx
import structlog
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from src.ingestion.scrapers.base import BaseScraper, RawOddsSnapshot
from src.ingestion.scrapers.betsson import (
    DEFAULT_SOCCER_SLUG_PREFIXES,
    BetssonContractError,
    BetssonScraper,
)
from src.ingestion.scrapers.betsson_diffusion import (
    MARKET_1X2,
    MARKET_MESSAGE_TYPE,
    decode_value_frame,
    encode_subscribe_frame,
    events_selector,
    markets_selector,
    outcome_of_selection,
    selection_decimal_price,
)

log = structlog.get_logger(__name__)

WS_BASE_URL: Final[str] = "wss://pba.betsson.bet.ar/diffusion"

# WB (WebSocket) transport connection params, from the captured session.
# `sp` carries Diffusion session properties; `r` is the reconnect window (ms).
_CONNECT_PARAMS: Final[dict[str, str]] = {
    "ty": "WB",
    "v": "28",
    "ca": "10",
    "r": "60000",
    "sp": json.dumps(
        {
            "--isReconnection-0--": "",
            "--channel-1--": "",
            "--os-MacOSX--": "",
            "$Environment": "BROWSER_Chrome_148.0.0.0",
        },
        separators=(",", ":"),
    ),
}

_RAW_MARKET_NAME = "Ganador del partido"  # the MW3W 1X2 market's human label
DEFAULT_STREAM_DURATION_SEC: Final[float] = 20.0
_CONNECT_TIMEOUT_SEC: Final[float] = 15.0
_MAX_FRAME_BYTES: Final[int] = 1 << 24  # initial snapshots can be large
# Topic selectors per obg/gossip/subscribe frame — one frame carries many
# (verified live). Batching keeps the conversation id and frame count small.
_SUBSCRIBE_BATCH: Final[int] = 50


def _connect_url() -> str:
    return WS_BASE_URL + "?" + urllib.parse.urlencode(_CONNECT_PARAMS)


def market_value_to_snapshots(
    value: dict[str, Any], raw_event_name: str, observed_at: float
) -> list[RawOddsSnapshot]:
    """Build 1X2 ``RawOddsSnapshot``s from a decoded market value.

    Returns ``[]` for anything that isn't an MW3W (1X2) market message.
    Pure — the network layer feeds it decoded CBOR values."""
    if value.get("t") != MARKET_MESSAGE_TYPE:
        return []
    d = value.get("d")
    if not isinstance(d, dict) or d.get("mti") != MARKET_1X2:
        return []
    event_id = d.get("ei")
    odds = d.get("odds")
    if not isinstance(event_id, str) or not isinstance(odds, dict):
        return []
    market_id = str(value.get("id", ""))
    out: list[RawOddsSnapshot] = []
    for sel_id, sel in odds.items():
        outcome = outcome_of_selection(sel_id)
        price = selection_decimal_price(sel)
        if outcome is None or price is None:
            continue
        out.append(
            RawOddsSnapshot(
                platform="betsson-pba",
                platform_event_id=event_id,
                platform_market_id=market_id,
                platform_outcome_id=str(sel_id),
                raw_event_name=raw_event_name or event_id,
                raw_market_name=_RAW_MARKET_NAME,
                raw_outcome_name=outcome,
                decimal_odds=price,
                max_stake=None,
                timestamp=observed_at,
            )
        )
    return out


class BetssonWsScraper(BaseScraper):
    """Live in-play 1X2 odds for Betsson PBA via the Diffusion WS feed.

    ``fetch_event_odds(event_id)`` is the surgical single-event path.
    ``fetch_live_soccer()`` discovers candidate soccer events from the HTTP
    categories tree (needs an ``http_client``) and subscribes to them; only
    events that are actually in-running publish on the Diffusion transient
    channel, so the live filter is implicit. Inject ``discover`` to override
    the event source (e.g. in tests).
    """

    platform_name = "betsson-pba"
    poll_interval_sec = 5.0
    backoff_seconds_on_error = 30.0

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        stream_duration_sec: float = DEFAULT_STREAM_DURATION_SEC,
        discover: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None,
        soccer_slug_prefixes: tuple[str, ...] = DEFAULT_SOCCER_SLUG_PREFIXES,
    ) -> None:
        self._http_client = http_client
        self._stream_duration_sec = stream_duration_sec
        self._discover = discover
        self._soccer_slug_prefixes = soccer_slug_prefixes
        self._log = log.bind(platform=self.platform_name, mode="ws")

    async def fetch_event_odds(
        self, event_id: str, raw_event_name: str = ""
    ) -> list[RawOddsSnapshot]:
        """Subscribe to one event and return its latest 1X2 snapshot per
        selection seen during the stream window."""
        latest: dict[str, RawOddsSnapshot] = {}
        selectors = [markets_selector(event_id), events_selector(event_id)]
        async for snap in self._stream(selectors, {event_id: raw_event_name}):
            latest[snap.platform_outcome_id] = snap
        return list(latest.values())

    async def fetch_live_soccer(self) -> AsyncIterator[RawOddsSnapshot]:
        candidates = await self._discover_live_events()
        if not candidates:
            self._log.debug("ws.no_candidate_events")
            return
        names = dict(candidates)
        # Subscribe to every soccer event's market topic; only in-running
        # events publish on the transient channel, so non-live ones are
        # silently filtered. (events-topic omitted here — markets carry the
        # odds; keeps the subscription set half the size.)
        selectors = [markets_selector(event_id) for event_id, _ in candidates]
        self._log.debug("ws.subscribing", candidates=len(selectors))
        async for snap in self._stream(selectors, names):
            yield snap

    # ---- internals ----

    async def _discover_live_events(self) -> list[tuple[str, str]]:
        """Return ``(event_id, name)`` for candidate soccer events. The HTTP
        categories tree marks which events are soccer (the live filter then
        falls out of the Diffusion subscription — non-live events don't
        publish on the transient channel). Override via ``discover``."""
        if self._discover is not None:
            return await self._discover()
        if self._http_client is None:
            self._log.warning("ws.no_discovery_source")
            return []
        scraper = BetssonScraper(
            self._http_client, soccer_slug_prefixes=self._soccer_slug_prefixes
        )
        try:
            fixtures = await scraper._discover_soccer_fixtures()
        except BetssonContractError as exc:
            self._log.warning("ws.discovery_failed", error=str(exc))
            return []
        return [(fx.event_id, scraper._event_name_from_slug(fx.slug)) for fx in fixtures]

    async def _stream(
        self, selectors: list[str], names: dict[str, str]
    ) -> AsyncIterator[RawOddsSnapshot]:
        """Open a connection, subscribe to the given topic selectors, and
        yield 1X2 snapshots for the stream window. Selectors are sent in
        batched frames (one ``obg/gossip/subscribe`` carries many) to keep
        the conversation id small and the frame count low. A transport
        failure is logged and ends the cycle (the framework backs off)."""
        if not selectors:
            return
        try:
            async with connect(
                _connect_url(), open_timeout=_CONNECT_TIMEOUT_SEC, max_size=_MAX_FRAME_BYTES
            ) as ws:
                for conv, start in enumerate(
                    range(0, len(selectors), _SUBSCRIBE_BATCH), start=1
                ):
                    batch = selectors[start : start + _SUBSCRIBE_BATCH]
                    await ws.send(encode_subscribe_frame(conv, *batch))
                deadline = time.monotonic() + self._stream_duration_sec
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except TimeoutError:
                        break
                    if not isinstance(msg, bytes | bytearray):
                        continue
                    value = decode_value_frame(bytes(msg))
                    if value is None:
                        continue
                    d = value.get("d")
                    ei = d.get("ei") if isinstance(d, dict) else None
                    name = names.get(ei, "") if isinstance(ei, str) else ""
                    for snap in market_value_to_snapshots(value, name, time.time()):
                        yield snap
        except (OSError, WebSocketException) as exc:
            self._log.warning("ws.stream_failed", error=str(exc))
