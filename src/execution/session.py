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
import json
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PROFILE_ROOT = REPO_ROOT / "recon" / "profile"

# Pseudo / hop-by-hop headers to strip before replaying a captured header set.
_HEADER_DROP = frozenset(
    {":authority", ":method", ":path", ":scheme", "host", "content-length", "accept-encoding"}
)


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
        if req.headers.get("x-sb-user-context-id", "").startswith("ctx-"):
            self._captured_ctx = dict(req.headers)

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
        # Execute the request inside the page context — inherits cookies/fingerprint.
        result = await self._page.evaluate(
            """async ({method, url, body, headers}) => {
                const resp = await fetch(url, {
                    method, headers: headers || {},
                    body: body !== null ? JSON.stringify(body) : null,
                    credentials: 'include',
                });
                return {status: resp.status, text: await resp.text()};
            }""",
            {"method": method, "url": url, "body": json_body, "headers": headers or {}},
        )
        status = int(result["status"])
        try:
            parsed = json.loads(result["text"]) if result["text"] else {}
        except ValueError as exc:
            raise TransportError(f"non-JSON response from {url}: {exc!s}") from exc
        return status, parsed if isinstance(parsed, dict) else {}
