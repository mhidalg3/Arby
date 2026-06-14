"""Tests for HotSessionManager lifecycle (fake transports)."""

from __future__ import annotations

import asyncio

from src.execution.guardrails import Guardrails
from src.execution.hot_session import HotSessionManager
from src.execution.session import SessionBlock


def _overlay(phrase: str) -> SessionBlock:
    return SessionBlock(phrase=phrase, is_overlay=True)  # a true blocking lockout


class _FakeTransport:
    def __init__(
        self, *, context_ok: bool = True, ready: bool = True, blocked: SessionBlock | None = None
    ) -> None:
        self.entered = False
        self.exited = False
        self.armed = False
        self.context_ok = context_ok  # Betsson readiness (establish context)
        self.ready = ready  # Betano / BetWarrior readiness probes
        self.blocked = blocked  # a SessionBlock (overlay/banner), or None when usable
        self.establish_calls = 0
        self.capture_calls: list[str] = []  # capture_block_evidence reasons

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

    async def check_betano_ready(self) -> bool:
        return self.ready

    async def check_betwarrior_ready(self) -> bool:
        return self.ready

    async def check_session_blocked(self) -> SessionBlock | None:
        return self.blocked

    async def capture_block_evidence(self, reason: str) -> str | None:
        self.capture_calls.append(reason)
        return f"fake/{reason}.json"


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
        assert any("NOT READY" in s for s in note.sent)
        bsn.context_ok = True  # operator re-logs in
        await asyncio.sleep(0.04)
        assert not g.kill_switch_tripped  # auto-resumed
        assert any("ready again" in s for s in note.sent)


async def test_status_heartbeat_reports_what_is_live() -> None:
    """A 'Bot started' status fires on startup and a periodic 'Bot alive' status names
    each platform's readiness — so silence never gets mistaken for a live bot."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=3600.0,
        status_interval_sec=0.01,  # fire fast for the test
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        assert any("Bot started" in s for s in note.sent)
        await asyncio.sleep(0.03)
        assert any("Bot alive" in s for s in note.sent)
        assert any("betano ✅" in s and "betsson ✅" in s for s in note.sent)


async def test_not_ready_platform_other_than_betsson_suspends_and_is_named() -> None:
    """Readiness now covers Betano + BetWarrior, not just Betsson — a not-ready Betano
    session suspends auto-placement at startup and the alert names the platform."""
    bano = _FakeTransport(ready=False)  # /api/balance probe fails
    bsn, g, note = _FakeTransport(), _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=3600.0,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        assert g.kill_switch_tripped  # suspended because Betano isn't placeable
        assert any("betano" in s and "NOT READY" in s for s in note.sent)


async def test_rg_lockout_suspends_with_popup_specific_alert_then_resumes() -> None:
    """An RG lockout overlay blocks the window while the readiness probe stays green
    (the session is still authenticated). The manager must suspend on it anyway, name
    the platform with a POPUP-specific message (not 'NOT READY'/re-login), and resume
    when it clears."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        bsn.blocked = _overlay("tomate un descanso")  # lockout overlay; context_ok stays True
        await asyncio.sleep(0.04)
        assert g.kill_switch_tripped  # suspended even though establish_betsson_context==True
        alert = next(s for s in note.sent if "betsson" in s and "LOCKOUT" in s)
        assert "tomate un descanso" in alert
        assert "NOT READY" not in alert  # popup-specific, not the cold-session message
        bsn.blocked = None  # operator clears the break
        await asyncio.sleep(0.04)
        assert not g.kill_switch_tripped  # auto-resumed
        assert any("ready again" in s for s in note.sent)


async def test_rg_banner_does_not_suspend_but_is_captured() -> None:
    """A non-blocking BANNER (phrase in page text, no overlay — Betano's confirmed case)
    must NOT suspend auto-placement, yet is still captured/logged so we never go blind."""
    bano, bsn, g = _FakeTransport(), _FakeTransport(), _guard()
    m = _mgr(bano, bsn, g)
    async with m:
        bano.blocked = SessionBlock("tomate un descanso", is_overlay=False)  # banner
        assert await m.heartbeat() is True  # banner ⇒ still placeable
        assert not g.kill_switch_tripped  # NOT suspended (the false-positive fix)
        assert m._readiness["betano"] is True
        assert bano.capture_calls == ["betano:tomate un descanso"]  # but evidence captured


async def test_rg_banner_escalating_to_overlay_is_recaptured() -> None:
    """Betano shows the non-blocking banner first; if it ESCALATES to a real overlay
    lockout, we must re-capture + re-log — without this the platform is already in
    _blocks from the banner and the exact event we exist to ground is silently skipped."""
    bano, bsn, g = _FakeTransport(), _FakeTransport(), _guard()
    m = _mgr(bano, bsn, g)
    async with m:
        bano.blocked = SessionBlock("tomate un descanso", is_overlay=False)  # banner first
        await m.heartbeat()
        assert bano.capture_calls == ["betano:tomate un descanso"]  # captured once
        await m.heartbeat()  # banner persists → NOT re-captured
        assert len(bano.capture_calls) == 1
        bano.blocked = SessionBlock("tomate un descanso", is_overlay=True)  # escalates to lockout
        await m.heartbeat()
        assert len(bano.capture_calls) == 2  # the real overlay IS captured
        assert m._readiness["betano"] is False  # and now not placeable
        await m.heartbeat()  # overlay persists → NOT re-captured
        assert len(bano.capture_calls) == 2


async def test_rg_lockout_logs_an_occurrence_for_trigger_learning() -> None:
    """Each new lockout is logged with platform + uptime so the trigger pattern is
    learnable. We assert the readiness state reflects the block (states[betano] False)
    even with the balance probe green."""
    bano, bsn, g = _FakeTransport(blocked=_overlay("12h de descanso")), _FakeTransport(), _guard()
    m = _mgr(bano, bsn, g)
    async with m:
        # heartbeat_sec is large; probe directly. Betano is "ready" but blocked → not placeable.
        assert await m.heartbeat() is False
        assert m._readiness["betano"] is False
        assert m._blocks["betano"].phrase == "12h de descanso"


async def test_rg_lockout_captures_dom_evidence_once_per_episode() -> None:
    """The first detection of a block dumps its DOM (to ground the selector from the real
    event); it is NOT re-captured every heartbeat while the block persists, but a fresh
    block after a recovery captures again."""
    bano, bsn, g = _FakeTransport(), _FakeTransport(), _guard()
    m = _mgr(bano, bsn, g)  # heartbeat_sec large → drive probes manually
    async with m:
        assert bsn.capture_calls == []  # clean startup, no block
        bsn.blocked = _overlay("12h de descanso")
        await m.heartbeat()  # first detection → capture
        await m.heartbeat()  # still blocked → no re-capture
        assert bsn.capture_calls == ["betsson:12h de descanso"]
        bsn.blocked = None
        await m.heartbeat()  # clears
        bsn.blocked = _overlay("12h de descanso")
        await m.heartbeat()  # re-block → capture again
        assert len(bsn.capture_calls) == 2


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
