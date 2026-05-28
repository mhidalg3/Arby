"""One real-API polling cycle against Bplay PBA — first end-to-end smoke.

Validates that the scraper survives contact with the live SportNCO XML
feeds and that the parser shape matches today's response. Read-only
(GET only), no logins, no bet-slip activity. Same envelope as
`scripts/smoke_betsson.py`.

Usage:
    uv run python scripts/smoke_bplay.py                   # all marquee comps
    BPLAY_LIMIT=10 uv run python scripts/smoke_bplay.py    # cap snapshots
    BPLAY_COMPS=63057 uv run python scripts/smoke_bplay.py # World Cup only
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from itertools import islice

import httpx

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.ingestion.scrapers.bplay import (
    TARGET_COMPETITIONS,
    BplayContractError,
    BplayPbaScraper,
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml, text/xml, */*",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.5",
    "Referer": "https://deportespba.bplay.bet.ar/",
}


def _summarize(snaps: list[RawOddsSnapshot]) -> None:
    print()
    print("=" * 72)
    print(f"{len(snaps)} snapshots")
    print("=" * 72)
    if not snaps:
        print("  (no matches in any target competition right now)")
        return
    print(f"  fixtures: {len({s.platform_event_id for s in snaps})}")
    print(f"  markets:  {len({s.platform_market_id for s in snaps})}")
    print(f"  platform: {snaps[0].platform}")
    print()
    print("  markets-by-friendly-name (top 10):")
    market_names = Counter(s.raw_market_name for s in snaps)
    for name, count in market_names.most_common(10):
        print(f"    {count:>3}  {name}")
    print()
    print("  sample snapshots (first 6):")
    for s in islice(snaps, 6):
        print(f"    [{s.raw_event_name}] {s.raw_market_name}")
        print(
            f"      outcome: {s.raw_outcome_name}  odds: {s.decimal_odds}"
            f"  market_id: {s.platform_market_id}"
        )


async def main() -> int:
    # Optional competition filter
    comps_env = os.environ.get("BPLAY_COMPS", "").strip()
    if comps_env:
        try:
            wanted_ids = {int(x) for x in comps_env.split(",")}
        except ValueError:
            print(
                f"error: BPLAY_COMPS must be comma-separated ints, got {comps_env!r}",
                file=sys.stderr,
            )
            return 2
        competitions = {
            cid: label for cid, label in TARGET_COMPETITIONS.items() if cid in wanted_ids
        }
        if not competitions:
            print(
                f"error: BPLAY_COMPS={wanted_ids!r} matched no known competitions {set(TARGET_COMPETITIONS)}",
                file=sys.stderr,
            )
            return 2
    else:
        competitions = None  # use scraper default

    limit_str = os.environ.get("BPLAY_LIMIT", "")
    limit = int(limit_str) if limit_str else None
    print(
        f"Bplay smoke — competitions={list(competitions) if competitions else 'all marquee'}  "
        f"limit={limit or 'no cap'}",
        flush=True,
    )

    snaps: list[RawOddsSnapshot] = []
    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=True,
    ) as client:
        scraper = BplayPbaScraper(http_client=client, competitions=competitions)
        try:
            async for snap in scraper.fetch_live_soccer():
                snaps.append(snap)
                if limit is not None and len(snaps) >= limit:
                    break
        except BplayContractError as exc:
            print(f"\ncontract error: {exc}", file=sys.stderr)
            _summarize(snaps)
            return 1
        except httpx.HTTPError as exc:
            print(f"\nhttp error: {exc!r}", file=sys.stderr)
            _summarize(snaps)
            return 1

    _summarize(snaps)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
