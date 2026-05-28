"""Per-platform drift analysis for `arb:verification_results`.

Reads every entry in `arb:verification_results`, decodes
`drift_per_leg_json`, and aggregates drift statistics grouped by
platform. The intent is to surface whether different sportsbooks have
distinct drift profiles — e.g., one platform may exhibit larger or
more frequent line movement than another within the
detection→verification window, which would inform per-platform
acceptance policy tuning.

Output sections:

1. **Run summary** — total entries, verdict distribution, time-since-
   detection summary, Tier-2 reach %.
2. **Per-platform drift** — median, mean, p95, max of |odds_delta_pct|;
   sign breakdown (positive = odds drifted up, negative = drifted
   down); count of "missing" (fresh_odds is None — market gone).
3. **Per-(platform, market_id) drift** — same stats but bucketed by
   market type, so we can see whether 1X2 vs OU vs BTTS drift
   differently per platform.
4. **Tier distribution per platform** — share of legs verified via
   Tier-2 surgical refetch vs Tier-1 stream/hash cache.

When `KICKOFF_UTC` is set, the analyzer runs the report twice —
once over pre-kickoff entries, once over in-play entries — using
the entry's detection time (`verified_at - time_since_detection`)
to bucket. UTC is required to avoid ambiguity across timezones.

Usage:

    uv run python -m scripts.analyze_drift

Env vars:
- `STREAM_NAME` — default `arb:verification_results`.
- `MAX_ENTRIES` — default 100_000 (cap on XRANGE result count).
- `KICKOFF_UTC` — ISO 8601 kickoff time in UTC, e.g.
  `2026-05-30T16:00:00Z`. When set, splits the report into
  pre-match / in-play sections. Unset → single combined report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from redis.asyncio import from_url

from src.config import get_settings

DEFAULT_STREAM: Final[str] = "arb:verification_results"
DEFAULT_MAX_ENTRIES: Final[int] = 100_000


def _quantile(xs: Sequence[float], q: float) -> float:
    """Inclusive linear-interpolation quantile. q in [0, 1]."""
    if not xs:
        return float("nan")
    if len(xs) == 1:
        return xs[0]
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _fmt_pct(x: float, places: int = 3) -> str:
    if math.isnan(x):
        return "—"
    return f"{x:+.{places}f}%"


def _fmt_abs_pct(x: float, places: int = 3) -> str:
    if math.isnan(x):
        return "—"
    return f"{x:.{places}f}%"


def _fmt_count(n: int) -> str:
    return f"{n:>6,}"


def _summarize(values: Iterable[float], *, signed: bool) -> dict[str, float]:
    xs = list(values)
    if not xs:
        return {
            "n": 0,
            "mean": float("nan"),
            "median": float("nan"),
            "p95": float("nan"),
            "max": float("nan"),
            "stdev": float("nan"),
        }
    abs_xs = [abs(x) for x in xs]
    return {
        "n": len(xs),
        "mean": statistics.mean(xs) if signed else statistics.mean(abs_xs),
        "median": statistics.median(xs) if signed else statistics.median(abs_xs),
        "p95": _quantile(abs_xs, 0.95),
        "max": max(abs_xs),
        "stdev": statistics.stdev(xs) if len(xs) > 1 else float("nan"),
    }


def _decode(raw: dict[Any, Any]) -> dict[str, str]:
    return {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in raw.items()
    }


def _parse_kickoff_utc(raw: str) -> float | None:
    """Parse an ISO 8601 UTC kickoff string into epoch seconds.

    Accepts forms like `2026-05-30T16:00:00Z` and
    `2026-05-30T16:00:00+00:00`. Naive datetimes are interpreted as
    UTC (this is documented). Returns None if `raw` is empty.
    Raises ValueError on malformed input — caller surfaces it.
    """
    s = raw.strip()
    if not s:
        return None
    # datetime.fromisoformat in 3.11+ accepts 'Z' suffix, but be
    # explicit since the docstring promises both forms work.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _detection_time(fields: dict[str, str]) -> float | None:
    """Recover the detection epoch from a verification entry.

    `detection_time = verified_at - time_since_detection_sec`.
    Returns None if either field is malformed/missing — caller
    drops the entry from partition analysis.
    """
    try:
        verified_at = float(fields.get("verified_at", "0") or "0")
        tsd = float(fields.get("time_since_detection_sec", "0") or "0")
    except ValueError:
        return None
    if verified_at <= 0:
        return None
    return verified_at - tsd


def _render_report(
    entries: Sequence[tuple[Any, dict[Any, Any]]],
    *,
    label: str,
    stream_name: str,
) -> None:
    """Aggregate + print the full drift report for one bucket of
    entries. Pulled out so we can call it twice (pre-match,
    in-play) when `KICKOFF_UTC` is set."""
    n_total = 0
    by_verdict: dict[str, int] = defaultdict(int)
    time_since: list[float] = []
    fully_tier_2_count = 0

    per_platform_drift: dict[str, list[float]] = defaultdict(list)
    per_platform_missing: dict[str, int] = defaultdict(int)
    per_platform_tier: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    per_platform_total_legs: dict[str, int] = defaultdict(int)
    per_platform_market_drift: dict[tuple[str, str], list[float]] = defaultdict(list)
    margin_delta_by_verdict: dict[str, list[float]] = defaultdict(list)

    for _entry_id, raw_fields in entries:
        fields = _decode(raw_fields)
        n_total += 1
        verdict = fields.get("verdict", "?")
        by_verdict[verdict] += 1
        with contextlib.suppress(ValueError):
            time_since.append(float(fields.get("time_since_detection_sec", "0") or "0"))
        if fields.get("fully_tier_2", "0") == "1":
            fully_tier_2_count += 1
        md = fields.get("margin_delta_pct", "")
        if md:
            with contextlib.suppress(ValueError):
                margin_delta_by_verdict[verdict].append(float(md))

        market_id = fields.get("market_id", "")
        legs_raw = fields.get("drift_per_leg_json", "[]")
        try:
            legs = json.loads(legs_raw)
        except json.JSONDecodeError:
            continue

        for leg in legs:
            platform = leg.get("platform", "unknown")
            per_platform_total_legs[platform] += 1
            tier = int(leg.get("tier", 0))
            per_platform_tier[platform][tier] += 1
            fresh = leg.get("fresh_odds")
            if fresh is None:
                per_platform_missing[platform] += 1
                continue
            delta = leg.get("odds_delta_pct")
            if delta is None:
                continue
            per_platform_drift[platform].append(float(delta))
            per_platform_market_drift[(platform, market_id)].append(float(delta))

    print()
    print(f"=== Drift analysis [{label}]: `{stream_name}` ({n_total:,} entries) ===")
    if n_total == 0:
        print("  (no entries in this bucket)")
        print()
        return
    print()
    print("--- Verdict distribution ---")
    for v, c in sorted(by_verdict.items(), key=lambda kv: -kv[1]):
        pct = 100 * c / n_total if n_total else 0
        print(f"  {v:<28} {_fmt_count(c)}  ({pct:5.1f}%)")
    print()

    if time_since:
        print("--- Time since detection (sec) ---")
        print(
            f"  median = {statistics.median(time_since):.3f}   "
            f"mean = {statistics.mean(time_since):.3f}   "
            f"p95 = {_quantile(time_since, 0.95):.3f}   "
            f"max = {max(time_since):.3f}"
        )
        tier_2_pct = 100 * fully_tier_2_count / n_total if n_total else 0
        print(f"  fully Tier-2: {fully_tier_2_count}/{n_total} ({tier_2_pct:.1f}%)")
        print()

    print("--- Margin delta by verdict (detection − fresh, % points) ---")
    print("    Positive = margin shrunk between detection and verification.")
    for verdict, deltas in sorted(margin_delta_by_verdict.items()):
        if not deltas:
            continue
        print(
            f"  {verdict:<28} n={len(deltas):>5}  "
            f"median={_fmt_pct(statistics.median(deltas), 4)}  "
            f"mean={_fmt_pct(statistics.mean(deltas), 4)}  "
            f"max={_fmt_pct(max(deltas), 4)}"
        )
    print()

    print("--- Per-platform |odds drift| (signed mean shows direction) ---")
    print(f"  {'platform':<18} {'legs':>6} {'|median|':>10} {'|p95|':>10} "
          f"{'|max|':>10} {'signed mean':>14} {'stdev':>10} {'missing':>9}")
    for platform in sorted(per_platform_drift.keys() | per_platform_missing.keys()):
        drifts = per_platform_drift.get(platform, [])
        signed = _summarize(drifts, signed=True)
        abs_stats = _summarize(drifts, signed=False)
        missing = per_platform_missing.get(platform, 0)
        total = per_platform_total_legs.get(platform, 0)
        print(
            f"  {platform:<18} {total:>6} "
            f"{_fmt_abs_pct(abs_stats['median']):>10} "
            f"{_fmt_abs_pct(abs_stats['p95']):>10} "
            f"{_fmt_abs_pct(abs_stats['max']):>10} "
            f"{_fmt_pct(signed['mean'], 4):>14} "
            f"{_fmt_abs_pct(signed['stdev']):>10} "
            f"{missing:>4}/{total:<4}"
        )
    print()

    print("--- Per-(platform, market) |odds drift| ---")
    print(f"  {'platform':<18} {'market_id':<26} {'n':>5} "
          f"{'|median|':>10} {'|p95|':>10} {'|max|':>10}")
    for (platform, market_id), drifts in sorted(per_platform_market_drift.items()):
        if not drifts:
            continue
        s = _summarize(drifts, signed=False)
        print(
            f"  {platform:<18} {market_id[:26]:<26} {len(drifts):>5} "
            f"{_fmt_abs_pct(s['median']):>10} "
            f"{_fmt_abs_pct(s['p95']):>10} "
            f"{_fmt_abs_pct(s['max']):>10}"
        )
    print()

    print("--- Tier distribution per platform (% of legs) ---")
    print(f"  {'platform':<18} {'Tier-2':>10} {'Tier-1':>10} {'Tier-0':>10}")
    for platform in sorted(per_platform_tier.keys()):
        buckets = per_platform_tier[platform]
        total = sum(buckets.values()) or 1
        t2 = 100 * buckets.get(2, 0) / total
        t1 = 100 * buckets.get(1, 0) / total
        t0 = 100 * buckets.get(0, 0) / total
        print(f"  {platform:<18} {t2:>9.1f}% {t1:>9.1f}% {t0:>9.1f}%")
    print()


async def main() -> int:
    settings = get_settings()
    stream_name = os.environ.get("STREAM_NAME", DEFAULT_STREAM).strip() or DEFAULT_STREAM
    max_entries_raw = os.environ.get("MAX_ENTRIES", "").strip()
    max_entries = int(max_entries_raw) if max_entries_raw else DEFAULT_MAX_ENTRIES

    kickoff_raw = os.environ.get("KICKOFF_UTC", "")
    kickoff_epoch = _parse_kickoff_utc(kickoff_raw)

    client = from_url(settings.redis_url, decode_responses=False)
    try:
        entries = await client.xrange(stream_name, "-", "+", count=max_entries)
    finally:
        await client.aclose()

    if not entries:
        print(f"No entries in stream `{stream_name}`. Run the verifier first.")
        return 0

    if kickoff_epoch is None:
        _render_report(entries, label="ALL", stream_name=stream_name)
        return 0

    # Partition by detection time vs kickoff.
    pre_match: list[tuple[Any, dict[Any, Any]]] = []
    in_play: list[tuple[Any, dict[Any, Any]]] = []
    skipped = 0
    for entry_id, raw_fields in entries:
        fields = _decode(raw_fields)
        det = _detection_time(fields)
        if det is None:
            skipped += 1
            continue
        if det < kickoff_epoch:
            pre_match.append((entry_id, raw_fields))
        else:
            in_play.append((entry_id, raw_fields))

    kickoff_iso = (
        datetime.fromtimestamp(kickoff_epoch, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    print(f"Partition: kickoff = {kickoff_iso} (epoch {kickoff_epoch:.0f})")
    print(
        f"           pre-match = {len(pre_match):,}   "
        f"in-play = {len(in_play):,}   skipped = {skipped:,}"
    )
    _render_report(
        pre_match, label=f"PRE-MATCH (< {kickoff_iso})", stream_name=stream_name
    )
    _render_report(
        in_play, label=f"IN-PLAY (≥ {kickoff_iso})", stream_name=stream_name
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
