"""In-session HTTP transport for bet placement.

Placement requests are sent **from inside the logged-in browser session** —
an in-page ``fetch`` driven via Playwright on the persistent profile — so they
inherit the session's cookies, CSRF/fingerprint and TLS. Raw ``httpx`` was
WAF-blocked during recon; this is the anti-detection design (no LLM, no
clicking — a single deterministic fetch in the real page context).

`Transport` is the seam the LegPlacers depend on, so their build→send→parse
wiring is unit-testable with a fake. `InSessionTransport` is the real
Playwright implementation; its live send is gated behind ``arm()`` AND only
runs when constructed with ``dry_run=False`` — and it's validated against a
live tiny bet in the Phase-1 trial, not unit tests.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import structlog

from src.config import get_settings

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROFILE_ROOT = REPO_ROOT / "recon" / "profile"

# Pseudo / hop-by-hop headers to strip before replaying a captured header set.
_HEADER_DROP = frozenset(
    {":authority", ":method", ":path", ":scheme", "host", "content-length", "accept-encoding"}
)
_BETSSON_HOME = "https://pba.betsson.bet.ar/apuestas-deportivas"
# BetWarrior (Kambi) sends its session bearer on calls to the authenticated player
# API host; we capture it from those outgoing requests (no nav, no storage probing).
_KAMBI_PLAYER_API = "kambicdn.com/player/"
# Clock skew tolerance when judging a captured JWT bearer expired (seconds).
_BEARER_EXP_SKEW_SEC = 30.0

# Responsible-gambling LOCKOUT phrases (lowercased). These strings appear only when a
# PBA platform has BLOCKED the betting UI behind a mandatory-break / accumulated-play-
# time-limit overlay — the session stays authenticated (so the balance / ctx- / bearer
# readiness probes all keep passing), and this overlay is the ONLY signal that the
# window is actually unusable. Deliberately the LOCKOUT text, NOT the generic "juego
# responsable" footer link that sits on every page (which would false-positive). Avoid
# bare "tiempo de juego" — a live match shows elapsed match time. Grounded in the live
# Betano lockout: "TOMATE UN DESCANSO — 12h de descanso de apostar y jugar". Extend
# from captures (scripts/trial_place.py --capture-popup) as other platforms' wording
# is grounded.
_RG_BLOCK_PHRASES: Final[tuple[str, ...]] = (
    "tomate un descanso",
    "tomá un descanso",
    "descanso de apostar",
    "12h de descanso",
    "límite de tiempo de juego",
    "llevás jugando",
    "cuánto tiempo llevás",
)

# Session-expired phrases (lowercased). These appear when a PBA platform has terminated
# the session server-side — typically an inactivity logout (BetWarrior's "Estabas
# desconectado / Su sesión se terminó por inactividad", Betsson's "sesión cerrada por
# falta de actividad"). The captured bearer / ctx- / balance probe may keep passing for
# minutes (the JWT `exp` is set at issuance, not at session kill — see ledger 2026-06-10
# "BetWarrior inactivity logout"), so the readiness probe reads GREEN while the window
# is unusable behind this popup. The popup itself is the backstop signal. Operator action
# differs from RG lockout: re-login (not wait out a mandatory break).
_SESSION_EXPIRED_PHRASES: Final[tuple[str, ...]] = (
    "estabas desconectado",
    "se terminó por inactividad",
    "su sesión se terminó",
    "sesión cerrada por falta de actividad",
    "volver a iniciar sesión",
)

# Session-timer phrases (lowercased). These appear when a PBA platform is WARNING that
# the session is about to expire (session still alive, action optional) — Betano's
# "Temporizador de sesión" popup with "Sí, conservarlo" / "No, quiero desconectarme".
# Distinct from session_expired (already dead) and rg_lockout (mandatory break): the
# session is still usable until the timer lapses, but the modal occludes the betting UI
# (placement would fail behind it). Operator (or auto-click) action: extend the session.
_SESSION_TIMER_PHRASES: Final[tuple[str, ...]] = (
    "temporizador de sesión",
    "¿querés conservarlo?",
    "sí, conservarlo",
    "quiero desconectarme",
    "tu sesión está activa por",
)

# Visible modal/overlay selector — used by capture_block_evidence to dump the block's
# markup. A populated `dialogs` list means a true blocking OVERLAY; an empty one with a
# phrase hit means a non-blocking BANNER (e.g. Betano's "12h descanso"). This is exactly
# the distinction we need to ground but couldn't reproduce in the lab.
_RG_BLOCK_DIALOG_SELECTOR = (
    # Standard ARIA + legacy class names (React Aria / Radix / MUI / plain .modal).
    "[role=dialog],[aria-modal=true],.modal,.overlay,.modal-overlay,"
    # Betano-specific (captured 2026-06-19): #session-timer is the stable outer id of
    # the session-timer popup; .modal-container is Betano's generic modal class. Neither
    # matches .modal (CSS class selectors are not substring matches). Add other platforms'
    # classes here as their popup DOMs are captured.
    "#session-timer,.modal-container,"
    # BetWarrior-specific (captured 2026-06-19): the inactivity-logout popup
    # ("Estabas desconectado") + session-summary popup both use stable IDs on a
    # styled-components shell — sg-modal-backdrop is position:fixed z-index 100000000
    # (the visible backdrop), sg-modal-wrapper is the inner content. Neither has
    # role=dialog nor aria-modal. Multiple sg-modal-backdrop siblings can co-exist.
    "#sg-modal-backdrop,#sg-modal-wrapper"
)
# Where the first production block dumps its DOM + screenshot, so the exact lockout
# selector is grounded from the real event.
_BLOCK_EVIDENCE_DIR = REPO_ROOT / "recon" / "artifacts" / "rg_blocks"

# Per-platform "conserve session" button selectors — clicked by ``attempt_session_extend``
# to auto-recover from a session_timer_warning popup. Betano's is grounded from the
# captured DOM (id="st-maintain-button", 2026-06-19). Add other platforms' button
# selectors here as their session-timer popups are captured. Absence from this mapping
# means the platform has no known auto-extend path → manager falls back to suspend + alert.
# Operator-authorized 2026-06-19 (per AGENTS.md: explicit confirmation for any real-
# bookmaker interaction; the click is the same action the operator would take manually).
_SESSION_EXTEND_BUTTON_SELECTORS: Final[dict[str, str]] = {
    "betano": "#st-maintain-button",
}

# Returns the visible-overlay text and the full body text (both lowercased) so the
# Python side can match RG phrases and classify overlay-vs-banner. Matching is kept in
# Python (testable); the JS only extracts text.
_BLOCK_SCAN_JS = """({sel}) => {
    const vis = el => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        // Fast path: any element with a valid offsetParent is visible. Covers absolute /
        // relative / static / sticky positioned overlays (the common [role=dialog],
        // .modal, .modal-overlay cases). MUST stay above the fixed-position fallback so
        // existing non-fixed popups keep matching as overlays.
        if (el.offsetParent !== null) return true;
        // Fallback: WebKit returns offsetParent === null for position: fixed (Betano's
        // session-timer outer wrapper is fixed) AND for display: none. Accept fixed
        // elements as visible if their computed style says they're shown. (display: none
        // elements already failed the rect check above with zero width/height.)
        const cs = getComputedStyle(el);
        return cs.position === 'fixed' && cs.display !== 'none' && cs.visibility !== 'hidden';
    };
    let overlayText = '';
    for (const el of document.querySelectorAll(sel)) {
        if (vis(el)) overlayText += ' ' + (el.innerText || '');
    }
    return {
        overlayText: overlayText.toLowerCase(),
        bodyText: (document.body && document.body.innerText || '').toLowerCase(),
    };
}"""


_CDP_PORT_OFFSETS: Final[dict[str, int]] = {
    "betano": 0,
    "betsson": 1,
    "betwarrior": 2,
}


def _cdp_debug_args(platform: str, port_base: int | None) -> list[str]:
    """Per-platform Chromium CDP args, derived from ``Settings.cdp_port_base``.

    Off by default — when ``port_base`` is None, returns ``[]`` so launch behavior is
    identical to before. When set to N, exposes Chrome DevTools Protocol on
    ``localhost:{N+offset}`` for each platform (betano +0, betsson +1, betwarrior +2),
    letting an external READ-ONLY client (the operator's assistant via puppeteer CDP
    attach) observe the accessibility tree, screenshot, and read DOM of the logged-in
    window.

    Read-only is a hard contract: navigate / click / side-effecting evaluate on a live
    betting session can disrupt real-money placement. Port range / type validation is
    handled by pydantic in Settings (``ge=1, le=65535``); this helper trusts its input.
    """
    if port_base is None:
        return []
    port = port_base + _CDP_PORT_OFFSETS.get(platform, 0)
    return [f"--remote-debugging-port={port}"]


@dataclass(frozen=True)
class SessionBlock:
    """A block detected on the betting window that makes it unusable for placement.

    Two ``kind`` values (both go through the same overlay-vs-banner classification;
    both suspend placement when they appear as a visible overlay):
    - ``"rg_lockout"`` — responsible-gambling mandatory break (e.g. Betano's
      "Tomate un descanso"). May not be resolvable by re-login; operator may need to
      wait out the break.
    - ``"session_expired"`` — session terminated server-side (e.g. BetWarrior's
      inactivity logout "Estabas desconectado"). Operator re-logs in to resolve. The
      captured bearer's JWT ``exp`` may not have lapsed yet, so the readiness probe
      alone can't see it — this popup is the backstop detection.

    ``is_overlay`` True ⇒ the phrase sits inside a visible blocking modal/overlay — a
    real LOCKOUT that suspends placement. False ⇒ the phrase is only in the page text
    with no overlay — a non-blocking BANNER (e.g. Betano's "12h descanso", confirmed
    placeable), which is logged + captured but does NOT suspend.
    """

    phrase: str
    is_overlay: bool
    kind: str = "rg_lockout"


def _jwt_exp(token: str) -> float | None:
    """Best-effort decode of a JWT bearer's ``exp`` (epoch seconds), or ``None`` if
    the token isn't a decodable JWT with an ``exp`` claim. The Kambi session bearer is
    a JWT; reading its expiry lets readiness detect an inactivity logout (token TTL
    lapses, the SPA stops refreshing it) WITHOUT any network/CORS/DOM probe. ``None`` ⇒
    caller falls back to presence-only (never worse than before)."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) else None
# Betano's cookie-auth balance endpoint — same-origin, so the readiness probe's
# in-page GET works (unlike BetWarrior's cross-origin PAM host).
_BETANO_BALANCE_URL = "https://www.betano.bet.ar/api/balance"


class Transport(Protocol):
    async def fetch(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Send a request and return ``(status_code, parsed_json_body)``."""
        ...


class TransportError(RuntimeError):
    """Transport-level failure (not launched, not armed, network/parse error).
    The executor escalates these to recovery rather than treating them as a
    placement rejection."""


class InSessionTransport:
    """Sends requests via an in-page ``fetch`` on the platform's logged-in
    persistent profile.

    SAFETY: constructed with ``dry_run=True`` by default; a live send requires
    ``dry_run=False`` **and** an explicit :meth:`arm` call. Until then ``fetch``
    raises ``TransportError`` — nothing can place by accident.
    """

    def __init__(
        self,
        platform: str,
        *,
        dry_run: bool = True,
        channel: str | None = "chrome",
        restore_session: bool = False,
    ) -> None:
        self._platform = platform
        self._dry_run = dry_run
        self._channel = channel
        # Re-injecting saved session-only cookies (a recon-era hack) CONTAMINATES a
        # live operator login — stale cookies make the app read as not-logged-in so
        # the authenticated ctx- never resolves. The proven Betsson path uses the
        # profile's own live session, so default OFF.
        self._restore_session = restore_session
        self._armed = False
        self._context: Any = None  # playwright BrowserContext, set in __aenter__
        self._page: Any = None
        self._pw: Any = None
        self._captured_ctx: dict[str, str] | None = None  # Betsson authenticated header set
        self._captured_bearer: str | None = None  # BetWarrior (Kambi) session bearer token
        self._bearer_exp: float | None = None  # its JWT exp (epoch s), for liveness
        # Serializes page-mutating ops (the heartbeat's establish-nav vs a placement
        # fetch) so a re-navigation can't abort an in-flight in-page fetch.
        self._page_lock = asyncio.Lock()
        self._log = log.bind(component="in_session_transport", platform=platform, dry_run=dry_run)

    def arm(self) -> None:
        """Explicitly enable live sending. No-op effect unless dry_run=False."""
        self._armed = True
        self._log.warning("transport.armed")

    @property
    def _profile_dir(self) -> Path:
        return _PROFILE_ROOT / self._platform

    async def __aenter__(self) -> InSessionTransport:
        if self._dry_run:
            return self
        # Live: launch the logged-in persistent profile.
        from playwright.async_api import async_playwright

        from scripts.recon.stealth import apply_stealth

        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=False,
            channel=self._channel,
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            # Betsson (and possibly others) validate region via the browser
            # geolocation prompt. Grant it + pin a coordinate INSIDE Provincia de
            # Buenos Aires — La Plata, the provincial capital — so it routes to
            # the PBA (Iplyc) jurisdiction. NOTE: CABA city-center coords
            # (-34.60, -58.38) route to the CABA jurisdiction instead; PBA ≠ the city.
            permissions=["geolocation"],
            geolocation={"latitude": -34.9215, "longitude": -57.9545},
            # READ-ONLY CDP attach point for the operator's assistant. Off by default;
            # set Settings.cdp_port_base (env: CDP_PORT_BASE) to expose CDP on
            # localhost:N+per-platform-offset. See _cdp_debug_args.
            args=_cdp_debug_args(self._platform, get_settings().cdp_port_base),
        )
        await apply_stealth(self._context)
        # Capture the authenticated context header set (Betsson `ctx-`); harmless
        # for platforms that never send it.
        self._context.on("request", self._on_request)
        if self._restore_session:
            from scripts.recon.recon import _restore_session  # session-cookie re-inject

            await _restore_session(self._context, self._platform)
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        return self

    def _on_request(self, req: Any) -> None:
        # Betsson: capture ONLY from /api/sb/ requests — those carry the full
        # coupons-compatible header set (brandid/marketcode/x-sb-type). /sb/fe-api/
        # requests also bear a ctx- but lack those → replaying them 400s.
        if "/api/sb/" in req.url and req.headers.get("x-sb-user-context-id", "").startswith("ctx-"):
            self._captured_ctx = dict(req.headers)
        # BetWarrior (Kambi): capture the session bearer the SPA sends on its
        # authenticated player-API calls (session.json, coupon/validate, …).
        if _KAMBI_PLAYER_API in req.url:
            auth = req.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1].strip()
                # Latest token wins: while logged-in + active the SPA refreshes it, so
                # we always hold the freshest bearer + its expiry. After an inactivity
                # logout these stop arriving and the held token's exp lapses.
                self._captured_bearer = token
                self._bearer_exp = _jwt_exp(token)

    async def __aexit__(self, *exc: object) -> None:
        if self._context is not None:
            await self._context.close()
        if self._pw is not None:
            await self._pw.stop()

    async def goto(self, url: str) -> None:
        """Navigate so the in-page fetch is same-origin (cookies apply)."""
        if self._dry_run:
            self._log.info("transport.dry_run_goto", url=url)
            return
        await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)

    async def eval_js(self, expression: str) -> Any:
        """Read a value out of the live page (e.g. Bplay's bootstrap CSRF from
        the app's JS state). Dry-run returns None — no page is open."""
        if self._dry_run:
            self._log.info("transport.dry_run_eval")
            return None
        return await self._page.evaluate(expression)

    async def prepare_betsson_context(self, timeout_s: int = 30) -> dict[str, str] | None:
        """Return the live authenticated header set (sessiontoken + ``ctx-`` +
        x-sb-* context) the app is currently using, captured passively.

        CRITICAL: we do NOT navigate or reload here. Betsson's betting context
        lives in the SPA's in-memory state and is established by *client-side*
        in-app navigation after login (e.g. visiting My Account → routes to the
        sportsbook home). A hard load/reload cold-boots the SPA and DESTROYS that
        context ("login before placing"), so reloading is exactly wrong. The
        caller must drive the SPA into the placeable state first (operator click
        now; automated in-app nav later); we just read the ctx- the app emits.

        Returns ``None`` if no ctx- appears within ``timeout_s`` (context not
        established → not logged in / not navigated)."""
        if self._dry_run:
            self._log.info("transport.dry_run_prepare")
            return None
        for _ in range(timeout_s):
            if self._captured_ctx is not None:
                break
            await asyncio.sleep(1)
        if self._captured_ctx is None:
            return None
        return {k: v for k, v in self._captured_ctx.items() if k not in _HEADER_DROP}

    async def prepare_betwarrior_auth(self, timeout_s: int = 30) -> str | None:
        """Return the live BetWarrior (Kambi) session bearer token, captured
        passively from the SPA's own authenticated player-API calls. Like the
        Betsson context we do NOT navigate — the logged-in SPA emits the bearer on
        its background polling. Returns ``None`` if none appears within
        ``timeout_s`` (not logged in / no authenticated call yet), or if the held
        bearer's JWT ``exp`` has lapsed (inactivity logout / server revocation) —
        fail-closed so the placer rejects "session bearer not captured" rather
        than placing with a stale token and 401'ing. The single source of truth
        for bearer liveness; `check_betwarrior_ready` delegates here."""
        if self._dry_run:
            self._log.info("transport.dry_run_prepare")
            return None
        for _ in range(timeout_s):
            if self._captured_bearer is not None:
                break
            await asyncio.sleep(1)
        bearer = self._captured_bearer
        if bearer is None:
            return None
        # Fail-closed on a stale bearer: an inactivity logout stops the SPA's token
        # refresh, so the held token's exp lapses while it's still present. Return
        # None (placer rejects) rather than sending a known-dead token to placement.
        if self._bearer_exp is not None and time.time() >= self._bearer_exp - _BEARER_EXP_SKEW_SEC:
            self._log.warning("transport.betwarrior_bearer_expired", exp=self._bearer_exp)
            return None
        return bearer

    async def _read_json(self, url: str) -> tuple[int, dict[str, Any]]:
        """Ungated in-page GET for READS (readiness probes, balance) — NOT placement,
        so it does not require :meth:`arm`. Returns ``(status, parsed_json)``; ``(0, {})``
        if no page is open."""
        if self._page is None:
            return 0, {}
        try:
            async with self._page_lock:
                result = await self._page.evaluate(
                    """async (url) => {
                        try {
                            const resp = await fetch(url, {method: 'GET', credentials: 'include'});
                            return {status: resp.status, text: await resp.text()};
                        } catch (e) { return {status: 0, text: ''}; }
                    }""",
                    url,
                )
        except Exception as exc:  # noqa: BLE001 — a read must never crash readiness/startup
            self._log.warning("transport.read_json_error", url=url[:80], error=str(exc))
            return 0, {}
        status = int(result["status"])
        try:
            parsed = json.loads(result["text"]) if result["text"] else {}
        except ValueError:
            return status, {}
        return status, parsed if isinstance(parsed, dict) else {}

    async def check_betwarrior_ready(self, timeout_s: int = 10) -> bool:
        """Readiness probe: is the BetWarrior session live? PASSIVE — no network/CORS/DOM
        probe (the PAM checkSessionAlive host is cross-origin and crashed the page fetch).
        The captured Kambi bearer is a JWT; we hold the freshest one the logged-in SPA
        emits and check its ``exp``. A bearer that is present AND unexpired ⇒ ready. An
        inactivity logout stops the SPA's token refresh, so the held bearer's exp lapses ⇒
        not ready ⇒ the manager suspends placement + alerts, and auto-resumes when a
        re-login emits a fresh bearer. (Bearer with no decodable exp ⇒ presence-only, as
        before.) The placer still fail-closes on a stale bearer at place time."""
        if self._dry_run:
            return False
        # Liveness (bearer present AND unexpired) is gated inside
        # prepare_betwarrior_auth — the single source of truth shared with the
        # placer, so readiness and placement can never disagree about staleness.
        return await self.prepare_betwarrior_auth(timeout_s) is not None

    async def check_session_blocked(self) -> SessionBlock | None:
        """Detect a responsible-gambling block on the betting window.

        PBA platforms enforce an accumulated-play-time limit that, once hit, can replace
        the betting UI with a mandatory-break notice ("TOMATE UN DESCANSO — 12h de
        descanso de apostar y jugar"). The underlying session stays authenticated, so
        every readiness probe (balance, ``ctx-``, bearer ``exp``) keeps passing — this is
        the only signal the window is unusable. Returns a :class:`SessionBlock` or
        ``None``: ``is_overlay=True`` when an RG phrase sits inside a visible blocking
        modal (a true LOCKOUT → suspend); ``is_overlay=False`` when the phrase is only in
        page text with no overlay (a non-blocking BANNER, e.g. Betano's "12h descanso",
        confirmed placeable → logged/captured but NOT suspended). Dry-run / no page ⇒
        ``None``.

        Residual risk: if a real lockout's modal doesn't match ``_RG_BLOCK_DIALOG_
        SELECTOR`` it reads as a banner and won't suspend, so the bot may attempt to place
        into a locked window. This is only PARTIALLY backstopped — there is no lockout-aware
        check at place time; a leg only fails if the SERVER also rejects it (descanso
        enforced server-side, unconfirmed) or a leg errors (generic naked-leg recovery). A
        UI-only lockout would let the bet through. The evidence capture grounds the exact
        selector from the first real event so this gap can be closed. Fail-OPEN on a probe
        fault (return ``None``): a flaky DOM read must never crash the heartbeat (the
        hard-won startup-crash lesson)."""
        if self._dry_run or self._page is None:
            return None
        try:
            async with self._page_lock:
                res = await self._page.evaluate(
                    _BLOCK_SCAN_JS, {"sel": _RG_BLOCK_DIALOG_SELECTOR}
                )
        except Exception as exc:  # noqa: BLE001 — a read must never crash the heartbeat
            self._log.warning("transport.block_probe_error", error=str(exc))
            return None
        if not isinstance(res, dict):
            return None
        overlay_text = res.get("overlayText", "")
        body_text = res.get("bodyText", "")
        if not isinstance(overlay_text, str) or not isinstance(body_text, str):
            return None
        # An RG phrase inside a visible overlay = a real blocking lockout. The same phrase
        # only in page text (no overlay) = a non-blocking banner.
        for phrase in _RG_BLOCK_PHRASES:
            if phrase in overlay_text:
                return SessionBlock(phrase=phrase, is_overlay=True, kind="rg_lockout")
        for phrase in _RG_BLOCK_PHRASES:
            if phrase in body_text:
                return SessionBlock(phrase=phrase, is_overlay=False, kind="rg_lockout")
        # Session-expired popups (inactivity logout, server-side kill) — same overlay-vs-
        # banner classification, distinct kind so the alert can tell the operator to
        # re-login (vs wait out an RG break). Backstops the JWT-exp-only readiness probe
        # that can't see a server-side kill before the captured bearer lapses.
        for phrase in _SESSION_EXPIRED_PHRASES:
            if phrase in overlay_text:
                return SessionBlock(phrase=phrase, is_overlay=True, kind="session_expired")
        for phrase in _SESSION_EXPIRED_PHRASES:
            if phrase in body_text:
                return SessionBlock(phrase=phrase, is_overlay=False, kind="session_expired")
        # Session-timer warnings (Betano "Temporizador de sesión") — session still alive
        # but expiring, modal occludes the betting UI. Same overlay-vs-banner rule: a
        # visible overlay suspends (placement would fail behind it); body-only is a
        # non-blocking banner. Distinct kind so the alert can tell the operator to extend
        # the session (vs re-login or wait out an RG break).
        for phrase in _SESSION_TIMER_PHRASES:
            if phrase in overlay_text:
                return SessionBlock(phrase=phrase, is_overlay=True, kind="session_timer_warning")
        for phrase in _SESSION_TIMER_PHRASES:
            if phrase in body_text:
                return SessionBlock(phrase=phrase, is_overlay=False, kind="session_timer_warning")
        return None

    async def attempt_session_extend(self) -> bool:
        """Auto-extend a session-timer warning by clicking the platform's "conserve
        session" button (Betano's ``Sí, conservarlo``). Returns True iff the click
        landed AND the popup dismissed within ~1s; False otherwise (no platform mapping,
        no button, click fault, popup persisted). On False the manager falls back to
        the normal suspend + alert path.

        REAL BOOKMAKER INTERACTION — clicks a button in a logged-in betting window.
        Operator-authorized 2026-06-19 (AGENTS.md: explicit confirmation required). The
        risk surface is minimal: same action the operator takes manually, no bet placed,
        no account state modified. Logs every attempt for audit."""
        if self._dry_run or self._page is None:
            return False
        button_sel = _SESSION_EXTEND_BUTTON_SELECTORS.get(self._platform)
        if button_sel is None:
            return False  # no known extend button for this platform → fall back to alert
        try:
            async with self._page_lock:
                button = await self._page.query_selector(button_sel)
                if button is None:
                    return False  # popup already dismissed, or wrong selector
                self._log.info(
                    "transport.session_extend_click", platform=self._platform, selector=button_sel
                )
                await button.click()
                # Poll up to 1s for the button to disappear (proxy for popup dismissal).
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    if await self._page.query_selector(button_sel) is None:
                        return True
                self._log.warning(
                    "transport.session_extend_failed_popup_did_not_dismiss",
                    platform=self._platform, selector=button_sel,
                )
                return False
        except Exception as exc:  # noqa: BLE001 — extend must never crash the heartbeat
            self._log.warning("transport.session_extend_error", error=str(exc))
            return False

    async def keepalive(self) -> None:
        """Minimal inactivity avoidance — mouse move + scroll nudge + an occasional
        click on a non-interactive area of the viewport. Best-effort; never raises.

        REAL BOOKMAKER INTERACTION — synthetic user activity in a logged-in betting
        window. Operator-authorized 2026-06-19. The 2026-06-13 lab result established
        that mouse + scroll ALONE do not defeat BetWarrior's server-side inactivity
        detection (the SPA fires zero bearer-bearing requests in steady state, so the
        session times out without user action). This method adds OCCASIONAL CLICKS as
        the escalation; if clicks prove insufficient too, the next step is an active
        authenticated Kambi API call via ``page.evaluate`` (separate commit). For
        Betano + Betsson the heartbeat's authenticated readiness probes
        (``/api/balance``, ctx- nav) already register as server-side activity — this
        keepalive is primarily for BetWarrior, but applied uniformly because it's cheap
        and there's no reason to special-case."""
        if self._dry_run or self._page is None:
            return
        try:
            async with self._page_lock:
                # Human-like mouse move to a random viewport spot (4 intermediate steps
                # so the motion curve looks real, not a teleport).
                x = random.randint(150, 900)
                y = random.randint(150, 550)
                await self._page.mouse.move(x, y, steps=4)
                # Small scroll nudge and back — no net navigation, no scroll-position
                # disruption to whatever the operator may be doing in the window.
                await self._page.mouse.wheel(0, 120)
                await asyncio.sleep(0.3)
                await self._page.mouse.wheel(0, -120)
                # Occasional click on a non-interactive area. The element-from-point
                # check skips clicks that would land on a button/link/input/select —
                # we only want the activity signal, never to trigger a real action.
                if random.random() < 0.34:
                    cx = random.randint(200, 800)
                    cy = random.randint(200, 500)
                    tag = await self._page.evaluate(
                        "({x, y}) => {"
                        "  const e = document.elementFromPoint(x, y);"
                        "  return e ? e.tagName.toLowerCase() : '';"
                        "}",
                        {"x": cx, "y": cy},
                    )
                    if tag not in {"button", "a", "input", "select", "textarea"}:
                        await self._page.mouse.click(cx, cy, delay=50)
        except Exception as exc:  # noqa: BLE001 — keepalive must never crash the bot
            self._log.warning("transport.keepalive_error", platform=self._platform, error=str(exc))

    async def capture_block_evidence(self, reason: str) -> str | None:
        """On the FIRST detection of a session block, dump the page's visible text + any
        modal/overlay markup + a screenshot to ``recon/artifacts/rg_blocks/`` — so the
        real production lockout grounds the exact selector (the lab couldn't reproduce
        it idly). The saved ``dialogs`` list distinguishes a true blocking OVERLAY (non-
        empty) from a non-blocking BANNER (empty + a phrase hit, e.g. Betano's "12h
        descanso"). Returns the JSON artifact path, or ``None`` (dry-run / no page /
        fault). Never raises — evidence capture must not crash the heartbeat."""
        if self._dry_run or self._page is None:
            return None
        ts = int(time.time())
        base = _BLOCK_EVIDENCE_DIR / f"{self._platform}_{ts}"
        try:
            _BLOCK_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            async with self._page_lock:
                data = await self._page.evaluate(
                    """(sel) => ({
                        url: location.href,
                        text: (document.body && document.body.innerText || '').slice(0, 4000),
                        dialogs: [...document.querySelectorAll(sel)]
                            .filter(el => el.offsetParent !== null
                                && el.getBoundingClientRect().width > 0)
                            .map(el => ({tag: el.tagName, cls: String(el.className),
                                         html: el.outerHTML.slice(0, 8000)})),
                    })""",
                    _RG_BLOCK_DIALOG_SELECTOR,
                )
                with contextlib.suppress(Exception):
                    await self._page.screenshot(path=f"{base}.png", full_page=False)
            out = base.with_suffix(".json")
            out.write_text(
                json.dumps(
                    {"platform": self._platform, "reason": reason, "ts": ts, **data},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self._log.warning("transport.block_evidence_saved", path=str(out))
            return str(out)
        except Exception as exc:  # noqa: BLE001 — must never crash the heartbeat
            self._log.warning("transport.block_evidence_error", error=str(exc))
            return None

    async def check_betano_ready(self) -> bool:
        """Readiness probe: is the Betano session authorized? Hits the cookie-auth
        ``/api/balance`` endpoint — a logged-in session returns ``data.customerCode``;
        an expired one fails."""
        if self._dry_run:
            return False
        status, resp = await self._read_json(_BETANO_BALANCE_URL)
        data = resp.get("data")
        if not isinstance(data, dict):
            return False
        return status == 200 and bool(data.get("customerCode"))

    async def establish_betsson_context(self, timeout_s: int = 30) -> bool:
        """Drive the SPA into the placeable state and confirm the authenticated
        context resolved (returns True). Betsson's betting context lives in SPA
        memory and is established by *client-side* in-app navigation after login —
        a cold load alone leaves the betslip refusing — so we cold-load the app,
        then perform an in-app navigation (the SPA router runs; a hard reload would
        cold-boot and drop it), then verify via :meth:`prepare_betsson_context`.

        This is the automation of the operator's manual "My Account" click. The
        exact in-app nav trigger is operator-validated against the live DOM; the
        verify (a real ctx- request) is the source of truth either way. Returns
        False if the context never resolves (session not logged in / nav failed →
        the caller escalates to recovery)."""
        if self._dry_run:
            self._log.info("transport.dry_run_establish")
            return False
        self._captured_ctx = None
        # Hold the page lock only for the navigation (a placement fetch must not run
        # mid-nav); the passive ctx-poll below doesn't touch the page, so leave it
        # unlocked to keep the hold short.
        async with self._page_lock:
            await self._page.goto(_BETSSON_HOME, wait_until="networkidle", timeout=60000)
            # In-app (client-side) navigation: clicking an internal account link routes
            # via the SPA router (Playwright locators pierce shadow DOM). Best-effort —
            # if no candidate matches, we still verify (the session may already be live).
            for name in ("Mi cuenta", "My Account", "Mi Cuenta", "Cuenta"):
                link = self._page.get_by_role("link", name=name)
                try:
                    if await link.count():
                        await link.first.click(timeout=5000)
                        break
                except Exception as exc:  # noqa: BLE001 — try the next candidate
                    self._log.info("transport.betsson_nav_miss", name=name, error=str(exc))
        headers = await self.prepare_betsson_context(timeout_s)
        ok = headers is not None
        self._log.info("transport.betsson_context", established=ok)
        return ok

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if self._dry_run or not self._armed:
            raise TransportError(
                f"transport not armed for live send (dry_run={self._dry_run}, armed={self._armed})"
            )
        # A browser fetch with a body but no content-type defaults to text/plain →
        # JSON APIs answer 415. Default to application/json for JSON bodies (callers
        # can override).
        final_headers = dict(headers or {})
        if json_body is not None and not any(k.lower() == "content-type" for k in final_headers):
            final_headers["content-type"] = "application/json"
        # Execute the request inside the page context — inherits cookies/fingerprint.
        # The lock keeps a heartbeat re-navigation from aborting this in-flight fetch.
        async with self._page_lock:
            result = await self._page.evaluate(
                """async ({method, url, body, headers}) => {
                    const resp = await fetch(url, {
                        method, headers: headers || {},
                        body: body !== null ? JSON.stringify(body) : null,
                        credentials: 'include',
                    });
                    return {status: resp.status, text: await resp.text()};
                }""",
                {"method": method, "url": url, "body": json_body, "headers": final_headers},
            )
        status = int(result["status"])
        try:
            parsed = json.loads(result["text"]) if result["text"] else {}
        except ValueError as exc:
            raise TransportError(f"non-JSON response from {url}: {exc!s}") from exc
        return status, parsed if isinstance(parsed, dict) else {}
