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
    uv run python scripts/trial_place.py --platform betwarrior --discover
    uv run python scripts/trial_place.py --platform betwarrior \
        --selection 4206111729 --event-id 1027854437 --odds 1.41 \
        --stake 50 --arm --yes-real-money     # re-reads live odds, places if within tol; SENDS
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
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer, BetWarriorLegPlacer
from src.execution.session import InSessionTransport
from src.ingestion.scrapers.betano import BetanoScraper
from src.ingestion.scrapers.betsson import BetssonScraper
from src.ingestion.scrapers.betwarrior import BetWarriorPbaDepthScraper, BetWarriorPbaScraper

TRIAL_HARD_CAP_ARS = 300.0  # a bug cannot bet more than this in trial mode
REPO_ROOT_ARTIFACTS = Path(__file__).resolve().parent.parent / "recon" / "artifacts"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BASE_URL = {
    "betsson": "https://pba.betsson.bet.ar/apuestas-deportivas",
    "betano": "https://www.betano.bet.ar/",
    "betwarrior": "https://pba.betwarrior.bet.ar/",
    "bplay": "https://deportespba.bplay.bet.ar/",
}

# URL substrings that mark an authenticated session / readiness call — the API
# equivalents of "is the place button green". `--capture-session` records these so
# the per-platform readiness check is built against the real contract, not a guess.
_READINESS_HINTS = (
    "validate", "balance", "account", "wallet", "session", "punter",
    "user-context", "profile", "kambicdn.com/player/",
)


async def _discover(platform: str) -> None:
    """Read-only: print current bettable events grouped by event, with the real
    placement IDs. Pre-match preferred for a trial (stable odds, no suspension).
    For Betsson it also prints the event-page `slug` the armed run navigates to."""
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "es-AR,es;q=0.9"}
    slug_by_event: dict[str, str] = {}
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        if platform == "betsson":
            bsc = BetssonScraper(http_client=client)
            scraper: BetssonScraper | BetanoScraper | BetWarriorPbaScraper = bsc
            slug_by_event = {fx.event_id: fx.slug for fx in await bsc._discover_soccer_fixtures()}
        elif platform == "betano":
            scraper = BetanoScraper(http_client=client, mode="prematch")
        elif platform == "betwarrior":
            scraper = BetWarriorPbaScraper(http_client=client)
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
    elif args.platform == "betwarrior":
        req = placers.build_betwarrior_request(
            outcome_id=int(args.selection),
            odds_x1000=round(args.odds * 1000),
            stake_thousandths=round(args.stake * 1000),
        )
        print("POST https://cf-al-auth-api.kambicdn.com/player/api/v2019/bwargbap/coupon.json")
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


async def _arm_betano(args: argparse.Namespace) -> None:
    """Betano (Kaizen) place via the production BetanoLegPlacer + transport.
    Cookie-authenticated (no Betsson-style ctx-/SPA-context dance expected), but
    the stored session goes stale, so we pause for an operator login first. The
    placer runs plain-leg → updatebets → place over the in-session fetch."""
    leg = _build_leg(args)  # match_id=event_id, platform_outcome_id=selection
    print(f"⚠️  LIVE (Betano, production placer): real {args.stake} ARS — watch the browser.")
    transport = InSessionTransport("betano", dry_run=False)
    async with transport:
        transport.arm()
        await transport.goto(_BASE_URL["betano"])  # same-origin so the fetch carries cookies
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            input,
            "\n  ▶ LOG IN to Betano in the window (complete any anti-bot challenge). When you "
            "see your balance / logged-in state, press ENTER to place… ",
        )
        res = await BetanoLegPlacer(transport).place(leg)
    print(
        f"\n=== RESULT (betano) ===\naccepted={res.accepted}  ref={res.ref!r}  "
        f"stake_filled={res.stake_filled}  odds_filled={res.odds_filled}\ndetail: {res.detail}"
    )


async def _capture_bplay_ui(args: argparse.Namespace) -> None:
    """Operator places ONE bet via the Bplay app; capture the FULL bettingslip flow
    (togglebet → place) + the rotating ``csrf_token`` — ground truth for the stateful
    placer + where the bootstrap CSRF comes from. Bodies printed + saved."""
    import time as _time  # noqa: PLC0415

    from playwright.async_api import async_playwright  # noqa: PLC0415

    from scripts.recon.stealth import apply_stealth  # noqa: PLC0415

    print(f"⚠️  CAPTURE MODE on bplay — you place via the app; I record it.\n  {_BASE_URL['bplay']}")
    calls: list[dict[str, object]] = []
    resps: dict[str, tuple[int, str]] = {}

    def on_request(req: object) -> None:
        r = req
        if r.method == "POST" and "/bettingslip" in r.url:  # type: ignore[attr-defined]
            calls.append({"url": r.url, "body": r.post_data})  # type: ignore[attr-defined]

    async def on_response(resp: object) -> None:
        r = resp
        if "/bettingslip" in r.url and r.request.method == "POST":  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                resps[r.url] = (r.status, (await r.text())[:500])  # type: ignore[attr-defined]

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir="recon/profile/bplay",
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
        await page.goto(_BASE_URL["bplay"], wait_until="networkidle", timeout=60000)
        print(
            "\n  ▶ In the app: log in, open a match, add a selection, enter a tiny stake, and "
            "PLACE the bet.\n  Waiting up to 8 min — the window stays open until you actually "
            "place (togglebet/update don't end it)…"
        )

        def _placed() -> bool:
            # The PLACE call is `.../bettingslip` exactly; togglebet/update are sub-paths.
            return any(
                str(c["url"]).split("?")[0].rstrip("/").endswith("/bettingslip") for c in calls
            )

        for _ in range(240):
            if _placed():
                break
            await asyncio.sleep(2)
        if not calls:
            print("\n=== RESULT ===\nno bettingslip POST observed (no bet placed in the window)")
            return
        complete = _placed()
        tag = "COMPLETE" if complete else "INCOMPLETE — the PLACE call (.../bettingslip) was NOT seen"
        print(f"\n=== CAPTURED bettingslip flow on bplay ({len(calls)} calls) — {tag} ===")
        for c in calls:
            print(f"\n  POST {c['url']}")
            if c["body"]:
                print(f"     body: {str(c['body'])[:400]}")
            rp = resps.get(str(c["url"]))
            if rp:
                print(f"     -> {rp[0]}  {rp[1]}")
        out = REPO_ROOT_ARTIFACTS / f"bplay_betslip_capture_{int(_time.time())}.json"
        out.write_text(json.dumps({"calls": calls, "responses": {k: list(v) for k, v in resps.items()}}, indent=2))
        print(f"\n  saved → {out}")
        if not complete:
            print("  ⚠️  Re-run and actually PLACE the bet — I need the .../bettingslip POST.")
    finally:
        await ctx.close()
        await pw.stop()


async def _capture_session(platform: str) -> None:
    """Read-only: record the authenticated session/readiness API calls the app makes
    (validate.json / balance / account / user-context) so the per-platform "is this
    session placeable?" check can be built against the real contract. NO bet placed."""
    import time as _time  # noqa: PLC0415

    from playwright.async_api import async_playwright  # noqa: PLC0415

    from scripts.recon.stealth import apply_stealth  # noqa: PLC0415

    reqs: list[dict[str, object]] = []
    resps: dict[str, tuple[int, str]] = {}

    def on_request(req: object) -> None:
        u = req.url  # type: ignore[attr-defined]
        if any(h in u.lower() for h in _READINESS_HINTS):
            reqs.append({"method": req.method, "url": u, "body": req.post_data})  # type: ignore[attr-defined]

    async def on_response(resp: object) -> None:
        u = resp.url  # type: ignore[attr-defined]
        if any(h in u.lower() for h in _READINESS_HINTS):
            with contextlib.suppress(Exception):
                resps[u] = (resp.status, (await resp.text())[:400])  # type: ignore[attr-defined]

    print(f"⚠️  CAPTURE SESSION on {platform} (read-only; no bet).\n  {_BASE_URL[platform]}")
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=f"recon/profile/{platform}",
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
        await page.goto(_BASE_URL[platform], wait_until="networkidle", timeout=60000)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            input,
            f"\n  ▶ Log into {platform}, VIEW YOUR BALANCE, and ADD a selection to the betslip "
            "with a stake (do NOT place). That fires the session/validate calls.\n"
            "  Press ENTER when done to dump what I captured… ",
        )
        print(f"\n=== CAPTURED session/readiness calls on {platform} ({len(reqs)}) ===")
        for r in reqs:
            resp = resps.get(str(r["url"]))
            print(f"\n  {r['method']} {r['url']}")
            if r["body"]:
                print(f"     body: {str(r['body'])[:300]}")
            if resp:
                print(f"     -> {resp[0]}  {resp[1]}")
        out = REPO_ROOT_ARTIFACTS / f"{platform}_session_capture_{int(_time.time())}.json"
        out.write_text(
            json.dumps({"requests": reqs, "responses": {k: list(v) for k, v in resps.items()}}, indent=2)
        )
        print(f"\n  saved → {out}")
        print("  Paste the validate / balance / session call (URL, method, body, response) and")
        print("  I'll build the readiness check against it.")
    finally:
        await ctx.close()
        await pw.stop()


async def _capture_popup(platform: str, minutes: float) -> None:
    """Leave a logged-in window open and WATCH for the responsible-gambling LOCKOUT
    overlay (the "TOMATE UN DESCANSO — 12h de descanso" mandatory-break popup that
    blocked all three sessions during the long deployment). Read-only; never places.

    Polls the DOM on an interval; when a lockout phrase OR a visible modal/overlay
    appears it dumps the element HTML + a full-page screenshot + the elapsed watch
    time, so we can (a) ground the EXACT selector for `check_session_blocked` and
    (b) learn the trigger — how long until it fires, and at what wall-clock time."""
    import time as _time  # noqa: PLC0415

    from playwright.async_api import async_playwright  # noqa: PLC0415

    from scripts.recon.stealth import apply_stealth  # noqa: PLC0415
    from src.execution.session import _RG_BLOCK_PHRASES  # noqa: PLC0415

    poll_sec = 15.0
    started = _time.time()
    deadline = started + minutes * 60.0
    print(
        f"⚠️  WATCH POPUP on {platform} (read-only; no bet). Watching {minutes:.0f} min.\n"
        f"  {_BASE_URL[platform]}\n  Log in, then leave the window open — Ctrl-C to stop early."
    )
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=f"recon/profile/{platform}",
        headless=False,
        channel="chrome",
        locale="es-AR",
        timezone_id="America/Argentina/Buenos_Aires",
        permissions=["geolocation"],
        geolocation={"latitude": -34.9215, "longitude": -57.9545},
    )
    try:
        await apply_stealth(ctx)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(_BASE_URL[platform], wait_until="networkidle", timeout=60000)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, input, "\n  ▶ Log in, then press ENTER to start watching… "
        )
        fired = False
        while _time.time() < deadline:
            try:
                found = await page.evaluate(
                    """(phrases) => {
                        const body = (document.body && document.body.innerText || '').toLowerCase();
                        const hits = phrases.filter(p => body.includes(p));
                        const sel = '[role=dialog],[aria-modal=true],.modal,.overlay,.modal-overlay';
                        const dialogs = [...document.querySelectorAll(sel)]
                            .filter(el => el.offsetParent !== null
                                && el.getBoundingClientRect().width > 0)
                            .map(el => ({tag: el.tagName, cls: String(el.className),
                                         html: el.outerHTML.slice(0, 6000)}));
                        return {hits, dialogs};
                    }""",
                    list(_RG_BLOCK_PHRASES),
                )
            except Exception as exc:  # noqa: BLE001 — keep watching through a transient read fault
                print(f"  (read fault: {exc})")
                await asyncio.sleep(poll_sec)
                continue
            elapsed_min = (_time.time() - started) / 60.0
            if found["hits"] or found["dialogs"]:
                ts = int(_time.time())
                out = REPO_ROOT_ARTIFACTS / f"{platform}_popup_capture_{ts}.json"
                out.write_text(
                    json.dumps(
                        {
                            "platform": platform,
                            "elapsed_min": round(elapsed_min, 1),
                            "wall_clock": _time.strftime("%Y-%m-%d %H:%M:%S"),
                            "url": page.url,
                            "phrase_hits": found["hits"],
                            "dialogs": found["dialogs"],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                with contextlib.suppress(Exception):
                    await page.screenshot(
                        path=str(REPO_ROOT_ARTIFACTS / f"{platform}_popup_{ts}.png"), full_page=True
                    )
                tag = "LOCKOUT PHRASE" if found["hits"] else "modal/overlay"
                print(
                    f"\n  🚫 {tag} after {elapsed_min:.1f} min "
                    f"(hits={found['hits']}) → saved {out.name}"
                )
                fired = True
                # Keep watching (in case it clears + recurs) but back off so we don't
                # spam identical dumps every poll.
                await asyncio.sleep(60.0)
                continue
            print(f"  …{elapsed_min:.0f} min: clear")
            await asyncio.sleep(poll_sec)
        print(f"\n  watch ended ({'a popup fired' if fired else 'no popup seen'}).")
    finally:
        await ctx.close()
        await pw.stop()


async def _capture_betwarrior_ui(args: argparse.Namespace) -> None:
    """Operator places ONE bet through the BetWarrior app UI; we intercept the exact
    `coupon.json` request the app sends — ground truth for the odds scale, the
    allowOddsChange value, and every field — so the deterministic builder can be
    reconciled instead of guessed. The body is printed and saved (bearer redacted)."""
    import time as _time  # noqa: PLC0415

    from playwright.async_api import async_playwright  # noqa: PLC0415

    from scripts.recon.stealth import apply_stealth  # noqa: PLC0415

    print(f"⚠️  CAPTURE MODE on betwarrior — you place via the app; I record it.\n  {_BASE_URL['betwarrior']}")
    cap: dict[str, object] = {}

    def on_request(req: object) -> None:
        r = req
        # The place is `.../bwargbap/coupon.json`; the pre-check is `.../coupon/validate.json`
        # (which does NOT contain "/coupon.json"), so this matches only the real placement.
        if r.method == "POST" and "/coupon.json" in r.url:  # type: ignore[attr-defined]
            cap["req_url"] = r.url  # type: ignore[attr-defined]
            cap["req_headers"] = dict(r.headers)  # type: ignore[attr-defined]
            cap["req_body"] = r.post_data  # type: ignore[attr-defined]

    async def on_response(resp: object) -> None:
        r = resp
        if "/coupon.json" in r.url and r.request.method == "POST":  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                cap["resp"] = (r.status, await r.text())  # type: ignore[attr-defined]

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir="recon/profile/betwarrior",
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
        await page.goto(_BASE_URL["betwarrior"], wait_until="networkidle", timeout=60000)
        print(
            "\n  ▶ In the app: log in, open a match, add a selection, enter a tiny stake, and "
            "place the bet.\n  Waiting up to 5 min for your coupon POST…"
        )
        for _ in range(150):
            if "resp" in cap:
                break
            await asyncio.sleep(2)
        if "req_body" not in cap:
            print("\n=== RESULT ===\nno coupon.json POST observed (no bet placed in the window)")
            return
        status, text = cap.get("resp", (0, ""))  # type: ignore[misc]
        print(f"\n=== CAPTURED coupon.json (HTTP {status}) ===\n  url: {cap.get('req_url')}")
        print("  BODY (this is what we must replicate):")
        print(cap.get("req_body"))
        hdrs = dict(cap.get("req_headers", {}))  # type: ignore[arg-type]
        if "authorization" in hdrs:
            hdrs["authorization"] = "[REDACTED]"
        out = REPO_ROOT_ARTIFACTS / f"betwarrior_coupon_capture_{int(_time.time())}.json"
        out.write_text(json.dumps({"headers": hdrs, "body": cap.get("req_body")}, indent=2))
        print(f"\n  saved → {out}")
        print("  Paste the BODY here and I'll reconcile build_betwarrior_request (odds scale,")
        print("  allowOddsChange value, fields) to match it exactly.")
    finally:
        await ctx.close()
        await pw.stop()


async def _betwarrior_live_odds(event_id: str, outcome_id: str) -> float | None:
    """Re-fetch BetWarrior's CURRENT odds for one outcome via the public Kambi per-event
    endpoint — the live re-verify primitive (so placement uses fresh odds, not a stale
    CLI value). Returns None if the outcome isn't found."""
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "es-AR,es;q=0.9"}
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        depth = BetWarriorPbaDepthScraper(http_client=client)
        for snap in await depth.fetch_event_quotes(event_id):
            if snap.platform_outcome_id == outcome_id:
                return snap.decimal_odds
    return None


async def _arm_betwarrior(args: argparse.Namespace) -> None:
    """BetWarrior (Kambi) place via the production BetWarriorLegPlacer + transport, with a
    LIVE odds re-verify at placement time: re-fetch the selection's current odds, confirm
    they still clear the arb threshold (within `--odds-tolerance-pct` of the expected
    `--odds`), then place AT the current odds. This is the dynamic-capture model — not a
    stale fixed odds. The placer reads the Kambi session bearer the transport captures from
    the SPA's authenticated calls, so be logged in with the balance visible."""
    print(f"⚠️  LIVE (BetWarrior, production placer): real {args.stake} ARS — watch the browser.")
    transport = InSessionTransport("betwarrior", dry_run=False)
    async with transport:
        transport.arm()
        await transport.goto(_BASE_URL["betwarrior"])
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            input,
            "\n  ▶ LOG IN to BetWarrior in the window. Make sure your BALANCE is visible "
            "(that fires the authenticated call whose bearer we capture). Then press ENTER "
            "— I'll re-read the live odds and place if they still clear the threshold… ",
        )
        # Dynamic capture: re-read the CURRENT odds right now (not the stale CLI value).
        current = await _betwarrior_live_odds(args.event_id, args.selection)
        if current is None:
            print("\n=== RESULT (betwarrior) ===\nABORT: couldn't read live odds for that "
                  "selection (check --event-id / --selection from --discover) — nothing placed")
            return
        floor = args.odds * (1.0 - args.odds_tolerance_pct / 100.0)
        print(
            f"  expected(discovery)={args.odds}  live={current}  "
            f"threshold floor={floor:.3f} (tol {args.odds_tolerance_pct}%)"
        )
        # Arb-threshold check: only place if the live odds haven't dropped below the floor
        # (higher is always fine — better for us). This is what `odds_still_acceptable` does.
        if current < floor:
            print(
                f"\n=== RESULT (betwarrior) ===\nABORT: live odds {current} < floor {floor:.3f} "
                "— drifted past tolerance, the arb would not hold. Nothing placed."
            )
            return
        # Place AT the verified current odds — Kambi wants the EXACT current odds with
        # allowOddsChange:NO (as the app does); a sub-second move just 400s and we retry.
        leg = Leg(
            platform="betwarrior-pba", match_id=args.event_id, market="1X2",
            outcome=args.outcome, stake_ars=args.stake, odds=current,
            platform_outcome_id=args.selection,
        )
        res = await BetWarriorLegPlacer(transport).place(leg)
    print(
        f"\n=== RESULT (betwarrior) ===\naccepted={res.accepted}  ref={res.ref!r}  "
        f"stake_filled={res.stake_filled}  odds_filled={res.odds_filled}\ndetail: {res.detail}"
    )


class _PrintNotifier:
    """Prints the executor's play-by-play to the console (trial visibility)."""

    async def send(self, text: str) -> bool:
        print(f"  [notify] {text}")
        return True


async def _two_leg(args: argparse.Namespace) -> None:
    """Live two-leg execution mechanics test: Betsson + Betano on tiny stakes via
    the production Executor (per-platform placer routing, naked-exposure guards).
    NOT a verified arb — a small loss is expected; this proves the machinery.
    Betsson is Leg A (fragile context) so its failure aborts before the Betano leg."""
    from src.execution.executor import Executor  # noqa: PLC0415
    from src.execution.guardrails import Guardrails  # noqa: PLC0415
    from src.execution.recovery import HumanRecoveryHandler  # noqa: PLC0415

    leg_a = Leg(
        platform="betsson-pba",
        match_id="2leg-betsson",
        market="1X2",
        outcome=args.betsson_outcome,
        stake_ars=args.stake,
        odds=args.betsson_odds,
        platform_outcome_id=args.betsson_selection,
        platform_event_ref=args.betsson_slug,
    )
    leg_b = Leg(
        platform="betano-pba",
        match_id=args.betano_event_id,
        market="1X2",
        outcome=args.betano_outcome,
        stake_ars=args.stake,
        odds=args.betano_odds,
        platform_outcome_id=args.betano_selection,
        # Betano's max stake is dynamic; the guardrail fail-closes without a live
        # limit. Operator-provided stand-in here (real exec queries the limits API).
        live_max_stake_ars=args.betano_max_stake or None,
    )
    print(
        f"⚠️  LIVE TWO-LEG (mechanics test, NOT a verified arb): real {args.stake} ARS ×2 — "
        f"Betsson {args.betsson_outcome} @ {args.betsson_odds} + Betano {args.betano_outcome} "
        f"@ {args.betano_odds}. A small loss is expected."
    )
    betsson_t = InSessionTransport("betsson", dry_run=False)
    betano_t = InSessionTransport("betano", dry_run=False)
    async with betsson_t, betano_t:
        betsson_t.arm()
        betano_t.arm()
        await betsson_t.goto("https://pba.betsson.bet.ar/apuestas-deportivas")
        await betano_t.goto(_BASE_URL["betano"])
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            input,
            "\n  ▶ Ready BOTH windows, then press ENTER:\n"
            "     • Betano: log in (complete any challenge).\n"
            "     • Betsson: log in, then click profile → My Account so the betslip is "
            "placeable — do NOT refresh.\n  ENTER to run the two-leg execution… ",
        )
        cap = max(args.stake * 5, 1000.0)
        guard = Guardrails(
            max_position_per_match_ars=cap,
            max_total_exposure_ars=cap,
            max_daily_loss_ars=cap,
            odds_tolerance_pct=100.0,  # mechanics test: don't abort on odds drift
        )
        notifier = _PrintNotifier()
        executor = Executor(
            guardrails=guard,
            notifier=notifier,
            recovery=HumanRecoveryHandler(notifier),
            placers={
                "betsson-pba": BetssonLegPlacer(betsson_t),
                "betano-pba": BetanoLegPlacer(betano_t),
            },
            dry_run=False,
        )
        result = await executor.execute_two_leg("trial-2leg", leg_a, leg_b)
    print(
        f"\n=== TWO-LEG RESULT ===\noutcome={result.outcome}\nreason={result.reason!r}\n"
        f"leg_a (betsson): {result.leg_a}\nleg_b (betano):  {result.leg_b}"
    )


async def _arm_and_send(args: argparse.Namespace) -> None:
    if args.stake > TRIAL_HARD_CAP_ARS:
        sys.exit(f"REFUSED: stake {args.stake} > trial hard cap {TRIAL_HARD_CAP_ARS} ARS")
    if args.capture_ui and args.platform == "betsson":
        await _capture_betsson_ui(args)
        return
    if args.capture_ui and args.platform == "betwarrior":
        await _capture_betwarrior_ui(args)
        return
    if args.capture_ui and args.platform == "bplay":
        await _capture_bplay_ui(args)
        return
    if args.via_placer and args.platform == "betsson":
        await _revalidate_betsson(args)
        return
    if args.platform == "betsson":
        await _arm_betsson(args)
        return
    if args.platform == "betano":
        await _arm_betano(args)
        return
    if args.platform == "betwarrior":
        await _arm_betwarrior(args)
        return
    sys.exit(f"arm not wired for {args.platform}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--platform", choices=["betsson", "betano", "betwarrior", "bplay"])
    p.add_argument("--discover", action="store_true")
    p.add_argument("--selection", default="", help="platform_outcome_id from --discover")
    p.add_argument("--event-id", default="", help="Betano eventId (from --discover)")
    p.add_argument("--slug", default="", help="Betsson event-page slug (from --discover)")
    p.add_argument("--outcome", default="home", help="label only, for logging")
    p.add_argument("--odds", type=float, default=0.0, help="expected odds (from discovery) — the re-verify floor reference")
    p.add_argument(
        "--odds-tolerance-pct",
        type=float,
        default=2.0,
        help="betwarrior: live odds may drop at most this %% below --odds and still place",
    )
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
    p.add_argument(
        "--capture-popup",
        action="store_true",
        help="watch a logged-in window for the responsible-gambling lockout overlay (read-only)",
    )
    p.add_argument(
        "--watch-minutes",
        type=float,
        default=720.0,
        help="how long --capture-popup watches before exiting (default 720 = 12h)",
    )
    p.add_argument(
        "--capture-session",
        action="store_true",
        help="read-only: record the session/readiness API calls (validate/balance) — no bet",
    )
    # Two-leg mechanics test (Betsson Leg A + Betano Leg B, both live).
    p.add_argument("--two-leg", action="store_true", help="run a live two-leg execution test")
    p.add_argument("--betsson-slug", default="")
    p.add_argument("--betsson-selection", default="")
    p.add_argument("--betsson-odds", type=float, default=0.0)
    p.add_argument("--betsson-outcome", default="home")
    p.add_argument("--betano-event-id", default="")
    p.add_argument("--betano-selection", default="")
    p.add_argument("--betano-odds", type=float, default=0.0)
    p.add_argument("--betano-outcome", default="away")
    p.add_argument(
        "--betano-max-stake",
        type=float,
        default=0.0,
        help="two-leg: live max-stake for the Betano leg (its cap is dynamic; guard fails closed without it)",
    )
    args = p.parse_args()

    if args.two_leg:
        if not args.yes_real_money:
            sys.exit("REFUSED: --two-leg places real money; pass --yes-real-money")
        if args.stake > TRIAL_HARD_CAP_ARS:
            sys.exit(f"REFUSED: stake {args.stake} > trial hard cap {TRIAL_HARD_CAP_ARS} ARS")
        missing = [
            f
            for f in (
                "betsson_slug",
                "betsson_selection",
                "betsson_odds",
                "betano_event_id",
                "betano_selection",
                "betano_odds",
            )
            if not getattr(args, f)
        ]
        if missing:
            sys.exit(f"REFUSED: --two-leg needs {missing}")
        asyncio.run(_two_leg(args))
        return
    if not args.platform:
        sys.exit("--platform is required (or use --two-leg)")
    if args.capture_popup:
        asyncio.run(_capture_popup(args.platform, args.watch_minutes))  # read-only, no bet
        return
    if args.capture_session:
        asyncio.run(_capture_session(args.platform))  # read-only, no bet
        return
    if args.discover:
        asyncio.run(_discover(args.platform))
    elif args.arm:
        if not args.yes_real_money:
            sys.exit("REFUSED: --arm requires --yes-real-money")
        if args.capture_ui:
            if args.platform not in ("betsson", "betwarrior", "bplay"):
                sys.exit("REFUSED: --capture-ui supports betsson / betwarrior / bplay")
            if args.platform == "betsson" and not args.slug:
                sys.exit("REFUSED: betsson --capture-ui needs --slug")
        else:
            if not args.selection or not args.odds:
                sys.exit("REFUSED: --arm needs --selection and --odds")
            if args.platform == "betsson" and not args.slug:
                sys.exit("REFUSED: betsson --arm needs --slug (from --discover)")
            if args.platform == "betano" and not args.event_id:
                sys.exit("REFUSED: betano --arm needs --event-id (from --discover)")
            if args.platform == "betwarrior" and not args.event_id:
                sys.exit("REFUSED: betwarrior --arm needs --event-id (for the live odds re-verify)")
        asyncio.run(_arm_and_send(args))
    else:
        if not args.selection or not args.odds:
            sys.exit("preview needs --selection and --odds")
        _preview(args)


if __name__ == "__main__":
    main()
