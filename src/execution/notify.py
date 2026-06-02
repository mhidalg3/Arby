"""Telegram notifications for the execution pipeline.

Notifications are **non-critical**: a send failure must never break execution
(it's logged and swallowed). `TelegramNotifier` posts via the Bot API;
`NullNotifier` no-ops (used in dry-run / dev, or when Telegram isn't
configured). Secrets (bot token, chat id) live in the OS keychain — not in
code or env. Build the right notifier with `build_notifier()`.

Configure once, locally:

    python -c "import keyring; keyring.set_password('arby','telegram:bot_token','<token>')"
    python -c "import keyring; keyring.set_password('arby','telegram:chat_id','<chat_id>')"
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx
import keyring
import structlog

log = structlog.get_logger(__name__)

_KEYCHAIN_SERVICE = "arby"
_TOKEN_KEY = "telegram:bot_token"
_CHAT_KEY = "telegram:chat_id"
_SEND_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@runtime_checkable
class Notifier(Protocol):
    async def send(self, text: str) -> bool:
        """Send a notification. Returns True on delivery, False otherwise.
        Never raises — delivery is best-effort."""
        ...


class NullNotifier:
    """No-op notifier (logs only). Used when Telegram isn't configured."""

    async def send(self, text: str) -> bool:
        log.info("notify.skipped", text=text[:200])
        return False


class TelegramNotifier:
    """Sends messages to a Telegram chat via the Bot API. Fail-soft."""

    def __init__(self, http_client: httpx.AsyncClient, bot_token: str, chat_id: str) -> None:
        self._client = http_client
        self._token = bot_token
        self._chat_id = chat_id

    async def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            resp = await self._client.post(
                url, json={"chat_id": self._chat_id, "text": text}, timeout=_SEND_TIMEOUT
            )
        except httpx.HTTPError as exc:
            log.warning("notify.transport_error", error=str(exc))
            return False
        if resp.status_code != 200:
            log.warning("notify.bad_status", status=resp.status_code)
            return False
        return True


def telegram_credentials() -> tuple[str, str] | None:
    """(bot_token, chat_id) from the keychain, or None if not configured."""
    token = keyring.get_password(_KEYCHAIN_SERVICE, _TOKEN_KEY)
    chat_id = keyring.get_password(_KEYCHAIN_SERVICE, _CHAT_KEY)
    if token and chat_id:
        return token, chat_id
    return None


def build_notifier(http_client: httpx.AsyncClient) -> Notifier:
    """A `TelegramNotifier` if configured in the keychain, else a `NullNotifier`."""
    creds = telegram_credentials()
    if creds is None:
        log.info("notify.telegram_not_configured")
        return NullNotifier()
    return TelegramNotifier(http_client, *creds)
