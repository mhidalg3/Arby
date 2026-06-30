"""check_session_blocked: detect the responsible-gambling LOCKOUT overlay.

A PBA play-time limit replaces the betting UI with a mandatory-break notice while the
session stays authenticated — so the balance/ctx-/bearer readiness probes all keep
passing and this DOM scan is the only signal the window is actually unusable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import src.execution.session as session_mod
from src.execution.session import InSessionTransport


class _FakeMouse:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.moves: list[tuple[float, float, int | None]] = []
        self.clicks: list[tuple[float, float]] = []

    async def move(self, x: float, y: float, steps: int | None = None) -> None:
        self.moves.append((x, y, steps))

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        self._page.click_reality_check()


class _FakeLocator:
    """A no-match Playwright locator: ``count()``→0 so ``establish_betsson_context``'s
    in-app nav finds no link and skips on, mirroring a page where the nav anchor is absent."""

    async def count(self) -> int:
        return 0


class _FakePage:
    """Stands in for the Playwright page: `evaluate` returns the scan payloads the
    transport expects (or raises, to exercise fail-open paths). Text arrives lowercased
    from the real page scanner."""

    def __init__(
        self,
        *,
        body: str = "",
        overlay: str = "",
        shadow: dict[str, Any] | None = None,
        close_ok: bool = False,
        promo: bool = False,
        inicio_ok: bool = False,
        raises: bool = False,
        stale_removed: list[int] | None = None,
    ) -> None:
        self._body = body.lower()
        self._overlay = overlay.lower()
        self._shadow = shadow or {"found": False, "phrase": ""}
        self._close_ok = close_ok
        self._promo = promo
        self._inicio_ok = inicio_ok
        self._raises = raises
        # Sequence of {removed: N} the betslip-cleanup JS returns per call (popped in order).
        self._stale_removed = list(stale_removed) if stale_removed else []
        self.close_calls = 0
        self.close_target_js = ""
        self.mouse = _FakeMouse(self)
        self.inicio_calls = 0
        self.goto_calls = 0

    def click_reality_check(self) -> None:
        if self._shadow.get("found") is not True:
            return
        self.close_calls += 1
        if self._close_ok:
            self._shadow = {"found": False, "phrase": ""}

    def get_by_role(self, role: str, **kwargs: Any) -> _FakeLocator:
        return _FakeLocator()

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls += 1

    async def evaluate(self, expr: str, *args: Any) -> Any:
        if self._raises:
            raise RuntimeError("page detached")
        if "OBG-M-BETSLIP-REMOVE-SELECTION-BUTTON" in expr:
            return {"removed": self._stale_removed.pop(0) if self._stale_removed else 0}
        if "querySelectorAll('a,button" in expr:
            self.inicio_calls += 1
            if self._inicio_ok:
                self._promo = False
            return self._inicio_ok
        if "reality-check-btn-1" in expr:
            self.close_target_js = expr
            if self._shadow.get("found") is not True:
                return None
            return [12.0, 34.0]
        if "document.title" in expr:
            if self._promo:
                return {
                    "url": "https://pba.betwarrior.bet.ar/es-ar/promotions",
                    "title": "BetWarrior Province",
                    "bodyText": "PROMOCIONES Promociones Bonos APOSTAR AHORA",
                }
            return {
                "url": "https://pba.betwarrior.bet.ar/es-ar/sports/home",
                "title": "BetWarrior Province",
                "bodyText": "INICIO EN VIVO HOY",
            }
        if "¿sabés qué hora es?" in expr:
            return self._shadow
        return {"overlayText": self._overlay, "bodyText": self._body}


def _transport(page: _FakePage | None, platform: str = "betano") -> InSessionTransport:
    t = InSessionTransport(platform, dry_run=False)
    t._page = page  # type: ignore[assignment]
    return t


async def _noop_prepare(*_args: object, **_kwargs: object) -> None:
    return None


async def test_phrase_inside_an_overlay_is_a_lockout() -> None:
    # An RG phrase inside a visible modal = a true blocking lockout → suspend.
    page = _FakePage(overlay="su tiempo de juego — tomate un descanso", body="… tomate un descanso")
    block = await _transport(page).check_session_blocked()
    assert block is not None and block.is_overlay is True
    assert block.phrase == "tomate un descanso"


async def test_phrase_in_page_text_without_overlay_is_a_banner() -> None:
    # Betano's CONFIRMED non-blocking banner: phrase in the page body, no overlay → the
    # platform stays placeable (is_overlay False). This is the false-positive fix.
    page = _FakePage(body="boca river  tomate un descanso  12h de descanso de apostar y jugar")
    block = await _transport(page).check_session_blocked()
    assert block is not None and block.is_overlay is False
    assert block.phrase == "tomate un descanso"


async def test_not_blocked_on_a_usable_page() -> None:
    # A normal sportsbook page — even with a "juego responsable" footer link, which is
    # deliberately NOT in the lockout set — is not a block.
    page = _FakePage(body="boca river 2.10 empate 3.40 juego responsable ayuda")
    assert await _transport(page).check_session_blocked() is None


async def test_dry_run_is_never_blocked() -> None:
    t = InSessionTransport("betano", dry_run=True)  # no page open
    assert await t.check_session_blocked() is None


async def test_no_page_is_never_blocked() -> None:
    assert await _transport(None).check_session_blocked() is None


async def test_fails_open_when_the_read_raises() -> None:
    # A flaky DOM read must never crash the heartbeat — degrade to "not blocked" and
    # let the real readiness probes decide a genuinely broken page.
    assert await _transport(_FakePage(raises=True)).check_session_blocked() is None


class _FakeCapturePage:
    """Page whose `evaluate` returns the block-evidence object and whose `screenshot`
    records where it would write (no real file)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.shot_path: str | None = None

    async def evaluate(self, expr: str, *args: Any) -> Any:
        return self._data

    async def screenshot(self, path: str | None = None, full_page: bool = False) -> None:
        self.shot_path = path


async def test_capture_block_evidence_writes_overlay_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ground the real lockout DOM: the saved artifact carries the overlay markup so we can
    # pin the exact selector + distinguish a true overlay from a banner.
    monkeypatch.setattr("src.execution.session._BLOCK_EVIDENCE_DIR", tmp_path)
    data = {
        "url": "https://www.betano.bet.ar/",
        "text": "tomate un descanso 12h de descanso",
        "dialogs": [
            {"tag": "DIV", "cls": "rg-lockout modal", "html": "<div class='modal'>…</div>"}
        ],
    }
    t = InSessionTransport("betano", dry_run=False)
    t._page = _FakeCapturePage(data)  # type: ignore[assignment]
    out = await t.capture_block_evidence("betano:tomate un descanso")
    assert out is not None
    saved = json.loads(Path(out).read_text())  # noqa: ASYNC240 — tiny test artifact read
    assert saved["platform"] == "betano"
    assert saved["reason"] == "betano:tomate un descanso"
    assert saved["dialogs"][0]["cls"] == "rg-lockout modal"  # true overlay grounded


async def test_capture_block_evidence_none_in_dry_run() -> None:
    t = InSessionTransport("betano", dry_run=True)  # no page
    assert await t.capture_block_evidence("x") is None


async def test_session_expired_phrase_in_overlay_is_a_session_expired_lockout() -> None:
    # BetWarrior's inactivity-logout popup ('Estabas desconectado') in a visible modal =
    # a session_expired blocking overlay → suspend. Distinct kind from RG lockout so the
    # alert tells the operator to re-login (not wait out a break). Backstops the
    # JWT-exp-only readiness probe that misses server-side session kills.
    page = _FakePage(
        overlay="Estabas desconectado\nSu sesión se terminó por inactividad",
        body="…",
    )
    block = await _transport(page).check_session_blocked()
    assert block is not None
    assert block.is_overlay is True
    assert block.kind == "session_expired"
    assert block.phrase == "estabas desconectado"


async def test_betsson_inactivity_phrase_in_overlay_is_also_session_expired() -> None:
    # Betsson's variant wording ("sesión cerrada por falta de actividad") is in the same
    # phrase set — same platform-agnostic classification.
    page = _FakePage(overlay="Sesión cerrada por falta de actividad", body="")
    block = await _transport(page).check_session_blocked()
    assert block is not None
    assert block.is_overlay is True
    assert block.kind == "session_expired"


async def test_session_expired_phrase_in_body_only_is_a_non_blocking_banner() -> None:
    # Same overlay-vs-banner rule as RG: phrase in page text without an overlay = a
    # non-blocking banner (logged + captured, NOT a suspend). is_overlay False, kind kept.
    page = _FakePage(body="volver a iniciar sesión  boca river 2.10")
    block = await _transport(page).check_session_blocked()
    assert block is not None
    assert block.is_overlay is False
    assert block.kind == "session_expired"


async def test_default_kind_is_rg_lockout_for_backward_compat() -> None:
    # SessionBlock defaults kind to "rg_lockout" so existing callers / fixtures that
    # construct one without specifying kind (e.g. test_hot_session._overlay) keep the
    # historical classification.
    from src.execution.session import SessionBlock

    assert SessionBlock("x", is_overlay=True).kind == "rg_lockout"


async def test_session_timer_phrase_in_overlay_is_a_session_timer_warning() -> None:
    # Betano's "Temporizador de sesión" popup in a visible modal = a session_timer_warning
    # overlay → suspend (placement would fail behind it). Distinct kind so the alert
    # tells the operator to extend (not re-login, not wait out a break).
    page = _FakePage(
        overlay="Temporizador de sesión\n¿Querés conservarlo?\nSí, conservarlo",
        body="…",
    )
    block = await _transport(page).check_session_blocked()
    assert block is not None
    assert block.is_overlay is True
    assert block.kind == "session_timer_warning"
    assert block.phrase == "temporizador de sesión"


async def test_session_timer_phrase_in_body_only_is_a_non_blocking_banner() -> None:
    # Same overlay-vs-banner rule: session-timer phrase in page text without an overlay =
    # a non-blocking banner (logged + captured, NOT a suspend).
    page = _FakePage(body="boca river  temporizador de sesión  2.10")
    block = await _transport(page).check_session_blocked()
    assert block is not None
    assert block.is_overlay is False
    assert block.kind == "session_timer_warning"


async def test_betano_session_timer_phrases_cover_all_button_labels() -> None:
    # Defensive: each distinct phrase Betano's popup uses (title, CTA, both buttons)
    # triggers detection — so a future A/B test variant of the popup still catches.
    for phrase in [
        "temporizador de sesión",
        "¿querés conservarlo?",
        "sí, conservarlo",
        "quiero desconectarme",
        "tu sesión está activa por",
    ]:
        page = _FakePage(overlay=phrase.upper(), body="")  # case-insensitive
        block = await _transport(page).check_session_blocked()
        assert block is not None
        assert block.is_overlay is True
        assert block.kind == "session_timer_warning"


async def test_betsson_reality_check_shadow_popup_is_detected() -> None:
    # Betsson renders the reality-check modal in open shadow DOM; the light-DOM block
    # scan is empty, but the Betsson-specific shadow scan marks it as a closeable overlay.
    page = _FakePage(
        shadow={
            "found": True,
            "phrase": "¿sabés qué hora es?",
        }
    )
    block = await _transport(page, platform="betsson").check_session_blocked()
    assert block is not None
    assert block.is_overlay is True
    assert block.kind == "reality_check"
    assert block.phrase == "¿sabés qué hora es?"


async def test_betsson_reality_check_close_clicks_grounded_fds_cerrar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live close control is a custom element:
    # <fds-button data-test-id="reality-check-btn-1">Cerrar</fds-button>. The close path
    # must find that grounded target and use a real pointer click, not synthetic .click().
    monkeypatch.setattr(session_mod, "_BETSSON_REALITY_CHECK_CLOSE_COOLDOWN_S", 0.0)
    page = _FakePage(
        shadow={
            "found": True,
            "phrase": "el juego compulsivo es perjudicial para vos y tu familia",
        },
        close_ok=True,
    )
    closed = await _transport(page, platform="betsson").attempt_reality_check_close()
    assert closed is True
    assert "fds-button" in page.close_target_js
    assert "reality-check-btn-1" in page.close_target_js
    assert "tag === 'button'" not in page.close_target_js
    assert "getAttribute('role')" not in page.close_target_js
    assert page.mouse.moves == [(12.0, 34.0, 4)]
    assert page.mouse.clicks == [(12.0, 34.0)]
    assert page.close_calls == 1


async def test_reality_check_close_requires_grounded_phrase() -> None:
    # A generic/class-only marker is not enough to click a live bookmaker control.
    # Without the grounded phrase scan, the safe behavior is no click and fallback alert.
    page = _FakePage(shadow={"found": False, "phrase": ""}, close_ok=True)
    assert await _transport(page, platform="betsson").attempt_reality_check_close() is False
    assert page.close_calls == 0


async def test_establish_skips_goto_when_reality_check_up() -> None:
    # A page reload cannot dismiss Betsson's reality-check popup (only the Cerrar button
    # does), so establish must NOT goto while the overlay is up — otherwise the heartbeat
    # refreshes every cycle, fighting the close recovery and re-triggering the SPA. It
    # returns not-ready; the manager detects the block separately and clicks Cerrar.
    page = _FakePage(shadow={"found": True, "phrase": "¿sabés qué hora es?"})
    result = await _transport(page, platform="betsson").establish_betsson_context()
    assert result is False
    assert page.goto_calls == 0


async def test_establish_gotos_normally_when_no_reality_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contrast: without a blocking overlay, establish reloads the SPA to re-establish
    # context. The goto skip is reality-check-specific, not a universal short-circuit.
    page = _FakePage(body="boca river 2.10")
    t = _transport(page, platform="betsson")
    monkeypatch.setattr(t, "prepare_betsson_context", _noop_prepare)
    result = await t.establish_betsson_context()
    assert result is False  # ctx unresolved (stubbed), but the goto ran
    assert page.goto_calls >= 1


async def test_betwarrior_promotions_page_is_blocking() -> None:
    page = _FakePage(promo=True)
    block = await _transport(page, platform="betwarrior").check_session_blocked()
    assert block is not None
    assert block.is_overlay is True
    assert block.kind == "promotions_page"
    assert block.phrase == "promociones"


async def test_betwarrior_promotions_home_clicks_inicio() -> None:
    page = _FakePage(promo=True, inicio_ok=True)
    returned = await _transport(page, platform="betwarrior").attempt_betwarrior_promotions_home()
    assert returned is True
    assert page.inicio_calls == 1


async def test_betwarrior_promotions_home_is_betwarrior_only() -> None:
    page = _FakePage(promo=True, inicio_ok=True)
    assert await _transport(page, platform="betsson").attempt_betwarrior_promotions_home() is False
    assert page.inicio_calls == 0


async def test_reality_check_close_waits_through_cerrar_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Betsson's Cerrar is visible before it is usable; wait through the observed cooldown
    # before the single real click, rather than burning the one-attempt guard early.
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(session_mod.asyncio, "sleep", fake_sleep)
    page = _FakePage(
        shadow={
            "found": True,
            "phrase": "¿sabés qué hora es?",
        },
        close_ok=True,
    )
    closed = await _transport(page, platform="betsson").attempt_reality_check_close()
    assert closed is True
    assert page.close_calls == 1
    assert sleeps[0] >= 5.0


async def test_reality_check_close_is_betsson_only() -> None:
    page = _FakePage(shadow={"found": True, "phrase": "¿sabés qué hora es?"}, close_ok=True)
    assert await _transport(page, platform="betano").attempt_reality_check_close() is False
    assert page.close_calls == 0


def test_dialog_selector_includes_known_platform_modal_ids() -> None:
    """Regression guard for the selector additions that grounded Betano + BetWarrior
    popup detection from production captures. The standard selector pattern
    (``[role=dialog],[aria-modal=true],.modal,.overlay,.modal-overlay``) misses both
    platforms' popups — Betano uses ``.modal-container`` / ``#session-timer``, BetWarrior
    uses ``#sg-modal-backdrop`` / ``#sg-modal-wrapper``. If a future refactor drops one of
    these IDs, the corresponding popup detection silently downgrades to banner (no suspend,
    no alert) — the exact bug that masked BetWarrior's inactivity logout in production.

    Unit tests can't exercise the JS scan against real DOM, so this string-containment
    check is the cheapest way to lock the IDs in."""
    from src.execution.session import _RG_BLOCK_DIALOG_SELECTOR

    # Betano session-timer (captured 2026-06-19).
    assert "#session-timer" in _RG_BLOCK_DIALOG_SELECTOR
    assert ".modal-container" in _RG_BLOCK_DIALOG_SELECTOR
    # BetWarrior inactivity-logout (captured 2026-06-19).
    assert "#sg-modal-backdrop" in _RG_BLOCK_DIALOG_SELECTOR
    assert "#sg-modal-wrapper" in _RG_BLOCK_DIALOG_SELECTOR


# ---- Betsson stale-betslip cleanup (clear_stale_betslip) ----


async def test_clear_stale_betslip_removes_until_empty() -> None:
    # Two unavailable selections → the JS reports removed=1 twice then 0; method returns 2.
    page = _FakePage(stale_removed=[1, 1, 0])
    removed = await _transport(page, platform="betsson").clear_stale_betslip()
    assert removed == 2


async def test_clear_stale_betslip_caps_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pathological slip that keeps reporting a removable selection must not loop forever.
    monkeypatch.setattr(session_mod, "_BETSSON_STALE_SLIP_MAX", 3)
    page = _FakePage(stale_removed=[1] * 20)  # always something removable
    removed = await _transport(page, platform="betsson").clear_stale_betslip()
    assert removed == 3  # capped, not 20


async def test_clear_stale_betslip_fail_soft_on_page_error() -> None:
    # A detached page (evaluate raises) must never crash the heartbeat — returns 0.
    page = _FakePage(raises=True)
    removed = await _transport(page, platform="betsson").clear_stale_betslip()
    assert removed == 0


async def test_clear_stale_betslip_guards_non_betsson_and_dry_run() -> None:
    # Only the Betsson transport clears; other platforms + dry-run are no-ops.
    page = _FakePage(stale_removed=[1, 1])
    assert await _transport(page, platform="betano").clear_stale_betslip() == 0
    dry = InSessionTransport("betsson", dry_run=True)
    dry._page = page  # type: ignore[assignment]
    assert await dry.clear_stale_betslip() == 0


async def test_clear_stale_betslip_noop_on_clean_slip() -> None:
    # A slip with no stale selections (JS reports 0 immediately) → 0, no clicks.
    page = _FakePage(stale_removed=[0])
    assert await _transport(page, platform="betsson").clear_stale_betslip() == 0
