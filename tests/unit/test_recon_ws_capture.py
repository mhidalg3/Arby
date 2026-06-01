"""Tests for the recon harness's WebSocket frame serialization.

Live/in-play odds on some sportsbooks (e.g. Betsson's OBG pub/sub) arrive
over WebSocket, which the HAR doesn't record. `_ws_frame_row` is the pure
serializer behind the capture; these pin its handling of text/binary/
oversized frames so the recon artifact stays readable and bounded.
"""

from __future__ import annotations

import base64

from scripts.recon.recon import _ws_frame_row


def test_text_frame_stored_verbatim() -> None:
    row = _ws_frame_row("wss://x/sb", "recv", '{"odds":3.95}')
    assert row == {
        "ws_url": "wss://x/sb",
        "dir": "recv",
        "binary": False,
        "payload": '{"odds":3.95}',
    }


def test_utf8_bytes_decoded_to_text() -> None:
    row = _ws_frame_row("wss://x/sb", "sent", '{"k":"é"}'.encode())
    assert row["binary"] is False
    assert row["payload"] == '{"k":"é"}'


def test_binary_frame_base64_encoded_and_flagged() -> None:
    raw = b"\x00\x01\x02\xff\xfe"
    row = _ws_frame_row("wss://x/sb", "recv", raw)
    assert row["binary"] is True
    assert base64.b64decode(str(row["payload"])) == raw


def test_oversized_payload_truncated() -> None:
    row = _ws_frame_row("wss://x/sb", "recv", "a" * 50, max_len=10)
    assert row["payload"] == "a" * 10
    assert row["truncated"] is True


def test_normal_payload_not_marked_truncated() -> None:
    row = _ws_frame_row("wss://x/sb", "recv", "short", max_len=10)
    assert "truncated" not in row
