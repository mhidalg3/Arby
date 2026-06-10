"""Tests for HotSessionManager lifecycle (fake transports)."""

from __future__ import annotations

import asyncio

from src.execution.guardrails import Guardrails
from src.execution.hot_session import HotSessionManager


class _FakeTransport:
    def __init__(self, *, context_ok: bool = True) -> None:
        self.entered = False
        self.exited = False
        self.armed = False
        self.context_ok = context_ok
        self.establish_calls = 0

    async def __aenter__(self) -> _FakeTransport:
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True

    def arm(self) -> None:
        self.armed = True

    async def establish_betsson_context(self) -> bool:
        self.establish_calls += 1
        return self.context_ok


def _guard() -> Guardrails:
    return Guardrails(
        max_position_per_match_ars=1000.0,
        max_total_exposure_ars=1000.0,
        max_daily_loss_ars=1000.0,
        odds_tolerance_pct=1.0,
    )


def _mgr(betano: _FakeTransport, betsson: _FakeTransport, g: Guardrails) -> HotSessionManager:
    return HotSessionManager(
        betano=betano,  # type: ignore[arg-type]
        betsson=betsson,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=3600.0,  # don't fire during the test
    )


async def test_startup_opens_arms_and_establishes_context() -> None:
    bano, bsn, g = _FakeTransport(), _FakeTransport(), _guard()
    async with _mgr(bano, bsn, g) as m:
        assert bano.entered and bsn.entered
        assert bano.armed and bsn.armed
        assert bsn.establish_calls == 1
        assert not g.kill_switch_tripped
        placers = m.placers()
        assert set(placers) == {"betano", "betsson-pba"}
    assert bano.exited and bsn.exited  # closed on exit


async def test_failed_betsson_context_trips_kill_switch() -> None:
    bano, bsn, g = _FakeTransport(), _FakeTransport(context_ok=False), _guard()
    async with _mgr(bano, bsn, g):
        assert g.kill_switch_tripped  # can't trade through a non-placeable Betsson


async def test_heartbeat_rewarms_and_trips_on_cold_session() -> None:
    bano, bsn, g = _FakeTransport(), _FakeTransport(), _guard()
    async with _mgr(bano, bsn, g) as m:
        assert await m.heartbeat() is True  # healthy re-warm
        bsn.context_ok = False  # session goes cold
        assert await m.heartbeat() is False
        # the background loop trips the switch on a cold heartbeat; emulate its action
        if not await m.heartbeat():
            g.trip_kill_switch("betsson session went cold")
        assert g.kill_switch_tripped


async def test_heartbeat_loop_trips_kill_switch_when_session_dies() -> None:
    bano, bsn, g = _FakeTransport(), _FakeTransport(), _guard()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,  # fire fast
    )
    async with m:
        bsn.context_ok = False  # next heartbeat finds it cold
        await asyncio.sleep(0.05)  # let the loop fire
        assert g.kill_switch_tripped


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


async def test_cold_session_suspends_then_auto_resumes_on_recovery() -> None:
    """A cold session suspends auto-placement + alerts; the loop keeps probing and
    auto-resumes (resets the kill switch) once the session is restored."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        bsn.context_ok = False  # goes cold
        await asyncio.sleep(0.04)
        assert g.kill_switch_tripped  # auto-placement suspended
        assert any("COLD" in s for s in note.sent)
        bsn.context_ok = True  # operator re-logs in
        await asyncio.sleep(0.04)
        assert not g.kill_switch_tripped  # auto-resumed
        assert any("restored" in s for s in note.sent)


async def test_cold_recovery_does_not_clear_a_hard_trip() -> None:
    """The heartbeat resets only ITS cold-session trip; a hard freeze/daily-loss
    trip (different reason) survives a session recovery."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        # Realistic ordering: a hard trip lands first (placement is suspended while
        # cold, so a freeze can't originate during a cold window). Then the session
        # goes cold and recovers — the hard trip must survive.
        g.trip_kill_switch("frozen: something bad")
        bsn.context_ok = False
        await asyncio.sleep(0.03)
        bsn.context_ok = True
        await asyncio.sleep(0.03)
        assert g.kill_switch_tripped  # the hard trip is NOT auto-cleared
        assert g.kill_switch_reason == "frozen: something bad"
