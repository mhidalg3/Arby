"""Postgres recorder — second independent consumer of the ``odds:raw`` Redis stream.

Reads snapshots via a consumer group (``XREADGROUP``), canonicalizes them with
its own ``Canonicalizer``, and writes change-compressed rows to the
``odds_snapshots`` TimescaleDB hypertable.

Decoupled from:
- the ingestion daemon's ``asyncio.Queue`` (reads the stream directly via XREADGROUP);
- the ``ArbDetector``'s plain ``XREAD`` consumer (separate consumer group name).

Best-effort: on Postgres outage, logs + drops. Recording never crashes the
daemon loop. Enabled by the daemon when ``RECORD_TO_PG=1``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis

from src.ingestion.redis_sink import STREAM_NAME, stream_fields_to_snapshot
from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import CanonicalQuote
from src.semantic.canonicalizer import Canonicalizer
from src.semantic.fixture_resolver import FixtureResolver
from src.storage.db import get_session
from src.storage.models import OddsSnapshot

log = structlog.get_logger(__name__)

CONSUMER_GROUP = "pg_recorder"
DEFAULT_BATCH_SIZE = 500
DEFAULT_BLOCK_MS = 5000
DEFAULT_HEARTBEAT_SEC = 60.0


class PgRecorder:
    """Consumes ``odds:raw`` and writes change-compressed ticks to Postgres.

    Lifecycle: ``run()`` loops until ``stop_event`` is set. Idempotent consumer
    group creation (swallows ``BUSYGROUP``). Each ``XREADGROUP`` batch is decoded,
    canonicalized, change-compressed, and batch-inserted. On any Postgres error
    the batch is dropped (logged) — recording is best-effort, never fatal.
    """

    def __init__(
        self,
        redis_client: Redis,
        canonicalizer: Canonicalizer | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        block_ms: int = DEFAULT_BLOCK_MS,
        heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
    ) -> None:
        self._redis = redis_client
        self._canonicalizer = canonicalizer or Canonicalizer(fixture_resolver=FixtureResolver())
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._heartbeat_sec = heartbeat_sec
        self._session_id = uuid.uuid4().hex
        # Change-compression state: (platform, platform_outcome_id) → (odds, observed_at_epoch)
        self._last: dict[tuple[str, str], tuple[float, float]] = {}
        # Heartbeat tracking: (platform, platform_outcome_id) → last heartbeat epoch
        self._last_heartbeat: dict[tuple[str, str], float] = {}
        self._log = log.bind(component="pg_recorder", session=self._session_id)

    async def _ensure_consumer_group(self) -> None:
        """Create the consumer group idempotently. BUSYGROUP = already exists."""
        try:
            await self._redis.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="$", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            self._log.debug("pg_recorder.group_exists")

    async def run(self, stop_event) -> None:  # type: ignore[no-untyped-def]
        """Main loop: XREADGROUP → decode → canonicalize → compress → insert → XACK."""
        await self._ensure_consumer_group()
        consumer = f"recorder-{self._session_id[:8]}"
        self._log.info("pg_recorder.starting", stream=STREAM_NAME, consumer=consumer)

        while not stop_event.is_set():
            try:
                # XREADGROUP returns: {stream_name: [(entry_id, {fields}), ...]}
                resp = await self._redis.xreadgroup(
                    CONSUMER_GROUP,
                    consumer,
                    {STREAM_NAME: ">"},
                    count=self._batch_size,
                    block=self._block_ms,
                )
            except Exception as exc:
                self._log.warning("pg_recorder.xreadgroup_error", error=str(exc))
                continue

            if not resp:
                continue

            # redis-py returns [(stream_name, [(entry_id, fields), ...]), ...]
            entries: list[tuple[str, dict[str, str]]] = []
            for _stream, items in resp:
                entries.extend(items)
            if not entries:
                continue

            await self._process_batch(entries)

        self._log.info("pg_recorder.stopped")

    async def _process_batch(self, entries: Sequence[tuple[str, dict[str, str]]]) -> None:
        """Decode, canonicalize, change-compress, and insert one batch."""
        rows: list[OddsSnapshot] = []
        entry_ids: list[str] = []

        for entry_id, fields in entries:
            entry_ids.append(entry_id)
            snapshot = stream_fields_to_snapshot(fields)
            if snapshot is None:
                continue
            row = await self._build_row(snapshot)
            if row is not None:
                rows.append(row)

        if not rows:
            # Still ACK the entries so they don't pile up in the PEL.
            await self._ack(entry_ids)
            return

        try:
            async with get_session() as session:
                session.add_all(rows)
            self._log.debug("pg_recorder.wrote", n=len(rows))
        except Exception as exc:
            self._log.warning("pg_recorder.insert_failed", error=str(exc), n=len(rows))
            # Best-effort: drop the batch on DB error. Don't re-deliver —
            # the stream is a firehose, not a work queue; re-processing
            # stale ticks would add noise without value.

        await self._ack(entry_ids)

    async def _ack(self, entry_ids: list[str]) -> None:
        """Acknowledge processed entries so they leave the pending list."""
        if not entry_ids:
            return
        try:
            await self._redis.xack(STREAM_NAME, CONSUMER_GROUP, *entry_ids)
        except Exception as exc:
            self._log.warning("pg_recorder.xack_error", error=str(exc))

    async def _build_row(self, snapshot: RawOddsSnapshot) -> OddsSnapshot | None:
        """Canonicalize, change-compress, and build an ORM row.

        Returns None when the snapshot should be skipped (not yielded at all).
        For unchanged observations, returns a heartbeat row only if
        ``heartbeat_sec`` has elapsed since the last heartbeat.
        """
        cq = await self._canonicalizer.canonicalize(snapshot)

        key = (snapshot.platform, snapshot.platform_outcome_id)
        prev = self._last.get(key)
        now_epoch = snapshot.timestamp

        is_change: bool
        prev_observed: datetime | None
        if prev is None:
            # First sighting.
            is_change = True
            prev_observed = None
        elif abs(prev[0] - snapshot.decimal_odds) > 1e-9:
            # Odds moved.
            is_change = True
            prev_observed = datetime.fromtimestamp(prev[1], tz=UTC)
        else:
            # Unchanged — heartbeat row only if enough time passed since the last WRITE.
            is_change = False
            prev_observed = datetime.fromtimestamp(prev[1], tz=UTC)
            last_written = self._last_heartbeat.get(key, prev[1])
            if now_epoch - last_written < self._heartbeat_sec:
                # Skip this observation entirely (no row written).
                self._last[key] = (snapshot.decimal_odds, now_epoch)
                return None

        # Update state — always track last-written time for this outcome
        # so the heartbeat gate compares against the last change OR heartbeat.
        self._last[key] = (snapshot.decimal_odds, now_epoch)
        self._last_heartbeat[key] = now_epoch

        row = OddsSnapshot(
            time=datetime.fromtimestamp(now_epoch, tz=UTC),
            platform=snapshot.platform,
            platform_outcome_id=snapshot.platform_outcome_id,
            platform_event_id=snapshot.platform_event_id,
            raw_market_name=snapshot.raw_market_name,
            raw_outcome_name=snapshot.raw_outcome_name,
            decimal_odds=snapshot.decimal_odds,
            max_stake=snapshot.max_stake,
            platform_market_id=snapshot.platform_market_id,
            raw_event_name=snapshot.raw_event_name,
            raw_competition=snapshot.raw_competition,
            transport=snapshot.transport,
            is_change=is_change,
            prev_observed_at=prev_observed,
            recorder_session_id=self._session_id,
            kickoff_utc=(
                datetime.fromtimestamp(snapshot.kickoff_utc, tz=UTC)
                if snapshot.kickoff_utc is not None
                else None
            ),
        )

        if cq is not None:
            self._stamp_canonical(row, cq, snapshot)

        return row

    def _stamp_canonical(
        self, row: OddsSnapshot, cq: CanonicalQuote, snapshot: RawOddsSnapshot
    ) -> None:
        """Fill the canonical columns from the resolved quote + snapshot."""
        row.market_code = cq.outcome.market.code.value
        row.line = cq.outcome.market.line
        row.cell = cq.outcome.cell
        row.session_fixture_id = cq.fixture.fixture_id

        # fixture_key: date-bucketed (UTC date of kickoff), NOT the exact instant.
        # Platforms report kickoff with minutes-level jitter; an exact-instant key
        # would fail the cross-platform join the key exists for. The full-precision
        # kickoff_utc is set in _build_row regardless; here we only derive the
        # join key from the canonical team names + kickoff date.
        if snapshot.kickoff_utc is not None:
            ko = datetime.fromtimestamp(snapshot.kickoff_utc, tz=UTC)
            date_str = ko.strftime("%Y-%m-%d")
            # cq.fixture.home_team/away_team are already normalized by the resolver.
            if cq.fixture.home_team and cq.fixture.away_team:
                row.fixture_key = f"{cq.fixture.home_team}|{cq.fixture.away_team}|{date_str}"
