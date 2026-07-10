"""Recon watchdog — emits ALERT lines when daemon health degrades.

Designed for `Monitor`-style live alerting during a recon run. Stays
silent in the steady state; writes one stdout line per anomaly so
the operator (or supervising agent) gets a clean per-line
notification stream and can intervene.

Checks every `CHECK_INTERVAL_SEC` seconds (default 30):

1. **odds:raw growth** — alerts if stream length is unchanged for
   ≥ 60s (ingestion stalled).
2. **Per-platform freshness** — scans the last 500 `odds:raw`
   entries, finds the max `timestamp` per platform, alerts if any
   platform's most-recent snapshot is older than `STALL_SEC` (60s).
3. **Verifier output rate** — alerts if `arb:verification_results`
   is stuck for ≥ 3 min once it's started receiving data.
4. **Verdict mix** — alerts if ≥ 75% of the last 20 verifications
   are `MARKET_UNAVAILABLE` (suspension storm or scraper failure).
5. **Target match presence** — when `WATCHDOG_TARGET_MATCH` is set
   (pipe-separated substrings, lowercase), alerts if no recent
   `odds:raw` entry's `raw_event_name` matches any substring.

Env vars:
- `WATCHDOG_TARGET_MATCH` — e.g. `independiente del valle|rosario central`
- `CHECK_INTERVAL_SEC` — default 30
- `STALL_SEC` — default 60 (per-platform freshness threshold)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import from_url

from src.config import get_settings

DEFAULT_CHECK_INTERVAL_SEC: Final[int] = 30
DEFAULT_STALL_SEC: Final[int] = 60
RECENT_VERIF_WINDOW: Final[int] = 20  # last N for verdict mix
# odds:raw can run 50+ entries/sec at peak; we want a window that
# covers ~60-90s in any throughput regime so a slow-polling platform
# isn't falsely flagged as missing.
RAW_SAMPLE_COUNT: Final[int] = 5_000


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _emit(level: str, msg: str) -> None:
    print(f"{level} [{_iso_now()}] {msg}", flush=True)


def _alert(msg: str) -> None:
    _emit("ALERT", msg)


def _info(msg: str) -> None:
    _emit("INFO", msg)


async def _check_raw_growth(
    client: object,
    state: dict[str, int],
    interval_sec: int,
) -> None:
    """Check via the LATEST entry's stream-ID timestamp, not xlen.

    Redis stream entries get IDs of the form `<ms>-<seq>`. When the
    stream is at `maxlen` and approximately trimming, `xlen` is
    near-constant even though new entries are being written. The
    head ID's millisecond timestamp is the correct freshness
    signal: if it's old, the sink genuinely stopped writing.
    """
    del state, interval_sec  # legacy params kept for signature stability
    entries = await client.xrevrange(  # type: ignore[attr-defined]
        "odds:raw", "+", "-", count=1
    )
    if not entries:
        _alert("odds:raw empty (sink not writing or stream cleared)")
        return
    entry_id_raw, _fields = entries[0]
    entry_id = entry_id_raw.decode() if isinstance(entry_id_raw, bytes) else entry_id_raw
    ms_part = entry_id.split("-", 1)[0]
    try:
        latest_ts = int(ms_part) / 1000.0
    except ValueError:
        return
    age = time.time() - latest_ts
    if age > 60.0:
        _alert(f"odds:raw most-recent entry is {age:.0f}s old (sink stopped writing)")


async def _check_per_platform_freshness(
    client: object,
    stall_sec: int,
) -> dict[str, float]:
    """Per-platform freshness via the `odds:latest` hash — source of
    truth regardless of stream-tail composition. The hash is keyed
    by `<platform>:<outcome_id>`; we read the timestamp out of each
    field's JSON-encoded snapshot. HSCAN streams the hash so this
    is safe at any size.

    Returns the per-platform max timestamp dict."""
    import json as _json

    per_platform_max: dict[str, float] = defaultdict(float)
    cursor = 0
    while True:
        # redis-py's hscan returns (cursor, dict[field, value]).
        cursor, batch = await client.hscan(  # type: ignore[attr-defined]
            "odds:latest", cursor=cursor, count=1000
        )
        for field, value in batch.items():
            platform = field.split(":", 1)[0] if ":" in field else ""
            if not platform:
                continue
            try:
                snap = _json.loads(value)
                ts = float(snap.get("timestamp", "0") or "0")
            except (ValueError, _json.JSONDecodeError):
                continue
            if ts > per_platform_max[platform]:
                per_platform_max[platform] = ts
        if cursor == 0:
            break

    now = time.time()
    # `EXCLUDE_PLATFORMS` (comma-separated) lets the operator silence
    # alerts for platforms intentionally not running (e.g. Bplay
    # disabled during a rate-limit cool-down).
    excluded = {p.strip() for p in os.environ.get("EXCLUDE_PLATFORMS", "").split(",") if p.strip()}
    expected_platforms = tuple(
        p for p in ("betsson-pba", "betwarrior-pba", "bplay-pba") if p not in excluded
    )
    for platform in expected_platforms:
        last_ts = per_platform_max.get(platform, 0)
        if last_ts <= 0:
            _alert(f"{platform}: NO snapshots in odds:latest hash (scraper down or never started?)")
            continue
        age = now - last_ts
        if age > stall_sec:
            _alert(f"{platform}: most-recent snapshot is {age:.0f}s old (threshold {stall_sec}s)")
    return dict(per_platform_max)


async def _check_verifier_rate(
    client: object,
    state: dict[str, int],
    interval_sec: int,
) -> None:
    verif_len = int(
        await client.xlen("arb:verification_results")  # type: ignore[attr-defined]
    )
    last = state.get("last_verif_len", -1)
    if verif_len == last and last > 0:
        state["verif_unchanged_cycles"] = state.get("verif_unchanged_cycles", 0) + 1
        cycles = state["verif_unchanged_cycles"]
        # 3 minutes of idle once we've already produced output
        if cycles * interval_sec >= 180:
            _alert(
                f"arb:verification_results stalled at {verif_len} entries "
                f"({cycles * interval_sec}s idle)"
            )
    else:
        state["verif_unchanged_cycles"] = 0
    state["last_verif_len"] = verif_len


async def _check_verdict_mix(
    client: object,
) -> None:
    recent = await client.xrevrange(  # type: ignore[attr-defined]
        "arb:verification_results", "+", "-", count=RECENT_VERIF_WINDOW
    )
    if not recent or len(recent) < RECENT_VERIF_WINDOW:
        return
    counts: dict[str, int] = defaultdict(int)
    for _entry_id, fields in recent:
        counts[fields.get("verdict", "?")] += 1
    unavail = counts.get("MARKET_UNAVAILABLE", 0)
    threshold = int(RECENT_VERIF_WINDOW * 0.75)
    if unavail >= threshold:
        _alert(
            f"verifier: {unavail}/{RECENT_VERIF_WINDOW} recent verdicts = "
            f"MARKET_UNAVAILABLE (suspensions or scraper failure)"
        )


async def _check_target_match(
    entries: list[tuple[object, dict[str, str]]],
    target_substrings: tuple[str, ...],
    state: dict[str, int],
) -> None:
    if not target_substrings:
        return
    hits = 0
    for _entry_id, fields in entries:
        name = fields.get("raw_event_name", "").lower()
        if any(sub in name for sub in target_substrings):
            hits += 1
    state["target_hits_last_cycle"] = hits
    if hits == 0:
        prev_zero = state.get("target_zero_cycles", 0) + 1
        state["target_zero_cycles"] = prev_zero
        if prev_zero >= 2:  # ≥ 60s without target
            _alert(
                f"target match not in last {RAW_SAMPLE_COUNT} odds:raw "
                f"(substrings={list(target_substrings)})"
            )
    else:
        if state.get("target_zero_cycles", 0) > 0:
            _info(f"target match present again ({hits} hits in last {RAW_SAMPLE_COUNT} entries)")
        state["target_zero_cycles"] = 0


async def main() -> int:
    settings = get_settings()
    interval_sec = int(os.environ.get("CHECK_INTERVAL_SEC", str(DEFAULT_CHECK_INTERVAL_SEC)))
    stall_sec = int(os.environ.get("STALL_SEC", str(DEFAULT_STALL_SEC)))
    target_raw = os.environ.get("WATCHDOG_TARGET_MATCH", "").strip().lower()
    target_substrings = tuple(s.strip() for s in target_raw.split("|") if s.strip())

    _info(
        f"watchdog start; interval={interval_sec}s stall={stall_sec}s "
        f"target_substrings={list(target_substrings) or '(none)'}"
    )

    client = from_url(settings.redis_url, decode_responses=True)
    state: dict[str, int] = {}

    try:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                # Sample the recent tail once; reuse across checks.
                entries = await client.xrevrange("odds:raw", "+", "-", count=RAW_SAMPLE_COUNT)
            except Exception as exc:
                _alert(f"redis read failed: {exc}")
                continue

            await _check_raw_growth(client, state, interval_sec)
            await _check_per_platform_freshness(client, stall_sec)
            await _check_verifier_rate(client, state, interval_sec)
            await _check_verdict_mix(client)
            await _check_target_match(entries, target_substrings, state)
    except (KeyboardInterrupt, asyncio.CancelledError):
        _info("watchdog stop")
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
