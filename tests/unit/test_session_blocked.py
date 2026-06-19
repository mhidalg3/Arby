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

from src.execution.session import InSessionTransport


class _FakePage:
    """Stands in for the Playwright page: `evaluate` returns the {overlayText, bodyText}
    the scan JS would produce (or raises, to exercise the fail-open path). Both arrive
    lowercased from the page."""

    def __init__(self, *, body: str = "", overlay: str = "", raises: bool = False) -> None:
        self._body = body.lower()
        self._overlay = overlay.lower()
        self._raises = raises

    async def evaluate(self, expr: str, *args: Any) -> Any:
        if self._raises:
            raise RuntimeError("page detached")
        return {"overlayText": self._overlay, "bodyText": self._body}


def _transport(page: _FakePage | None) -> InSessionTransport:
    t = InSessionTransport("betano", dry_run=False)
    t._page = page  # type: ignore[assignment]
    return t


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
        "dialogs": [{"tag": "DIV", "cls": "rg-lockout modal", "html": "<div class='modal'>…</div>"}],
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
    for phrase in ["temporizador de sesión", "¿querés conservarlo?", "sí, conservarlo", "quiero desconectarme", "tu sesión está activa por"]:
        page = _FakePage(overlay=phrase.upper(), body="")  # case-insensitive
        block = await _transport(page).check_session_blocked()
        assert block is not None, f"phrase {phrase!r} should trigger detection"
        assert block.kind == "session_timer_warning"
        assert block.is_overlay is True



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
