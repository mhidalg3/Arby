"""Unit tests for `RedisSnapshotSink`. Real Redis is not required —
the redis client is mocked at the `xadd` boundary.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from redis.asyncio import Redis

from src.ingestion.redis_sink import (
    STREAM_NAME,
    RedisSnapshotSink,
    snapshot_to_stream_fields,
    stream_fields_to_snapshot,
)
from src.ingestion.scrapers.base import RawOddsSnapshot


def _snapshot(
    platform: str = "betsson-pba",
    market_id: str = "m-f-evt-MW3W",
    outcome_id: str = "s-m-f-evt-MW3W-home",
    odds: float = 2.10,
    max_stake: float | None = None,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id="f-evt",
        platform_market_id=market_id,
        platform_outcome_id=outcome_id,
        raw_event_name="Boca Juniors vs River Plate",
        raw_market_name="Ganador del partido",
        raw_outcome_name="Boca Juniors",
        decimal_odds=odds,
        max_stake=max_stake,
        timestamp=1779742576.0,
    )


class TestSnapshotSerialization:
    def test_all_fields_become_strings(self) -> None:
        fields = snapshot_to_stream_fields(_snapshot())
        for k, v in fields.items():
            assert isinstance(k, str), k
            assert isinstance(v, str), (k, v)

    def test_includes_all_snapshot_fields(self) -> None:
        fields = snapshot_to_stream_fields(_snapshot())
        expected = {
            "platform",
            "platform_event_id",
            "platform_market_id",
            "platform_outcome_id",
            "raw_event_name",
            "raw_market_name",
            "raw_outcome_name",
            "decimal_odds",
            "max_stake",
            "timestamp",
            "kickoff_utc",
            "transport",
        }
        assert set(fields.keys()) == expected

    def test_decimal_odds_is_decimal_string(self) -> None:
        fields = snapshot_to_stream_fields(_snapshot(odds=3.95))
        assert fields["decimal_odds"] == "3.95"
        # Round-trip: consumer should be able to parse it back.
        assert float(fields["decimal_odds"]) == 3.95

    def test_missing_max_stake_becomes_empty_string(self) -> None:
        """An explicit empty string is unambiguous; "None" would be a
        confusing string-vs-null trap on the consumer side."""
        fields = snapshot_to_stream_fields(_snapshot(max_stake=None))
        assert fields["max_stake"] == ""

    def test_present_max_stake_is_serialized(self) -> None:
        fields = snapshot_to_stream_fields(_snapshot(max_stake=1500.0))
        assert fields["max_stake"] == "1500.0"

    def test_missing_kickoff_becomes_empty_string(self) -> None:
        snap = _snapshot()
        assert snap.kickoff_utc is None
        fields = snapshot_to_stream_fields(snap)
        assert fields["kickoff_utc"] == ""

    def test_present_kickoff_is_serialized(self) -> None:
        snap = RawOddsSnapshot(
            platform="betano",
            platform_event_id="e1",
            platform_market_id="m1",
            platform_outcome_id="o1",
            raw_event_name="A vs B",
            raw_market_name="1X2",
            raw_outcome_name="A",
            decimal_odds=2.0,
            max_stake=None,
            timestamp=1000.0,
            kickoff_utc=1779742576.0,
        )
        fields = snapshot_to_stream_fields(snap)
        assert float(fields["kickoff_utc"]) == 1779742576.0

    def test_kickoff_and_transport_round_trip(self) -> None:
        """Round-trip: kickoff_utc=None and transport='push' survive."""
        original = RawOddsSnapshot(
            platform="betsson-pba",
            platform_event_id="e1",
            platform_market_id="m1",
            platform_outcome_id="o1",
            raw_event_name="A vs B",
            raw_market_name="1X2",
            raw_outcome_name="A",
            decimal_odds=2.0,
            max_stake=None,
            timestamp=1000.0,
            kickoff_utc=None,
            transport="push",
        )
        fields = snapshot_to_stream_fields(original)
        restored = stream_fields_to_snapshot(fields)
        assert restored is not None
        assert restored.kickoff_utc is None
        assert restored.transport == "push"

    def test_kickoff_value_round_trips(self) -> None:
        original = RawOddsSnapshot(
            platform="betano",
            platform_event_id="e1",
            platform_market_id="m1",
            platform_outcome_id="o1",
            raw_event_name="A vs B",
            raw_market_name="1X2",
            raw_outcome_name="A",
            decimal_odds=2.0,
            max_stake=None,
            timestamp=1000.0,
            kickoff_utc=1779742576.5,
            transport="poll",
        )
        restored = stream_fields_to_snapshot(snapshot_to_stream_fields(original))
        assert restored is not None
        assert restored.kickoff_utc == 1779742576.5
        assert restored.transport == "poll"

    def test_transport_defaults_poll_on_missing_field(self) -> None:
        """Old stream entries (pre-kickoff_utc) should default transport='poll'."""
        snap = _snapshot()
        fields = snapshot_to_stream_fields(snap)
        del fields["kickoff_utc"]
        del fields["transport"]
        restored = stream_fields_to_snapshot(fields)
        assert restored is not None
        assert restored.transport == "poll"
        assert restored.kickoff_utc is None


def _mock_redis() -> MagicMock:
    """Mock Redis client supporting the sink's pipelined `xadd + hset`.

    redis-py's actual pipeline behavior: `pipe.xadd(...)` is SYNC
    (returns the pipeline for chaining); `await pipe.execute()` is
    the only async point. The mock mirrors this.

    Tests introspect calls via:
        redis.pipe.xadd.call_args
        redis.pipe.hset.call_args
        redis.pipe.execute.await_args
    """
    client = MagicMock(spec=Redis)
    pipe = MagicMock()
    pipe.xadd = MagicMock(return_value=pipe)
    pipe.hset = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=None)

    client.pipeline = MagicMock(return_value=pipe)
    client.pipe = pipe  # for test introspection
    return client


class TestSinkLoop:
    async def test_writes_one_snapshot(self) -> None:
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        snap = _snapshot()
        await queue.put(snap)
        stop.set()  # let the loop drain and exit

        await sink.run(queue, stop)

        redis.pipe.xadd.assert_called_once()
        call = redis.pipe.xadd.call_args
        assert call.args[0] == STREAM_NAME
        # Field-value map is the second positional arg.
        fields = call.args[1]
        assert fields["platform"] == snap.platform
        assert fields["decimal_odds"] == str(snap.decimal_odds)
        # MAXLEN cap is set, approximately (~MAXLEN, faster than exact).
        assert call.kwargs.get("approximate") is True

    async def test_hash_write_per_snapshot(self) -> None:
        """Each snapshot also gets HSET to `odds:latest` so the
        Tier-1 verifier can do O(1) HGET instead of XREVRANGE scan."""
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()
        snap = _snapshot(
            platform="betsson-pba",
            outcome_id="s-out-1",
        )
        await queue.put(snap)
        stop.set()
        await sink.run(queue, stop)

        redis.pipe.hset.assert_called_once()
        call = redis.pipe.hset.call_args
        # Hash name + field key + value
        assert call.args[0] == "odds:latest"
        # Field is `<platform>:<outcome_id>`
        assert call.args[1] == "betsson-pba:s-out-1"
        # Value is JSON-encoded snapshot fields
        import json

        value_decoded = json.loads(call.args[2])
        assert value_decoded["platform"] == "betsson-pba"
        assert value_decoded["platform_outcome_id"] == "s-out-1"
        assert value_decoded["decimal_odds"] == str(snap.decimal_odds)

    async def test_pipeline_execute_called_per_snapshot(self) -> None:
        """Pipelining XADD + HSET keeps the sink at ~one Redis
        round-trip per snapshot (one `execute`)."""
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()
        for i in range(5):
            await queue.put(_snapshot(outcome_id=f"out-{i}"))
        stop.set()
        await sink.run(queue, stop)
        # One execute per snapshot, even though each pipelines two commands
        assert redis.pipe.execute.await_count == 5

    async def test_drains_full_queue_before_exit(self) -> None:
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        for i in range(5):
            await queue.put(_snapshot(market_id=f"m-{i}"))
        stop.set()

        await sink.run(queue, stop)

        assert redis.pipe.xadd.call_count == 5
        assert queue.empty()

    async def test_idle_returns_when_stop_set_and_queue_empty(self) -> None:
        """No snapshots enqueued; sink should not hang."""
        redis = _mock_redis()
        # Short queue-get timeout so the test doesn't take a real second.
        sink = RedisSnapshotSink(redis_client=redis, queue_get_timeout_sec=0.05)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()
        stop.set()

        await asyncio.wait_for(sink.run(queue, stop), timeout=2.0)
        redis.pipe.xadd.assert_not_called()

    async def test_polls_stop_event_during_idle_period(self) -> None:
        """When the queue is empty and producer is still running,
        the sink must keep checking stop_event regularly."""
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis, queue_get_timeout_sec=0.05)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        sink_task = asyncio.create_task(sink.run(queue, stop))
        # Let it spin idle for a moment.
        await asyncio.sleep(0.2)
        assert not sink_task.done()
        # Trip stop; sink should notice within ~one queue_get_timeout.
        stop.set()
        await asyncio.wait_for(sink_task, timeout=2.0)
        redis.pipe.xadd.assert_not_called()

    async def test_continues_after_write_failure(self) -> None:
        """A failed xadd should be logged and skipped, not crash the loop."""
        redis = _mock_redis()
        # Fail the first call, succeed on subsequent ones.
        redis.pipe.execute.side_effect = [
            ConnectionError("redis ate the message"),
            None,
            None,
        ]
        sink = RedisSnapshotSink(redis_client=redis)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        for i in range(3):
            await queue.put(_snapshot(market_id=f"m-{i}"))
        stop.set()

        await sink.run(queue, stop)
        # All three attempts made; one dropped, two written.
        assert redis.pipe.xadd.call_count == 3

    async def test_passes_through_serialization_for_present_max_stake(self) -> None:
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        await queue.put(_snapshot(max_stake=2500.0))
        stop.set()

        await sink.run(queue, stop)

        fields = redis.pipe.xadd.call_args.args[1]
        assert fields["max_stake"] == "2500.0"

    async def test_uses_custom_stream_name(self) -> None:
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis, stream_name="custom:stream")
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        await queue.put(_snapshot())
        stop.set()
        await sink.run(queue, stop)

        assert redis.pipe.xadd.call_args.args[0] == "custom:stream"

    async def test_keeps_draining_while_producer_task_alive(self) -> None:
        """Regression: at SIGTERM time, the producer is mid-poll-cycle and
        keeps appending to the queue until that cycle finishes. The sink
        must not exit on the first transient empty-queue moment; it must
        wait for the producer task to fully complete, otherwise trailing
        snapshots get dropped (the bug that motivated this kwarg)."""
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis, queue_get_timeout_sec=0.05)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        # Simulate a producer that puts items slowly, with stop already
        # set — without the producer_task gate the sink would race past
        # the first put and exit on the empty queue between iterations.
        async def fake_producer() -> None:
            for i in range(5):
                await queue.put(_snapshot(market_id=f"m-{i}"))
                await asyncio.sleep(0.05)

        producer_task: asyncio.Task[None] = asyncio.create_task(fake_producer())
        stop.set()  # tripped *before* the producer is done

        await asyncio.wait_for(sink.run(queue, stop, producer_task=producer_task), timeout=3.0)
        assert producer_task.done()
        assert redis.pipe.xadd.call_count == 5

    async def test_exits_when_producer_done_and_queue_empty(self) -> None:
        """Once the producer has finished AND the queue is drained, the
        sink should exit promptly — not hang waiting for more work."""
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis, queue_get_timeout_sec=0.05)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        async def quick_producer() -> None:
            await queue.put(_snapshot())

        producer_task: asyncio.Task[None] = asyncio.create_task(quick_producer())
        await producer_task  # let it finish first
        stop.set()

        await asyncio.wait_for(sink.run(queue, stop, producer_task=producer_task), timeout=2.0)
        assert redis.pipe.xadd.call_count == 1

    async def test_maxlen_argument_is_passed(self) -> None:
        redis = _mock_redis()
        sink = RedisSnapshotSink(redis_client=redis, maxlen=42)
        queue: asyncio.Queue[RawOddsSnapshot] = asyncio.Queue()
        stop = asyncio.Event()

        await queue.put(_snapshot())
        stop.set()
        await sink.run(queue, stop)

        assert redis.pipe.xadd.call_args.kwargs["maxlen"] == 42
        assert redis.pipe.xadd.call_args.kwargs["approximate"] is True
