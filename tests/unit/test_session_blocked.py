"""check_session_blocked: detect the responsible-gambling LOCKOUT overlay.

A PBA play-time limit replaces the betting UI with a mandatory-break notice while the
session stays authenticated — so the balance/ctx-/bearer readiness probes all keep
passing and this DOM scan is the only signal the window is actually unusable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

import src.execution.session as session_mod
from src.execution.session import InSessionTransport


class _FakeKeyboard:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.presses: list[str] = []
        self.typed: list[str] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)

    async def type(self, text: str, delay: int | None = None) -> None:
        self.typed.append(text)


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
    """Minimal Playwright locator fake.

    ``count()`` defaults to 0 so ``establish_betsson_context``'s in-app nav finds no
    link and skips on. ``click()`` records bounded locator clicks for Betsson opener
    tests.
    """

    def __init__(
        self, page: _FakePage | None = None, selector: str = "", *, raises: bool = False
    ) -> None:
        self._page = page
        self._selector = selector
        self._raises = raises

    async def count(self) -> int:
        return 0

    async def click(self, **kwargs: Any) -> None:
        if self._page is not None:
            self._page.locator_clicks.append((self._selector, kwargs.get("timeout")))
        if self._raises:
            raise RuntimeError("actionability timeout")


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
        close_target: dict[str, Any] | None = None,
        promo: bool = False,
        inicio_ok: bool = False,
        raises: bool = False,
        stale_removed: list[int] | None = None,
        auth_payload: dict[str, Any] | None = None,
        auth_payloads: list[dict[str, Any]] | None = None,
        auth_raises: bool = False,
        visible_centers: dict[str, list[float]] | None = None,
        visible_center_sequences: dict[str, list[list[float] | None]] | None = None,
        trigger_center: list[float] | None = None,
        trigger_center_sequences: list[list[float] | None] | None = None,
        locator_raises: set[str] | None = None,
        visible_center_min_limits: dict[str, int] | None = None,
        visible_center_options: dict[str, list[list[float]]] | None = None,
    ) -> None:
        self._body = body.lower()
        self._overlay = overlay.lower()
        self._shadow = shadow or {"found": False, "phrase": ""}
        self._close_ok = close_ok
        self._close_target = close_target
        self._promo = promo
        self._inicio_ok = inicio_ok
        self._raises = raises
        # Betsson auth-scan payload (open shadow DOM header/popup truth). Default
        # loggedIn=True so existing Betsson reality-check/establish tests fall through
        # unchanged unless a test opts into a logged-out / expired state. auth_raises
        # isolates the auth-scan fail-open path (raises=True would fail the block scan first).
        self._auth_payload = auth_payload or {
            "loggedIn": True,
            "hasBalance": True,
            "loggedOut": False,
            "expiredPhrase": "",
        }
        self._auth_raises = auth_raises
        # Optional SEQUENCE of auth-scan payloads (popped per evaluate; the last one
        # repeats) — models a hydrating header that settles, for the debounce tests.
        self._auth_payloads = list(auth_payloads) if auth_payloads else []
        self.auth_scan_calls = 0
        # Sequence of {removed: N} the betslip-cleanup JS returns per call (popped in order).
        self._stale_removed = list(stale_removed) if stale_removed else []
        self._visible_centers = visible_centers or {}
        self._visible_center_sequences = {
            k: list(v) for k, v in (visible_center_sequences or {}).items()
        }
        self._trigger_center = trigger_center
        self._trigger_center_sequences = (
            list(trigger_center_sequences) if trigger_center_sequences else []
        )
        self._visible_center_min_limits = visible_center_min_limits or {}
        self._visible_center_options = visible_center_options or {}
        self._locator_raises = locator_raises or set()
        self.close_calls = 0
        self.close_target_js = ""
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard(self)
        self.inicio_calls = 0
        self.goto_calls = 0
        self.reload_calls = 0
        self.locator_clicks: list[tuple[str, Any]] = []

    def click_reality_check(self) -> None:
        if self._shadow.get("found") is not True:
            return
        self.close_calls += 1
        if self._close_ok:
            self._shadow = {"found": False, "phrase": ""}

    def get_by_role(self, role: str, **kwargs: Any) -> _FakeLocator:
        return _FakeLocator()

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector, raises=selector in self._locator_raises)

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls += 1

    async def reload(self, **kwargs: Any) -> None:
        self.reload_calls += 1

    def _visible_center_for(
        self, sel: Any, limit: int, *, bottom: bool = False
    ) -> list[float] | None:
        if not isinstance(sel, str) or not sel:
            return None
        min_limit = self._visible_center_min_limits.get(sel)
        if min_limit is not None and limit < min_limit:
            return None
        if sel in self._visible_center_sequences:
            seq = self._visible_center_sequences[sel]
            if len(seq) > 1:
                return seq.pop(0)
            return seq[0]
        options = self._visible_center_options.get(sel)
        if options:
            if bottom:
                return max(options, key=lambda xy: xy[1])
            return options[0]
        return self._visible_centers.get(sel)

    async def evaluate(self, expr: str, *args: Any) -> Any:
        if self._raises:
            raise RuntimeError("page detached")
        if "loggedOut" in expr:  # _BETSSON_AUTH_SCAN_JS — unique return-key literal
            if self._auth_raises:
                raise RuntimeError("auth scan detached")
            self.auth_scan_calls += 1
            if self._auth_payloads:
                if len(self._auth_payloads) > 1:
                    return self._auth_payloads.pop(0)
                return self._auth_payloads[0]
            return self._auth_payload
        if "OBG-M-BETSLIP-REMOVE-SELECTION-BUTTON" in expr:
            return {"removed": self._stale_removed.pop(0) if self._stale_removed else 0}
        if "querySelectorAll('a,button" in expr:
            self.inicio_calls += 1
            if self._inicio_ok:
                self._promo = False
            return self._inicio_ok
        if "BETSSON_REALITY_CHECK_CLOSE" in expr:
            self.close_target_js = expr
            if self._close_target is not None:
                return self._close_target
            if self._shadow.get("found") is not True:
                return {"found": False, "reason": "no_phrase", "candidates": []}
            return {
                "found": True,
                "x": 12.0,
                "y": 34.0,
                "tier": 1,
                "tag": "fds-button",
                "testId": "reality-check-btn-1",
                "label": "cerrar",
            }
        if "BETSSON_PASSWORD_TRACE" in expr:
            cfg = args[0] if args and isinstance(args[0], dict) else {}
            return {
                "action": cfg.get("action", "snapshot"),
                "activePath": [
                    {
                        "tag": "input",
                        "testId": "password-input",
                        "type": "password",
                        "valuePresent": True,
                        "selectionPresent": True,
                    }
                ],
                "hitPath": [{"tag": "fds-input", "testId": "input-container"}],
                "eventSeen": {"keydown": bool(self.keyboard.typed)},
                "events": [{"type": "keydown", "keyKind": "printable", "dataPresent": False}],
            }
        if "BETSSON_RELOGIN_EVIDENCE" in expr:
            selectors = args[0] if args and isinstance(args[0], dict) else {}
            limit = max((int(x) for x in re.findall(r"visited > (\d+)", expr)), default=0)
            found: dict[str, Any] = {}
            for name, spec in selectors.items():
                fallback = False
                if isinstance(spec, dict):
                    center = self._visible_center_for(spec.get("primary"), limit)
                    if center is None:
                        center = self._visible_center_for(spec.get("fallback"), limit, bottom=True)
                        fallback = center is not None
                else:
                    center = self._visible_center_for(spec, limit)
                if center is None:
                    found[str(name)] = None
                else:
                    found[str(name)] = {"visible": True, "center": center, "fallback": fallback}
            return {
                "url": "https://pba.betsson.bet.ar/inicio",
                "title": "Betsson",
                "selectors": found,
            }
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
        if "BETSSON_LOGIN_TRIGGER_FINDER" in expr:
            if self._trigger_center_sequences:
                return self._trigger_center_sequences.pop(0)
            return self._trigger_center
        if "BETSSON_CLICKABLE_CENTER" in expr:
            sel = args[0] if args else ""
            limit = max((int(x) for x in re.findall(r"visited > (\d+)", expr)), default=0)
            return self._visible_center_for(sel, limit)
        if "el.matches(sel)" in expr:
            sel = args[0] if args else ""
            limit = max((int(x) for x in re.findall(r"visited > (\d+)", expr)), default=0)
            return self._visible_center_for(sel, limit, bottom="best = null" in expr)
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


class _FakeReloginEvidencePage:
    """Page that exposes separate auth and selector snapshots for relogin evidence."""

    url = "https://pba.betsson.bet.ar/apuestas-deportivas"

    def __init__(self) -> None:
        self.shot_paths: list[str] = []

    async def evaluate(self, expr: str, *args: Any) -> Any:
        if "loggedOut" in expr:
            return {
                "loggedIn": False,
                "hasBalance": False,
                "loggedOut": True,
                "expiredPhrase": "",
                "unexpected": "do-not-persist",
            }
        if "BETSSON_RELOGIN_EVIDENCE" in expr:
            return {
                "url": self.url,
                "title": "Betsson",
                "selectors": {
                    "email": {
                        "visible": True,
                        "tag": "input",
                        "text": "operator@example.com",
                        "value": "operator@example.com",
                        "html": "<input value='operator@example.com'>",
                        "matchCount": 1,
                        "pointTarget": {
                            "tag": "input",
                            "testId": "email-input",
                            "role": "textbox",
                            "aria": "Email",
                            "point": [10, 20],
                            "text": "operator@example.com",
                            "value": "operator@example.com",
                            "html": "<input value='operator@example.com'>",
                        },
                    },
                    "password": {
                        "visible": True,
                        "tag": "input",
                        "type": "password",
                        "text": "secret",
                        "value": "secret",
                        "matchCount": 1,
                        "descendantInput": True,
                        "container": {
                            "visible": True,
                            "center": [10, 25],
                            "tag": "fds-input",
                            "testId": "input-container",
                            "text": "operator@example.com",
                            "value": "operator@example.com",
                            "html": "<fds-input>operator@example.com</fds-input>",
                            "pointTarget": {
                                "tag": "fds-input",
                                "testId": "input-container",
                                "role": "",
                                "aria": "",
                                "point": [10, 25],
                                "value": "operator@example.com",
                            },
                        },
                    },
                    "submit": {
                        "visible": True,
                        "tag": "fds-button",
                        "testId": "account-login-btn-1",
                        "text": "Iniciar sesión",
                        "matchCount": 1,
                    },
                    "balance": {
                        "visible": True,
                        "text": "ARS 12345",
                    },
                },
            }
        return {}

    async def screenshot(self, path: str | None = None, full_page: bool = False) -> None:
        if path is not None:
            self.shot_paths.append(path)


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


async def test_betsson_relogin_evidence_sanitizes_selector_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "_BETSSON_RELOGIN_EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(session_mod.time, "time_ns", lambda: 123456789)
    page = _FakeReloginEvidencePage()
    t = InSessionTransport("betsson", dry_run=False)
    t._page = page  # type: ignore[assignment]
    t._betsson_password_trace = [  # noqa: SLF001
        {
            "stage": "after_password_type",
            "eventSeen": {"keydown": True},
            "activePath": [
                {
                    "tag": "input",
                    "testId": "password-input",
                    "type": "password",
                    "valuePresent": True,
                    "selectionPresent": True,
                }
            ],
            "events": [{"type": "keydown", "keyKind": "printable", "dataPresent": False}],
        }
    ]

    out = await t._capture_betsson_relogin_evidence("open_miss_before_reload")  # noqa: SLF001

    assert out is not None
    assert out == str(tmp_path / "betsson_open_miss_before_reload_123456789.json")
    saved = json.loads(Path(out).read_text())  # noqa: ASYNC240
    assert saved["stage"] == "open_miss_before_reload"
    assert saved["auth"] == {
        "loggedIn": False,
        "hasBalance": False,
        "loggedOut": True,
        "expiredPhrase": "",
    }
    assert "balance" not in saved["selectors"]
    assert saved["selectors"]["email"]["text"] == ""
    assert saved["selectors"]["password"]["text"] == ""
    assert saved["selectors"]["password"]["descendantInput"] is True
    assert saved["selectors"]["password"]["container"] == {
        "visible": True,
        "center": [10, 25],
        "tag": "fds-input",
        "testId": "input-container",
        "pointTarget": {
            "tag": "fds-input",
            "testId": "input-container",
            "role": "",
            "aria": "",
            "point": [10, 25],
        },
    }
    assert "value" not in saved["selectors"]["email"]
    assert "html" not in saved["selectors"]["email"]
    assert saved["selectors"]["email"]["pointTarget"] == {
        "tag": "input",
        "testId": "email-input",
        "role": "textbox",
        "aria": "Email",
        "point": [10, 20],
    }
    assert saved["selectors"]["submit"]["text"] == "Iniciar sesión"
    assert saved["screenshot"] == str(tmp_path / "betsson_open_miss_before_reload_123456789.png")
    assert page.shot_paths == [saved["screenshot"]]
    persisted_trace = saved["passwordTrace"][0]
    assert persisted_trace["activePath"][0]["selectionPresent"] is True
    assert persisted_trace["events"][0]["keyKind"] == "printable"
    assert "key" not in persisted_trace["events"][0]
    assert "valueLength" not in persisted_trace["activePath"][0]
    assert "selectionStart" not in persisted_trace["activePath"][0]
    assert "selectionEnd" not in persisted_trace["activePath"][0]


async def test_betsson_relogin_evidence_skips_screenshot_after_typing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "_BETSSON_RELOGIN_EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(session_mod.time, "time_ns", lambda: 223456789)
    page = _FakeReloginEvidencePage()
    t = InSessionTransport("betsson", dry_run=False)
    t._page = page  # type: ignore[assignment]

    out = await t._capture_betsson_relogin_evidence(  # noqa: SLF001
        "form_incomplete_submit", missing="submit"
    )
    assert out is not None

    saved = json.loads(Path(out).read_text())  # noqa: ASYNC240
    assert saved["missing"] == "submit"
    assert saved["screenshot"] is None
    assert page.shot_paths == []


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


async def test_betsson_reality_check_close_clicks_tiered_cerrar(
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
    assert "startsWith('reality-check')" in page.close_target_js  # prefix, not exact testId
    assert "fdsp-button" in page.close_target_js  # tag drift tolerated
    assert "sesión" in page.close_target_js  # logout guard present
    assert (
        "cerrarSafe.length === 1" in page.close_target_js
    )  # tier 1: single cerrar, no first-match
    assert (
        "cerrarSafe.concat(cerrarHosts)" in page.close_target_js
    )  # tier 3: page-wide cerrar (prefix+non-prefix counted together)
    assert page.mouse.moves == [(12.0, 34.0, 4)]
    assert page.mouse.clicks == [(12.0, 34.0)]
    assert page.close_calls == 1


async def test_reality_check_target_missing_returns_false_and_captures_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Close-target finder returns no_candidate: the close must NOT click, must capture
    # failure evidence (the one-episode diagnosability this fix promises), and report the
    # exact stage so the next DOM drift is pin-downable from the log line alone.
    page = _FakePage(
        shadow={"found": True, "phrase": "¿sabés qué hora es?"},
        close_target={
            "found": False,
            "reason": "no_candidate",
            "candidates": [{"tag": "fds-button", "testId": "reality-check-x", "label": ""}],
        },
    )
    t = _transport(page, platform="betsson")
    stages: list[str] = []

    async def _cap(stage: str, **_kw: object) -> str | None:
        stages.append(stage)
        return "recon/artifacts/betsson_relogin/fake.png"

    monkeypatch.setattr(t, "_capture_betsson_relogin_evidence", _cap)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(session_mod.asyncio, "sleep", fake_sleep)
    assert await t.attempt_reality_check_close() is False
    assert page.close_calls == 0
    assert stages == ["reality_check_target_missing"]


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


async def test_betsson_logged_out_header_is_session_expired_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The header auth scan (open shadow DOM) sees no balance/Retirar-Depósito but DOES
    # see the login trigger → a blocking session_expired overlay that schedules auto-
    # reauth. The light-DOM block scan is blind to Betsson's header. PERSISTENT across
    # the settle window (the payload repeats) — a transient read must not block.
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": True,
            "expiredPhrase": "",
        }
    )
    block = await _transport(page, platform="betsson").check_session_blocked()
    assert block is not None
    assert block.kind == "session_expired"
    assert block.is_overlay is True
    assert "header logged out" in block.phrase


async def test_betsson_shadow_expired_phrase_is_session_expired_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A session-expired popup rendered in open shadow DOM (the light-DOM phrase scan
    # can't see it) is caught by the Betsson auth scan's shadow walk → same overlay.
    # Persistent across the settle window; a transient phrase must not block.
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": False,
            "expiredPhrase": "sesión cerrada por falta de actividad",
        }
    )
    block = await _transport(page, platform="betsson").check_session_blocked()
    assert block is not None
    assert block.kind == "session_expired"
    assert block.is_overlay is True
    assert block.phrase == "sesión cerrada por falta de actividad"


async def test_betsson_reality_check_wins_over_logged_out_header() -> None:
    # Ordering pin: when BOTH a reality-check popup and a logged-out header are present,
    # reality_check classifies first (a closeable reminder that still occludes placement).
    page = _FakePage(
        shadow={"found": True, "phrase": "¿sabés qué hora es?"},
        auth_payload={"loggedIn": False, "loggedOut": True, "expiredPhrase": ""},
    )
    block = await _transport(page, platform="betsson").check_session_blocked()
    assert block is not None
    assert block.kind == "reality_check"


async def test_light_dom_body_expired_phrase_is_banner_on_light_dom_platform() -> None:
    # A session-expired phrase in the LIGHT-DOM body only is caught by the generic loops
    # as a non-blocking banner (is_overlay False). Pinned on BetWarrior — a light-DOM
    # platform where this state is real. (On Betsson it CANNOT arise: the auth scan's
    # walk covers light DOM too, so any visible expired phrase surfaces — debounced — as
    # an overlay via the Betsson branch, never as a body-only banner.)
    page = _FakePage(body="volver a iniciar sesión  boca river 2.10")
    block = await _transport(page, platform="betwarrior").check_session_blocked()
    assert block is not None
    assert block.kind == "session_expired"
    assert block.is_overlay is False


async def test_betsson_auth_scan_fails_open() -> None:
    # A flaky auth-scan read must never crash the heartbeat — degrade to None. Targeted:
    # only the auth scan raises (block scan + reality scan succeed), exercising the
    # transport.betsson_auth_probe_error fail-open path specifically.
    page = _FakePage(auth_raises=True)
    assert await _transport(page, platform="betsson").check_session_blocked() is None


async def test_establish_skips_goto_when_session_expired_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hard goto can reset the login popup mid-relogin and can never revive a dead
    # session, so establish must NOT goto while a session_expired OVERLAY is up — the
    # manager's auto-relogin owns recovery. Returns not-ready; goto_calls == 0.
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": True,
            "expiredPhrase": "",
        }
    )
    assert await _transport(page, platform="betsson").establish_betsson_context() is False
    assert page.goto_calls == 0


async def test_establish_gotos_on_session_expired_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # STRUCTURAL pin of establish's guard: the goto skip is OVERLAY-scoped — a
    # non-overlay session_expired block must NOT skip the goto, so a recoverable cold
    # session is re-established, not stranded. The block is INJECTED directly: on real
    # Betsson DOM the debounced auth scan owns expiry classification (see the debounce
    # tests), and the guard consumes a SessionBlock regardless of its source.
    page = _FakePage(body="boca river 2.10")
    t = _transport(page, platform="betsson")

    async def _banner_block() -> session_mod.SessionBlock:
        return session_mod.SessionBlock(
            phrase="volver a iniciar sesión", is_overlay=False, kind="session_expired"
        )

    monkeypatch.setattr(t, "check_session_blocked", _banner_block)
    await t.establish_betsson_context()
    assert page.goto_calls >= 1


# ---- Betsson auth-scan debounce (reality-check → false-relogin logout regression) ----


async def test_betsson_transient_logged_out_header_heals_no_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-goto / post-reality-check-Cerrar, Betsson's SPA renders the login trigger
    BEFORE the session hydrates. A logged-out read that heals within the settle window
    must NOT classify session_expired — that false block scheduled the relogin that
    logged a healthy session out (2026-07-05)."""
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payloads=[
            {"loggedIn": False, "hasBalance": False, "loggedOut": True, "expiredPhrase": ""},
            {"loggedIn": True, "hasBalance": True, "loggedOut": False, "expiredPhrase": ""},
        ]
    )
    assert await _transport(page, platform="betsson").check_session_blocked() is None
    assert page.auth_scan_calls == 2  # bad first sample → re-sample → healed → stop


async def test_betsson_transient_expired_phrase_heals_no_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broad "volver a iniciar sesión" can transiently surface (login CTA / popup
    mid-render). A phrase hit must persist across EVERY settle sample to block — the
    auth scan deliberately reads ALL visible text (the expired-popup-over-mounted-
    session case), so the debounce is its only guard against transients."""
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payloads=[
            {
                "loggedIn": False,
                "hasBalance": True,
                "loggedOut": False,
                "expiredPhrase": "volver a iniciar sesión",
            },
            {"loggedIn": True, "hasBalance": True, "loggedOut": False, "expiredPhrase": ""},
        ]
    )
    assert await _transport(page, platform="betsson").check_session_blocked() is None
    assert page.auth_scan_calls == 2


async def test_betsson_persistent_logged_out_consumes_all_settle_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GENUINE logout persists across the whole settle window and still blocks — the
    debounce adds bounded latency, never blindness."""
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": True,
            "expiredPhrase": "",
        }
    )
    block = await _transport(page, platform="betsson").check_session_blocked()
    assert block is not None
    assert block.kind == "session_expired"
    assert page.auth_scan_calls == session_mod._BETSSON_AUTH_SETTLE_SAMPLES


async def test_betsson_relogin_transient_logged_out_never_touches_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempt_betsson_relogin invoked while the header is mid-hydration (e.g. the
    executor's reactive 401 racing a page settle) must NOT log out or open the login
    form — the debounced scan heals and the method degrades to a cheap re-establish.
    This is the exact mechanism that turned a handled reality-check into a logged-out
    Betsson (2026-07-05)."""
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payloads=[
            {"loggedIn": False, "hasBalance": False, "loggedOut": True, "expiredPhrase": ""},
            {"loggedIn": True, "hasBalance": True, "loggedOut": False, "expiredPhrase": ""},
        ]
    )
    t = _transport(page, platform="betsson")
    established: list[bool] = []

    async def _fake_establish() -> bool:
        established.append(True)
        return True

    monkeypatch.setattr(t, "establish_betsson_context", _fake_establish)
    assert await t.attempt_betsson_relogin() is True
    assert established == [True]  # recovered via re-establish…
    assert page.mouse.clicks == []  # …with ZERO bookmaker interaction


async def test_betsson_relogin_ambiguous_header_skips_destructive_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A header that reads ambiguous (neither balance nor login trigger — mid-render)
    must NEVER reach the logout→login flow: re-establish only, distinct operator event."""
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": False,
            "expiredPhrase": "",
        }
    )
    t = _transport(page, platform="betsson")

    async def _fake_establish() -> bool:
        return True

    monkeypatch.setattr(t, "establish_betsson_context", _fake_establish)
    assert await t.attempt_betsson_relogin() is True
    assert page.mouse.clicks == []
    assert page.auth_scan_calls == 1  # ambiguous is decisive — no extra samples


async def test_betsson_relogin_error_attaches_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Playwright fault during the destructive login path preserves trace evidence."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(
        session_mod,
        "get_credential",
        lambda _platform: SimpleNamespace(username="u", password="p"),
    )
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": True,
            "expiredPhrase": "",
        },
        visible_centers={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [10.0, 20.0],
            session_mod._BETSSON_LOGIN_SUBMIT_SEL: [30.0, 40.0],
        },
    )
    t = _transport(page, platform="betsson")

    async def _open_form() -> bool:
        return True

    async def _type_raises(_text: str, delay: int | None = None) -> None:
        page.keyboard.typed.append(_text)
        raise RuntimeError("keyboard detached")

    page.keyboard.type = _type_raises  # type: ignore[method-assign]

    async def _record_evidence(stage: str, *, missing: str | None = None) -> str:
        assert stage == "relogin_error"
        assert t._page_lock.locked()
        assert missing is None
        assert [rec["stage"] for rec in t._betsson_password_trace] == [
            "before_password_click",
            "after_password_click",
            "before_password_type",
            "after_password_type",
        ]
        return "/tmp/relogin_error.json"

    monkeypatch.setattr(t, "_open_betsson_login_form", _open_form)
    monkeypatch.setattr(t, "_capture_betsson_relogin_evidence", _record_evidence)

    with structlog.testing.capture_logs() as logs:
        assert await t.attempt_betsson_relogin() is False

    error = [e for e in logs if e["event"] == "transport.betsson_relogin_error"]
    assert error and error[0]["error"] == "keyboard detached"
    assert error[0]["evidence"] == "/tmp/relogin_error.json"


async def test_betsson_await_logged_in_accepts_balance_without_header_text() -> None:
    """Post-login gate keys on hasBalance (Betsson renders balance-button ONLY when
    authenticated), NOT the stricter loggedIn (balance AND Retirar/Depósito text) — a
    header-layout variant made a SUCCESSFUL login read as a relogin failure and
    stranded the session (2026-07-05)."""
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": True,
            "loggedOut": False,
            "expiredPhrase": "",
        }
    )
    t = _transport(page, platform="betsson")
    assert await t._await_betsson_logged_in() is True


async def test_betsson_relogin_ambiguous_then_settled_bad_escalates_to_login_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manager runs ONE reauth per block episode (_session_reauth_attempted): an
    ambiguous first read whose cheap re-establish fails must NOT consume the attempt
    doing nothing — a second SETTLED bad read escalates to the destructive login flow
    within the same call. Destructive ONLY after the settled bad read."""
    monkeypatch.setattr(session_mod, "_BETSSON_AUTH_SETTLE_GAP_S", 0.0)
    page = _FakePage(
        auth_payloads=[
            {"loggedIn": False, "hasBalance": False, "loggedOut": False, "expiredPhrase": ""},
            {"loggedIn": False, "hasBalance": False, "loggedOut": True, "expiredPhrase": ""},
        ]
    )
    t = _transport(page, platform="betsson")

    async def _fail_establish() -> bool:
        return False

    opened: list[bool] = []

    async def _record_open_form() -> bool:
        opened.append(True)
        return False  # abort before typing — REACHING the form flow is the pin

    monkeypatch.setattr(t, "establish_betsson_context", _fail_establish)
    monkeypatch.setattr(t, "_open_betsson_login_form", _record_open_form)
    monkeypatch.setattr(
        session_mod, "get_credential", lambda _p: SimpleNamespace(username="u", password="p")
    )
    assert await t.attempt_betsson_relogin() is False
    assert opened == [True]  # destructive flow reached — via the settled second read only
    # first read: 1 sample (ambiguous short-circuits); second read: all samples (bad).
    assert page.auth_scan_calls == 1 + session_mod._BETSSON_AUTH_SETTLE_SAMPLES


async def test_betsson_relogin_persistently_unreadable_gives_up_without_touching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two settled ambiguous reads bracketing a failed re-establish → give up to the
    suspend+alert path (manual login) with the DISTINCT unreadable event; never a blind
    form flow, zero bookmaker interaction."""
    page = _FakePage(
        auth_payload={
            "loggedIn": False,
            "hasBalance": False,
            "loggedOut": False,
            "expiredPhrase": "",
        }
    )
    t = _transport(page, platform="betsson")

    async def _fail_establish() -> bool:
        return False

    monkeypatch.setattr(t, "establish_betsson_context", _fail_establish)
    with structlog.testing.capture_logs() as logs:
        assert await t.attempt_betsson_relogin() is False
    assert page.mouse.clicks == []  # zero bookmaker interaction
    assert any(e["event"] == "transport.betsson_relogin_auth_unreadable" for e in logs)
    assert page.auth_scan_calls == 2  # ambiguous is decisive per read — one sample each


async def test_betsson_login_form_opens_on_password_when_email_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remembered-user popup has no email input: form readiness must key off password.

    Regression pin for the live sentinel drill: old code waited for email-input,
    timed out, then reload()ed — closing the perfectly usable password-only popup.
    """

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(
        visible_centers={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [3.0, 4.0],
        },
        trigger_center=[1.0, 2.0],
    )
    assert await _transport(page, platform="betsson")._open_betsson_login_form() is True
    assert page.reload_calls == 0
    assert page.locator_clicks == [
        (session_mod._BETSSON_LOGIN_TRIGGER_SEL, session_mod._BETSSON_LOGIN_CLICK_TIMEOUT_MS)
    ]
    assert page.mouse.clicks == []


async def test_betsson_login_form_opens_on_bottom_input_container_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live popup variant: password-input absent, bottom input-container is the gate."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    page = _FakePage(
        trigger_center=[1.0, 2.0],
        visible_center_options={
            session_mod._BETSSON_LOGIN_INPUT_CONTAINER_SEL: [[5.0, 6.0], [10.0, 20.0]]
        },
    )
    assert await _transport(page, platform="betsson")._open_betsson_login_form() is True
    assert page.reload_calls == 0


async def test_betsson_login_form_uses_evidence_shadow_walk_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live miss: evidence saw password, but the old opener walker stopped too early."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    page = _FakePage(
        visible_centers={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [3.0, 4.0],
        },
        trigger_center=[1.0, 2.0],
        visible_center_min_limits={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: session_mod._BETSSON_SHADOW_WALK_LIMIT
        },
    )

    evidence = await page.evaluate(
        session_mod._BETSSON_RELOGIN_EVIDENCE_JS,
        {"password": session_mod._BETSSON_LOGIN_PASSWORD_SEL},
    )
    assert evidence["selectors"]["password"]["visible"] is True
    assert await _transport(page, platform="betsson")._open_betsson_login_form() is True
    assert page.reload_calls == 0


async def test_betsson_login_form_falls_back_to_center_click_after_locator_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short locator timeout must not suppress the existing center-click path."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    page = _FakePage(
        visible_centers={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [3.0, 4.0],
        },
        trigger_center=[1.0, 2.0],
        locator_raises={session_mod._BETSSON_LOGIN_TRIGGER_SEL},
    )

    assert await _transport(page, platform="betsson")._open_betsson_login_form() is True

    assert page.locator_clicks == [
        (session_mod._BETSSON_LOGIN_TRIGGER_SEL, session_mod._BETSSON_LOGIN_CLICK_TIMEOUT_MS)
    ]
    assert page.mouse.clicks == [(1.0, 2.0)]


async def test_betsson_login_form_center_retries_after_noop_locator_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If locator click resolves but opens nothing, retry the proved trigger center."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    page = _FakePage(
        trigger_center=[1.0, 2.0],
        visible_center_sequences={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [None, None, None, None, [3.0, 4.0]],
        },
    )

    assert await _transport(page, platform="betsson")._open_betsson_login_form() is True

    assert page.locator_clicks == [
        (session_mod._BETSSON_LOGIN_TRIGGER_SEL, session_mod._BETSSON_LOGIN_CLICK_TIMEOUT_MS)
    ]
    assert page.mouse.clicks == [(1.0, 2.0)]


async def test_betsson_login_form_handles_geolocation_before_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geolocation CTA opens the login popup on its own; click CTA, then accept
    password readiness — no immediate trigger re-click, still no reload/no_form."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(
        trigger_center=[1.0, 2.0],
        visible_centers={
            session_mod._BETSSON_GEOLOCATION_CTA_SEL: [7.0, 8.0],
        },
        visible_center_sequences={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [None, [3.0, 4.0]],
        },
    )
    assert await _transport(page, platform="betsson")._open_betsson_login_form() is True
    assert page.reload_calls == 0
    assert page.locator_clicks == [
        (session_mod._BETSSON_LOGIN_TRIGGER_SEL, session_mod._BETSSON_LOGIN_CLICK_TIMEOUT_MS),
    ]
    assert page.mouse.clicks == [(7.0, 8.0)]


async def test_betsson_login_form_miss_logs_pre_reload_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trigger finder misses → give up after one reload, never clicking a guessed
    point; the pre-/post-reload artifacts are still captured."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    calls: list[tuple[str, str | None]] = []

    async def _record_evidence(stage: str, *, missing: str | None = None) -> str:
        calls.append((stage, missing))
        return f"/tmp/{stage}.json"

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    page = _FakePage()
    t = _transport(page, platform="betsson")
    monkeypatch.setattr(t, "_capture_betsson_relogin_evidence", _record_evidence)

    with structlog.testing.capture_logs() as logs:
        assert await t._open_betsson_login_form() is False

    assert calls == [("open_miss_before_reload", None), ("no_form_after_reload", None)]
    open_miss = [e for e in logs if e["event"] == "transport.betsson_relogin_open_miss"]
    assert open_miss and open_miss[0]["evidence"] == "/tmp/open_miss_before_reload.json"
    no_form = [e for e in logs if e["event"] == "transport.betsson_relogin_no_form"]
    assert no_form and no_form[0]["evidence"] == "/tmp/no_form_after_reload.json"
    assert page.reload_calls == 1
    assert page.mouse.clicks == []


async def test_betsson_fill_login_skips_email_when_prefilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remembered-user popup: type only password, then click account-login-btn-1."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(
        visible_centers={
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [10.0, 20.0],
            session_mod._BETSSON_LOGIN_SUBMIT_SEL: [30.0, 40.0],
        }
    )
    t = _transport(page, platform="betsson")
    with structlog.testing.capture_logs() as logs:
        ok = await t._fill_betsson_login(
            SimpleNamespace(username="user@example.com", password="secret")
        )
    assert ok is True
    assert page.keyboard.typed == ["secret"]
    assert page.keyboard.presses == ["ControlOrMeta+A"]
    assert page.mouse.clicks == [(10.0, 20.0), (30.0, 40.0)]
    assert any(e["event"] == "transport.betsson_relogin_email_prefilled" for e in logs)
    assert [rec["stage"] for rec in t._betsson_password_trace] == [
        "before_password_click",
        "after_password_click",
        "before_password_type",
        "after_password_type",
    ]
    assert t._betsson_password_trace[-1]["eventSeen"] == {"keydown": True}
    trace = t._betsson_password_trace[-1]
    assert trace["activePath"][0]["selectionPresent"] is True
    assert trace["activePath"][0]["valuePresent"] is True
    assert trace["events"][0]["keyKind"] == "printable"
    assert "key" not in trace["events"][0]
    assert "valueLength" not in trace["activePath"][0]
    assert "selectionStart" not in trace["activePath"][0]
    assert "selectionEnd" not in trace["activePath"][0]


async def test_betsson_fill_login_uses_bottom_input_container_when_password_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password fallback chooses the lower input-container, not the upper/email wrapper."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(
        visible_centers={session_mod._BETSSON_LOGIN_SUBMIT_SEL: [30.0, 40.0]},
        visible_center_options={
            session_mod._BETSSON_LOGIN_INPUT_CONTAINER_SEL: [[5.0, 6.0], [10.0, 20.0]]
        },
    )
    ok = await _transport(page, platform="betsson")._fill_betsson_login(
        SimpleNamespace(username="user@example.com", password="secret")
    )
    assert ok is True
    assert page.keyboard.typed == ["secret"]
    assert page.mouse.clicks == [(10.0, 20.0), (30.0, 40.0)]


@pytest.mark.parametrize(
    ("visible_centers", "expected_stage", "expected_missing"),
    [
        ({}, "form_incomplete_password", "password"),
        (
            {session_mod._BETSSON_LOGIN_PASSWORD_SEL: [10.0, 20.0]},
            "form_incomplete_submit",
            "submit",
        ),
    ],
)
async def test_betsson_fill_login_incomplete_form_logs_evidence(
    monkeypatch: pytest.MonkeyPatch,
    visible_centers: dict[str, list[float]],
    expected_stage: str,
    expected_missing: str,
) -> None:
    async def _noop_sleep(_seconds: float) -> None:
        return None

    calls: list[tuple[str, str | None]] = []

    async def _record_evidence(stage: str, *, missing: str | None = None) -> str:
        calls.append((stage, missing))
        return f"/tmp/{stage}.json"

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(visible_centers=visible_centers)
    t = _transport(page, platform="betsson")
    monkeypatch.setattr(t, "_capture_betsson_relogin_evidence", _record_evidence)

    with structlog.testing.capture_logs() as logs:
        ok = await t._fill_betsson_login(SimpleNamespace(username="u", password="p"))

    assert ok is False
    assert calls == [(expected_stage, expected_missing)]
    incomplete = [e for e in logs if e["event"] == "transport.betsson_relogin_form_incomplete"]
    assert incomplete and incomplete[0]["missing"] == expected_missing
    assert incomplete[0]["evidence"] == f"/tmp/{expected_stage}.json"
    if expected_missing == "password":
        assert page.keyboard.typed == []
        assert page.mouse.clicks == []
    else:
        assert page.keyboard.typed == ["p"]
        assert page.mouse.clicks == [(10.0, 20.0)]


async def test_betsson_fill_login_fallback_password_then_missing_submit_logs_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container fallback reaches password before submit-missing evidence is emitted."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    calls: list[tuple[str, str | None]] = []

    async def _record_evidence(stage: str, *, missing: str | None = None) -> str:
        calls.append((stage, missing))
        return f"/tmp/{stage}.json"

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(
        visible_center_options={
            session_mod._BETSSON_LOGIN_INPUT_CONTAINER_SEL: [[5.0, 6.0], [10.0, 20.0]]
        }
    )
    t = _transport(page, platform="betsson")
    monkeypatch.setattr(t, "_capture_betsson_relogin_evidence", _record_evidence)

    with structlog.testing.capture_logs() as logs:
        ok = await t._fill_betsson_login(SimpleNamespace(username="u", password="p"))

    assert ok is False
    assert calls == [("form_incomplete_submit", "submit")]
    incomplete = [e for e in logs if e["event"] == "transport.betsson_relogin_form_incomplete"]
    assert incomplete and incomplete[0]["missing"] == "submit"
    assert page.keyboard.typed == ["p"]
    assert page.mouse.clicks == [(10.0, 20.0)]


async def test_betsson_fill_login_still_types_email_when_field_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-login popup: email remains supported when Betsson does not prefill it."""

    async def _noop_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(session_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(session_mod.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(session_mod.random, "randint", lambda *_args: 0)
    page = _FakePage(
        visible_centers={
            session_mod._BETSSON_LOGIN_EMAIL_SEL: [5.0, 6.0],
            session_mod._BETSSON_LOGIN_PASSWORD_SEL: [10.0, 20.0],
            session_mod._BETSSON_LOGIN_SUBMIT_SEL: [30.0, 40.0],
        }
    )
    ok = await _transport(page, platform="betsson")._fill_betsson_login(
        SimpleNamespace(username="user@example.com", password="secret")
    )
    assert ok is True
    assert page.keyboard.typed == ["user@example.com", "secret"]
    assert page.keyboard.presses == ["ControlOrMeta+A", "ControlOrMeta+A"]
    assert page.mouse.clicks == [(5.0, 6.0), (10.0, 20.0), (30.0, 40.0)]


@pytest.mark.parametrize(
    "selector_name",
    [
        "_BETSSON_LOGIN_TRIGGER_SEL",
        "_BETSSON_LOGIN_SUBMIT_SEL",
    ],
)
async def test_betsson_relogin_required_selectors_abort_before_page_access(
    monkeypatch: pytest.MonkeyPatch,
    selector_name: str,
) -> None:
    """Trigger/password/submit selectors are required; a blank required selector is
    fail-soft before any auth scan or page mutation."""

    monkeypatch.setattr(session_mod, selector_name, "")
    page = _FakePage()
    with structlog.testing.capture_logs() as logs:
        ok = await _transport(page, platform="betsson").attempt_betsson_relogin()
    assert ok is False
    assert page.auth_scan_calls == 0
    assert page.mouse.clicks == []
    assert any(e["event"] == "transport.betsson_relogin_not_configured" for e in logs)


async def test_betsson_relogin_password_fallback_selector_is_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank primary password selector is OK while the input-container fallback exists."""

    async def _establish() -> bool:
        return True

    monkeypatch.setattr(session_mod, "_BETSSON_LOGIN_PASSWORD_SEL", "")
    page = _FakePage()
    t = _transport(page, platform="betsson")
    monkeypatch.setattr(t, "establish_betsson_context", _establish)
    with structlog.testing.capture_logs() as logs:
        ok = await t.attempt_betsson_relogin()
    assert ok is True
    assert page.auth_scan_calls == 1
    events = {e["event"] for e in logs}
    assert "transport.betsson_relogin_not_configured" not in events
    assert "transport.betsson_relogin_already_logged_in" in events


async def test_betsson_relogin_requires_some_password_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Password is configured if primary OR fallback exists; both blank abort."""

    monkeypatch.setattr(session_mod, "_BETSSON_LOGIN_PASSWORD_SEL", "")
    monkeypatch.setattr(session_mod, "_BETSSON_LOGIN_INPUT_CONTAINER_SEL", "")
    page = _FakePage()
    with structlog.testing.capture_logs() as logs:
        ok = await _transport(page, platform="betsson").attempt_betsson_relogin()
    assert ok is False
    assert page.auth_scan_calls == 0
    assert page.mouse.clicks == []
    assert any(e["event"] == "transport.betsson_relogin_not_configured" for e in logs)


async def test_betsson_relogin_email_selector_is_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remembered-user relogin must not be disabled just because email-input is absent."""

    async def _establish() -> bool:
        return True

    monkeypatch.setattr(session_mod, "_BETSSON_LOGIN_EMAIL_SEL", "")
    page = _FakePage()
    t = _transport(page, platform="betsson")
    monkeypatch.setattr(t, "establish_betsson_context", _establish)
    with structlog.testing.capture_logs() as logs:
        ok = await t.attempt_betsson_relogin()
    assert ok is True
    assert page.auth_scan_calls == 1
    events = {e["event"] for e in logs}
    assert "transport.betsson_relogin_not_configured" not in events
    assert "transport.betsson_relogin_already_logged_in" in events
