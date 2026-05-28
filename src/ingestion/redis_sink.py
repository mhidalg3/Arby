"""Redis stream sink for raw odds snapshots.

The ingestion pipeline (per `docs/architecture.md`):

    scraper.poll_forever  →  asyncio.Queue  →  RedisSnapshotSink  →  Redis stream

The intermediate `asyncio.Queue` decouples scrape cadence from Redis
latency. The sink is intentionally thin: serialize the snapshot to a
flat field map and `XADD` it to a stream with an approximate length
cap.

Downstream consumers (the normalizer, audit tooling) read this stream
to do canonical resolution and persist to Postgres. We do not write
the canonical / OddsQuote form here — that is the semantic layer's
job.
"""

from __future__ import annotations

import asyncio
from typing import Final

import structlog
from redis.asyncio import Redis

from src.ingestion.scrapers.base import RawOddsSnapshot

log = structlog.get_logger(__name__)

STREAM_NAME: Final[str] = "odds:raw"

# Redis hash holding the LATEST snapshot per (platform, outcome_id).
# Updated on every XADD via a pipelined HSET. Used by the Tier-1
# verifier's `StreamCacheRefresher.refresh_batch` for O(1) HGET
# lookup instead of XREVRANGE scan-back-N. Field keys are
# `<platform>:<platform_outcome_id>`; values are JSON-encoded
# snapshot dicts (the same field map XADD writes).
LATEST_HASH_NAME: Final[str] = "odds:latest"

# Approximate cap on the stream so unbounded memory growth isn't a
# foot-gun if no consumer is attached. 100k entries × ~300 bytes ≈ 30MB
# RAM — well below the docker-compose redis cap (256MB).
DEFAULT_STREAM_MAXLEN: Final[int] = 100_000

# Queue.get() timeout so the sink loop can re-check stop_event regularly
# even when no snapshots are arriving (idle period between polls).
DEFAULT_QUEUE_GET_TIMEOUT_SEC: Final[float] = 1.0


def snapshot_to_latest_hash_field_and_value(
    snapshot: RawOddsSnapshot,
) -> tuple[str, str]:
    """Build the (hash_field, hash_value) pair for `odds:latest`.

    Field: `<platform>:<platform_outcome_id>` — unique per outcome
    across all platforms.

    Value: JSON-encoded snapshot field map (the same payload XADD
    writes). The verifier deserializes it via
    `json.loads(...)` + `stream_fields_to_snapshot(...)`.
    """
    import json
    return (
        f"{snapshot.platform}:{snapshot.platform_outcome_id}",
        json.dumps(snapshot_to_stream_fields(snapshot)),
    )


def snapshot_to_stream_fields(snapshot: RawOddsSnapshot) -> dict[str, str]:
    """Flatten a `RawOddsSnapshot` into Redis stream field-value entries.

    Redis stream entries are flat string-to-string maps. Floats become
    decimal strings; an absent `max_stake` becomes the empty string
    (rather than the literal "None" — empty is unambiguous and trivial
    for the consumer to coerce back).
    """
    return {
        "platform": snapshot.platform,
        "platform_event_id": snapshot.platform_event_id,
        "platform_market_id": snapshot.platform_market_id,
        "platform_outcome_id": snapshot.platform_outcome_id,
        "raw_event_name": snapshot.raw_event_name,
        "raw_market_name": snapshot.raw_market_name,
        "raw_outcome_name": snapshot.raw_outcome_name,
        "decimal_odds": str(snapshot.decimal_odds),
        "max_stake": "" if snapshot.max_stake is None else str(snapshot.max_stake),
        "timestamp": str(snapshot.timestamp),
    }


def stream_fields_to_snapshot(fields: dict[str, str]) -> RawOddsSnapshot | None:
    """Inverse of `snapshot_to_stream_fields`. Returns None on malformed
    input — callers (the arb detector) skip the entry and continue.

    Co-located with the forward helper so the two stay in sync. If you
    change one, change the other.
    """
    try:
        max_stake_str = fields.get("max_stake", "")
        return RawOddsSnapshot(
            platform=fields["platform"],
            platform_event_id=fields["platform_event_id"],
            platform_market_id=fields["platform_market_id"],
            platform_outcome_id=fields["platform_outcome_id"],
            raw_event_name=fields["raw_event_name"],
            raw_market_name=fields["raw_market_name"],
            raw_outcome_name=fields["raw_outcome_name"],
            decimal_odds=float(fields["decimal_odds"]),
            max_stake=float(max_stake_str) if max_stake_str else None,
            timestamp=float(fields["timestamp"]),
        )
    except (KeyError, ValueError):
        return None


class RedisSnapshotSink:
    """Drains snapshots from an `asyncio.Queue` into a Redis stream.

    Designed to run concurrently with a `BaseScraper.poll_forever` producer
    that puts to the same queue. Exits cleanly when:

        (a) the shared `stop_event` is set, AND
        (b) the queue has been fully drained.

    Per-write failures are logged and the snapshot is dropped — we never
    block ingestion on a flaky Redis. If Redis is sustained-down, the
    queue fills up to its maxsize and the producer back-pressures (this
    is preferable to silently consuming memory).
    """

    def __init__(
        self,
        redis_client: Redis,
        stream_name: str = STREAM_NAME,
        latest_hash_name: str = LATEST_HASH_NAME,
        maxlen: int = DEFAULT_STREAM_MAXLEN,
        queue_get_timeout_sec: float = DEFAULT_QUEUE_GET_TIMEOUT_SEC,
    ) -> None:
        self._redis = redis_client
        self._stream_name = stream_name
        self._latest_hash_name = latest_hash_name
        self._maxlen = maxlen
        self._queue_get_timeout_sec = queue_get_timeout_sec
        self._log = log.bind(component="redis_sink", stream=stream_name)

    async def run(
        self,
        queue: asyncio.Queue[RawOddsSnapshot],
        stop_event: asyncio.Event,
        producer_task: asyncio.Task[None] | None = None,
    ) -> None:
        """Drain `queue` until `stop_event` is set, the queue is empty,
        AND (if known) the producer task has finished.

        The `producer_task` kwarg closes a real shutdown race: scrapers
        only check `stop_event` between polling cycles, so after `stop`
        trips the producer keeps appending to the queue until the
        current cycle completes. Without visibility into producer
        liveness the sink would exit on the first transient
        empty-queue moment and any subsequent snapshots get
        garbage-collected. Passing the task lets the sink wait until the
        producer is fully done before exiting — no silent drops on
        SIGTERM. Callers that don't pass it (existing tests, throwaway
        scripts) get the old "stop + empty" semantics.
        """
        self._log.info("sink.started")
        written = 0
        dropped = 0
        while True:
            producer_running = producer_task is not None and not producer_task.done()
            if stop_event.is_set() and queue.empty() and not producer_running:
                break
            try:
                snapshot = await asyncio.wait_for(queue.get(), timeout=self._queue_get_timeout_sec)
            except TimeoutError:
                continue
            try:
                # Pipeline XADD + HSET so the sink stays at ~one
                # Redis round-trip per snapshot. The hash entry powers
                # the Tier-1 verifier's O(1) HGET lookup; without it
                # the verifier would scan the stream back N entries.
                # redis-py's xadd/hset stubs type `fields` invariantly;
                # our `dict[str, str]` is valid at runtime — silence
                # the stub-narrowness warning.
                hash_field, hash_value = snapshot_to_latest_hash_field_and_value(
                    snapshot
                )
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.xadd(
                        self._stream_name,
                        snapshot_to_stream_fields(snapshot),  # type: ignore[arg-type]
                        maxlen=self._maxlen,
                        approximate=True,
                    )
                    pipe.hset(self._latest_hash_name, hash_field, hash_value)
                    await pipe.execute()
                written += 1
                if written % 100 == 0:
                    self._log.debug("sink.progress", written=written, dropped=dropped)
            except Exception as exc:
                # Don't re-queue; the snapshot is best-effort. Sustained
                # Redis failure will back-pressure via the bounded queue.
                dropped += 1
                self._log.warning(
                    "sink.write_failed",
                    error=str(exc),
                    platform=snapshot.platform,
                    platform_market_id=snapshot.platform_market_id,
                )
            finally:
                queue.task_done()
        self._log.info("sink.stopped", written=written, dropped=dropped)
