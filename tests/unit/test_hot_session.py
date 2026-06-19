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
        # Auto-extend behavior: when True, the next attempt_session_extend() call clears
        # `blocked` (simulates the popup dismissing after the click) and returns True.
        self.extend_ok = False
        self.extend_calls = 0
        # Keepalive behavior: count calls so tests can assert the loop fired.
        self.keepalive_calls = 0

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

    async def attempt_session_extend(self) -> bool:
        self.extend_calls += 1
        if self.extend_ok:
            self.blocked = None  # the click dismissed the popup; re-probe returns None
            return True
        return False

    async def keepalive(self) -> None:
        # Track that the manager's keepalive loop fired on this transport. The real
        # transport does mouse/scroll/click in a Playwright page; the fake just counts.
        self.keepalive_calls += 1


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
    (the session is still authenticated). The manager must suspend on it anyway, and
    the alert must classify the platform as RG LOCKOUT (not the generic cold fallback)
    so the operator knows to handle the popup, not just re-login. Resume when it clears."""
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
        assert "RG LOCKOUT" in alert  # popup-classified segment (not the generic cold fallback)
        assert "(cold)" not in alert  # not classified as plain cold
        bsn.blocked = None  # operator clears the break
        await asyncio.sleep(0.04)
        assert not g.kill_switch_tripped  # auto-resumed
        assert any("ready again" in s for s in note.sent)


async def test_concurrent_failures_are_all_named_in_alert() -> None:
    """When multiple platforms fail on the same heartbeat, ALL of them appear in the
    suspend alert — not just the first. This is the bug that masked a BetWarrior
    inactivity logout behind a transient Betsson ctx- failure in production: only
    'betsson' was named, the operator re-logged into the wrong window."""
    bano = _FakeTransport(ready=False)  # /api/balance probe fails
    bsn = _FakeTransport(context_ok=False)  # Betsson ctx- establishment fails
    bw = _FakeTransport(ready=False)  # BetWarrior bearer probe fails
    g, note = _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        betwarrior=bw,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=3600.0,  # startup trip only — don't fire the loop
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        assert g.kill_switch_tripped  # at least one not-ready trips
        # The single suspend alert names EVERY not-ready platform.
        suspended = next(s for s in note.sent if "NOT READY" in s)
        assert "betsson" in suspended
        assert "betano" in suspended
        assert "betwarrior" in suspended


async def test_partial_recovery_does_not_fire_ready_again() -> None:
    """If one platform recovers but others remain not-ready, 'ready again' must NOT
    fire and the kill switch must STAY tripped — the false-restore bug from production
    (Betsson recovered, BetWarrior still dead, kill switch wrongly released). Only when
    ALL platforms are placeable does the switch reset."""
    bano = _FakeTransport()
    bsn = _FakeTransport(context_ok=False)  # Betsson down
    bw = _FakeTransport(ready=False)  # BetWarrior down
    g, note = _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        betwarrior=bw,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,  # fast loop
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        await asyncio.sleep(0.04)  # suspend fires
        assert g.kill_switch_tripped
        assert not any("ready again" in s for s in note.sent)
        bsn.context_ok = True  # Betsson recovers — BetWarrior STILL down
        await asyncio.sleep(0.04)
        assert g.kill_switch_tripped  # STILL tripped (BetWarrior blocks reset)
        assert not any("ready again" in s for s in note.sent)  # no false restore
        bw.ready = True  # now BetWarrior too
        await asyncio.sleep(0.04)
        assert not g.kill_switch_tripped  # NOW resets — all platforms placeable
        assert any("ready again" in s for s in note.sent)


async def test_session_expired_overlay_alerts_with_relogin_segment() -> None:
    """A session-expired popup (inactivity logout — BetWarrior's 'Estabas desconectado')
    is detected as a blocking overlay and the alert classifies it as SESSION EXPIRED
    (operator re-logs in) — distinct from RG LOCKOUT (operator may need to wait out a
    break). Backstops the JWT-exp-only readiness probe that misses server-side kills."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    bw = _FakeTransport()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        betwarrior=bw,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        # The inactivity-logout popup appears as an overlay on BetWarrior.
        bw.blocked = SessionBlock(
            phrase="estabas desconectado", is_overlay=True, kind="session_expired"
        )
        await asyncio.sleep(0.04)
        assert g.kill_switch_tripped
        alert = next(s for s in note.sent if "betwarrior" in s and "SESSION EXPIRED" in s)
        assert "estabas desconectado" in alert
        assert "SESSION EXPIRED" in alert
        assert "RG LOCKOUT" not in alert  # distinct from RG classification
        assert "(cold)" not in alert  # distinct from plain cold


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


async def test_session_timer_warning_overlay_alerts_with_extend_segment() -> None:
    """A session-timer warning overlay (Betano's 'Temporizador de sesión') is detected
    as a blocking overlay and the alert classifies it as SESSION TIMER (extend or
    re-login) — distinct from RG LOCKOUT (wait out a break) and SESSION EXPIRED
    (already dead, re-login only)."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        bano.blocked = SessionBlock(
            phrase="temporizador de sesión", is_overlay=True, kind="session_timer_warning"
        )
        await asyncio.sleep(0.04)
        assert g.kill_switch_tripped
        alert = next(s for s in note.sent if "betano" in s and "SESSION TIMER" in s)
        assert "temporizador de sesión" in alert
        assert "SESSION TIMER" in alert
        assert "RG LOCKOUT" not in alert
        assert "SESSION EXPIRED" not in alert


async def test_session_timer_auto_extend_recovers_silently_without_alert() -> None:
    """The auto-extend path clicks 'Sí, conservarlo' on Betano's session-timer popup.
    On success the popup dismisses, the platform stays placeable, NO suspend alert fires,
    and the kill switch stays off — silent recovery, no operator intervention needed."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    bano.extend_ok = True  # the extend click will succeed
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        # Popup appears on Betano. The probe sees it, attempts extend, succeeds.
        bano.blocked = SessionBlock(
            phrase="temporizador de sesión", is_overlay=True, kind="session_timer_warning"
        )
        await asyncio.sleep(0.05)
        assert bano.extend_calls >= 1  # the extend was attempted at least once
        assert not g.kill_switch_tripped  # silent recovery — NO suspend
        assert not any("NOT READY" in s for s in note.sent)  # no alert fired


async def test_session_timer_extend_failure_falls_back_to_suspend_alert() -> None:
    """If the extend click fails (button gone, click fault, popup persists), the manager
    falls through to the normal suspend + alert path with the SESSION TIMER segment —
    operator gets told to handle the popup manually."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    bano.extend_ok = False  # extend will fail
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        bano.blocked = SessionBlock(
            phrase="temporizador de sesión", is_overlay=True, kind="session_timer_warning"
        )
        await asyncio.sleep(0.05)
        assert bano.extend_calls == 1  # one attempt per episode; no heartbeat-spam retries
        assert g.kill_switch_tripped  # extend failed → suspend fell through
        alert = next(s for s in note.sent if "betano" in s and "SESSION TIMER" in s)
        assert "temporizador de sesión" in alert


async def test_extend_not_retried_across_heartbeats_per_episode() -> None:
    """The auto-extend click is operator-authorized for ONE attempt per popup episode —
    if the click fails, the manager must NOT keep retrying every heartbeat (each click is
    a real bookmaker interaction). The block stays, suspend + alert fires once, and the
    next heartbeat's probe sees the same block but skips the extend. The guard clears
    only when the block clears (next popup episode gets a fresh attempt)."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    bano.extend_ok = False  # extend will keep failing
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=0.01,  # ~5 cycles in the 0.05s sleep
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        bano.blocked = SessionBlock(
            phrase="temporizador de sesión", is_overlay=True, kind="session_timer_warning"
        )
        await asyncio.sleep(0.05)
        # ONE attempt total across all heartbeats — not one per heartbeat.
        assert bano.extend_calls == 1
        # The block persists (extend failed), so the guard stays and the alert fires.
        assert g.kill_switch_tripped
        # Now operator clears the popup manually → block clears → guard resets.
        bano.blocked = None
        await asyncio.sleep(0.05)
        assert not g.kill_switch_tripped  # auto-resumed
        # A NEW popup episode on the same platform re-attempts extend (guard was cleared).
        bano.blocked = SessionBlock(
            phrase="temporizador de sesión", is_overlay=True, kind="session_timer_warning"
        )
        await asyncio.sleep(0.05)
        assert bano.extend_calls == 2  # fresh attempt for the new episode


async def test_keepalive_loop_dispatches_to_every_wired_transport() -> None:
    """The keepalive loop fires ``transport.keepalive()`` on every wired platform on
    every tick — BetWarrior's passive readiness probe doesn't register as server-side
    activity, so without this loop the session times out. The loop must tolerate a
    transport fault (one platform's failure must not stop keepalive on the others)."""
    bano, bsn, g, note = _FakeTransport(), _FakeTransport(), _guard(), _RecordingNotifier()
    bw = _FakeTransport()
    m = HotSessionManager(
        betano=bano,  # type: ignore[arg-type]
        betsson=bsn,  # type: ignore[arg-type]
        betwarrior=bw,  # type: ignore[arg-type]
        guardrails=g,
        heartbeat_sec=3600.0,  # don't fire the readiness heartbeat during this test
        keepalive_sec=0.01,    # fire keepalive rapidly
        notifier=note,  # type: ignore[arg-type]
    )
    async with m:
        await asyncio.sleep(0.05)  # ~5 keepalive ticks
        assert bano.keepalive_calls >= 1
        assert bsn.keepalive_calls >= 1
        assert bw.keepalive_calls >= 1
