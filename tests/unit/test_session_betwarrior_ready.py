"""BetWarrior readiness = captured Kambi bearer present AND its JWT exp not lapsed.

This is what catches an inactivity logout (the SPA stops refreshing the token, so
the held bearer expires) WITHOUT any cross-origin probe — so the manager can suspend
placement + alert instead of silently believing BetWarrior is still live.
"""

from __future__ import annotations

import base64
import json
import time

from src.execution.session import InSessionTransport, _jwt_exp


def _jwt(exp: float | None) -> str:
    """A minimal JWT (header.payload.sig) carrying the given exp claim."""
    claims: dict[str, float] = {"exp": exp} if exp is not None else {}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


def test_jwt_exp_reads_claim() -> None:
    assert _jwt_exp(_jwt(1900000000.0)) == 1900000000.0


def test_jwt_exp_none_for_non_jwt_or_missing_claim() -> None:
    assert _jwt_exp("opaque-token") is None  # not a JWT
    assert _jwt_exp(_jwt(None)) is None  # JWT without exp
    assert _jwt_exp("a.!!!notbase64!!!.c") is None  # undecodable payload


def _transport_with_bearer(token: str, exp: float | None) -> InSessionTransport:
    t = InSessionTransport("betwarrior", dry_run=False)
    t._captured_bearer = token  # already captured (skip the page wait)
    t._bearer_exp = exp
    return t


async def test_ready_when_bearer_unexpired() -> None:
    exp = time.time() + 3600
    t = _transport_with_bearer(_jwt(exp), exp)
    assert await t.check_betwarrior_ready(timeout_s=1) is True


async def test_not_ready_when_bearer_expired() -> None:
    # Inactivity logout: held token's exp is in the past → not ready → manager alerts.
    exp = time.time() - 60
    t = _transport_with_bearer(_jwt(exp), exp)
    assert await t.check_betwarrior_ready(timeout_s=1) is False


async def test_not_ready_when_no_bearer() -> None:
    t = InSessionTransport("betwarrior", dry_run=False)  # never captured one
    assert await t.check_betwarrior_ready(timeout_s=1) is False


async def test_ready_falls_back_to_presence_when_exp_undecodable() -> None:
    # Opaque (non-JWT) bearer ⇒ no exp ⇒ presence-only, never worse than before.
    t = _transport_with_bearer("opaque-token", None)
    assert await t.check_betwarrior_ready(timeout_s=1) is True
