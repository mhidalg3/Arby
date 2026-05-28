"""One real-API polling cycle against Betsson PBA — first end-to-end smoke.

Validates that the scraper built from recon survives contact with the
live site: the parser shape matches today's response, no shape drift,
no anti-bot block on the JSON endpoints with a realistic User-Agent.

This hits the real Betsson API. Read-only (GET only), no logins, no
bet-slip activity. It is the production-volume equivalent of opening
the sportsbook in a browser tab.

Usage:
    uv run python scripts/smoke_betsson.py                   # PBA, default
    BETSSON_SUBDOMAIN=caba uv run python scripts/smoke_betsson.py
    BETSSON_LIMIT=5 uv run python scripts/smoke_betsson.py   # cap snapshots
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from itertools import islice

import httpx

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.ingestion.scrapers.betsson import (
    VALID_SUBDOMAINS,
    BetssonContractError,
    BetssonScraper,
)

# Realistic Chrome on macOS — httpx's default `python-httpx/...` UA is
# the surest way to a 403 from AWS WAF. Spanish accept-language matches
# the Argentine site's locale.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.5",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


def _summarize(snaps: list[RawOddsSnapshot]) -> None:
    print()
    print("=" * 72)
    print(f"{len(snaps)} snapshots")
    print("=" * 72)
    if not snaps:
        print("  (nothing to show — no open Argentine soccer markets right now)")
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
    subdomain = os.environ.get("BETSSON_SUBDOMAIN", "pba")
    if subdomain not in VALID_SUBDOMAINS:
        print(
            f"error: BETSSON_SUBDOMAIN={subdomain!r} not in {sorted(VALID_SUBDOMAINS)}",
            file=sys.stderr,
        )
        return 2

    limit_str = os.environ.get("BETSSON_LIMIT", "")
    limit = int(limit_str) if limit_str else None

    print(f"Betsson smoke — subdomain={subdomain}  limit={limit or 'no cap'}", flush=True)

    snaps: list[RawOddsSnapshot] = []
    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=True,
    ) as client:
        scraper = BetssonScraper(http_client=client, subdomain=subdomain)
        try:
            async for snap in scraper.fetch_live_soccer():
                snaps.append(snap)
                if limit is not None and len(snaps) >= limit:
                    break
        except BetssonContractError as exc:
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
