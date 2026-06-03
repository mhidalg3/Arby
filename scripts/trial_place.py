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
import json
import sys
from collections import defaultdict

import httpx

from src.execution import placers
from src.execution.executor import Leg
from src.execution.leg_placer import BetanoLegPlacer, BetssonLegPlacer
from src.execution.session import InSessionTransport
from src.ingestion.scrapers.betano import BetanoScraper
from src.ingestion.scrapers.betsson import BetssonScraper

TRIAL_HARD_CAP_ARS = 300.0  # a bug cannot bet more than this in trial mode

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
    placement IDs. Pre-match preferred for a trial (stable odds, no suspension)."""
    headers = {"User-Agent": _BROWSER_UA, "Accept-Language": "es-AR,es;q=0.9"}
    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        if platform == "betsson":
            scraper: BetssonScraper | BetanoScraper = BetssonScraper(http_client=client)
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


async def _arm_and_send(args: argparse.Namespace) -> None:
    if args.stake > TRIAL_HARD_CAP_ARS:
        sys.exit(f"REFUSED: stake {args.stake} > trial hard cap {TRIAL_HARD_CAP_ARS} ARS")
    leg = _build_leg(args)
    base = _BASE_URL[args.platform]
    print(f"⚠️  LIVE: sending real {args.stake} ARS on {args.platform} — watch the browser.")
    transport = InSessionTransport(args.platform, dry_run=False)
    async with transport:
        await transport.goto(base)  # same-origin so the in-page fetch carries cookies
        transport.arm()
        if args.platform == "betsson":
            placer = BetssonLegPlacer(
                transport,
                session_token=lambda: transport.eval_js(
                    "JSON.parse(localStorage.getItem('session')||'{}').token || ''"
                ),
            )
            res = await placer.place(leg)
        elif args.platform == "betano":
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
    p.add_argument("--outcome", default="home", help="label only, for logging")
    p.add_argument("--odds", type=float, default=0.0)
    p.add_argument("--stake", type=float, default=50.0)
    p.add_argument("--arm", action="store_true", help="actually send (real money)")
    p.add_argument("--yes-real-money", action="store_true", help="required alongside --arm")
    args = p.parse_args()

    if args.discover:
        asyncio.run(_discover(args.platform))
    elif args.arm:
        if not args.yes_real_money:
            sys.exit("REFUSED: --arm requires --yes-real-money")
        if not args.selection or not args.odds:
            sys.exit("REFUSED: --arm needs --selection and --odds")
        asyncio.run(_arm_and_send(args))
    else:
        if not args.selection or not args.odds:
            sys.exit("preview needs --selection and --odds")
        _preview(args)


if __name__ == "__main__":
    main()
