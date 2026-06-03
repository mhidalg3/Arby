"""Phase-1 trial: place ONE tiny real bet on ONE platform, end to end.

This is the only script that sends real money. It is built to make an accidental
or oversized bet hard:

  * default mode is **preview** — it builds the exact place request and prints it,
    sending nothing. No browser, no network.
  * ``--discover`` is read-only: it lists current bettable events + the real
    selection IDs (so we never hand-guess an outcome id).
  * ``--arm`` is the only mode that sends, and it ALSO requires
    ``--yes-real-money`` and a stake at or under ``TRIAL_HARD_CAP_ARS``. It opens
    a visible browser on the logged-in profile so the operator watches it happen.

Usage:
    uv run python scripts/trial_place.py --platform betsson --discover
    uv run python scripts/trial_place.py --platform betsson \
        --selection s-m-f-...-home --odds 2.62 --stake 50           # preview
    uv run python scripts/trial_place.py --platform betano \
        --selection 9698869897 --event-id 86489358 --odds 3.65 \
        --stake 50 --arm --yes-real-money                            # SENDS
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import httpx

from src.execution import placers
from src.execution.executor import Leg
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer
from src.execution.session import InSessionTransport
from src.ingestion.scrapers.betano import BetanoScraper
from src.ingestion.scrapers.betsson import BetssonScraper

TRIAL_HARD_CAP_ARS = 300.0  # a bug cannot bet more than this in trial mode
REPO_ROOT_ARTIFACTS = Path(__file__).resolve().parent.parent / "recon" / "artifacts"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BASE_URL = {
    "betsson": "https://pba.betsson.bet.ar/apuestas-deportivas",
    "betano": "https://www.betano.bet.ar/",
}


async def _discover(platform: str) -> None:
    """Read-only: print current bettable events grouped by event, with the real
    placement IDs. Pre-match preferred for a trial (stable odds, no suspension).
    For Betsson it also prints the event-page `slug` the armed run navigates to."""
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "es-AR,es;q=0.9"}
    slug_by_event: dict[str, str] = {}
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        if platform == "betsson":
            bsc = BetssonScraper(http_client=client)
            scraper: BetssonScraper | BetanoScraper = bsc
            slug_by_event = {fx.event_id: fx.slug for fx in await bsc._discover_soccer_fixtures()}
        elif platform == "betano":
            scraper = BetanoScraper(http_client=client, mode="prematch")
        else:
            print(f"discovery not wired for {platform}", file=sys.stderr)
            return
        by_event: dict[str, list] = defaultdict(list)
        async for snap in scraper.fetch_live_soccer():
            by_event[snap.raw_event_name].append(snap)

    print(f"\n{len(by_event)} events on {platform}. First 8 with full 1X2:\n")
    for name, snaps in list(by_event.items())[:8]:
        ev = snaps[0].platform_event_id
        print(f"  ▸ {name}   (event_id={ev})")
        if slug_by_event.get(ev):
            print(f"      slug={slug_by_event[ev]}")
        for s in snaps:
            cap = f" max={s.max_stake}" if s.max_stake else ""
            print(
                f"      {s.raw_outcome_name:22} odds={s.decimal_odds:<6} "
                f"selection={s.platform_outcome_id}{cap}"
            )
        print()


def _build_leg(args: argparse.Namespace) -> Leg:
    return Leg(
        platform=args.platform,
        match_id=args.event_id or "trial",
        market="1X2",
        outcome=args.outcome,
        stake_ars=args.stake,
        odds=args.odds,
        platform_outcome_id=args.selection,
    )


def _preview(args: argparse.Namespace) -> None:
    """Build the exact place request the placer would send — print, send nothing.
    For stateful Betano only the first (plain-leg) request is deterministic
    offline; the hash-bearing updatebets/place bodies are shown by the armed run."""
    if args.platform == "betsson":
        req = placers.build_betsson_request([(args.selection, f"{args.odds:.2f}")], args.stake)
        print("POST https://pba.betsson.bet.ar/api/sb/v2/coupons")
    elif args.platform == "betano":
        req = placers.build_betano_plain_leg(args.selection, args.event_id)
        print("POST https://www.betano.bet.ar/api/betslip/v3/plain-leg/  (step 1 of 3)")
    else:
        print(f"preview not wired for {args.platform}", file=sys.stderr)
        return
    print(json.dumps(req, indent=2, ensure_ascii=False))
    print(f"\nstake={args.stake} ARS  odds={args.odds}  (hard cap {TRIAL_HARD_CAP_ARS})")


async def _arm_betsson(args: argparse.Namespace) -> None:
    """Betsson place. The coupons POST needs the *authenticated* session context
    (`x-sb-user-context-id: ctx-…`), which the app resolves only for a genuinely
    logged-in session (NOT derivable offline). A fresh token expiry is NOT enough
    — an anonymous post-logout session also has one — so we gate on
    `/sb/fe-api/v1/user-context` reporting `isLoggedIn: true`. Flow: open the
    profile, land on the event page, wait (up to 180s) for the operator to be
    logged in, reload so the app starts using `ctx-`, capture that live header
    set, and fire the deterministic coupons POST."""
    from playwright.async_api import Request, async_playwright  # noqa: PLC0415

    from scripts.recon.stealth import apply_stealth  # noqa: PLC0415

    url = f"https://pba.betsson.bet.ar/apuestas-deportivas/{args.slug}"
    print(f"⚠️  LIVE: real {args.stake} ARS on betsson — watch the browser.\n  event: {url}")
    captured: dict[str, dict[str, str]] = {}

    state: dict[str, bool] = {}  # login flag, read from the app's own user-context call
    diag: dict[str, list] = {"uc": [], "ctx": []}  # observability for the fail states

    def on_request(req: Request) -> None:
        # Capture the authenticated (ctx-) header set the app uses once logged in.
        uctx = req.headers.get("x-sb-user-context-id", "")
        if uctx.startswith("ctx-"):
            captured["headers"] = dict(req.headers)
            if uctx not in diag["ctx"]:
                diag["ctx"].append(uctx)

    async def on_response(resp: object) -> None:
        # Source of truth for login: the app's OWN user-context response. (A
        # reconstructed call is unreliable — these auth via cookie, not the token
        # header.)
        r = resp  # playwright Response
        if "/sb/fe-api/v1/user-context" in r.url:  # type: ignore[attr-defined]
            status = r.status  # type: ignore[attr-defined]
            li: object = "?"
            try:
                j = await r.json()  # type: ignore[attr-defined]
                li = j.get("userContext", {}).get("isLoggedIn")
                if li:
                    state["logged_in"] = True
            except Exception as exc:  # noqa: BLE001
                li = f"err:{type(exc).__name__}"
            diag["uc"].append((status, li))

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir="recon/profile/betsson",
        headless=False,
        channel="chrome",
        locale="es-AR",
        timezone_id="America/Argentina/Buenos_Aires",
        permissions=["geolocation"],
        # La Plata (PBA provincial capital) → routes to the PBA/Iplyc jurisdiction.
        # CABA city-center coords would route to the CABA jurisdiction instead.
        geolocation={"latitude": -34.9215, "longitude": -57.9545},
    )
    try:
        await apply_stealth(ctx)
        # Listen at the CONTEXT level so a login popup / new tab can't hide the
        # auth requests from us.
        ctx.on("request", on_request)
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)

        drop = {
            ":authority",
            ":method",
            ":path",
            ":scheme",
            "host",
            "content-length",
            "accept-encoding",
        }

        def authed() -> bool:
            # The authenticated ctx- only exists for a logged-in session; the
            # app's user-context isLoggedIn:true is the other proof.
            return bool(state.get("logged_in")) or "headers" in captured

        # Operator-gated, not auto-detected: log in by hand, then press ENTER.
        # Auto-detecting "done logging in" proved racy (the SPA doesn't re-query
        # user-context on its own), so we just ask — the operator runs this in a
        # real terminal, so input() blocks for the keypress.
        if not authed():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                input,
                "\n  ▶ LOG IN in the browser window (stay on PBA / Iplyc). When you see your "
                "balance and you're logged in, press ENTER here to place… ",
            )

        # Reload now that you're authenticated so the app issues the ctx- requests
        # we capture. The betting context lags login — a REFRESH syncs ctx- into
        # the betting layer (operator-confirmed). Refresh up to 3x until a ctx-
        # request appears.
        for attempt in range(3):
            await page.reload(wait_until="networkidle", timeout=60000)
            for _ in range(10):
                if "headers" in captured:
                    break
                await asyncio.sleep(1)
            if "headers" in captured:
                break
            print(f"  ctx- not synced yet (refresh {attempt + 1}/3) — refreshing again…")
        if "headers" not in captured:
            print(
                "\n=== RESULT ===\naccepted=False  detail: authenticated context (ctx-) "
                "not seen after login\n"
                f"  DIAG: user-context responses (status,isLoggedIn) = {diag['uc']}\n"
                f"  DIAG: distinct ctx- contexts seen = {diag['ctx']}\n"
                "  (if user-context is still (200,False) you're not logged in on this "
                "profile/region; if it's 502 the backend is flaking)"
            )
            return

        print(f"  ctx- captured ({diag['ctx'][-1][:26]}…) — placing…")
        headers = {k: v for k, v in captured["headers"].items() if k not in drop}
        headers.update(
            {
                "content-type": "application/json",
                "x-sb-identifier": "BETSLIP_SUBMIT_COUPONS_REQUEST",
            }
        )
        print(f"  authenticated context: {headers['x-sb-user-context-id'][:28]}…")
        # build_betsson_request now includes the validated updateSources.
        body = placers.build_betsson_request([(args.selection, f"{args.odds:.2f}")], args.stake)
        raw = await page.evaluate(
            """async ({url, body, headers}) => {
                const r = await fetch(url, {method:'POST', headers,
                    body: JSON.stringify(body), credentials:'include'});
                return {status: r.status, text: await r.text()};}""",
            {
                "url": "https://pba.betsson.bet.ar/api/sb/v2/coupons",
                "body": body,
                "headers": headers,
            },
        )
        parsed = json.loads(raw["text"]) if raw["text"] else {}
        res = placers.parse_betsson(parsed if isinstance(parsed, dict) else {})
        print(f"\n=== RESULT ===\nHTTP {raw['status']}  accepted={res.accepted}  ref={res.ref!r}")
        print(f"detail: {res.detail or raw['text'][:300]}")
    finally:
        await ctx.close()
        await pw.stop()


async def _capture_betsson_ui(args: argparse.Namespace) -> None:
    """Operator places ONE bet through the app's own UI; we intercept the exact
    coupon request + response. Sidesteps all our auth/ctx-/updateSources detection
    (the app builds a perfect request), gets the first real bet down, and captures
    the working request as a template for the deterministic path. The captured
    body is saved with the sessiontoken redacted."""
    import time as _time  # noqa: PLC0415

    from playwright.async_api import async_playwright  # noqa: PLC0415

    from scripts.recon.stealth import apply_stealth  # noqa: PLC0415

    url = f"https://pba.betsson.bet.ar/apuestas-deportivas/{args.slug}"
    print(f"⚠️  CAPTURE MODE on betsson — you place via the app; I record it.\n  event: {url}")
    cap: dict[str, object] = {}

    def on_request(req: object) -> None:
        r = req
        if r.method == "POST" and "/api/sb/v2/coupons" in r.url:  # type: ignore[attr-defined]
            cap["req_headers"] = dict(r.headers)  # type: ignore[attr-defined]
            cap["req_body"] = r.post_data  # type: ignore[attr-defined]

    async def on_response(resp: object) -> None:
        r = resp
        if "/api/sb/v2/coupons" in r.url and r.request.method == "POST":  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                cap["resp"] = (r.status, await r.text())  # type: ignore[attr-defined]

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir="recon/profile/betsson",
        headless=False,
        channel="chrome",
        locale="es-AR",
        timezone_id="America/Argentina/Buenos_Aires",
        permissions=["geolocation"],
        geolocation={"latitude": -34.9215, "longitude": -57.9545},
    )
    try:
        await apply_stealth(ctx)
        ctx.on("request", on_request)
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)
        print(
            "\n  ▶ In the app: log in (stay on PBA), add a selection to the betslip, "
            "enter your stake, and click APOSTAR.\n  Waiting up to 5 min for your bet…"
        )
        for _ in range(150):
            if "resp" in cap:
                break
            await asyncio.sleep(2)
        if "resp" not in cap:
            print("\n=== RESULT ===\nno coupon POST observed (no bet placed in the window)")
            return
        status, text = cap["resp"]  # type: ignore[misc]
        parsed = json.loads(text) if text else {}
        res = placers.parse_betsson(parsed if isinstance(parsed, dict) else {})
        print(
            f"\n=== RESULT (placed via app UI) ===\nHTTP {status}  "
            f"accepted={res.accepted}  ref={res.ref!r}\ndetail: {res.detail or text[:300]}"
        )
        # Save the exact working request as a template (redact the sessiontoken).
        hdrs = dict(cap.get("req_headers", {}))  # type: ignore[arg-type]
        if "sessiontoken" in hdrs:
            hdrs["sessiontoken"] = "[REDACTED]"
        out = REPO_ROOT_ARTIFACTS / f"betsson_coupon_capture_{int(_time.time())}.json"
        out.write_text(json.dumps({"headers": hdrs, "body": cap.get("req_body")}, indent=2))
        print(f"\n  captured the app's exact coupon request → {out}")
        print("  (compare its `updateSources` + headers to our builder to finalize determinism)")
    finally:
        await ctx.close()
        await pw.stop()


async def _revalidate_betsson(args: argparse.Namespace) -> None:
    """Live re-validate the PRODUCTION path: `InSessionTransport` +
    `BetssonLegPlacer` (which calls `prepare_betsson_context`), not the trial's
    inline recipe. Operator logs in in the window, presses ENTER, then the
    production placer navigates/refresh-syncs/captures/posts on its own."""
    leg = Leg(
        platform="betsson-pba",
        match_id="trial",
        market="1X2",
        outcome=args.outcome,
        stake_ars=args.stake,
        odds=args.odds,
        platform_outcome_id=args.selection,
        platform_event_ref=args.slug,
    )
    print(f"⚠️  LIVE (via production BetssonLegPlacer): real {args.stake} ARS — watch the browser.")
    transport = InSessionTransport("betsson", dry_run=False)
    async with transport:
        transport.arm()
        await transport.goto("https://pba.betsson.bet.ar/apuestas-deportivas")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            input,
            "\n  ▶ In the window: LOG IN (stay on PBA), then drive the SPA into the placeable "
            "state — click your profile/balance → My Account (this in-app nav establishes the "
            "betting context). Do NOT refresh (a reload destroys it). When the betslip would let "
            "you place (green button), press ENTER — the production placer reads the live ctx- "
            "and posts (no navigation/reload)… ",
        )
        res = await BetssonLegPlacer(transport).place(leg)
    print(
        f"\n=== RESULT (production path) ===\naccepted={res.accepted}  ref={res.ref!r}\n"
        f"detail: {res.detail}"
    )


async def _arm_and_send(args: argparse.Namespace) -> None:
    if args.stake > TRIAL_HARD_CAP_ARS:
        sys.exit(f"REFUSED: stake {args.stake} > trial hard cap {TRIAL_HARD_CAP_ARS} ARS")
    if args.capture_ui and args.platform == "betsson":
        await _capture_betsson_ui(args)
        return
    if args.via_placer and args.platform == "betsson":
        await _revalidate_betsson(args)
        return
    if args.platform == "betsson":
        await _arm_betsson(args)
        return
    leg = _build_leg(args)
    base = _BASE_URL[args.platform]
    print(f"⚠️  LIVE: sending real {args.stake} ARS on {args.platform} — watch the browser.")
    transport = InSessionTransport(args.platform, dry_run=False)
    async with transport:
        await transport.goto(base)  # same-origin so the in-page fetch carries cookies
        transport.arm()
        if args.platform == "betano":
            res = await BetanoLegPlacer(transport).place(leg)
        else:
            sys.exit(f"arm not wired for {args.platform}")
    print("\n=== RESULT ===")
    print(
        f"accepted={res.accepted}  ref={res.ref!r}  "
        f"stake_filled={res.stake_filled}  odds_filled={res.odds_filled}"
    )
    print(f"detail: {res.detail}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--platform", required=True, choices=["betsson", "betano"])
    p.add_argument("--discover", action="store_true")
    p.add_argument("--selection", default="", help="platform_outcome_id from --discover")
    p.add_argument("--event-id", default="", help="Betano eventId (from --discover)")
    p.add_argument("--slug", default="", help="Betsson event-page slug (from --discover)")
    p.add_argument("--outcome", default="home", help="label only, for logging")
    p.add_argument("--odds", type=float, default=0.0)
    p.add_argument("--stake", type=float, default=50.0)
    p.add_argument("--arm", action="store_true", help="actually send (real money)")
    p.add_argument("--yes-real-money", action="store_true", help="required alongside --arm")
    p.add_argument(
        "--capture-ui",
        action="store_true",
        help="betsson: operator places via the app UI; intercept the exact request",
    )
    p.add_argument(
        "--via-placer",
        action="store_true",
        help="betsson: place through the production BetssonLegPlacer + transport",
    )
    args = p.parse_args()

    if args.discover:
        asyncio.run(_discover(args.platform))
    elif args.arm:
        if not args.yes_real_money:
            sys.exit("REFUSED: --arm requires --yes-real-money")
        if args.capture_ui:
            if args.platform != "betsson" or not args.slug:
                sys.exit("REFUSED: --capture-ui is betsson-only and needs --slug")
        else:
            if not args.selection or not args.odds:
                sys.exit("REFUSED: --arm needs --selection and --odds")
            if args.platform == "betsson" and not args.slug:
                sys.exit("REFUSED: betsson --arm needs --slug (from --discover)")
            if args.platform == "betano" and not args.event_id:
                sys.exit("REFUSED: betano --arm needs --event-id (from --discover)")
        asyncio.run(_arm_and_send(args))
    else:
        if not args.selection or not args.odds:
            sys.exit("preview needs --selection and --odds")
        _preview(args)


if __name__ == "__main__":
    main()
