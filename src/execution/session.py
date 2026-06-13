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
import time
from pathlib import Path
from typing import Any, Final, Protocol

import structlog

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

# Visible modal/overlay selector — used by capture_block_evidence to dump the block's
# markup. A populated `dialogs` list means a true blocking OVERLAY; an empty one with a
# phrase hit means a non-blocking BANNER (e.g. Betano's "12h descanso"). This is exactly
# the distinction we need to ground but couldn't reproduce in the lab.
_RG_BLOCK_DIALOG_SELECTOR = "[role=dialog],[aria-modal=true],.modal,.overlay,.modal-overlay"
# Where the first production block dumps its DOM + screenshot, so the exact lockout
# selector is grounded from the real event.
_BLOCK_EVIDENCE_DIR = REPO_ROOT / "recon" / "artifacts" / "rg_blocks"


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
        ``timeout_s`` (not logged in / no authenticated call yet)."""
        if self._dry_run:
            self._log.info("transport.dry_run_prepare")
            return None
        for _ in range(timeout_s):
            if self._captured_bearer is not None:
                break
            await asyncio.sleep(1)
        return self._captured_bearer

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
        if await self.prepare_betwarrior_auth(timeout_s) is None:
            return False
        if self._bearer_exp is not None and time.time() >= self._bearer_exp - _BEARER_EXP_SKEW_SEC:
            self._log.warning("transport.betwarrior_bearer_expired", exp=self._bearer_exp)
            return False
        return True

    async def check_session_blocked(self) -> str | None:
        """Detect a responsible-gambling LOCKOUT overlay blocking the betting UI.

        PBA platforms enforce an accumulated-play-time limit that, once hit, replaces
        the betting UI with a mandatory-break notice ("TOMATE UN DESCANSO — 12h de
        descanso de apostar y jugar"). The underlying session stays authenticated, so
        every readiness probe (balance, ``ctx-``, bearer ``exp``) keeps passing — this
        overlay is the ONLY signal that the window is actually unusable. Scans the
        page's VISIBLE text (``innerText`` excludes hidden nodes) for a lockout phrase
        and returns the matched phrase (for the alert + the trigger-learning log), or
        ``None`` if the page is usable. Dry-run / no page ⇒ ``None``.

        Fail-OPEN on a probe fault (return ``None``): the real readiness probes already
        suspend a genuinely broken page, and a flaky DOM read must never crash the
        heartbeat (the hard-won startup-crash lesson)."""
        if self._dry_run or self._page is None:
            return None
        try:
            async with self._page_lock:
                text = await self._page.evaluate(
                    "() => (document.body && document.body.innerText || '').toLowerCase()"
                )
        except Exception as exc:  # noqa: BLE001 — a read must never crash the heartbeat
            self._log.warning("transport.block_probe_error", error=str(exc))
            return None
        if not isinstance(text, str):
            return None
        return next((p for p in _RG_BLOCK_PHRASES if p in text), None)

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
