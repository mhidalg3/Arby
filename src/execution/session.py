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
from src.credentials import get_credential

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

# Betsson reality-check popup (open shadow DOM). This is not a lockout and not a dead
# session: it is a responsible-gaming reminder that blocks the UI until the operator
# clicks the orange "Cerrar" button. Grounded from session-viewer captures
# 2026-06-22/23: h1.reality-check-question + p.reality-check-message.
_REALITY_CHECK_PHRASES: Final[tuple[str, ...]] = (
    "¿sabés qué hora es?",
    "el juego compulsivo es perjudicial para vos y tu familia",
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

# Betsson reality-check "Cerrar" has an observed cooldown after the popup appears/loads.
# Wait through that window before each bounded manager retry; otherwise a click can land
# on a disabled/no-op button. The manager bounds total attempts per popup episode.
_BETSSON_REALITY_CHECK_CLOSE_COOLDOWN_S: Final[float] = 5.25
# Max stale Betsson betslip selections cleared per heartbeat. A failed/moved direct-coupon
# placement can leave server-side slip residue (an `obg-m-betslip-selection` flagged
# `-error`); enough of them re-validating keeps the SPA off `networkidle` and wedges the
# heartbeat's goto (2026-06-24 incident). Cap so a pathological slip can't loop the clear.
_BETSSON_STALE_SLIP_MAX: Final[int] = 12

# BetWarrior can drift into the promotions SPA route (`/es-ar/promotions`, title/body
# "PROMOCIONES"). Kambi bearer liveness still passes there, but the sportsbook UI is not
# placeable. The safe recovery is the operator action: click the top-nav "INICIO".
_BETWARRIOR_PROMOTIONS_SCAN_JS = """() => {
    const body = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
    return {
        url: location.href,
        title: document.title || '',
        bodyText: body.slice(0, 2000),
    };
}"""

_BETWARRIOR_CLICK_INICIO_JS = """() => {
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) {
            return false;
        }
    };
    const els = Array.from(document.querySelectorAll('a,button,[role=link],[role=button]'));
    const target = els.find((el) => {
        const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        return vis(el) && text === 'inicio';
    });
    if (!target) return false;
    target.click();
    return true;
}"""

# BetWarrior auto re-auth (operator-authorized 2026-06-26). On a placement 401 / a
# session_expired popup the transport drives a full logout→login on its OWN window using
# keyring creds, so a server-killed Kambi session is replaced and its bearer re-captured.
# Login-form selectors are STABLE test IDs captured live 2026-06-26 on the public
# pba.betwarrior.bet.ar login modal; the session-expired CTA is from the recon DOM dump
# (SessionInactivityModal, 2026-06-19). Success keys on a FRESH bearer (≠ the pre-login
# token), NOT a balance element — a stale-but-valid-JWT bearer can fake the UI signal.
# See InSessionTransport.attempt_betwarrior_relogin.
_BETWARRIOR_RELOGIN_TIMEOUT_S: Final[float] = 45.0
# The home/soccer promo overlay (Popup__PopupContainerCss) intercepts the login trigger;
# dismiss its close icon first (fail-soft — absent on some routes). Captured 2026-06-26.
_BW_PROMO_DISMISS_SEL: Final[str] = "[class*='Popup__IconContainerCss']"
# Session-expired "INICIA SESIÓN AHORA" CTA (recon DOM dump 2026-06-19) — opens the same
# login form the header trigger does; preferred when the dead-session popup is up.
_BW_LOGIN_CTA_SEL: Final[str] = "[class*='SessionInactivityModal__CtaButtonCss']"
_BW_LOGIN_TRIGGER_SEL: Final[str] = "[data-testid='login-button']"
_BW_USER_SEL: Final[str] = "[data-testid='login-email']"
_BW_PASS_SEL: Final[str] = "[data-testid='login-password']"
_BW_SUBMIT_SEL: Final[str] = "[data-testid='login-submit-button']"
# Sportsbook home — navigating here after a successful login forces the Kambi widget to
# load and emit its session bearer (punter/session.json), which auth alone does NOT
# surface (validated live 2026-06-26: a manual sportsbook nav made the bot capture the
# bearer + flip "ready again"). The retry placement needs that bearer.
_BW_SPORTSBOOK_HOME: Final[str] = "https://pba.betwarrior.bet.ar/es-ar/sports/home"
# After the authenticated UI is reached + the sportsbook reloaded, wait this long for the
# fresh bearer. If it still hasn't arrived the session is live anyway (the sportsbook is
# active, so prepare_betwarrior_auth in the retry will capture it) — relogin returns True.
_BW_FRESH_BEARER_WAIT_S: Final[float] = 15.0
# "Is the logged-in account trigger visible?" — the reliable authenticated-UI signal (NOT
# login-button, a wrapper ancestor). querySelectorAll+some so a hidden clone (BetWarrior
# duplicates these test IDs) can't false-positive/negative.
_BW_USER_TRIGGER_VISIBLE_JS = (
    "() => [...document.querySelectorAll(\"[data-testid='user-trigger']\")]"
    ".some(e => { const r = e.getBoundingClientRect();"
    " return !!e.offsetParent && getComputedStyle(e).visibility !== 'hidden'"
    " && r.width > 0 && r.height > 0; })"
)
# Challenge markers (conservative + fail-safe): a challenged login (OTP/captcha) is
# escalated to the operator — relogin returns False → caller falls back to abort/naked.
# An undetected challenge shape just waits out the timeout → False (still safe).
_BW_CHALLENGE_JS = """() => ({
    recaptcha: !!document.querySelector(
        "iframe[src*='recaptcha'],.g-recaptcha,[data-sitekey]"
    ),
    otp: !!document.querySelector(
        "input[autocomplete*='one-time-code' i],input[name*='code' i]"
    )
})"""

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

# Betsson renders the sportsbook in open shadow DOM. The generic light-DOM block scan
# cannot see its reality-check popup. This bounded shadow walk detects the popup
# EXCLUSIVELY: it requires BOTH a grounded reality-check phrase AND the popup's own close
# button (fds-button[data-test-id^="reality-check"]), so responsible-gambling footer
# text on promo/bonus/API pages (ofertas.pba.betsson.bet.ar, 502 error pages, etc.) does
# NOT false-positive as a popup. Read-only.
_BETSSON_REALITY_CHECK_SCAN_JS = """() => {
    const PHRASES = [
        '¿sabés qué hora es?',
        'el juego compulsivo es perjudicial para vos y tu familia',
    ];
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) {
            return false;
        }
    };
    let visited = 0;
    let foundPhrase = '';
    let foundButton = false;
    const walk = (root, depth) => {
        if ((foundPhrase && foundButton) || !root || !root.querySelectorAll || depth > 12 || visited > 4000) return;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if ((foundPhrase && foundButton) || ++visited > 4000) return;
            if (vis(el)) {
                if (!foundPhrase) {
                    const text = String(el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const phrase = PHRASES.find((p) => text.includes(p));
                    if (phrase) foundPhrase = phrase;
                }
                if (!foundButton) {
                    const tag = el.tagName.toLowerCase();
                    const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
                    if (tag === 'fds-button' && testId.startsWith('reality-check')) foundButton = true;
                }
            }
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    return (foundPhrase && foundButton)
        ? {found: true, phrase: foundPhrase}
        : {found: false, phrase: ''};
}"""

# Same bounded shadow walk, but target-finding: within the SAME document/shadow root
# where one grounded Betsson reality-check phrase is visible, return the viewport center
# of the visible orange "Cerrar" control. Current live DOM (2026-06-27) is
# <fds-button data-test-id="reality-check-btn-1">Cerrar</fds-button>. The caller performs
# a real pointer click at the returned coordinates (custom elements may ignore
# synthetic DOM .click()). This is the operator-approved manual recovery and never
# submits a bet.
_BETSSON_REALITY_CHECK_CLOSE_JS = """() => {
    const PHRASES = [
        '¿sabés qué hora es?',
        'el juego compulsivo es perjudicial para vos y tu familia',
    ];
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) {
            return false;
        }
    };
    let visited = 0;
    const scanRoot = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > 12 || visited > 4000) return null;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return null; }
        let sawPhrase = false;
        const candidates = [];
        for (const el of els) {
            if (++visited > 4000) return null;
            const text = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
            const lower = text.toLowerCase();
            if (vis(el) && PHRASES.some((p) => lower.includes(p))) {
                sawPhrase = true;
            }
            const tag = el.tagName.toLowerCase();
            const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
            const grounded = tag === 'fds-button' && testId === 'reality-check-btn-1' && lower === 'cerrar';
            if (vis(el) && grounded) {
                const r = el.getBoundingClientRect();
                candidates.push({x: r.x + r.width / 2, y: r.y + r.height / 2, grounded});
            }
            if (el.shadowRoot) {
                const nested = scanRoot(el.shadowRoot, depth + 1);
                if (nested) return nested;
            }
        }
        if (!sawPhrase || !candidates.length) return null;
        const target = candidates.find((c) => c.grounded) || candidates[0];
        return [target.x, target.y];
    };
    return scanRoot(document, 0);
}"""

# Betsson betslip cleanup. A failed/moved direct-coupon placement leaves server-side slip
# residue in the sidebar betslip (site-drawer#drawer's shadow DOM): an
# `obg-m-betslip-selection-REFERENCE` (the sidebar variant; the bare `obg-m-betslip-selection`
# is a different view). We remove only the ones that are genuinely unusable (the selection is
# no longer available / suspended / market closed / event finished) and KEEP odds-changed ones
# ("Las cuotas han cambiado" — still bettable, operator may want to see them). The -REFERENCE
# element's class carries NO -error marker — the unavailability is in its text — so we classify
# by TEXT, conservatively (fail-safe: if it's neither clearly unavailable nor clearly
# odds-changed, leave it). Side-effecting shadow walk like the reality-check close; never
# submits a bet. Removes ONE per call so the caller can re-scan a fresh DOM between clicks.
_BETSSON_CLEAR_STALE_BETSLIP_JS = """() => {
    const UNAVAILABLE = /no disponible|no est[aá] disponible|suspend|finalizado|mercado cerrado|cerrad[oa]|no se puede apostar|resultado ya conocido|settled/i;
    const ODDS_CHANGED = /cuotas han cambiado|odds(?: have)? changed/i;
    const vis = (el) => {
        try { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    const findRemoveBtn = (sel) => {
        let btn = null;
        const w = (root, d) => {
            if (btn || d > 8) return;
            let els; try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const el of els) {
                if (el.tagName === 'OBG-M-BETSLIP-REMOVE-SELECTION-BUTTON') { btn = el; return; }
                if (el.shadowRoot) w(el.shadowRoot, d + 1);
            }
        };
        w(sel, 0);
        return btn;
    };
    let target = null;
    const walk = (root, depth) => {
        if (target || !root || !root.querySelectorAll || depth > 12) return;
        let els; try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if (target) return;
            if (vis(el) && /^OBG-M-BETSLIP-SELECTION(-REFERENCE)?$/.test(el.tagName)) {
                const text = String(el.innerText || '').toLowerCase();
                if (ODDS_CHANGED.test(text)) continue;     // keep odds-changed (still bettable)
                if (!UNAVAILABLE.test(text)) continue;      // keep ambiguous — fail-safe
                const btn = findRemoveBtn(el);
                if (btn && vis(btn)) { target = btn; return; }
            }
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    if (target) { try { target.click(); return { removed: 1 }; } catch (_) {} }
    return { removed: 0 };
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

    async def save_session(self) -> None:
        """Persist the live session (incl. the session-only cookies the persistent
        profile drops on close) to ``<profile>/<platform>-session.json`` so a later
        restart can re-inject it (see ``restore_session``). No-op in dry-run / before
        the context opens. Fail-soft — a save fault is logged, never raised (the
        heartbeat must never die on a persist failure)."""
        if self._dry_run or self._context is None:
            return
        try:
            from scripts.recon.recon import _session_state_path

            await self._context.storage_state(path=str(_session_state_path(self._platform)))
        except Exception as exc:  # noqa: BLE001 — persist must never break the loop
            self._log.warning(
                "transport.session_save_failed", platform=self._platform, error=str(exc)
            )

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
                res = await self._page.evaluate(_BLOCK_SCAN_JS, {"sel": _RG_BLOCK_DIALOG_SELECTOR})
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
        # Betsson's reality-check popup lives in open shadow DOM, so the light-DOM scan
        # above cannot see it. It is a closeable reminder, not a hard RG lockout, but it
        # occludes placement until dismissed.
        if self._platform == "betsson":
            try:
                async with self._page_lock:
                    reality = await self._page.evaluate(_BETSSON_REALITY_CHECK_SCAN_JS)
            except Exception as exc:  # noqa: BLE001 — a read must never crash the heartbeat
                self._log.warning("transport.reality_check_probe_error", error=str(exc))
                return None
            if isinstance(reality, dict) and reality.get("found") is True:
                raw_phrase = reality.get("phrase")
                phrase = (
                    raw_phrase if isinstance(raw_phrase, str) and raw_phrase else "reality-check"
                )
                return SessionBlock(phrase=phrase, is_overlay=True, kind="reality_check")
        # BetWarrior promotions route is a non-placeable SPA page. Bearer readiness can
        # still pass, so treat it as a blocking session state and let HotSessionManager
        # recover by clicking the top-nav INICIO.
        if self._platform == "betwarrior":
            try:
                async with self._page_lock:
                    promo = await self._page.evaluate(_BETWARRIOR_PROMOTIONS_SCAN_JS)
            except Exception as exc:  # noqa: BLE001 — a read must never crash the heartbeat
                self._log.warning("transport.promotions_probe_error", error=str(exc))
                return None
            if isinstance(promo, dict):
                url = promo.get("url")
                title = promo.get("title")
                text = promo.get("bodyText")
                haystack = " ".join(
                    part for part in (url, title, text) if isinstance(part, str)
                ).lower()
                if "/promotions" in haystack and "promociones" in haystack:
                    return SessionBlock(
                        phrase="promociones",
                        is_overlay=True,
                        kind="promotions_page",
                    )
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
                    platform=self._platform,
                    selector=button_sel,
                )
                return False
        except Exception as exc:  # noqa: BLE001 — extend must never crash the heartbeat
            self._log.warning("transport.session_extend_error", error=str(exc))
            return False

    async def attempt_reality_check_close(self) -> bool:
        """Dismiss Betsson's reality-check reminder by clicking its orange ``Cerrar``.

        REAL BOOKMAKER INTERACTION — clicks a close button in a logged-in betting
        window. Operator-authorized 2026-06-23 after the live viewer showed the popup
        blocking Betsson. The action only closes a responsible-gaming reminder; it never
        places, changes, or confirms a bet. Betsson keeps ``Cerrar`` in a ~5s cooldown
        after the popup appears, so wait through that window before each bounded manager
        retry. Returns True iff the click landed and the shadow-DOM reality marker disappeared.
        """
        if self._dry_run or self._page is None or self._platform != "betsson":
            return False
        try:
            async with self._page_lock:
                reality = await self._page.evaluate(_BETSSON_REALITY_CHECK_SCAN_JS)
                if not isinstance(reality, dict) or reality.get("found") is not True:
                    return False
                self._log.info(
                    "transport.reality_check_cooldown_wait",
                    platform=self._platform,
                    seconds=_BETSSON_REALITY_CHECK_CLOSE_COOLDOWN_S,
                )
                await asyncio.sleep(_BETSSON_REALITY_CHECK_CLOSE_COOLDOWN_S)
                target_xy = await self._page.evaluate(_BETSSON_REALITY_CHECK_CLOSE_JS)
                if not isinstance(target_xy, list) or len(target_xy) != 2:
                    self._log.warning(
                        "transport.reality_check_target_missing",
                        platform=self._platform,
                    )
                    return False
                await self._page.mouse.move(target_xy[0], target_xy[1], steps=4)
                await self._page.mouse.click(target_xy[0], target_xy[1])
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    reality = await self._page.evaluate(_BETSSON_REALITY_CHECK_SCAN_JS)
                    if not isinstance(reality, dict) or reality.get("found") is not True:
                        self._log.info("transport.reality_check_dismissed", platform=self._platform)
                        return True
                self._log.warning(
                    "transport.reality_check_dismiss_error",
                    platform=self._platform,
                    error="popup did not dismiss",
                )
                return False
        except Exception as exc:  # noqa: BLE001 — close must never crash the heartbeat
            self._log.warning(
                "transport.reality_check_dismiss_error",
                platform=self._platform,
                error=str(exc),
            )
            return False

    async def clear_stale_betslip(self) -> int:
        """Remove UNAVAILABLE Betsson betslip selections (suspended / market closed /
        event finished / "no disponible") left behind by failed or moved direct-coupon
        placements. Odds-changed selections ("Las cuotas han cambiado") are KEPT.

        REAL BOOKMAKER INTERACTION — clicks each stale selection's trashcan in a logged-in
        window. Operator-authorized 2026-06-24: stale residue keeps the SPA re-validating,
        which starves ``establish_betsson_context``'s ``networkidle`` goto (and wedged the
        heartbeat before the probe-bound fix). Never submits/changes a bet — only removes
        slip entries the user can't use anyway. Returns the count removed. Fail-soft: never
        raises (called from the heartbeat)."""
        if self._dry_run or self._page is None or self._platform != "betsson":
            return 0
        total = 0
        try:
            async with self._page_lock:
                for _ in range(_BETSSON_STALE_SLIP_MAX):
                    res = await self._page.evaluate(_BETSSON_CLEAR_STALE_BETSLIP_JS)
                    n = res.get("removed", 0) if isinstance(res, dict) else 0
                    if not n:
                        break
                    total += n
                    await asyncio.sleep(0.15)  # let the SPA process the removal before re-scan
            if total:
                self._log.info(
                    "transport.betsson_stale_betslip_cleared",
                    platform=self._platform,
                    removed=total,
                )
            return total
        except Exception as exc:  # noqa: BLE001 — cleanup must never crash the heartbeat
            self._log.warning(
                "transport.betsson_stale_betslip_clear_error",
                platform=self._platform,
                error=str(exc),
            )
            return total

    async def attempt_betwarrior_promotions_home(self) -> bool:
        """Leave BetWarrior's promotions page by clicking the top-nav ``INICIO``.

        REAL BOOKMAKER INTERACTION — clicks navigation in a logged-in betting window.
        Operator-authorized 2026-06-23 after live capture showed BetWarrior stuck on
        `/es-ar/promotions`. The action only navigates back to the sportsbook home; it
        never places, changes, or confirms a bet. Returns True iff the promotions marker
        disappears after the click.
        """
        if self._dry_run or self._page is None or self._platform != "betwarrior":
            return False
        try:
            async with self._page_lock:
                promo = await self._page.evaluate(_BETWARRIOR_PROMOTIONS_SCAN_JS)
                if not isinstance(promo, dict):
                    return False
                url = promo.get("url")
                text = promo.get("bodyText")
                haystack = " ".join(part for part in (url, text) if isinstance(part, str)).lower()
                if "/promotions" not in haystack or "promociones" not in haystack:
                    return False
                clicked = await self._page.evaluate(_BETWARRIOR_CLICK_INICIO_JS)
                if clicked is not True:
                    return False
                for _ in range(20):
                    await asyncio.sleep(0.25)
                    promo = await self._page.evaluate(_BETWARRIOR_PROMOTIONS_SCAN_JS)
                    if not isinstance(promo, dict):
                        self._log.info(
                            "transport.promotions_home_recovered", platform=self._platform
                        )
                        return True
                    url = promo.get("url")
                    text = promo.get("bodyText")
                    haystack = " ".join(
                        part for part in (url, text) if isinstance(part, str)
                    ).lower()
                    if "/promotions" not in haystack or "promociones" not in haystack:
                        self._log.info(
                            "transport.promotions_home_recovered", platform=self._platform
                        )
                        return True
                self._log.warning(
                    "transport.promotions_home_error",
                    platform=self._platform,
                    error="promotions page did not clear",
                )
                return False
        except Exception as exc:  # noqa: BLE001 — navigation recovery must not crash
            self._log.warning(
                "transport.promotions_home_error",
                platform=self._platform,
                error=str(exc),
            )
            return False

    async def attempt_betwarrior_relogin(self) -> bool:
        """Full logout→login on the BetWarrior window using keyring creds, so a
        server-killed Kambi session is replaced and its bearer re-captured.

        REAL BOOKMAKER INTERACTION — operator-authorized 2026-06-26 (the bot logs back
        into its own window on a placement 401 / session_expired). True iff a FRESH
        bearer (≠ the pre-login token) is captured within the timeout — the retry
        placement's proof the new session works. False on missing creds, an OTP/captcha
        challenge, or any error — the caller then falls back to abort/naked + alert, so a
        failed/challenged re-auth NEVER adds exposure. Fail-soft: never raises.

        The SPA keeps a stale (server-dead) session mounted that BLOCKS a fresh login
        until an explicit logout (operator-confirmed 2026-06-26), so this logs out first
        (account menu → 'Cerrar sesión', keyed on the account trigger disappearing), then
        opens the login form and submits keyring creds. The challenge detector is
        conservative (reCAPTCHA iframe / OTP input) and fail-safe."""
        if self._dry_run or self._page is None or self._platform != "betwarrior":
            return False
        try:
            async with self._page_lock:
                # Credential lookup is INSIDE the fail-soft try: a keyring fault must return
                # False (→ abort/naked), not escape and freeze the executor.
                cred = get_credential("betwarrior")
                if cred is None:
                    self._log.warning("transport.betwarrior_relogin_no_creds")
                    return False
                stale = self._captured_bearer
                # Invalidate the held bearer so the success gate can't be satisfied by a
                # re-delivered stale (server-dead) token; a fresh one must arrive from the
                # post-login SPA traffic (the _on_request listener).
                self._captured_bearer = None
                self._bearer_exp = None
                # Clear a persisted (server-dead) session FIRST: BetWarrior's SPA keeps a
                # stale session mounted after a server kill, which BLOCKS a fresh login
                # until an explicit logout (operator-confirmed 2026-06-26). Best-effort —
                # if it can't clear, the login step below is the real gate (a still-
                # persisted session → no fresh bearer → abort/naked, never adds exposure).
                if not await self._betwarrior_logout():
                    self._log.warning("transport.betwarrior_relogin_logout_failed")
                # Open the login form (dismiss the promo overlay first; after the logout
                # above the header login trigger is exposed). If the form still won't open,
                # reload once and retry (clears a wedged SPA state).
                if not await self._open_betwarrior_login_form():
                    await self._page.reload(wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(2.0)
                    if not await self._open_betwarrior_login_form():
                        self._log.warning("transport.betwarrior_relogin_no_form")
                        return False
                user = await self._page.query_selector(_BW_USER_SEL)
                pwd = await self._page.query_selector(_BW_PASS_SEL)
                submit = await self._page.query_selector(_BW_SUBMIT_SEL)
                if user is None or pwd is None or submit is None:
                    self._log.warning("transport.betwarrior_relogin_form_incomplete")
                    return False
                # Human-like pacing (anti-bot): type creds per-key with jittered timing
                # and a short think-pause before submit, instead of instant fills + a
                # dead-center click (the top behavioral tells — see recon/human.py).
                await user.fill("")  # clear autofill/prefill so type() sets, not appends
                await user.click()
                await user.type(cred.username, delay=random.randint(70, 190))
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await pwd.fill("")
                await pwd.click()
                await pwd.type(cred.password, delay=random.randint(70, 190))
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await submit.click()
                # Wait for the auth outcome: a challenge (OTP/captcha → False) OR the
                # authenticated UI (user-trigger visible) — NOT the bearer, which the SPA
                # does not emit on auth alone (validated 2026-06-26: a sportsbook nav is
                # what surfaces it). Then navigate to the sportsbook to emit a fresh bearer
                # for the retry placement.
                deadline = time.monotonic() + _BETWARRIOR_RELOGIN_TIMEOUT_S
                authed = False
                while time.monotonic() < deadline:
                    ch = await self._page.evaluate(_BW_CHALLENGE_JS)
                    if isinstance(ch, dict) and (ch.get("recaptcha") or ch.get("otp")):
                        self._log.warning("transport.betwarrior_relogin_challenged")
                        return False
                    if await self._page.evaluate(_BW_USER_TRIGGER_VISIBLE_JS):
                        authed = True
                        break
                    await asyncio.sleep(0.5)
                if not authed:
                    self._log.warning("transport.betwarrior_relogin_timeout")
                    return False
                # Authenticated UI reached — navigate to the sportsbook so the Kambi widget
                # loads + emits its session bearer (the retry placement needs it). The stale
                # bearer was cleared, so any captured now is fresh.
                with contextlib.suppress(Exception):
                    await asyncio.sleep(1.0)
                    await self._page.goto(
                        _BW_SPORTSBOOK_HOME, wait_until="domcontentloaded", timeout=45000
                    )
                bearer_deadline = time.monotonic() + _BW_FRESH_BEARER_WAIT_S
                while time.monotonic() < bearer_deadline:
                    if self._captured_bearer is not None and self._captured_bearer != stale:
                        self._log.info("transport.betwarrior_relogin_ok")
                        return True
                    await asyncio.sleep(0.5)
                # UI authed + sportsbook reloaded but no bearer captured — do NOT claim
                # success: the executor's retry would hit prepare_betwarrior_auth → None and
                # fail the same way. Return False (→ abort/naked); the restored session is
                # picked up by the next heartbeat (validated: a sportsbook nav surfaces the
                # bearer, so the next readiness probe flips ready again).
                self._log.warning("transport.betwarrior_relogin_no_bearer_after_nav")
                return False
        except Exception as exc:  # noqa: BLE001 — relogin must never crash the caller
            self._log.warning("transport.betwarrior_relogin_error", error=str(exc))
            return False

    async def _betwarrior_logout(self) -> bool:
        """Clear a persisted (server-dead) BetWarrior session so a fresh login is possible.

        The SPA keeps a stale session mounted after a server-side kill, which BLOCKS a
        fresh login until an explicit logout (operator-confirmed 2026-06-26). Opens the
        account menu ([data-testid='user-trigger']) and clicks the visible 'Cerrar sesión'.
        Returns True iff already logged out, or the logout landed AND the account trigger
        is no longer visible — the reliable logged-out signal (NOT login-button, which is a
        wrapper ancestor and proved misleading). Best-effort; must run under self._page_lock."""
        if not await self._page.evaluate(_BW_USER_TRIGGER_VISIBLE_JS):
            return True  # already logged out (e.g. session_expired popup already up)
        # Open the account menu + click "Cerrar sesión" with REAL pointer clicks (a
        # synthetic JS .click() does NOT open BetWarrior's React dropdown — operator-
        # observed 2026-06-26: the menu never opened, the session persisted, and the
        # relogin could not get a fresh login). Target each VISIBLE element's viewport
        # center (query_selector can return a hidden clone; BetWarrior duplicates these).
        trig_xy = await self._page.evaluate(
            "() => { const el=[...document.querySelectorAll(\"[data-testid='user-trigger']\")]"
            ".find(e=>{const r=e.getBoundingClientRect();return !!e.offsetParent"
            "&&getComputedStyle(e).visibility!=='hidden'&&r.width>0&&r.height>0;});"
            " if(!el) return null; el.scrollIntoView({block:'center'});"
            " const r=el.getBoundingClientRect(); return [r.x+r.width/2, r.y+r.height/2]; }"
        )
        if not isinstance(trig_xy, list) or len(trig_xy) != 2:
            self._log.warning("transport.betwarrior_relogin_logout_no_trigger")
            return False
        with contextlib.suppress(Exception):
            await self._page.mouse.move(trig_xy[0], trig_xy[1], steps=4)
            await self._page.mouse.click(trig_xy[0], trig_xy[1])
            await asyncio.sleep(0.9)
        # Click the visible logout ICON (now in the open menu) with a real pointer. The
        # logout control is an icon (class ...icon-logout) — NO text, NO testid
        # (operator-inspected 2026-06-26) — so target its stable icon class, not "cerrar
        # sesión" text (which never matched, so the logout never fired → session persisted).
        cerrar_xy = await self._page.evaluate(
            "() => { const el=[...document.querySelectorAll(\"[class*='icon-logout']\")]"
            ".find(e=>{const r=e.getBoundingClientRect();return !!e.offsetParent"
            "&&getComputedStyle(e).visibility!=='hidden'&&r.width>0&&r.height>0;});"
            " if(!el) return null; const r=el.getBoundingClientRect();"
            " return [r.x+r.width/2, r.y+r.height/2]; }"
        )
        if not isinstance(cerrar_xy, list) or len(cerrar_xy) != 2:
            self._log.warning("transport.betwarrior_relogin_logout_no_cerrar")
            return False  # menu didn't open / no visible logout icon
        with contextlib.suppress(Exception):
            await self._page.mouse.move(cerrar_xy[0], cerrar_xy[1], steps=4)
            await self._page.mouse.click(cerrar_xy[0], cerrar_xy[1])
        for _ in range(10):  # up to ~5s for the account trigger to disappear
            await asyncio.sleep(0.5)
            if not await self._page.evaluate(_BW_USER_TRIGGER_VISIBLE_JS):
                return True
        self._log.warning("transport.betwarrior_relogin_logout_persisted")
        return False  # account trigger still visible → not confidently logged out

    async def _open_betwarrior_login_form(self) -> bool:
        """Dismiss BetWarrior's promo overlay, then click whichever control opens the
        login form — the session-expired 'INICIA SESIÓN AHORA' CTA if the dead-session
        popup is up, else the header login trigger. True iff the email input appears
        within ~8s. Must run under ``self._page_lock``."""
        # The promo overlay intercepts the login trigger (captured 2026-06-26); dismiss
        # its close icon fail-soft (absent on some routes).
        promo = await self._page.query_selector(_BW_PROMO_DISMISS_SEL)
        if promo is not None:
            with contextlib.suppress(Exception):
                await asyncio.sleep(random.uniform(0.3, 0.9))
                await promo.click()
                await asyncio.sleep(0.4)
        cta = await self._page.query_selector(_BW_LOGIN_CTA_SEL)
        target = cta if cta is not None else await self._page.query_selector(_BW_LOGIN_TRIGGER_SEL)
        if target is None:
            return False
        with contextlib.suppress(Exception):
            await asyncio.sleep(random.uniform(0.3, 0.9))
            await target.click()
        for _ in range(16):  # up to ~8s for the login modal to render
            await asyncio.sleep(0.5)
            if await self._page.query_selector(_BW_USER_SEL) is not None:
                return True
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
                # Occasional click on a truly inert area. BetWarrior promo banners/cards
                # can be clickable DIVs, so tag-name-only checks are insufficient and can
                # drift the SPA into /promotions. Treat pointer cursors, onclick handlers,
                # and interactive ancestors as unsafe.
                if random.random() < 0.34:
                    cx = random.randint(200, 800)
                    cy = random.randint(200, 500)
                    unsafe = await self._page.evaluate(
                        """({x, y}) => {
                            let e = document.elementFromPoint(x, y);
                            for (let i = 0; e && i < 8; i++, e = e.parentElement) {
                                const tag = e.tagName.toLowerCase();
                                const role = (e.getAttribute('role') || '').toLowerCase();
                                const cs = getComputedStyle(e);
                                if (['button', 'a', 'input', 'select', 'textarea'].includes(tag)) return true;
                                if (role === 'button' || role === 'link') return true;
                                if (e.onclick || cs.cursor === 'pointer') return true;
                            }
                            return false;
                        }""",
                        {"x": cx, "y": cy},
                    )
                    if unsafe is not True:
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
        # If Betsson's reality-check popup is up, do NOT reload the page. A goto cannot
        # dismiss it (only the orange "Cerrar" / fds-button[data-test-id=reality-check-
        # btn-1] does), and reloading every heartbeat fights the close recovery and can
        # re-trigger the SPA after a successful close. Return not-ready; the manager
        # detects the block separately via check_session_blocked() and runs
        # attempt_reality_check_close() to click Cerrar. (Non-reality-check blocks — RG
        # lockout, session_expired, etc. — fall through to the goto as before.)
        block = await self.check_session_blocked()
        if block is not None and block.is_overlay and block.kind == "reality_check":
            self._log.info("transport.betsson_context_skipped_reality_check")
            return False
        self._captured_ctx = None
        # Clear stale (unavailable) betslip residue BEFORE the goto: enough error-state
        # selections re-validating starves the networkidle wait below (root cause of the
        # 2026-06-24 heartbeat wedge). clear_stale_betslip takes its own page lock.
        await self.clear_stale_betslip()
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
