"""First-pass sportsbook recon.

Opens a sportsbook landing page, dismisses cookie banners best-effort,
tries to navigate into the soccer section and drill into a match. At
every stop captures a screenshot, full DOM, and the current URL/title.
All network traffic is recorded to a HAR file (auto-written on context
close) plus a live-streamed JSONL of every request so we keep partial
data if the run crashes mid-way.

Platform-agnostic by design — same script, different `--platform`
argument. Each platform gets its own artifact directory and its own
browser profile so cookies / fingerprints don't bleed across sites
(which could otherwise flag the profile to anti-bot systems).

This is read-only reconnaissance — no clicks on bet slips, no logins,
no form submissions. We are looking at what the site renders and what
endpoints it calls so we can build a deterministic scraper afterward.

Output layout (per platform per run):
    recon/artifacts/<platform>/<UTC-timestamp>/
        network.har         — full network trace (Playwright HAR)
        requests.jsonl      — live log of every request (url/method/type)
        websocket_frames.jsonl — every WebSocket frame (ts/url/dir/payload);
                              the HAR does NOT record WS, and some platforms
                              push live odds over WS (e.g. Betsson's OBG
                              pub/sub) — this is how we recon in-play feeds
        summary.json        — nav steps with title + final URL each step
        NN_<slug>.png       — screenshot per nav step
        NN_<slug>.html      — full DOM dump per nav step

The browser profile lives at `recon/profile/<platform>/` and persists
across runs of that platform.

Usage:
    uv run python scripts/recon/recon.py --platform betsson
    uv run python scripts/recon/recon.py --platform bet365
    uv run python scripts/recon/recon.py --platform <name> --url https://...
    uv run python scripts/recon/recon.py --platform <name> --headless
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import (
    BrowserContext,
    Page,
    Request,
    ViewportSize,
    WebSocket,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from scripts.recon.block_detect import detect_block
from scripts.recon.human import HumanCursor, pacing_for
from scripts.recon.stealth import apply_stealth

REPO_ROOT = Path(__file__).parent.parent.parent
ARTIFACT_ROOT_BASE = REPO_ROOT / "recon" / "artifacts"
PROFILE_ROOT_BASE = REPO_ROOT / "recon" / "profile"

# Per-platform default landing URLs. `--url` overrides for ad-hoc recon
# (e.g. drilling into a specific match path).
DEFAULT_URLS: dict[str, str] = {
    "betsson": "https://www.betsson.com.ar",
    "bet365": "https://bet365.bet.ar",
    "codere": "https://codere.bet.ar",
    "bplay": "https://pba.bplay.bet.ar/",
    "betwarrior": "https://pba.betwarrior.bet.ar/",
    "betano": "https://www.betano.bet.ar/",
}

LOCALE = "es-AR"
TIMEZONE_ID = "America/Argentina/Buenos_Aires"
VIEWPORT: ViewportSize = {"width": 1440, "height": 900}

# Best-effort selectors used when we don't yet know the page structure.
COOKIE_ACCEPT_SELECTORS = [
    "button:has-text('Aceptar todo')",
    "button:has-text('Aceptar todas')",
    "button:has-text('Aceptar')",
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "button:has-text('OK')",
    "#onetrust-accept-btn-handler",
    "[id*='accept-all' i]",
    "[id*='accept' i]",
    "[aria-label*='Aceptar' i]",
    "[aria-label*='accept' i]",
]

SOCCER_LINK_SELECTORS = [
    "a:has-text('Fútbol'):visible",
    "a:has-text('Futbol'):visible",
    "a:has-text('Soccer'):visible",
    "a:has-text('Football'):visible",
    "a[href*='futbol' i]",
    "a[href*='soccer' i]",
    "a[href*='football' i]",
]

MATCH_LINK_SELECTORS = [
    "a[href*='/evento/' i]",
    "a[href*='/event/' i]",
    "a[href*='/match/' i]",
    "a[href*='/partido/' i]",
    "[data-testid*='match' i]",
    "[data-testid*='event' i]",
]


def _ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


class BlockDetectedError(Exception):
    """Raised when a page matches a bot-block / challenge signature.
    Aborts the recon so we stop hitting a site that's told us no."""


def _homepage_of(url: str) -> str:
    """Derive the bare homepage (scheme://host/) from any URL. We always
    enter a platform via its homepage and warm up before navigating
    deeper — cold deep-links are what tripped Betano's block on
    2026-05-27."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


async def _check_block(page: Page, step: str) -> None:
    """Abort the recon if the current page is a block/challenge page."""
    reason = await detect_block(page)
    if reason is not None:
        raise BlockDetectedError(f"[{step}] {reason}")


async def _settle(page: Page, *, networkidle_timeout_ms: int = 12000) -> None:
    """Best-effort wait for the page to stop churning. Many sportsbooks
    never reach true networkidle (websockets, polling) — swallow the
    timeout."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=networkidle_timeout_ms)


async def _snapshot(
    page: Page,
    slug: str,
    label: str,
    out_dir: Path,
    nav_steps: list[dict[str, str]],
) -> None:
    """Capture screenshot + DOM + step metadata at the current page state."""
    png = out_dir / f"{slug}.png"
    html = out_dir / f"{slug}.html"
    try:
        await page.screenshot(path=str(png), full_page=True)
    except Exception as exc:
        print(f"  ! screenshot failed for {slug}: {exc}")
    try:
        html.write_text(await page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"  ! html dump failed for {slug}: {exc}")
    title = ""
    with contextlib.suppress(Exception):
        title = await page.title()
    step = {"slug": slug, "label": label, "url": page.url, "title": title}
    nav_steps.append(step)
    print(f"  · {slug}: {label}")
    print(f"      title: {title}")
    print(f"      url:   {page.url}")


def _request_logger(stream: TextIO) -> Callable[[Request], None]:
    """Returns a callback that streams each request to a JSONL file."""

    def on_request(req: Request) -> None:
        # Late events can fire during context teardown, after the finally
        # block closed the stream — drop them rather than crash.
        if stream.closed:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "url": req.url,
            "method": req.method,
            "type": req.resource_type,
        }
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()

    return on_request


# Cap a single WS frame's stored size — initial odds snapshots can be
# large; we keep enough to read the shape without unbounded files.
MAX_WS_FRAME_CHARS = 200_000


def _ws_frame_row(
    ws_url: str, direction: str, payload: str | bytes, max_len: int = MAX_WS_FRAME_CHARS
) -> dict[str, object]:
    """Build a JSONL row for one WebSocket frame (pure; no timestamp).

    Text frames are stored as-is; binary frames are base64-encoded and
    flagged. Oversized payloads are truncated with a `truncated` flag so
    the artifact stays bounded."""
    binary = False
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = base64.b64encode(payload).decode("ascii")
            binary = True
    else:
        text = payload
    truncated = len(text) > max_len
    if truncated:
        text = text[:max_len]
    row: dict[str, object] = {
        "ws_url": ws_url,
        "dir": direction,
        "binary": binary,
        "payload": text,
    }
    if truncated:
        row["truncated"] = True
    return row


def _websocket_logger(stream: TextIO) -> Callable[[WebSocket], None]:
    """Returns a `page.on('websocket', ...)` callback that streams every
    frame (both directions) to a JSONL file.

    The HAR does not record WebSocket traffic, but some sportsbooks push
    live/in-play odds over WS (e.g. Betsson's OBG `?obg/sportsbook/
    transient/...` pub/sub) — this is the only way to recon those feeds."""

    def on_websocket(ws: WebSocket) -> None:
        def write(direction: str, frame: object) -> None:
            # Late frames can fire during context teardown, after the
            # finally block closed the stream — drop them rather than crash.
            if stream.closed:
                return
            # Playwright may hand the payload directly or wrapped; handle both.
            payload = getattr(frame, "payload", frame)
            if not isinstance(payload, str | bytes):
                return
            row = {"ts": datetime.now(UTC).isoformat(), **_ws_frame_row(ws.url, direction, payload)}
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()

        ws.on("framereceived", lambda frame: write("recv", frame))
        ws.on("framesent", lambda frame: write("sent", frame))

    return on_websocket


async def _recon(context: BrowserContext, args: argparse.Namespace, out_dir: Path) -> None:
    nav_steps: list[dict[str, str]] = []
    requests_path = out_dir / "requests.jsonl"
    request_stream = requests_path.open("w", encoding="utf-8")
    context.on("request", _request_logger(request_stream))
    ws_stream = (out_dir / "websocket_frames.jsonl").open("w", encoding="utf-8")

    pacing = pacing_for(args.platform)
    homepage = _homepage_of(args.url)
    deep_target = args.url if args.url.rstrip("/") != homepage.rstrip("/") else None

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        # Capture WebSocket frames (live odds for OBG-style pub/sub feeds);
        # the HAR doesn't record these. Attached before any navigation.
        page.on("websocket", _websocket_logger(ws_stream))
        # One cursor for the whole session — continuous motion, like a
        # real hand that doesn't teleport between actions.
        cursor = HumanCursor(page, pacing)

        # [1] ALWAYS enter via the homepage and warm up. Cold deep-links
        # are what tripped Betano's block — a real visitor lands on the
        # homepage first.
        print(f"\n[1] Opening homepage {homepage} ...", flush=True)
        await page.goto(homepage, wait_until="domcontentloaded", timeout=45000)
        await _settle(page)
        await _check_block(page, "homepage")
        await _snapshot(page, "01_home_initial", "Initial landing", out_dir, nav_steps)

        print("\n[2] Warm-up (dwell / mouse / scroll)...", flush=True)
        await cursor.warm_up()

        print("\n[3] Cookie banner...", flush=True)
        dismissed = await cursor.click(COOKIE_ACCEPT_SELECTORS, "dismiss cookies")
        if dismissed:
            await _settle(page, networkidle_timeout_ms=5000)
            await _check_block(page, "after-consent")
            await _snapshot(
                page, "02_home_after_consent", "After cookie consent", out_dir, nav_steps
            )

        # [4] If a deep target was requested, navigate to it now — but
        # from a WARM session (cookies set, fingerprint established,
        # human dwell behind us), not cold.
        if deep_target is not None:
            print(f"\n[4] Navigate to target {deep_target} (warm) ...", flush=True)
            await cursor.warm_up()
            await page.goto(deep_target, wait_until="domcontentloaded", timeout=45000)
            await _settle(page)
            await _check_block(page, "deep-target")
            await _snapshot(page, "04_target", "Deep target", out_dir, nav_steps)
        else:
            # Otherwise drill into soccer + a match via human clicks.
            print("\n[4] Navigate to soccer...", flush=True)
            clicked = await cursor.click(SOCCER_LINK_SELECTORS, "click soccer")
            if clicked:
                await _settle(page)
                await _check_block(page, "soccer")
                await _snapshot(page, "04_soccer", "Soccer section", out_dir, nav_steps)
                await cursor.warm_up()

            print("\n[5] Open a match...", flush=True)
            clicked = await cursor.click(MATCH_LINK_SELECTORS, "open match")
            if clicked:
                await _settle(page)
                await _check_block(page, "match")
                await _snapshot(page, "05_match", "Match detail", out_dir, nav_steps)

        if args.interactive:
            # Logged-in capture: hold (HAR + WS still recording) while the
            # operator adds a selection to the bet slip by hand, which is
            # what surfaces stake limits (max_stake) the public feeds omit.
            print(
                "\n[6] INTERACTIVE: in the browser, add a selection to the bet slip "
                "(do NOT place the bet) to surface stake limits, then return here.",
                flush=True,
            )
            await asyncio.to_thread(
                input, "[interactive] Press Enter when done to stop capturing... "
            )
        else:
            # Let any late XHR / polling settle so we capture them in the HAR.
            print("\n[6] Idle to capture late traffic...", flush=True)
            await asyncio.sleep(5)
    finally:
        request_stream.close()
        ws_stream.close()
        (out_dir / "summary.json").write_text(
            json.dumps(
                {
                    "session_id": out_dir.name,
                    "url": args.url,
                    "nav_steps": nav_steps,
                    "headless": args.headless,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


async def _manual_login(context: BrowserContext, args: argparse.Namespace) -> None:
    """Open the site headed and wait for the operator to log in by hand.

    The persistent profile saves the session on `context.close()`, so we
    store no credentials and 2FA / captcha are handled manually in the
    browser. Blocks on `input()` (off the event loop) until the operator
    confirms they're logged in."""
    page = context.pages[0] if context.pages else await context.new_page()
    print(f"\n[login] Opening {args.url} — log in MANUALLY in the browser window.", flush=True)
    with contextlib.suppress(PlaywrightError):
        await page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
    print(
        "[login] Complete the login (including any 2FA / captcha) in the browser, "
        "then come back here.",
        flush=True,
    )
    await asyncio.to_thread(
        input, "[login] Press Enter once you are logged in to save the session... "
    )
    await _settle(page, networkidle_timeout_ms=5000)
    title = ""
    with contextlib.suppress(Exception):
        title = await page.title()
    print(f"[login] Current page: {title} | {page.url}", flush=True)


def _session_state_path(platform: str) -> Path:
    """Where `--login` stashes the full session. The persistent profile
    drops SESSION-ONLY cookies (no Expires/Max-Age) on close — which is how
    e.g. Bplay's JSESSIONID/playerSession auth is stored — so a plain
    profile re-open is logged out. `storage_state` captures those; we
    re-inject them on later runs. Lives under the gitignored profile dir."""
    return PROFILE_ROOT_BASE / f"{platform}-session.json"


async def _restore_session(context: BrowserContext, platform: str) -> None:
    """Re-inject the login session saved by `--login` (the session-only
    cookies the persistent profile can't keep). No-op if none saved."""
    path = _session_state_path(platform)
    if not path.exists():
        return
    try:
        cookies = json.loads(path.read_text(encoding="utf-8")).get("cookies", [])
    except (OSError, ValueError) as exc:
        print(f"  ! session restore failed to read {path.name}: {exc}")
        return
    if cookies:
        with contextlib.suppress(PlaywrightError):
            await context.add_cookies(cookies)
            print(f"  · restored {len(cookies)} session cookies from {path.name}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="First-pass sportsbook recon")
    parser.add_argument(
        "--platform",
        required=True,
        help="Platform slug (e.g. betsson, bet365). Used to scope artifacts and the browser profile.",
    )
    parser.add_argument(
        "--headless", action="store_true", help="run without showing a browser window"
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Entry URL. Defaults to the platform's canonical landing page; pass explicitly to recon a specific match URL.",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Browser channel, e.g. 'chrome' to drive installed Google Chrome instead of bundled Chromium (closes the Chromium-for-Testing fingerprint gap).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open the site headed and wait for a MANUAL login, then save the "
        "session into the persistent profile. Stores no credentials; handles "
        "2FA/captcha by hand. Re-run without --login to recon logged-in.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="After navigating, HOLD (still recording HAR/WS) until you press "
        "Enter — for logged-in bet-slip capture (add a selection by hand to "
        "surface stake limits). Use a profile already logged in via --login.",
    )
    args = parser.parse_args()

    url = args.url or DEFAULT_URLS.get(args.platform)
    if url is None:
        print(
            f"error: no default URL known for platform={args.platform!r}; pass --url explicitly",
            file=sys.stderr,
        )
        return 2
    args.url = url

    if args.interactive and args.headless:
        print(
            "error: --interactive needs a visible browser; do not pass --headless",
            file=sys.stderr,
        )
        return 2

    profile_dir = PROFILE_ROOT_BASE / args.platform
    profile_dir.mkdir(parents=True, exist_ok=True)

    if args.login:
        if args.headless:
            print(
                "error: --login needs a visible browser; do not pass --headless",
                file=sys.stderr,
            )
            return 2
        print(f"Platform: {args.platform}\nProfile:  {profile_dir}\nURL:      {args.url}")
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                channel=args.channel,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                viewport=VIEWPORT,
            )
            await apply_stealth(context)
            try:
                await _manual_login(context, args)
                # Capture the FULL session (incl. session-only cookies the
                # profile would drop) so later runs can restore it.
                await context.storage_state(path=str(_session_state_path(args.platform)))
            finally:
                await context.close()
        print(
            f"\nSession saved ({_session_state_path(args.platform).name}). "
            "Re-run without --login to recon logged-in."
        )
        return 0

    session_id = _ts()
    artifact_root = ARTIFACT_ROOT_BASE / args.platform
    out_dir = artifact_root / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Platform: {args.platform}")
    print(f"Session:  {session_id}")
    print(f"Output:   {out_dir}")
    print(f"Profile:  {profile_dir}")
    print(f"URL:      {args.url}")
    print(f"Headless: {args.headless}")

    blocked_reason: str | None = None
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            channel=args.channel,
            locale=LOCALE,
            timezone_id=TIMEZONE_ID,
            viewport=VIEWPORT,
            record_har_path=str(out_dir / "network.har"),
        )
        # Patch automation tells before any navigation.
        await apply_stealth(context)
        # Re-inject a saved login session (session-only cookies the profile drops).
        await _restore_session(context, args.platform)
        nav_error: str | None = None
        try:
            await _recon(context, args, out_dir)
        except BlockDetectedError as exc:
            blocked_reason = str(exc)
        except PlaywrightError as exc:
            # Network failure, timeout, navigation error — report
            # cleanly rather than dumping a traceback. Artifacts
            # captured so far are still flushed by context.close().
            nav_error = str(exc).splitlines()[0]
        finally:
            await context.close()  # flushes the HAR

    if nav_error is not None:
        print(f"\n!!! NAVIGATION FAILED: {nav_error}", file=sys.stderr)
        print(f"Partial artifacts: {out_dir}", file=sys.stderr)
        return 4

    if blocked_reason is not None:
        print(f"\n!!! BLOCKED: {blocked_reason}", file=sys.stderr)
        print(
            "Recon aborted — the site served a block/challenge page. "
            "Do NOT retry today; treat this platform as in cool-down "
            "(see scripts/recon/README.md).",
            file=sys.stderr,
        )
        print(f"Partial artifacts: {out_dir}", file=sys.stderr)
        return 3

    print(f"\nDone. Artifacts: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
