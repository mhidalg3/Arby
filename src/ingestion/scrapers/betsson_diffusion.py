"""Betsson live (in-play) odds decoder — Diffusion pub/sub feed.

Betsson's in-play odds are pushed over a Diffusion (Push Technology)
WebSocket at ``wss://pba.betsson.bet.ar/diffusion``. The prematch
``accordion/v1`` HTTP endpoint returns ``{"data": {}}`` once a match goes
live, so this feed is the only in-play source.

Protocol decoded from a captured live session (Nice vs Saint-Étienne,
recon 2026-05-30 — see ``scripts/recon/RECON_LOG.md``):

* The client subscribes to Diffusion topic selectors
  ``?obg/sportsbook/transient/markets/<eventId>/`` and
  ``.../events/<eventId>/`` (the three ``obg/gossip/subscribe`` frames).
* Server value frames start with type byte ``0x04``; the high bit
  (``0x84``) flags a **zlib**-compressed payload. After a 4-byte header
  (``0b b8`` + a 2-byte topic id) comes the payload: a zlib stream
  (``78 01 …``) when compressed, raw **CBOR** otherwise. Big initial
  snapshots arrive compressed (``0x84``); small live updates arrive
  uncompressed (``0x04``) — both must be handled.
* The payload (decompressed if needed) is **CBOR**. Topics publish full
  values, not binary deltas (``PUBLISH_VALUES_ONLY=true``), so every
  frame is a complete value — no delta-application needed.
* Market values carry ``t == 27`` and
  ``d = {ei, mti, odds}``. ``d.odds`` maps a selection id to
  ``{"of": {"1": "<decimal>", "2": "<american>"}, "sof": {...}}`` —
  ``of["1"]`` is the decimal price. The 1X2 market is ``mti == "MW3W"``
  and its selection ids end in ``-home`` / ``-draw`` / ``-away`` (same
  encoding as the prematch accordion), so outcomes need no external map.

This module is the **pure decoder** (wire bytes → odds). A live WS
subscriber that speaks the Diffusion connect/subscribe handshake will
wrap it (TODO: ``betsson_ws.py``).
"""

from __future__ import annotations

import json
import zlib
from typing import Any

import cbor2

# A Diffusion value frame: type byte 0x04, a 4-byte header, then the payload.
# The 0x80 bit flags zlib compression (0x84). Uncompressed payloads are CBOR
# starting with an indefinite-length map (0xbf, the OBG value marker).
_VALUE_FRAME_TYPE = 0x04
_COMPRESSED_FLAG = 0x80
_ZLIB_HEADERS = (b"\x78\x01", b"\x78\x9c", b"\x78\xda")
_CBOR_MAP_START = 0xBF

# CBOR "t" (message type) for a market value, and the 1X2 market code.
MARKET_MESSAGE_TYPE = 27
MARKET_1X2 = "MW3W"
_DECIMAL_FORMAT_KEY = "1"  # of["1"] = decimal odds; of["2"] = American


def decode_value_frame(frame: bytes) -> dict[str, Any] | None:
    """Decode one Diffusion value frame to its CBOR map.

    Handles both ``0x04`` (raw CBOR) and ``0x84`` (zlib-compressed CBOR).
    Returns ``None`` for anything that isn't a decodable value frame
    (control frames, truncated payloads, etc.) — defensive over untrusted
    wire bytes."""
    if not frame or (frame[0] & ~_COMPRESSED_FLAG) != _VALUE_FRAME_TYPE:
        return None
    if frame[0] & _COMPRESSED_FLAG:
        start = min((j for j in (frame.find(h) for h in _ZLIB_HEADERS) if j != -1), default=-1)
        if start == -1:
            return None
        try:
            payload = zlib.decompress(frame[start:])
        except zlib.error:
            return None
    else:
        start = frame.find(_CBOR_MAP_START.to_bytes(1, "big"), 1)
        if start == -1:
            return None
        payload = frame[start:]
    try:
        value = cbor2.loads(payload)
    except cbor2.CBORDecodeError:
        return None
    return value if isinstance(value, dict) else None


def market_1x2_odds(value: dict[str, Any]) -> tuple[str, dict[str, float]] | None:
    """If ``value`` is a 1X2 (MW3W) market message, return
    ``(event_id, {"home"|"draw"|"away": decimal_odds})``; else ``None``.

    Only outcomes with a valid decimal price are included; a market with
    none is reported as ``None``."""
    if value.get("t") != MARKET_MESSAGE_TYPE:
        return None
    d = value.get("d")
    if not isinstance(d, dict) or d.get("mti") != MARKET_1X2:
        return None
    event_id = d.get("ei")
    odds_raw = d.get("odds")
    if not isinstance(event_id, str) or not isinstance(odds_raw, dict):
        return None

    out: dict[str, float] = {}
    for sel_id, sel in odds_raw.items():
        outcome = outcome_of_selection(sel_id)
        price = selection_decimal_price(sel)
        if outcome is not None and price is not None:
            out[outcome] = price
    return (event_id, out) if out else None


def outcome_of_selection(sel_id: Any) -> str | None:
    """Map a 1X2 selection id (…-home/-draw/-away) to its outcome role."""
    if not isinstance(sel_id, str):
        return None
    if sel_id.endswith("-home"):
        return "home"
    if sel_id.endswith("-draw"):
        return "draw"
    if sel_id.endswith("-away"):
        return "away"
    return None


def selection_decimal_price(sel: Any) -> float | None:
    """Decimal price from a selection value (``of["1"]``), or None."""
    if not isinstance(sel, dict):
        return None
    offered = sel.get("of")
    if not isinstance(offered, dict):
        return None
    try:
        return float(offered.get(_DECIMAL_FORMAT_KEY))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---- Subscription encoding (client → server) ----
#
# To receive a topic's values the client sends a Diffusion messaging-send to
# the path "obg/gossip/subscribe" whose String content is a JSON array of
# topic selectors. Frame layout (verified byte-for-byte against the captured
# subscribe frames):
#
#   00 55 <convId> <len(path)> "obg/gossip/subscribe"
#         <len("string")> "string" <len(cborValue)> <cborValue>
#
# where cborValue = CBOR text string of `["<selector>", ...]`, and the length
# prefixes are unsigned varints (single byte for our always-<128 sizes).

_SEND_PREFIX = b"\x00\x55"
_SUBSCRIBE_PATH = b"obg/gossip/subscribe"
_STRING_DATATYPE = b"string"

# Topic selectors. `?` prefixes a topic-path selector ("this path and below").
FIXTURE_PHASE_SELECTOR = "?obg/sportsbook/transient/events/.*/fixture/phase"


def markets_selector(event_id: str) -> str:
    """Topic selector for an event's market/odds updates."""
    return f"?obg/sportsbook/transient/markets/{event_id}/"


def events_selector(event_id: str) -> str:
    """Topic selector for an event's event-level updates."""
    return f"?obg/sportsbook/transient/events/{event_id}/"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | 0x80 if n else byte)
        if not n:
            return bytes(out)


def encode_subscribe_frame(conversation_id: int, *selectors: str) -> bytes:
    """Build the ``obg/gossip/subscribe`` send frame for one or more topic
    selectors. ``conversation_id`` is a per-message counter (1, 2, 3, …)."""
    content = json.dumps(list(selectors), separators=(",", ":"))
    cbor_value = cbor2.dumps(content)
    return (
        _SEND_PREFIX
        + bytes([conversation_id])
        + _varint(len(_SUBSCRIBE_PATH))
        + _SUBSCRIBE_PATH
        + _varint(len(_STRING_DATATYPE))
        + _STRING_DATATYPE
        + _varint(len(cbor_value))
        + cbor_value
    )
