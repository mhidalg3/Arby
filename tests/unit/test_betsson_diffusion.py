"""Tests for the Betsson Diffusion live-odds decoder.

The golden fixture `betsson_diffusion_mw3w.b64` is a REAL value frame
captured from Betsson's live Diffusion feed (Nice vs Saint-Étienne,
recon 2026-05-30) — so this pins the decode against actual wire bytes,
not a synthetic mock.
"""

from __future__ import annotations

import base64
import zlib
from pathlib import Path

import cbor2

from src.ingestion.scrapers.betsson_diffusion import (
    FIXTURE_PHASE_SELECTOR,
    decode_value_frame,
    encode_subscribe_frame,
    events_selector,
    market_1x2_odds,
    markets_selector,
)

_EVENT = "f-rdwm7m-uK0yqVgGGcW5KIg"

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_FIXTURE = _FIXTURES / "betsson_diffusion_mw3w.b64"
_FIXTURE_UNCOMPRESSED = _FIXTURES / "betsson_diffusion_uncompressed.b64"


def _real_mw3w_frame() -> bytes:
    return base64.b64decode(_FIXTURE.read_text())


def test_decodes_real_mw3w_frame_to_cbor() -> None:
    value = decode_value_frame(_real_mw3w_frame())
    assert value is not None
    assert value["t"] == 27
    assert value["d"]["mti"] == "MW3W"
    assert value["d"]["ei"] == "f-rdwm7m-uK0yqVgGGcW5KIg"


def test_extracts_1x2_odds_from_real_frame() -> None:
    value = decode_value_frame(_real_mw3w_frame())
    assert value is not None
    result = market_1x2_odds(value)
    assert result is not None
    event_id, odds = result
    assert event_id == "f-rdwm7m-uK0yqVgGGcW5KIg"
    assert odds == {"home": 2.55, "draw": 2.25, "away": 3.90}


def test_decodes_real_uncompressed_frame() -> None:
    """0x04 frames carry raw (un-zlib'd) CBOR — small live updates use this."""
    frame = base64.b64decode(_FIXTURE_UNCOMPRESSED.read_text())
    assert frame[0] == 0x04  # uncompressed value frame
    value = decode_value_frame(frame)
    assert value is not None
    assert "id" in value and "t" in value


def test_non_value_frame_returns_none() -> None:
    assert decode_value_frame(b"\x00\x57\x00garbage") is None  # 0x00 = topic spec, not a value
    assert decode_value_frame(b"") is None
    assert decode_value_frame(b"\x84no zlib here") is None
    assert decode_value_frame(b"\x23\x1c\x64") is None  # 0x23 = connect response


def test_synthetic_market_roundtrip() -> None:
    """Build a frame exactly as the server does (CBOR → zlib → 0x84 header)
    and confirm the decoder recovers the odds — exercises the pipeline on
    constructed data independent of the captured fixture."""
    value = {
        "id": "m-f-EVT-MW3W",
        "t": 27,
        "d": {
            "ei": "f-EVT",
            "mti": "MW3W",
            "odds": {
                "s-m-f-EVT-MW3W-home": {"of": {"1": "1.95", "2": "-105"}},
                "s-m-f-EVT-MW3W-draw": {"of": {"1": "3.40"}},
                "s-m-f-EVT-MW3W-away": {"of": {"1": "4.10"}},
            },
        },
    }
    frame = b"\x84\x0b\xb8\x00\x00" + zlib.compress(cbor2.dumps(value))
    decoded = decode_value_frame(frame)
    assert decoded is not None
    assert market_1x2_odds(decoded) == ("f-EVT", {"home": 1.95, "draw": 3.40, "away": 4.10})


def test_non_1x2_market_ignored() -> None:
    value = {"t": 27, "d": {"ei": "f-EVT", "mti": "MTG2W", "odds": {}}}
    assert market_1x2_odds(value) is None


def test_subscribe_encoder_reproduces_captured_frames() -> None:
    """The encoder must byte-for-byte match the real subscribe frames the
    browser sent (golden fixture from recon 2026-05-29)."""
    frames = [
        base64.b64decode(line)
        for line in (_FIXTURES / "betsson_diffusion_subscribe_frames.b64")
        .read_text()
        .splitlines()
        if line
    ]
    assert encode_subscribe_frame(1, FIXTURE_PHASE_SELECTOR) == frames[0]
    assert encode_subscribe_frame(2, markets_selector(_EVENT)) == frames[1]
    assert encode_subscribe_frame(3, events_selector(_EVENT)) == frames[2]


def test_non_market_message_ignored() -> None:
    value = {"t": 32, "d": {"messageType": 32}}
    assert market_1x2_odds(value) is None
