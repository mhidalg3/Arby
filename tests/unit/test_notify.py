"""Tests for the execution Telegram notifier."""

from __future__ import annotations

import httpx
import pytest

from src.execution import notify as notify_mod
from src.execution.notify import NullNotifier, TelegramNotifier, build_notifier


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_telegram_send_posts_to_bot_api() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        import json

        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as c:
        ok = await TelegramNotifier(c, "TOK", "CHAT").send("hello")
    assert ok is True
    assert seen["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert seen["body"] == {"chat_id": "CHAT", "text": "hello"}


async def test_telegram_send_false_on_bad_status() -> None:
    async with _client(lambda r: httpx.Response(500)) as c:
        assert await TelegramNotifier(c, "TOK", "CHAT").send("x") is False


async def test_telegram_send_never_raises_on_transport_error() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _client(boom) as c:
        assert await TelegramNotifier(c, "TOK", "CHAT").send("x") is False


async def test_null_notifier_no_network() -> None:
    assert await NullNotifier().send("anything") is False


async def test_build_notifier_returns_telegram_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify_mod, "telegram_credentials", lambda: ("T", "C"))
    async with _client(lambda r: httpx.Response(200)) as c:
        assert isinstance(build_notifier(c), TelegramNotifier)


async def test_build_notifier_falls_back_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify_mod, "telegram_credentials", lambda: None)
    async with _client(lambda r: httpx.Response(200)) as c:
        assert isinstance(build_notifier(c), NullNotifier)
