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
# Betsson relogin failure artifacts. These are produced by the relogin code itself
# (same window, same page lock already held by the caller) when the live login popup
# does not match our selector contract. They intentionally store selector metadata,
# not full page HTML, so credentials are not persisted in debug JSON.
_BETSSON_RELOGIN_EVIDENCE_DIR = REPO_ROOT / "recon" / "artifacts" / "betsson_relogin"

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

_BETSSON_SHADOW_WALK_DEPTH: Final[int] = 16
_BETSSON_SHADOW_WALK_LIMIT: Final[int] = 10000
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

# Tiered close-target finder for Betsson's reality-check popup. The 2026-06-27 exact
# match (fds-button[data-test-id="reality-check-btn-1"] + host innerText "cerrar")
# went stale 2026-07-08: the scan still classified the popup (prefix testId, global
# walk) while this finder returned null — popup stuck, session expired server-side
# ~52min later, destructive relogin ran. Rules now mirror the scan's tolerance and
# can never click a logout control:
#   tier 1: visible fds-button/fdsp-button/button, data-test-id starting
#           "reality-check", label "cerrar" (label read from the innermost visible
#           shadow <button>, falling back to the host — non-slotted shadow labels
#           are invisible to host innerText);
#   tier 2: the SINGLE guard-passing prefix candidate, any/empty label;
#   tier 3: exactly ONE visible button labelled "cerrar" page-wide (testId scheme
#           changed entirely). Ambiguity at any tier -> no click + candidates
#           returned as evidence.
#   guard:  a label containing "sesión"/"sesion"/"desconect" is rejected everywhere
#           (that is the logout control, never the popup close).
# Phrase and button use GLOBAL flags across the whole walk (the old per-root
# conjunction broke when they stopped sharing a shadow root). The caller performs a
# real pointer click at the returned center (custom elements ignore synthetic
# .click()). Operator-approved manual recovery; never submits a bet.
_BETSSON_REALITY_CHECK_CLOSE_JS = """() => {
    // BETSSON_REALITY_CHECK_CLOSE
    const PHRASES = [
        '¿sabés qué hora es?',
        'el juego compulsivo es perjudicial para vos y tu familia',
    ];
    const BAD = ['sesión', 'sesion', 'desconect'];
    const TAGS = ['fds-button', 'fdsp-button', 'button'];
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    const resolveButton = (el) => {
        if ((el.tagName || '').toLowerCase() === 'button') return el;
        let dug = 0;
        let inner = null;
        const dig = (root, d) => {
            if (!root || !root.querySelectorAll || d > __DEPTH__ || dug > 2000 || inner) return;
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const c of els) {
                if (++dug > 2000 || inner) return;
                if (vis(c) && (c.tagName || '').toLowerCase() === 'button') { inner = c; return; }
                if (c.shadowRoot) dig(c.shadowRoot, d + 1);
            }
        };
        if (el.shadowRoot) dig(el.shadowRoot, 1);
        if (!inner) dig(el, 1);
        return inner || el;
    };
    const labelOf = (el) => {
        const t = resolveButton(el);
        const txt = String(t.innerText || t.getAttribute('aria-label') || el.innerText || el.getAttribute('aria-label') || '');
        return txt.replace(/\\s+/g, ' ').trim().toLowerCase();
    };
    let visited = 0;
    let sawPhrase = false;
    const prefixHosts = [];
    const cerrarHosts = [];
    const walk = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if (++visited > __LIMIT__) return;
            if (vis(el)) {
                if (!sawPhrase) {
                    const text = String(el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (PHRASES.some((p) => text.includes(p))) sawPhrase = true;
                }
                const tag = el.tagName.toLowerCase();
                if (TAGS.includes(tag)) {
                    const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
                    const label = labelOf(el);
                    if (testId.startsWith('reality-check')) {
                        prefixHosts.push({el, tag, testId, label});
                    } else if (label === 'cerrar') {
                        cerrarHosts.push({el, tag, testId, label});
                    }
                }
            }
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    // A host and its inner native <button> can both be collected for the same
    // physical control; dedupe by resolved click target so counts stay truthful.
    const dedupe = (arr) => {
        const seen = new Set();
        return arr.filter((c) => {
            const t = resolveButton(c.el);
            if (seen.has(t)) return false;
            seen.add(t);
            return true;
        });
    };
    const meta = (c) => ({tag: c.tag, testId: c.testId, label: c.label});
    const prefix = dedupe(prefixHosts);
    if (!sawPhrase) return {found: false, reason: 'no_phrase', candidates: prefix.map(meta)};
    const ok = (c) => !BAD.some((b) => c.label.includes(b));
    const safe = prefix.filter(ok);
    let tier = 1;
    // Tier 1 requires a SINGLE cerrar among guard-passing prefix hosts: two visible
    // cerrar controls is ambiguous (which is the real close button?), so fall through
    // to evidence rather than silently clicking the first match.
    const cerrarSafe = safe.filter((c) => c.label === 'cerrar');
    let pick = cerrarSafe.length === 1 ? cerrarSafe[0] : null;
    if (!pick && safe.length === 1) { tier = 2; pick = safe[0]; }
    if (!pick) {
        // Tier 3 = PAGE-WIDE uniqueness: count prefix AND non-prefix cerrar together,
        // deduped as one set. cerrarHosts alone excludes prefix buttons, so it would let
        // an unrelated non-prefix cerrar sneak a click through after prefix ambiguity.
        const wide = dedupe(cerrarSafe.concat(cerrarHosts)).filter(ok);
        if (wide.length === 1) { tier = 3; pick = wide[0]; }
    }
    if (!pick) {
        return {found: false, reason: 'no_candidate',
                candidates: prefix.concat(dedupe(cerrarHosts)).slice(0, 8).map(meta)};
    }
    const t = resolveButton(pick.el);
    try { t.scrollIntoView({block: 'center'}); } catch (_) {}
    const r = t.getBoundingClientRect();
    return {found: true, x: r.x + r.width / 2, y: r.y + r.height / 2,
            tier, tag: pick.tag, testId: pick.testId, label: pick.label};
}""".replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH)).replace(
    "__LIMIT__", str(_BETSSON_SHADOW_WALK_LIMIT)
)

# Betsson auth state — header logged-in/logged-out markers + session-expired popup
# phrases, all read via a bounded open-shadow walk (Betsson renders its ENTIRE SPA in
# open shadow DOM; the generic light-DOM _BLOCK_SCAN_JS is blind to its header + popups).
# Grounded selectors (read-only recon 2026-07-02 + operator-provided DOM facts):
#   logged-in  = visible [data-test-id="balance-button"] (account-menu trigger, absent
#                when logged out) AND visible "Retirar"/"Depósito" text in the header
#                band. The text marker is scoped to the top of the viewport so body /
#                promo copy can't false-green loggedIn; balance-button is already unique
#                to logged-in, and the text conjunction makes the criterion redundant-
#                safe rather than fragile.
#   logged-out = no logged-in markers AND the login trigger visible — the POSITIVE
#                logged-out signal is required (absence alone false-positives mid-render).
#   expired    = first _SESSION_EXPIRED_PHRASES hit in ANY visible text, light or
#                shadow DOM (the inactivity-logout popup the light-DOM phrase scan
#                cannot see). DELIBERATELY broad — the popup can sit over a still-
#                mounted session (balance visible), which drives the relogin's logout
#                leg. The breadth is safe ONLY behind _betsson_auth_settled's debounce:
#                a hit must persist across every settle sample before it is believed.
# The phrase list is injected from the Python tuple (single source of truth, no copy).
_BETSSON_AUTH_SCAN_JS = """() => {
    const PHRASES = __SESSION_EXPIRED_PHRASES__;
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    const headerBand = window.innerHeight / 5;  // top 20% of viewport = header zone
    let visited = 0;
    let hasBalance = false;
    let hasHeaderMarker = false;
    let hasLoginTrigger = false;
    let expiredPhrase = '';
    const walk = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > 12 || visited > 4000) return;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if (++visited > 4000) return;
            if (vis(el)) {
                const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
                if (!hasBalance && testId === 'balance-button') hasBalance = true;
                if (!hasLoginTrigger && testId === 'login-button') hasLoginTrigger = true;
                if (!hasHeaderMarker) {
                    const r = el.getBoundingClientRect();
                    if (r.top < headerBand) {
                        const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        if (/retirar|dep[oó]sito/.test(t)) hasHeaderMarker = true;
                    }
                }
                if (!expiredPhrase) {
                    const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const p = PHRASES.find((ph) => t.includes(ph));
                    if (p) expiredPhrase = p;
                }
            }
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    const loggedIn = hasBalance && hasHeaderMarker;
    return {
        loggedIn,
        hasBalance,
        loggedOut: !loggedIn && !hasBalance && hasLoginTrigger,
        expiredPhrase,
    };
}""".replace("__SESSION_EXPIRED_PHRASES__", json.dumps(_SESSION_EXPIRED_PHRASES))

# Debounce for the auth scan. Betsson's SPA renders the header login trigger BEFORE
# session hydration completes (observed post-goto and while settling after a reality-
# check Cerrar), so ONE sample can read a healthy session as logged out — and one such
# sample scheduled the relogin that logged a healthy session out for real (2026-07-05
# reality-check regression). A bad state (logged-out header / session-expired phrase)
# must persist across EVERY sample before it is believed; a healthy or ambiguous sample
# short-circuits, so the happy path stays one evaluate. Worst case adds ~4s to a GENUINE
# logout detection — negligible against the 300s heartbeat and the 10s recheck timeout.
_BETSSON_AUTH_SETTLE_SAMPLES: Final[int] = 3
_BETSSON_AUTH_SETTLE_GAP_S: Final[float] = 2.0

# Betsson auto re-auth (operator-authorized 2026-07-02). On a session_expired block / a
# placement 401 the transport drives a full logout→login on its OWN window using keyring
# creds, so a server-killed session is replaced and its ctx- re-captured. Betsson renders
# its ENTIRE SPA in open shadow DOM. The login trigger first uses Playwright's
# shadow-piercing locator click with a short timeout (mirrors the validated BetWarrior
# actionability-click opener), then falls back to the center-finder + real pointer path
# used for account menu, logout item, geolocation CTA, email/password inputs, and submit.
# Text is typed via page.keyboard into the real-
# pointer-focused input. See attempt_betsson_relogin.
_BETSSON_RELOGIN_TIMEOUT_S: Final[float] = 45.0
# Grounded selectors (read-only recon 2026-07-02 + live CDP triage 2026-07-03 +
# operator DOM facts). The email/password controls are the real login-popup inputs
# captured from the logged-out Betsson window; the geolocation selectors cover Betsson's
# app-level location interstitial that can consume the first login-trigger click.
_BETSSON_LOGIN_TRIGGER_SEL: Final[str] = "[data-test-id='login-button']"
_BETSSON_BALANCE_BUTTON_SEL: Final[str] = "[data-test-id='balance-button']"
_BETSSON_LOGOUT_SEL: Final[str] = "[data-test-id='site-menu-link-anchor-cerrar-sesión']"
_BETSSON_LOGIN_SUBMIT_SEL: Final[str] = "[data-test-id='account-login-btn-1']"
_BETSSON_GEOLOCATION_CONTAINER_SEL: Final[str] = "[data-test-id='geolocation-container']"
_BETSSON_GEOLOCATION_CTA_SEL: Final[str] = "[data-test-id='geolocation-cta-content-btn-1']"
_BETSSON_LOGIN_EMAIL_SEL: Final[str] = "[data-test-id='email-input']"
_BETSSON_LOGIN_PASSWORD_SEL: Final[str] = "[data-test-id='password-input']"
_BETSSON_LOGIN_INPUT_CONTAINER_SEL: Final[str] = "[data-test-id='input-container']"
# The header login trigger's visible label (operator DOM fact 2026-07-06). Used by the
# trigger finder as the fallback truth when the login-button wrapper testId is missing;
# compared normalized: lowercase, whitespace-collapsed.
_BETSSON_LOGIN_TRIGGER_LABEL: Final[str] = "iniciar sesión"
_BETSSON_LOGIN_CLICK_TIMEOUT_MS: Final[float] = 1500.0
_BETSSON_LOGIN_CENTER_FALLBACK_POLLS: Final[int] = 4
# Shadow-piercing target finders: the viewport center [x, y] of a VISIBLE element
# matching `sel` anywhere in light or open-shadow DOM, else null. The caller performs a
# REAL pointer click at the returned coords (Betsson custom elements may ignore a
# synthetic JS .click() — same lesson as BetWarrior's React dropdown, session.py:1167).
_BETSSON_FIND_VISIBLE_CENTER_JS = """(sel) => {
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    let visited = 0;
    const find = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return null;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return null; }
        for (const el of els) {
            if (++visited > __LIMIT__) return null;
            try {
                if (vis(el) && el.matches(sel)) {
                    el.scrollIntoView({ block: 'center' });
                    const r = el.getBoundingClientRect();
                    return [r.x + r.width / 2, r.y + r.height / 2];
                }
            } catch (_) {}
            if (el.shadowRoot) {
                const hit = find(el.shadowRoot, depth + 1);
                if (hit) return hit;
            }
        }
        return null;
    };
    return find(document, 0);
}""".replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH)).replace(
    "__LIMIT__", str(_BETSSON_SHADOW_WALK_LIMIT)
)

_BETSSON_FIND_BOTTOM_VISIBLE_CENTER_JS = """(sel) => {
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    let visited = 0;
    let best = null;
    const walk = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if (++visited > __LIMIT__) return;
            try {
                if (vis(el) && el.matches(sel)) {
                    const r = el.getBoundingClientRect();
                    const docBottom = r.bottom + window.scrollY;
                    if (best === null || docBottom > best.docBottom) best = { el, docBottom };
                }
            } catch (_) {}
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    if (best === null) return null;
    best.el.scrollIntoView({ block: 'center' });
    const r = best.el.getBoundingClientRect();
    return [r.x + r.width / 2, r.y + r.height / 2];
}""".replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH)).replace(
    "__LIMIT__", str(_BETSSON_SHADOW_WALK_LIMIT)
)

# Clickable-center finder: first VISIBLE match of the selector, resolved to its innermost
# visible shadow <button> before measuring. Betsson fds/fdsp hosts and the login-button
# wrapper render the real control inside an open shadow root; clicking the host/wrapper
# center can land outside it (live evidence 2026-07-05: the wrapper center hit
# site-header-version-manager). Buttons only — input finders are deliberately untouched.
_BETSSON_FIND_CLICKABLE_CENTER_JS = """(target) => {
    // BETSSON_CLICKABLE_CENTER
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    const resolveButton = (el) => {
        if ((el.tagName || '').toLowerCase() === 'button') return el;
        let dug = 0;
        let inner = null;
        const dig = (root, d) => {
            if (!root || !root.querySelectorAll || d > __DEPTH__ || dug > 2000 || inner) return;
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const c of els) {
                if (++dug > 2000 || inner) return;
                if (vis(c) && (c.tagName || '').toLowerCase() === 'button') { inner = c; return; }
                if (c.shadowRoot) dig(c.shadowRoot, d + 1);
            }
        };
        if (el.shadowRoot) dig(el.shadowRoot, 1);
        if (!inner) dig(el, 1);
        return inner || el;
    };
    const center = (el) => {
        const t = resolveButton(el);
        t.scrollIntoView({ block: 'center' });
        const r = t.getBoundingClientRect();
        return [r.x + r.width / 2, r.y + r.height / 2];
    };
    let visited = 0;
    const find = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return null;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return null; }
        for (const el of els) {
            if (++visited > __LIMIT__) return null;
            try { if (vis(el) && el.matches(target)) return center(el); } catch (_) {}
            if (el.shadowRoot) {
                const hit = find(el.shadowRoot, depth + 1);
                if (hit) return hit;
            }
        }
        return null;
    };
    return find(document, 0);
}""".replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH)).replace(
    "__LIMIT__", str(_BETSSON_SHADOW_WALK_LIMIT)
)

# Login-trigger finder (no args): the login-button wrapper resolved to its inner button;
# if the wrapper testId is gone, fall back to a visible fdsp-button labelled
# "iniciar sesión" in the header band (top 20% of the viewport — same band as the auth
# scan). fdsp-button host tag only: the popup submit shares the label but is fds-button.
_BETSSON_FIND_LOGIN_TRIGGER_CENTER_JS = (
    """() => {
    // BETSSON_LOGIN_TRIGGER_FINDER
    const SEL = __TRIGGER_SEL__;
    const LABEL = __TRIGGER_LABEL__;
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    const resolveButton = (el) => {
        if ((el.tagName || '').toLowerCase() === 'button') return el;
        let dug = 0;
        let inner = null;
        const dig = (root, d) => {
            if (!root || !root.querySelectorAll || d > __DEPTH__ || dug > 2000 || inner) return;
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const c of els) {
                if (++dug > 2000 || inner) return;
                if (vis(c) && (c.tagName || '').toLowerCase() === 'button') { inner = c; return; }
                if (c.shadowRoot) dig(c.shadowRoot, d + 1);
            }
        };
        if (el.shadowRoot) dig(el.shadowRoot, 1);
        if (!inner) dig(el, 1);
        return inner || el;
    };
    const center = (el) => {
        const t = resolveButton(el);
        t.scrollIntoView({ block: 'center' });
        const r = t.getBoundingClientRect();
        return [r.x + r.width / 2, r.y + r.height / 2];
    };
    let visited = 0;
    const find = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return null;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return null; }
        for (const el of els) {
            if (++visited > __LIMIT__) return null;
            try { if (vis(el) && el.matches(SEL)) return center(el); } catch (_) {}
            if (el.shadowRoot) {
                const hit = find(el.shadowRoot, depth + 1);
                if (hit) return hit;
            }
        }
        return null;
    };
    const hit = find(document, 0);
    if (hit) return hit;
    const band = window.innerHeight / 5;
    visited = 0;
    let labelled = null;
    const scan = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__ || labelled) return;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if (++visited > __LIMIT__ || labelled) return;
            try {
                if (vis(el) && (el.tagName || '').toLowerCase() === 'fdsp-button') {
                    const r = el.getBoundingClientRect();
                    const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (r.top < band && t === LABEL) { labelled = el; return; }
                }
            } catch (_) {}
            if (el.shadowRoot) scan(el.shadowRoot, depth + 1);
        }
    };
    scan(document, 0);
    return labelled ? center(labelled) : null;
}""".replace("__TRIGGER_SEL__", json.dumps(_BETSSON_LOGIN_TRIGGER_SEL))
    .replace("__TRIGGER_LABEL__", json.dumps(_BETSSON_LOGIN_TRIGGER_LABEL))
    .replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH))
    .replace("__LIMIT__", str(_BETSSON_SHADOW_WALK_LIMIT))
)

# BETSSON_RELOGIN_EVIDENCE: lock-free selector-state snapshot for relogin failures.
# No full HTML and no input values: artifacts may be created after credentials were typed.
_BETSSON_RELOGIN_EVIDENCE_JS = """(selectors) => {
    // BETSSON_RELOGIN_EVIDENCE
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    const textOf = (el) => {
        try {
            if (el.tagName && el.tagName.toLowerCase() === 'input') return '';
            return String(el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
        } catch (_) { return ''; }
    };
    const hitAt = (x, y) => {
        try {
            const el = document.elementFromPoint(x, y);
            if (!el) return null;
            return {
                tag: String(el.tagName || '').toLowerCase(),
                testId: el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '',
                role: el.getAttribute('role') || '',
                aria: el.getAttribute('aria-label') || '',
                point: [x, y],
            };
        } catch (_) { return null; }
    };
    const describe = (el) => {
        const r = el.getBoundingClientRect();
        const x = r.x + r.width / 2;
        const y = r.y + r.height / 2;
        return {
            visible: vis(el),
            center: [x, y],
            pointTarget: hitAt(x, y),
            tag: String(el.tagName || '').toLowerCase(),
            testId: el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '',
            type: el.getAttribute('type') || '',
            name: el.getAttribute('name') || '',
            placeholder: el.getAttribute('placeholder') || '',
            aria: el.getAttribute('aria-label') || '',
            text: textOf(el),
            matchCount: 0,
        };
    };
    const inputTarget = (container) => {
        let inputVisited = 0;
        let bottomInput = null;
        const inputWalk = (root, depth) => {
            if (!root || !root.querySelectorAll || depth > __DEPTH__ || inputVisited > __LIMIT__) return;
            let inputs;
            try { inputs = Array.from(root.querySelectorAll('input,textarea,[contenteditable="true"]')); } catch (_) { inputs = []; }
            for (const input of inputs) {
                if (++inputVisited > __LIMIT__) return;
                try {
                    if (vis(input)) {
                        const r = input.getBoundingClientRect();
                        const docBottom = r.bottom + window.scrollY;
                        if (bottomInput === null || docBottom > bottomInput.docBottom) bottomInput = { el: input, docBottom };
                    }
                } catch (_) {}
            }
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const el of els) {
                if (++inputVisited > __LIMIT__) return;
                if (el.shadowRoot) inputWalk(el.shadowRoot, depth + 1);
            }
        };
        inputWalk(container, 0);
        if (container.shadowRoot) inputWalk(container.shadowRoot, 1);
        return bottomInput ? bottomInput.el : null;
    };
    const resolveButton = (el) => {
        if ((el.tagName || '').toLowerCase() === 'button') return el;
        let dug = 0;
        let inner = null;
        const dig = (root, d) => {
            if (!root || !root.querySelectorAll || d > __DEPTH__ || dug > 2000 || inner) return;
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const c of els) {
                if (++dug > 2000 || inner) return;
                if (vis(c) && (c.tagName || '').toLowerCase() === 'button') { inner = c; return; }
                if (c.shadowRoot) dig(c.shadowRoot, d + 1);
            }
        };
        if (el.shadowRoot) dig(el.shadowRoot, 1);
        if (!inner) dig(el, 1);
        return inner || el;
    };
    const first = (sel, resolve) => {
        let visited = 0;
        let hidden = null;
        let count = 0;
        const walk = (root, depth) => {
            if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return null;
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return null; }
            for (const el of els) {
                if (++visited > __LIMIT__) return null;
                try {
                    if (el.matches(sel)) {
                        count += 1;
                        const visibleEl = vis(el);
                        let target = el;
                        if (resolve && visibleEl) {
                            const r = resolveButton(el);
                            if (r && r !== el) {
                                target = r;
                                target.scrollIntoView({ block: 'center' });
                            }
                        }
                        const described = describe(target);
                        if (visibleEl) return described;
                        if (hidden === null) hidden = described;
                    }
                } catch (_) {}
                if (el.shadowRoot) {
                    const hit = walk(el.shadowRoot, depth + 1);
                    if (hit) return hit;
                }
            }
            return null;
        };
        const visible = walk(document, 0);
        const match = visible || hidden;
        if (match) match.matchCount = count;
        return match;
    };
    const bottom = (sel) => {
        let visited = 0;
        let best = null;
        let count = 0;
        const walk = (root, depth) => {
            if (!root || !root.querySelectorAll || depth > __DEPTH__ || visited > __LIMIT__) return;
            let els;
            try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
            for (const el of els) {
                if (++visited > __LIMIT__) return;
                try {
                    if (el.matches(sel)) {
                        count += 1;
                        if (vis(el)) {
                            const r = el.getBoundingClientRect();
                            const docBottom = r.bottom + window.scrollY;
                            if (best === null || docBottom > best.docBottom) {
                                best = { el, docBottom };
                            }
                        }
                    }
                } catch (_) {}
                if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
            }
        };
        walk(document, 0);
        if (best === null) return null;
        const target = inputTarget(best.el) || best.el;
        target.scrollIntoView({ block: 'center' });
        const described = describe(target);
        described.matchCount = count;
        if (target !== best.el) {
            described.descendantInput = true;
            described.container = describe(best.el);
        }
        return described;
    };
    const RESOLVE_CLICK = { trigger: true, submit: true, geolocation: true };
    const choose = (spec, name) => {
        const resolve = !!RESOLVE_CLICK[name];
        if (typeof spec === 'string') return first(spec, resolve);
        if (!spec || typeof spec !== 'object') return null;
        const primary = first(spec.primary, resolve);
        if (primary && primary.visible) return primary;
        const fallback = bottom(spec.fallback);
        if (fallback) fallback.fallback = true;
        return fallback || primary;
    };
    const found = {};
    for (const [name, spec] of Object.entries(selectors)) found[name] = choose(spec, name);
    return {
        url: location.href,
        title: document.title,
        selectors: found,
    };
}""".replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH)).replace(
    "__LIMIT__", str(_BETSSON_SHADOW_WALK_LIMIT)
)

_BETSSON_PASSWORD_TRACE_JS = """(cfg) => {
    // BETSSON_PASSWORD_TRACE: one-shot, sanitized password-step telemetry.
    const action = cfg && cfg.action ? String(cfg.action) : 'snapshot';
    const point = cfg && Array.isArray(cfg.point) ? cfg.point : null;
    const KEY = '__arbyBetssonPasswordProbe';
    const state = window[KEY] || { events: [], listeners: [] };
    const cleanup = () => {
        for (const item of state.listeners || []) {
            try { item.root.removeEventListener(item.type, item.fn, true); } catch (_) {}
        }
        state.listeners = [];
    };
    const rectOf = (el) => {
        try {
            const r = el.getBoundingClientRect();
            return { center: [r.x + r.width / 2, r.y + r.height / 2], width: r.width, height: r.height };
        } catch (_) { return {}; }
    };
    const safe = (node) => {
        try {
            if (!node || !node.tagName) return { tag: '' };
            const tag = String(node.tagName || '').toLowerCase();
            const isInput = tag === 'input' || tag === 'textarea';
            const out = {
                tag,
                testId: node.getAttribute('data-test-id') || node.getAttribute('data-testid') || '',
                role: node.getAttribute('role') || '',
                aria: node.getAttribute('aria-label') || '',
                type: node.getAttribute('type') || '',
                name: node.getAttribute('name') || '',
                placeholder: node.getAttribute('placeholder') || '',
                contentEditable: String(node.getAttribute('contenteditable') || ''),
                disabled: node.disabled === true,
                readOnly: node.readOnly === true,
                valuePresent: isInput ? String(node.value || '').length > 0 : false,
                selectionPresent: (
                    typeof node.selectionStart === 'number' && typeof node.selectionEnd === 'number'
                ),
                ...rectOf(node),
            };
            return out;
        } catch (_) { return { tag: '' }; }
    };
    const parentOf = (node) => {
        try {
            if (node && node.parentElement) return node.parentElement;
            const root = node && node.getRootNode ? node.getRootNode() : null;
            return root && root.host ? root.host : null;
        } catch (_) { return null; }
    };
    const activePath = () => {
        const out = [];
        let el = document.activeElement;
        for (let i = 0; el && i < 8; i++) {
            out.push(safe(el));
            if (el.shadowRoot && el.shadowRoot.activeElement) {
                el = el.shadowRoot.activeElement;
            } else {
                break;
            }
        }
        return out;
    };
    const hitPath = () => {
        if (!point || point.length !== 2) return [];
        const out = [];
        let el = null;
        try { el = document.elementFromPoint(point[0], point[1]); } catch (_) { el = null; }
        for (let i = 0; el && i < 8; i++) {
            out.push(safe(el));
            el = parentOf(el);
        }
        return out;
    };
    const eventSeen = () => {
        const out = {};
        for (const ev of state.events || []) out[ev.type] = true;
        return out;
    };
    const eventSamples = () => {
        const out = {};
        for (const ev of state.events || []) out[ev.type] = ev;
        return Object.values(out);
    };
    const record = (ev) => {
        const path = [];
        try {
            for (const node of ev.composedPath().slice(0, 5)) path.push(safe(node));
        } catch (_) {}
        const keyKind = ev.type === 'keydown'
            ? (String(ev.key || '').length === 1 ? 'printable' : 'non_printable')
            : '';
        state.events.push({
            type: ev.type,
            trusted: ev.isTrusted === true,
            composed: ev.composed === true,
            defaultPrevented: ev.defaultPrevented === true,
            inputType: String(ev.inputType || ''),
            dataPresent: !!ev.data,
            keyKind,
            target: safe(ev.target),
            path,
        });
        if (state.events.length > 80) state.events.shift();
    };
    const installRoot = (root, depth, seen) => {
        if (!root || seen.has(root) || depth > __DEPTH__) return;
        seen.add(root);
        for (const type of ['focusin', 'keydown', 'beforeinput', 'input']) {
            root.addEventListener(type, record, true);
            state.listeners.push({ root, type, fn: record });
        }
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { els = []; }
        for (const el of els) if (el.shadowRoot) installRoot(el.shadowRoot, depth + 1, seen);
    };
    if (action === 'start') {
        cleanup();
        state.events = [];
        state.listeners = [];
        window[KEY] = state;
        installRoot(document, 0, new Set());
    }
    const snapshot = {
        url: location.href,
        title: document.title,
        action,
        activePath: activePath(),
        hitPath: hitPath(),
        eventSeen: eventSeen(),
        events: eventSamples(),
    };
    if (action === 'stop') cleanup();
    return snapshot;
}""".replace("__DEPTH__", str(_BETSSON_SHADOW_WALK_DEPTH))

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
        # Set by attempt_betsson_relogin when it gave up WITHOUT touching the session
        # because the header never settled (transport.betsson_relogin_auth_unreadable).
        # HotSessionManager._relogin_session reads it to NOT consume the episode's
        # single auto-reauth attempt on that outcome — a bool return can't carry the
        # distinction, and raising would escape into the executor's reactive path.
        self.betsson_relogin_unreadable = False
        self._betsson_password_trace: list[dict[str, Any]] = []
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

    async def _betsson_auth_settled(self) -> dict[str, Any] | None:
        """Debounced Betsson auth read: sample ``_BETSSON_AUTH_SCAN_JS`` up to
        ``_BETSSON_AUTH_SETTLE_SAMPLES`` times (``_BETSSON_AUTH_SETTLE_GAP_S`` apart,
        page lock held per sample only, sleeps outside it) and believe a BAD state
        (header logged-out / session-expired phrase) only if EVERY sample agrees.
        Betsson's SPA renders the login trigger before the session hydrates (post-goto,
        post-reality-check settle), so a single-sample read misclassified a healthy
        session as logged out and scheduled the relogin that logged it out for real
        (2026-07-05 regression). A healthy or ambiguous sample short-circuits — the
        happy path stays one evaluate. A balance-visible sample with an expired phrase
        is still BAD (a server-killed session can coexist with a mounted UI). Returns
        the last sample, or None on a non-dict scan result. Raises on page faults —
        callers own the fail-open policy."""
        auth: dict[str, Any] | None = None
        for i in range(_BETSSON_AUTH_SETTLE_SAMPLES):
            if i:
                await asyncio.sleep(_BETSSON_AUTH_SETTLE_GAP_S)
            async with self._page_lock:
                res = await self._page.evaluate(_BETSSON_AUTH_SCAN_JS)
            if not isinstance(res, dict):
                return None
            auth = res
            if not (bool(res.get("expiredPhrase")) or res.get("loggedOut") is True):
                return auth  # healthy or ambiguous — decisive, no more samples
        return auth

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
        # Betsson renders its ENTIRE SPA in open shadow DOM, so the light-DOM scan above
        # is blind to its reality-check popup AND its auth header. Run Betsson's own
        # shadow scans FIRST (before the generic light-DOM loops): a logged-out header or
        # a shadow session-expired popup must classify as a blocking overlay that
        # schedules auto-reauth, not be bypassed by a body-only banner hit below. Each
        # scan takes the page lock in its own `async with` (the lock is non-reentrant, so
        # they must NOT nest). Order within: reality-check (a closeable reminder that
        # still occludes placement) → auth (logged-out header / expired popup). Fail-open
        # per scan — a flaky read never crashes the heartbeat.
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
            try:
                # Debounced (see _betsson_auth_settled): a transiently-hydrating header
                # (post-goto / post-reality-check settle) must not classify as a
                # session_expired overlay — that block schedules the auto-relogin, and a
                # false one logged a healthy session out (2026-07-05).
                auth = await self._betsson_auth_settled()
            except Exception as exc:  # noqa: BLE001 — a read must never crash the heartbeat
                self._log.warning("transport.betsson_auth_probe_error", error=str(exc))
                return None
            if isinstance(auth, dict):
                expired = auth.get("expiredPhrase")
                if isinstance(expired, str) and expired:
                    return SessionBlock(phrase=expired, is_overlay=True, kind="session_expired")
                if auth.get("loggedOut") is True:
                    return SessionBlock(
                        phrase=(
                            "header logged out (no balance/retirar-depósito; login trigger visible)"
                        ),
                        is_overlay=True,
                        kind="session_expired",
                    )
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
        # Betsson is EXCLUDED from the overlay loop: its expiry truth routes exclusively
        # through the debounced auth scan above (whose walk covers light DOM too) — a
        # one-sample overlay hit here would queue the destructive auto-relogin and
        # bypass the debounce (the 2026-07-05 regression class). The body-text BANNER
        # loop below stays platform-wide: a banner never schedules reauth (recoveries
        # are overlay-scoped).
        if self._platform != "betsson":
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
                target = await self._page.evaluate(_BETSSON_REALITY_CHECK_CLOSE_JS)
                if not (isinstance(target, dict) and target.get("found") is True):
                    reason = target.get("reason") if isinstance(target, dict) else "bad_payload"
                    candidates = target.get("candidates") if isinstance(target, dict) else None
                    evidence = await self._capture_betsson_relogin_evidence(
                        "reality_check_target_missing"
                    )
                    self._log.warning(
                        "transport.reality_check_target_missing",
                        platform=self._platform,
                        reason=reason,
                        candidates=candidates,
                        evidence=evidence,
                    )
                    return False
                self._log.info(
                    "transport.reality_check_close_target",
                    platform=self._platform,
                    tier=target.get("tier"),
                    tag=target.get("tag"),
                    test_id=target.get("testId"),
                    label=target.get("label"),
                )
                await self._page.mouse.move(target["x"], target["y"], steps=4)
                await self._page.mouse.click(target["x"], target["y"])
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

    async def attempt_betsson_relogin(self) -> bool:
        """Full logout→login on the Betsson window using keyring creds, so a server-
        killed session is replaced and its ctx- re-captured.

        REAL BOOKMAKER INTERACTION — operator-authorized 2026-07-02 (the bot logs back
        into its own window on a header-logged-out / session_expired block or a placement
        401). Scan-first: the method trusts the header truth, so a caller may invoke it
        on ANY suspected auth problem without risking a logout of a healthy session —
        this prevents pointless full relogins on mere ctx-loss (Betsson ctx- can vanish to
        cold-load/SPA routing while still authenticated) and turns a manager/executor
        double-relogin race into a cheap re-establish. True iff the logged-in UI is
        reached AND a fresh ctx- is established (the placement artifact) within the
        timeout. False on missing creds, an un-transcribed form, a captcha/OTP challenge,
        or any error — the caller falls back to suspend/abort/naked + alert, so a
        failed/challenged re-auth NEVER adds exposure. Fail-soft: never raises.

        No challenge detector (BetWarrior's _BW_CHALLENGE_JS is BW-specific): a Betsson
        captcha/OTP makes the logged-in wait time out → False → operator alert. Betsson
        renders its ENTIRE SPA in open shadow DOM: the login trigger first uses a bounded
        Playwright locator click, then falls back to the shadow-piercing center-finder +
        REAL pointer path used for the remaining controls; text is typed via page.keyboard
        into the focused input. The trigger/submit selectors are mandatory; password
        readiness accepts either Betsson's direct password selector or the bottom
        input-container fallback. Email is optional because Betsson's remembered-user
        popup can prefill the account and only render the password field. The defensive
        empty-selector gate remains fail-soft: if a future edit blanks required selectors,
        the method logs betsson_relogin_not_configured and returns False before
        ANY page access."""
        self.betsson_relogin_unreadable = False  # one-shot; set only on the give-up path
        self._betsson_password_trace = []
        if self._dry_run or self._page is None or self._platform != "betsson":
            return False
        # Defensive guard: if a future selector refresh blanks a required value, relogin
        # is inert — no page touch, no navigation — and the operator gets an explicit
        # signal. Email is optional for the remembered-user popup variant.
        if (
            not _BETSSON_LOGIN_TRIGGER_SEL
            or (not _BETSSON_LOGIN_PASSWORD_SEL and not _BETSSON_LOGIN_INPUT_CONTAINER_SEL)
            or not _BETSSON_LOGIN_SUBMIT_SEL
        ):
            self._log.warning("transport.betsson_relogin_not_configured")
            return False
        try:
            # Debounced read OUTSIDE the page lock (the helper takes the lock per
            # sample; asyncio.Lock is non-reentrant). Tri-state decision:
            #   bad (persistent logged-out header / expired phrase) → destructive
            #       logout→login below — the only path allowed to touch the session.
            #   healthy (visible balance, no expired phrase)        → cheap re-establish.
            #   ambiguous / unreadable (mid-render, scan returned neither) → cheap
            #       re-establish, NEVER the form flow on an unsettled read: a transient
            #       post-goto / post-reality-check header is exactly what logged a
            #       healthy session out (2026-07-05). But the manager runs ONE reauth
            #       per block episode (_session_reauth_attempted), so if the establish
            #       also fails we take ONE more settled read — by then establish's goto
            #       + the settle window have passed — and only a SETTLED bad read may
            #       escalate to the destructive flow. Still unreadable → give up to the
            #       suspend + alert path (manual login), never a blind form flow.
            auth = await self._betsson_auth_settled()
            has_balance = isinstance(auth, dict) and auth.get("hasBalance") is True
            expired = isinstance(auth, dict) and bool(auth.get("expiredPhrase"))
            bad = expired or (isinstance(auth, dict) and auth.get("loggedOut") is True)
            if not bad and not has_balance:
                self._log.warning("transport.betsson_relogin_auth_ambiguous")
                if await self.establish_betsson_context():
                    self._log.info("transport.betsson_relogin_ok")
                    return True
                auth = await self._betsson_auth_settled()
                has_balance = isinstance(auth, dict) and auth.get("hasBalance") is True
                expired = isinstance(auth, dict) and bool(auth.get("expiredPhrase"))
                bad = expired or (isinstance(auth, dict) and auth.get("loggedOut") is True)
                if not bad and not has_balance:
                    self._log.warning("transport.betsson_relogin_auth_unreadable")
                    self.betsson_relogin_unreadable = True
                    return False
            if bad:
                # Persistent logged-out header or a session-expired popup (possibly over
                # a still-mounted session) → full logout→login. The login step is the
                # real gate; a failed logout below is non-fatal.
                cred = get_credential("betsson")
                if cred is None:
                    self._log.warning("transport.betsson_relogin_no_creds")
                    return False
                async with self._page_lock:
                    self._captured_ctx = None  # invalidate stale ctx- (mirror BW bearer)
                    if has_balance and not await self._betsson_logout():
                        # Clear a still-mounted session (balance visible, e.g. an expired
                        # popup over a live session) before relogin; a failed logout is
                        # non-fatal — login is the real gate.
                        self._log.warning("transport.betsson_relogin_logout_failed")
                    if not await self._open_betsson_login_form():
                        return False
                    if not await self._fill_betsson_login(cred):
                        return False
                    if not await self._await_betsson_logged_in():
                        return False
            else:
                # has_balance guaranteed here (ambiguous returned above). Visible balance
                # is the unambiguous logged-in signal (Betsson renders balance-button
                # ONLY when authenticated). A caller hit this on ctx-loss / a double-
                # relogin race; fall through to re-establish the ctx- below.
                self._log.info("transport.betsson_relogin_already_logged_in")
            # Establish gate — OUTSIDE the page lock (establish_betsson_context takes it
            # internally; asyncio.Lock is non-reentrant, so calling it under the lock would
            # deadlock) but INSIDE the fail-soft try, so a goto timeout / nav fault returns
            # False instead of escaping to the caller (the reactive path would otherwise
            # trip the kill switch). The retry placement needs prepare_betsson_context to
            # return headers; a logged-in-but-no-ctx session is picked up by the next
            # heartbeat instead.
            ok = await self.establish_betsson_context()
        except Exception as exc:  # noqa: BLE001 — relogin must never crash the caller
            evidence: str | None = None
            with contextlib.suppress(Exception):
                async with self._page_lock:
                    evidence = await self._capture_betsson_relogin_evidence("relogin_error")
            self._log.warning("transport.betsson_relogin_error", error=str(exc), evidence=evidence)
            return False
        if ok:
            self._log.info("transport.betsson_relogin_ok")
            return True
        self._log.warning("transport.betsson_relogin_no_ctx_after_login")
        return False

    async def _betsson_logout(self) -> bool:
        """Clear a mounted Betsson session so a fresh login is possible. Opens the
        account menu (balance button) and clicks the visible 'Cerrar sesión' item, then
        polls the auth scan for loggedOut. Every control is reached via the shadow-pierc-
        ing center-finder + a REAL pointer (Betsson custom elements ignore a synthetic
        .click()). Best-effort; must run under ``self._page_lock``. Returns True iff the
        header reads logged-out within ~5s."""
        bal_xy = await self._page.evaluate(
            _BETSSON_FIND_VISIBLE_CENTER_JS, _BETSSON_BALANCE_BUTTON_SEL
        )
        if not isinstance(bal_xy, list) or len(bal_xy) != 2:
            return False
        with contextlib.suppress(Exception):
            await self._page.mouse.move(bal_xy[0], bal_xy[1], steps=4)
            await self._page.mouse.click(bal_xy[0], bal_xy[1])
            await asyncio.sleep(0.9)
        out_xy = await self._page.evaluate(_BETSSON_FIND_VISIBLE_CENTER_JS, _BETSSON_LOGOUT_SEL)
        if not isinstance(out_xy, list) or len(out_xy) != 2:
            return False
        with contextlib.suppress(Exception):
            await self._page.mouse.move(out_xy[0], out_xy[1], steps=4)
            await self._page.mouse.click(out_xy[0], out_xy[1])
        for _ in range(10):  # up to ~5s for the header to read logged-out
            await asyncio.sleep(0.5)
            probe = await self._page.evaluate(_BETSSON_AUTH_SCAN_JS)
            if isinstance(probe, dict) and probe.get("loggedOut") is True:
                return True
        return False

    async def _capture_betsson_relogin_evidence(
        self, stage: str, *, missing: str | None = None
    ) -> str | None:
        """Dump Betsson relogin failure state without acquiring ``_page_lock``.

        Callers are already inside the relogin destructive path's page lock. Captures
        only selector metadata + auth truth + screenshot; no raw HTML or input values.
        Never raises — evidence capture must not change relogin behavior.
        """
        if self._dry_run or self._page is None or self._platform != "betsson":
            return None
        safe_stage = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stage)
        base = _BETSSON_RELOGIN_EVIDENCE_DIR / f"betsson_{safe_stage}_{time.time_ns()}"
        auth: dict[str, Any] | None = None
        data: dict[str, Any] = {}
        try:
            _BETSSON_RELOGIN_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Exception):
                probe = await self._page.evaluate(_BETSSON_AUTH_SCAN_JS)
                if isinstance(probe, dict):
                    auth = probe
            with contextlib.suppress(Exception):
                snapshot = await self._page.evaluate(
                    _BETSSON_RELOGIN_EVIDENCE_JS,
                    {
                        "trigger": _BETSSON_LOGIN_TRIGGER_SEL,
                        "email": _BETSSON_LOGIN_EMAIL_SEL,
                        "password": {
                            "primary": _BETSSON_LOGIN_PASSWORD_SEL,
                            "fallback": _BETSSON_LOGIN_INPUT_CONTAINER_SEL,
                        },
                        "submit": _BETSSON_LOGIN_SUBMIT_SEL,
                        "geolocation": _BETSSON_GEOLOCATION_CTA_SEL,
                    },
                )
                if isinstance(snapshot, dict):
                    data = snapshot
            screenshot: str | None = None
            if stage in {
                "open_miss_before_reload",
                "no_form_after_reload",
                "reality_check_target_missing",
            }:
                shot = base.with_suffix(".png")
                with contextlib.suppress(Exception):
                    await self._page.screenshot(path=str(shot), full_page=False)
                    screenshot = str(shot)
            safe_auth: dict[str, Any] | None = None
            if isinstance(auth, dict):
                safe_auth = {
                    k: auth[k]
                    for k in ("loggedIn", "hasBalance", "loggedOut", "expiredPhrase")
                    if k in auth
                }
            raw_selectors = data.get("selectors", {})
            selectors: dict[str, dict[str, Any] | None] = {}
            allowed_fields = {
                "visible",
                "center",
                "tag",
                "testId",
                "type",
                "name",
                "placeholder",
                "aria",
                "text",
                "matchCount",
                "pointTarget",
                "fallback",
                "descendantInput",
                "container",
            }
            allowed_point_target_fields = {"tag", "testId", "role", "aria", "point"}
            allowed_container_fields = {
                "visible",
                "center",
                "tag",
                "testId",
                "type",
                "name",
                "placeholder",
                "aria",
                "pointTarget",
            }
            allowed_selectors = {"trigger", "email", "password", "submit", "geolocation"}
            if isinstance(raw_selectors, dict):
                for key, value in raw_selectors.items():
                    if key not in allowed_selectors:
                        continue
                    if value is None:
                        selectors[str(key)] = None
                    elif isinstance(value, dict):
                        cleaned = {k: v for k, v in value.items() if k in allowed_fields}
                        point_target = cleaned.get("pointTarget")
                        if isinstance(point_target, dict):
                            cleaned["pointTarget"] = {
                                k: v
                                for k, v in point_target.items()
                                if k in allowed_point_target_fields
                            }
                        elif point_target is not None:
                            cleaned.pop("pointTarget", None)
                        container = cleaned.get("container")
                        if isinstance(container, dict):
                            cleaned_container = {
                                k: v for k, v in container.items() if k in allowed_container_fields
                            }
                            container_point_target = cleaned_container.get("pointTarget")
                            if isinstance(container_point_target, dict):
                                cleaned_container["pointTarget"] = {
                                    k: v
                                    for k, v in container_point_target.items()
                                    if k in allowed_point_target_fields
                                }
                            elif container_point_target is not None:
                                cleaned_container.pop("pointTarget", None)
                            cleaned["container"] = cleaned_container
                        elif container is not None:
                            cleaned.pop("container", None)
                        if key in {"email", "password"}:
                            cleaned["text"] = ""
                        selectors[str(key)] = cleaned
            out = base.with_suffix(".json")
            out.write_text(
                json.dumps(
                    {
                        "platform": self._platform,
                        "stage": stage,
                        "missing": missing,
                        "ts_ns": time.time_ns(),
                        "url": data.get("url") or getattr(self._page, "url", ""),
                        "title": data.get("title", ""),
                        "auth": safe_auth,
                        "selectors": selectors,
                        "screenshot": screenshot,
                        "passwordTrace": self._betsson_password_trace,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self._log.warning(
                "transport.betsson_relogin_evidence_saved", stage=stage, path=str(out)
            )
            return str(out)
        except Exception as exc:  # noqa: BLE001 — diagnostic capture must never alter relogin
            self._log.warning(
                "transport.betsson_relogin_evidence_error", stage=stage, error=str(exc)
            )
            return None

    async def _record_betsson_password_trace(
        self, stage: str, *, action: str, point: list[Any] | None = None
    ) -> None:
        """Record sanitized focus/input telemetry for the Betsson password step.

        No values, HTML, screenshots, or raw typed keys are captured. The JS probe is
        one-shot: ``start`` removes stale listeners and clears prior events; ``stop``
        snapshots and removes listeners so retries do not contaminate later evidence.
        """
        if self._dry_run or self._page is None or self._platform != "betsson":
            return
        try:
            snapshot = await self._page.evaluate(
                _BETSSON_PASSWORD_TRACE_JS,
                {"action": action, "point": point},
            )
            if not isinstance(snapshot, dict):
                return
            rec = {
                "stage": stage,
                "action": action,
                "ts_ns": time.time_ns(),
                "activePath": snapshot.get("activePath", []),
                "hitPath": snapshot.get("hitPath", []),
                "eventSeen": snapshot.get("eventSeen", {}),
                "events": snapshot.get("events", []),
            }
            self._betsson_password_trace.append(rec)
            self._log.warning(
                "transport.betsson_relogin_password_trace",
                stage=stage,
                action=action,
                eventSeen=rec["eventSeen"],
            )
        except Exception as exc:  # noqa: BLE001 — recon must never alter relogin
            self._log.warning(
                "transport.betsson_relogin_password_trace_error",
                stage=stage,
                action=action,
                error=str(exc),
            )

    async def _betsson_password_xy(self) -> list[Any] | None:
        """Visible password target for Betsson login.

        Prefer the explicit password selector. If Betsson's remembered-user popup exposes
        only generic ``input-container`` wrappers, use the bottommost visible container so
        the email/account wrapper is not selected by a broad first-match query.
        """
        if _BETSSON_LOGIN_PASSWORD_SEL:
            xy = await self._page.evaluate(
                _BETSSON_FIND_VISIBLE_CENTER_JS, _BETSSON_LOGIN_PASSWORD_SEL
            )
            if isinstance(xy, list) and len(xy) == 2:
                return xy
        if _BETSSON_LOGIN_INPUT_CONTAINER_SEL:
            xy = await self._page.evaluate(
                _BETSSON_FIND_BOTTOM_VISIBLE_CENTER_JS, _BETSSON_LOGIN_INPUT_CONTAINER_SEL
            )
            if isinstance(xy, list) and len(xy) == 2:
                return xy
        return None

    async def _open_betsson_login_form(self) -> bool:
        """Click the header login trigger to open the login popup, then poll for the
        PASSWORD input — the one control present in BOTH popup variants: the fresh
        login form (email + password) and the remembered-user form (Betsson pre-fills
        the account; only the password is asked — live 2026-07-05, where gating on the
        email input made this method reload the SPA and give up `no_form` while the
        popup was open and usable). The trigger resolves the login-button wrapper to
        its inner shadow <button> (Betsson custom elements hide the real control there),
        with a header "iniciar sesión" fdsp-button fallback when the wrapper testId is
        gone. Betsson may first show an app-level geolocation interstitial even though
        Playwright has geolocation permission; its CTA opens the login popup on its own,
        so the opener keeps polling without an immediate trigger re-click. If the
        trigger or the password input never appears, reload
        once (clears a wedged SPA / an occluding expired popup) and retry. Assumes the
        caller holds ``self._page_lock``. Returns True iff the password input is
        visible."""

        async def _click_visible(sel: str) -> bool:
            xy = await self._page.evaluate(_BETSSON_FIND_CLICKABLE_CENTER_JS, sel)
            if not isinstance(xy, list) or len(xy) != 2:
                return False
            with contextlib.suppress(Exception):
                await self._page.mouse.move(xy[0], xy[1], steps=4)
                await asyncio.sleep(random.uniform(0.2, 0.6))
                await self._page.mouse.click(xy[0], xy[1])
                return True
            return False

        async def _click_trigger(*, prefer_locator: bool = False) -> bool:
            xy = await self._page.evaluate(_BETSSON_FIND_LOGIN_TRIGGER_CENTER_JS)
            if not isinstance(xy, list) or len(xy) != 2:
                return False
            if prefer_locator:
                with contextlib.suppress(Exception):
                    await self._page.locator(_BETSSON_LOGIN_TRIGGER_SEL).click(
                        timeout=_BETSSON_LOGIN_CLICK_TIMEOUT_MS
                    )
                    return True
            with contextlib.suppress(Exception):
                await self._page.mouse.move(xy[0], xy[1], steps=4)
                await asyncio.sleep(random.uniform(0.2, 0.6))
                await self._page.mouse.click(xy[0], xy[1])
                return True
            return False

        async def _try() -> bool:
            await _click_trigger(prefer_locator=True)
            center_fallback_clicked = False
            fallback_after_poll = _BETSSON_LOGIN_CENTER_FALLBACK_POLLS
            for poll in range(16):  # up to ~8s for the popup/interstitial to settle
                await asyncio.sleep(0.5)
                pwd_xy = await self._betsson_password_xy()
                if pwd_xy is not None:
                    return True
                if await _click_visible(_BETSSON_GEOLOCATION_CTA_SEL):
                    await asyncio.sleep(1.0)
                    center_fallback_clicked = False
                    fallback_after_poll = poll + 1 + _BETSSON_LOGIN_CENTER_FALLBACK_POLLS
                    continue
                if not center_fallback_clicked and poll + 1 >= fallback_after_poll:
                    center_fallback_clicked = await _click_trigger()
            return False

        if await _try():
            return True
        evidence = await self._capture_betsson_relogin_evidence("open_miss_before_reload")
        self._log.warning("transport.betsson_relogin_open_miss", evidence=evidence)
        with contextlib.suppress(Exception):
            await self._page.reload(wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)
        if await _try():
            return True
        evidence = await self._capture_betsson_relogin_evidence("no_form_after_reload")
        self._log.warning("transport.betsson_relogin_no_form", evidence=evidence)
        return False

    async def _fill_betsson_login(self, cred: Any) -> bool:
        """Type keyring creds into the open login popup + click submit. Assumes the
        caller holds ``self._page_lock`` and the popup is open (password input
        visible). The EMAIL input is OPTIONAL: Betsson's remembered-user popup
        pre-fills the account and shows no email input (live 2026-07-05) — when the
        input is absent, only the password is typed; when present (fresh login form),
        the username is typed first. Every control is reached via the shadow-piercing
        center-finder + a REAL pointer; text is typed via page.keyboard into the
        focused input (real keystrokes — Betsson's React-controlled inputs ignore
        direct value sets). Each input is select-all'd (ControlOrMeta+A) before typing
        so a RETRY after a timed-out attempt — popup still open with the previous
        creds typed — REPLACES rather than appends (appending would submit invalid
        credentials until a manual reset). Human-like pacing mirrors
        attempt_betwarrior_relogin. Returns False if the password or submit control
        can't be found; True once submit is clicked (the caller's logged-in wait gates
        success). Fallback if the submit click proves flaky live:
        page.keyboard.press('Enter') after the password type."""
        self._betsson_password_trace = []
        email_xy = await self._page.evaluate(
            _BETSSON_FIND_VISIBLE_CENTER_JS, _BETSSON_LOGIN_EMAIL_SEL
        )
        if isinstance(email_xy, list) and len(email_xy) == 2:
            await self._page.mouse.move(email_xy[0], email_xy[1], steps=4)
            await self._page.mouse.click(email_xy[0], email_xy[1])
            await asyncio.sleep(random.uniform(0.2, 0.6))
            await self._page.keyboard.press("ControlOrMeta+A")  # clear any stale value (retry)
            await self._page.keyboard.type(cred.username, delay=random.randint(70, 190))
            await asyncio.sleep(random.uniform(0.4, 1.2))
        else:
            # Remembered-user popup: Betsson pre-filled the account — only the
            # password is asked. Decision-point event for log review.
            self._log.info("transport.betsson_relogin_email_prefilled")
        await asyncio.sleep(random.uniform(0.4, 1.2))
        pwd_xy = await self._betsson_password_xy()
        if pwd_xy is None:
            evidence = await self._capture_betsson_relogin_evidence(
                "form_incomplete_password", missing="password"
            )
            self._log.warning(
                "transport.betsson_relogin_form_incomplete",
                missing="password",
                evidence=evidence,
            )
            return False
        await self._record_betsson_password_trace(
            "before_password_click", action="start", point=pwd_xy
        )
        try:
            await self._page.mouse.move(pwd_xy[0], pwd_xy[1], steps=4)
            await self._page.mouse.click(pwd_xy[0], pwd_xy[1])
            await asyncio.sleep(random.uniform(0.2, 0.6))
            await self._record_betsson_password_trace(
                "after_password_click", action="snapshot", point=pwd_xy
            )
            await self._page.keyboard.press("ControlOrMeta+A")  # clear any stale value (retry)
            await self._record_betsson_password_trace(
                "before_password_type", action="start", point=pwd_xy
            )
            await self._page.keyboard.type(cred.password, delay=random.randint(70, 190))
        finally:
            await self._record_betsson_password_trace(
                "after_password_type", action="stop", point=pwd_xy
            )
        await asyncio.sleep(random.uniform(0.4, 1.2))
        sub_xy = await self._page.evaluate(
            _BETSSON_FIND_CLICKABLE_CENTER_JS, _BETSSON_LOGIN_SUBMIT_SEL
        )
        if not isinstance(sub_xy, list) or len(sub_xy) != 2:
            evidence = await self._capture_betsson_relogin_evidence(
                "form_incomplete_submit", missing="submit"
            )
            self._log.warning(
                "transport.betsson_relogin_form_incomplete",
                missing="submit",
                evidence=evidence,
            )
            return False
        await self._page.mouse.move(sub_xy[0], sub_xy[1], steps=4)
        await self._page.mouse.click(sub_xy[0], sub_xy[1])
        return True

    async def _await_betsson_logged_in(self) -> bool:
        """Poll the auth scan until the balance button is visible with no session-
        expired phrase, up to ``_BETSSON_RELOGIN_TIMEOUT_S``. Betsson renders
        balance-button ONLY when authenticated, so ``hasBalance`` is the same
        authenticated truth the healthy short-circuit in ``attempt_betsson_relogin``
        keys on. The stricter ``loggedIn`` (balance AND Retirar/Depósito header text)
        under-detects on a header-layout variant — a login that actually SUCCEEDED then
        read as a relogin failure and stranded the session (2026-07-05). Assumes the
        caller holds ``self._page_lock``. A captcha/OTP makes this time out → False (no
        Betsson challenge detector — operator alert, same net behavior as a challenged
        BW login). Logs the timeout itself so the caller only returns False."""
        deadline = time.monotonic() + _BETSSON_RELOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            probe = await self._page.evaluate(_BETSSON_AUTH_SCAN_JS)
            if (
                isinstance(probe, dict)
                and probe.get("hasBalance") is True
                and not probe.get("expiredPhrase")
            ):
                return True
            await asyncio.sleep(0.5)
        evidence = await self._capture_betsson_relogin_evidence("login_timeout")
        self._log.warning("transport.betsson_relogin_timeout", evidence=evidence)
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
        # If a Betsson overlay block is up, do NOT reload the page — the hard goto fights
        # recovery. reality_check: a goto can't dismiss it (only the orange "Cerrar" /
        # fds-button[data-test-id=reality-check-btn-1] does) and re-triggers the SPA after
        # a close. session_expired: a goto can reset the login popup mid-relogin and can
        # never revive a dead session — the manager's attempt_betsson_relogin() owns that
        # recovery. Return not-ready; check_session_blocked() latched the block, the
        # manager detects it separately and runs the right recovery. The is_overlay scoping
        # is EXACT: check_session_blocked can return session_expired as a body-only
        # non-blocking banner (is_overlay=False), and skipping the goto on a banner would
        # turn a recoverable cold session into permanent not-ready — banners fall through.
        block = await self.check_session_blocked()
        if (
            block is not None
            and block.is_overlay
            and block.kind
            in (
                "reality_check",
                "session_expired",
            )
        ):
            self._log.info("transport.betsson_context_skipped_blocked", kind=block.kind)
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
