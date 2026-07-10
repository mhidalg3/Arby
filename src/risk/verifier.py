"""Pre-execution quote verifier — guards against detection→execution drift.

After an opportunity is APPROVED by the risk daemon, but BEFORE any
real bet is placed, the verifier re-checks the legs against the
freshest available data. If odds have drifted, a leg has been
suspended, or the market is gone, the opportunity is aborted.

Two tiers of refresh:

- **Tier 1: stream-cache** — read the most-recent matching snapshot
  per leg from `odds:raw`. Latency microseconds; freshness bounded
  by the scrapers' polling cadence (5-30s). No platform call.
- **Tier 2: surgical refetch** — call the platform's read API for
  this leg's specific event/market. Truly fresh (~300-800ms old).
  Per-platform implementation; not all platforms support it
  (Bplay SSE is push-only; degrades to Tier 1).

For v1 only **Tier 1** is implemented. Per-platform Tier-2
refreshers slot in behind the `QuoteRefresher` Protocol with no
verifier changes.

The verifier matches stream snapshots to opportunity legs via
`(platform, platform_outcome_id)` — the OddsQuote carries
`platform_outcome_id` from the canonicalizer for exactly this
purpose.

Acceptance policy: fresh margin must clear BOTH an absolute floor
AND a fractional-retention bar versus the detection-time margin.
Defaults `min_fresh_margin_pct=1.0%` and `min_retention=0.60`
(fresh margin ≥ 60% of detected). Both must hold for
STILL_VALID.

Output: a `VerificationResult` capturing the verdict, fresh margin,
per-leg drift, and freshness tier. The dry-run daemon emits this
to `arb:verification_results` for analysis without placing bets.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

import structlog
from redis.asyncio import Redis

from src.arbitrage.dutch_book import ArbitrageOpportunity
from src.arbitrage.quotes import OddsQuote
from src.ingestion.redis_sink import LATEST_HASH_NAME as ODDS_LATEST_HASH_NAME
from src.ingestion.redis_sink import STREAM_NAME as ODDS_RAW_STREAM_NAME
from src.ingestion.redis_sink import stream_fields_to_snapshot

log = structlog.get_logger(__name__)


# -------- Output types --------


class VerificationVerdict(StrEnum):
    """Verdict produced by `QuoteVerifier.verify_opportunity`."""

    STILL_VALID = "STILL_VALID"
    DRIFT_BELOW_ACCEPTANCE = "DRIFT_BELOW_ACCEPTANCE"
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"  # a leg's fresh quote missing
    STALE_DATA = "STALE_DATA"  # quotes too old to verify confidently
    NO_OUTCOME_ID = "NO_OUTCOME_ID"  # leg has no platform_outcome_id (legacy)


@dataclass(frozen=True)
class FreshQuote:
    """A freshly-observed quote for one leg.

    `decimal_odds=None` means the leg's market wasn't found in the
    refresh (suspended, closed, removed).
    """

    platform: str
    platform_outcome_id: str
    decimal_odds: float | None
    observed_at: float | None
    tier: int  # 1 = stream-cache, 2 = surgical refetch


@dataclass(frozen=True)
class LegDrift:
    """Per-leg drift record. Carries through to the audit stream."""

    platform: str
    outcome: str
    platform_outcome_id: str | None
    detection_odds: float
    fresh_odds: float | None
    odds_delta_pct: float | None  # % change vs detection; None if no fresh
    tier: int


@dataclass(frozen=True)
class VerificationResult:
    """Audit-ready result of one verification pass."""

    verdict: VerificationVerdict
    detection_margin_pct: float
    fresh_margin_pct: float | None
    margin_delta_pct: float | None  # detection - fresh; positive = drift down
    per_leg_drift: tuple[LegDrift, ...]
    time_since_detection_sec: float
    verified_at: float
    fully_tier_2: bool  # True iff every leg got surgical refetch
    reason: str


# -------- Refresher protocol --------


class QuoteRefresher(Protocol):
    """Per-platform refresher. Implementations should return a
    `FreshQuote` (with `decimal_odds=None` if the market is gone),
    NOT raise."""

    platform_name: str

    async def refresh(self, leg: OddsQuote) -> FreshQuote: ...


# -------- Tier-1 stream-cache refresher --------


DEFAULT_SCAN_COUNT: Final[int] = 5_000


@dataclass
class StreamCacheRefresher:
    """Tier-1 refresher — reads the most recent snapshot per leg.

    Two-phase lookup:

    1. **HMGET on `odds:latest`** — O(1) per leg. Populated by the
       sink on every XADD; field key is `<platform>:<outcome_id>`,
       value is a JSON-encoded snapshot dict.
    2. **XREVRANGE scan on `odds:raw`** — fallback for legs whose
       hash entry is missing (e.g., the sink hasn't written it yet,
       or the field expired in a future TTL'd variant). Scans the
       last `scan_count` stream entries.

    Legs without `platform_outcome_id` set get a sentinel `tier=0`
    result — the verifier rejects them as unverifiable.
    """

    redis_client: Redis
    scan_count: int = DEFAULT_SCAN_COUNT
    stream_name: str = ODDS_RAW_STREAM_NAME
    latest_hash_name: str = ODDS_LATEST_HASH_NAME
    platform_name: str = "*"  # matches any platform; Tier-1 is platform-agnostic

    async def refresh(self, leg: OddsQuote) -> FreshQuote:
        results = await self.refresh_batch([leg])
        return results[0]

    async def refresh_batch(self, legs: Sequence[OddsQuote]) -> list[FreshQuote]:
        """Two-phase lookup. Returns one `FreshQuote` per leg in
        input order."""
        # Pre-flight: identify legs without `platform_outcome_id`.
        unverifiable = {i for i, leg in enumerate(legs) if not leg.platform_outcome_id}
        results: list[FreshQuote | None] = [None] * len(legs)
        for i in unverifiable:
            results[i] = FreshQuote(
                platform=legs[i].platform,
                platform_outcome_id="",
                decimal_odds=None,
                observed_at=None,
                tier=0,
            )

        # Phase 1: HMGET on `odds:latest` for the remaining legs.
        verifiable_indices = [i for i in range(len(legs)) if i not in unverifiable]
        if verifiable_indices:
            hash_fields = [
                f"{legs[i].platform}:{legs[i].platform_outcome_id}" for i in verifiable_indices
            ]
            # redis-py's hmget stub returns `Awaitable[list[Any]] | list[Any]`
            # to share types with the sync client; async returns the awaitable.
            hash_values = await self.redis_client.hmget(  # type: ignore[misc]
                self.latest_hash_name, hash_fields
            )
            for idx, raw_value in zip(verifiable_indices, hash_values, strict=True):
                if raw_value is None:
                    continue
                value = raw_value.decode() if isinstance(raw_value, bytes) else raw_value
                try:
                    fields = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
                snap = stream_fields_to_snapshot(fields)
                if snap is None:
                    continue
                results[idx] = FreshQuote(
                    platform=snap.platform,
                    platform_outcome_id=snap.platform_outcome_id,
                    decimal_odds=snap.decimal_odds,
                    observed_at=snap.timestamp,
                    tier=1,
                )

        # Phase 2: XREVRANGE scan fallback for any legs still
        # unresolved. Typically these are warmup-period legs (sink
        # hasn't HSET them yet) or edge cases.
        unresolved_indices = [i for i in verifiable_indices if results[i] is None]
        if unresolved_indices:
            targets: dict[tuple[str, str], int] = {}
            for i in unresolved_indices:
                outcome_id = legs[i].platform_outcome_id
                assert outcome_id is not None  # by filter above
                targets[(legs[i].platform, outcome_id)] = i

            entries = await self.redis_client.xrevrange(
                self.stream_name, "+", "-", count=self.scan_count
            )
            for _entry_id, raw_fields in entries:
                fields = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in raw_fields.items()
                }
                snap = stream_fields_to_snapshot(fields)
                if snap is None:
                    continue
                key = (snap.platform, snap.platform_outcome_id)
                target_idx = targets.get(key)
                if target_idx is None:
                    continue
                if results[target_idx] is not None:
                    continue
                results[target_idx] = FreshQuote(
                    platform=snap.platform,
                    platform_outcome_id=snap.platform_outcome_id,
                    decimal_odds=snap.decimal_odds,
                    observed_at=snap.timestamp,
                    tier=1,
                )
                if all(r is not None for r in results):
                    break

        # Fill in legs not found anywhere (market gone).
        out: list[FreshQuote] = []
        for i, leg in enumerate(legs):
            if results[i] is not None:
                out.append(results[i])  # type: ignore[arg-type]
            else:
                out.append(
                    FreshQuote(
                        platform=leg.platform,
                        platform_outcome_id=leg.platform_outcome_id or "",
                        decimal_odds=None,
                        observed_at=None,
                        tier=1,
                    )
                )
        return out


# -------- Verifier --------


class BatchRefresher(Protocol):
    """Either `StreamCacheRefresher` (Tier 1) or
    `MultiPlatformRefresher` (Tier 2 + Tier-1 fallback) — both
    expose `refresh_batch`."""

    async def refresh_batch(self, legs: Sequence[OddsQuote]) -> list[FreshQuote]: ...


@dataclass(frozen=True)
class VerificationPolicy:
    """Acceptance thresholds. Both rules must hold for STILL_VALID.

    `min_fresh_margin_pct` is the absolute floor — below it we
    don't trust the arb regardless of detection-time margin.

    `min_retention_fraction` is the relative floor — fresh margin
    must be at least this fraction of the detection margin. Catches
    significant drift even when the absolute floor is forgiving.

    `max_freshness_age_sec` bounds how stale the FRESHEST quote can
    be before the verifier punts with STALE_DATA.

    `pre_refresh_delay_sec` is a synthetic delay between detection
    and refresh — used to simulate realistic detection→placement
    gaps for drift characterization. Default 0 (no delay, current
    behavior). Production execution would set this to its measured
    end-to-end placement latency (~1-5s typical) so the verifier
    sees the same drift the executor will face. Strictly a
    measurement / safety knob; not a correctness gate.
    """

    min_fresh_margin_pct: float = 1.0
    min_retention_fraction: float = 0.60
    max_freshness_age_sec: float = 60.0
    pre_refresh_delay_sec: float = 0.0


@dataclass
class QuoteVerifier:
    """Re-verifies an opportunity's quotes pre-execution.

    Construct with a `BatchRefresher` — either `StreamCacheRefresher`
    (Tier 1 only) or `MultiPlatformRefresher` (Tier 2 with Tier-1
    fallback). The dry-run daemon calls
    `verify_opportunity(opp, detected_at)` for each detected opp.
    """

    refresher: BatchRefresher
    policy: VerificationPolicy = field(default_factory=VerificationPolicy)

    async def verify_opportunity(
        self, opp: ArbitrageOpportunity, detected_at: float
    ) -> VerificationResult:
        # Synthetic delay before refresh — simulates the
        # detection→placement gap a real execution flow would have.
        # Default 0 means refresh immediately (current behavior).
        if self.policy.pre_refresh_delay_sec > 0:
            await asyncio.sleep(self.policy.pre_refresh_delay_sec)

        now = time.time()
        time_since_detection = now - detected_at

        fresh = await self.refresher.refresh_batch(list(opp.legs))

        per_leg_drift = tuple(
            LegDrift(
                platform=leg.platform,
                outcome=leg.outcome,
                platform_outcome_id=leg.platform_outcome_id,
                detection_odds=leg.decimal_odds,
                fresh_odds=fq.decimal_odds,
                odds_delta_pct=(
                    (fq.decimal_odds - leg.decimal_odds) / leg.decimal_odds * 100.0
                    if fq.decimal_odds is not None
                    else None
                ),
                tier=fq.tier,
            )
            for leg, fq in zip(opp.legs, fresh, strict=True)
        )

        # If any leg has tier=0 (no platform_outcome_id), the verifier
        # cannot reliably verify Tier 1 for that leg. v1 punts.
        unverifiable = [d for d in per_leg_drift if d.tier == 0]
        if unverifiable:
            return VerificationResult(
                verdict=VerificationVerdict.NO_OUTCOME_ID,
                detection_margin_pct=opp.margin_pct,
                fresh_margin_pct=None,
                margin_delta_pct=None,
                per_leg_drift=per_leg_drift,
                time_since_detection_sec=time_since_detection,
                verified_at=now,
                fully_tier_2=False,
                reason=(
                    f"{len(unverifiable)} of {len(opp.legs)} leg(s) lack "
                    f"platform_outcome_id — cannot verify via Tier 1. "
                    f"Wire Tier-2 surgical refetch or re-emit opportunity "
                    f"with platform_outcome_id populated."
                ),
            )

        # MARKET_UNAVAILABLE: any leg's fresh quote is missing.
        missing = [d for d in per_leg_drift if d.fresh_odds is None]
        if missing:
            return VerificationResult(
                verdict=VerificationVerdict.MARKET_UNAVAILABLE,
                detection_margin_pct=opp.margin_pct,
                fresh_margin_pct=None,
                margin_delta_pct=None,
                per_leg_drift=per_leg_drift,
                time_since_detection_sec=time_since_detection,
                verified_at=now,
                fully_tier_2=False,
                reason=(
                    f"{len(missing)} of {len(opp.legs)} leg(s) missing "
                    f"fresh quote (market suspended / removed / never refreshed)"
                ),
            )

        # STALE_DATA: oldest fresh quote exceeds max age.
        ages = [now - (fq.observed_at or 0.0) for fq in fresh if fq.observed_at is not None]
        oldest = max(ages) if ages else float("inf")
        if oldest > self.policy.max_freshness_age_sec:
            return VerificationResult(
                verdict=VerificationVerdict.STALE_DATA,
                detection_margin_pct=opp.margin_pct,
                fresh_margin_pct=None,
                margin_delta_pct=None,
                per_leg_drift=per_leg_drift,
                time_since_detection_sec=time_since_detection,
                verified_at=now,
                fully_tier_2=all(fq.tier == 2 for fq in fresh),
                reason=(
                    f"oldest fresh quote is {oldest:.1f}s old "
                    f"(threshold {self.policy.max_freshness_age_sec}s)"
                ),
            )

        # All legs have fresh odds. Recompute the overround.
        fresh_overround = sum(1.0 / fq.decimal_odds for fq in fresh if fq.decimal_odds is not None)
        fresh_margin_pct = (1.0 - fresh_overround) * 100.0
        margin_delta_pct = opp.margin_pct - fresh_margin_pct
        fully_tier_2 = all(fq.tier == 2 for fq in fresh)

        if fresh_margin_pct < self.policy.min_fresh_margin_pct:
            return VerificationResult(
                verdict=VerificationVerdict.DRIFT_BELOW_ACCEPTANCE,
                detection_margin_pct=opp.margin_pct,
                fresh_margin_pct=fresh_margin_pct,
                margin_delta_pct=margin_delta_pct,
                per_leg_drift=per_leg_drift,
                time_since_detection_sec=time_since_detection,
                verified_at=now,
                fully_tier_2=fully_tier_2,
                reason=(
                    f"fresh_margin={fresh_margin_pct:.2f}% below absolute "
                    f"floor {self.policy.min_fresh_margin_pct}%"
                ),
            )

        required = opp.margin_pct * self.policy.min_retention_fraction
        if fresh_margin_pct < required:
            return VerificationResult(
                verdict=VerificationVerdict.DRIFT_BELOW_ACCEPTANCE,
                detection_margin_pct=opp.margin_pct,
                fresh_margin_pct=fresh_margin_pct,
                margin_delta_pct=margin_delta_pct,
                per_leg_drift=per_leg_drift,
                time_since_detection_sec=time_since_detection,
                verified_at=now,
                fully_tier_2=fully_tier_2,
                reason=(
                    f"fresh_margin={fresh_margin_pct:.2f}% below "
                    f"{self.policy.min_retention_fraction:.0%} of "
                    f"detected={opp.margin_pct:.2f}% "
                    f"(required ≥ {required:.2f}%)"
                ),
            )

        return VerificationResult(
            verdict=VerificationVerdict.STILL_VALID,
            detection_margin_pct=opp.margin_pct,
            fresh_margin_pct=fresh_margin_pct,
            margin_delta_pct=margin_delta_pct,
            per_leg_drift=per_leg_drift,
            time_since_detection_sec=time_since_detection,
            verified_at=now,
            fully_tier_2=fully_tier_2,
            reason=(
                f"fresh_margin={fresh_margin_pct:.2f}% within tolerance "
                f"(detected={opp.margin_pct:.2f}%, "
                f"retention={self.policy.min_retention_fraction:.0%})"
            ),
        )
