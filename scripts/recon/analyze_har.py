"""Post-recon HAR analysis: surface API endpoints, dump JSON bodies.

`scripts/recon/recon.py` produces a `network.har` Playwright network
trace per recon session. This script parses that HAR to do the
mechanical analysis we'd otherwise repeat in an interactive Python
shell every time:

1. **List XHR/fetch hosts** ordered by call count, with the number of
   unique paths each one touched.
2. **Surface API-looking paths** per host (anything with `/api/` or
   matching `--api-prefix`).
3. **Dump JSON responses** ≥ `--min-bytes` to `<slug>.json` next to
   the HAR, so we can grep / pretty-print them offline without
   re-loading the HAR.
4. **Peek at top-level schemas** of the JSON responses (top-level
   keys, list lengths, sample item keys) — enough to decide whether
   an endpoint is the real prize without manually pretty-printing
   100 KB of JSON.

Usage:
    uv run python scripts/recon/analyze_har.py <path/to/network.har>
    uv run python scripts/recon/analyze_har.py <har> --dump
    uv run python scripts/recon/analyze_har.py <har> --dump --min-bytes 5000

Output goes to stdout; dumped JSONs are written next to the HAR
file with slug-derived names (e.g. `live_overview_latest.json`).

This is read-only — it never re-fetches anything. The HAR is
captured during the original Playwright run; this script just
parses it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _slug_from_url(url: str) -> str:
    """Derive a filesystem-safe slug from the last interesting URL
    segment. Strips query strings; lowercases; replaces non-word
    runs with underscores."""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return "root"
    last = path.rsplit("/", 1)[-1] or path.split("/")[-2]
    # If the last segment is generic (e.g. "latest"), fall back to
    # the penultimate one too.
    if last in {"latest", "list", "all", "v1", "v2", "index"}:
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            last = f"{parts[-2]}_{parts[-1]}"
    return re.sub(r"\W+", "_", last).strip("_").lower() or "endpoint"


def _peek_schema(body: Any, depth: int = 0) -> str:
    """One-line description of a JSON value's top-level shape."""
    if isinstance(body, dict):
        keys = list(body.keys())
        return f"dict[{len(keys)} keys]: {keys[:10]}"
    if isinstance(body, list):
        sample_type = type(body[0]).__name__ if body else "empty"
        sample_keys = list(body[0].keys())[:10] if body and isinstance(body[0], dict) else None
        if sample_keys is not None:
            return f"list[{len(body)}] of dict: {sample_keys}"
        return f"list[{len(body)}] of {sample_type}"
    return f"{type(body).__name__}: {repr(body)[:60]}"


def _looks_odds_shaped(body: Any) -> bool:
    """Heuristic: does this JSON look like odds data?"""
    if not isinstance(body, dict):
        return False
    interesting = {
        "events",
        "markets",
        "selections",
        "outcomes",
        "fixtures",
        "matches",
        "competitions",
        "leagues",
        "odds",
        "prices",
        "betoffers",
        "betOffers",
        "data",
    }
    return bool(set(body.keys()) & interesting)


def analyze(har_path: Path, *, dump: bool, min_bytes: int, api_prefix: str) -> int:
    if not har_path.exists():
        print(f"HAR not found: {har_path}", file=sys.stderr)
        return 1

    har = json.loads(har_path.read_text(encoding="utf-8"))
    entries = har.get("log", {}).get("entries", [])
    out_dir = har_path.parent

    # 1. host frequency
    host_counts: Counter[str] = Counter()
    host_paths: dict[str, set[str]] = defaultdict(set)
    for e in entries:
        req = e["request"]
        rtype = (e.get("_resourceType") or "").lower()
        if rtype not in {"xhr", "fetch"}:
            # HAR doesn't carry resource type natively; check for
            # JSON content-type as fallback.
            ctype = e["response"].get("content", {}).get("mimeType", "").lower()
            if "json" not in ctype:
                continue
        u = urlparse(req["url"])
        host_counts[u.netloc] += 1
        host_paths[u.netloc].add(u.path)

    print(f"\n=== Hosts with JSON/XHR activity in {har_path} ===")
    for host, cnt in host_counts.most_common():
        print(f"  {host:<48} {cnt:>4} calls, {len(host_paths[host]):>3} unique paths")

    # 2. API-looking paths per host
    print(f"\n=== Paths matching prefix '{api_prefix}' ===")
    for host, paths in sorted(host_paths.items()):
        matched = sorted(p for p in paths if api_prefix in p)
        if not matched:
            continue
        print(f"\n{host}:")
        for p in matched[:30]:
            print(f"  {p}")
        if len(matched) > 30:
            print(f"  ... and {len(matched) - 30} more")

    # 3+4. JSON response dump + schema peek
    print(f"\n=== JSON responses ≥ {min_bytes} bytes (dump={dump}) ===")
    dumped_count = 0
    for e in entries:
        url = e["request"]["url"]
        resp = e.get("response", {})
        if resp.get("status") != 200:
            continue
        ctype = resp.get("content", {}).get("mimeType", "").lower()
        if "json" not in ctype:
            continue
        body_text = resp.get("content", {}).get("text", "") or ""
        size = len(body_text)
        if size < min_bytes:
            continue
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            continue
        shape = _peek_schema(body)
        odds_hint = "  <-- ODDS-SHAPED?" if _looks_odds_shaped(body) else ""
        print(f"\n  {url[:140]}")
        print(f"    size={size:,}  shape={shape}{odds_hint}")
        if dump:
            slug = _slug_from_url(url)
            dst = out_dir / f"{slug}.json"
            # Don't clobber existing files; suffix on collision.
            n = 0
            while dst.exists():
                n += 1
                dst = out_dir / f"{slug}__{n}.json"
            dst.write_text(body_text, encoding="utf-8")
            print(f"    → saved {dst.name}")
            dumped_count += 1

    if dump:
        print(f"\nDumped {dumped_count} JSON responses to {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", type=Path, help="Path to network.har produced by recon.py")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Write each qualifying JSON response to <slug>.json next to the HAR.",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=1_000,
        help="Skip JSON responses smaller than this (default 1,000).",
    )
    parser.add_argument(
        "--api-prefix",
        default="/api/",
        help="Substring used to surface API-looking paths (default '/api/').",
    )
    args = parser.parse_args()
    return analyze(
        args.har,
        dump=args.dump,
        min_bytes=args.min_bytes,
        api_prefix=args.api_prefix,
    )


if __name__ == "__main__":
    sys.exit(main())
