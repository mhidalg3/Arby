"""SQLAlchemy ORM models.

These mirror the schema in migrations/init.sql. When the schema changes,
both files must be updated; consider switching to Alembic-generated
migrations once the schema stabilizes.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    competition: Mapped[str] = mapped_column(String, nullable=False)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    kickoff_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_knockout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    platform_events: Mapped[list[PlatformEvent]] = relationship(back_populates="match")
    canonical_outcomes: Mapped[list[CanonicalOutcome]] = relationship(back_populates="match")


class PlatformEvent(Base):
    __tablename__ = "platform_events"
    __table_args__ = (UniqueConstraint("platform", "platform_event_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String, nullable=False)
    platform_event_id: Mapped[str] = mapped_column(String, nullable=False)
    raw_event_name: Mapped[str] = mapped_column(String, nullable=False)

    match: Mapped[Match] = relationship(back_populates="platform_events")


class CanonicalOutcome(Base):
    __tablename__ = "canonical_outcomes"
    __table_args__ = (UniqueConstraint("match_id", "market_type", "outcome_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    match_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    market_type: Mapped[str] = mapped_column(String, nullable=False)
    outcome_key: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)

    match: Mapped[Match] = relationship(back_populates="canonical_outcomes")


class PartitionPair(Base):
    __tablename__ = "partition_pairs"
    __table_args__ = (
        UniqueConstraint("outcome_a_id", "outcome_b_id"),
        CheckConstraint("outcome_a_id < outcome_b_id", name="canonical_pair_ordering"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    outcome_a_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canonical_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    outcome_b_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canonical_outcomes.id", ondelete="CASCADE"), nullable=False
    )
    validation_method: Mapped[str] = mapped_column(String, nullable=False)
    validation_confidence: Mapped[float | None] = mapped_column(Float)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OpportunityStatus(enum.StrEnum):
    DETECTED = "detected"
    APPROVED = "approved"
    LEG_A_PENDING = "leg_a_pending"
    LEG_A_FILLED = "leg_a_filled"
    LEG_B_PENDING = "leg_b_pending"
    COMPLETED = "completed"
    ABORTED_PRE_EXECUTION = "aborted_pre_execution"
    ABORTED_POST_LEG_A = "aborted_post_leg_a"  # naked exposure incident
    # bet submitted but acceptance unconfirmed — may be placed; halt + verify
    PENDING_UNKNOWN = "pending_unknown"
    EXPIRED = "expired"
    FROZEN = "frozen"  # recovery failed / unexpected state — halt


class Opportunity(Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_id: Mapped[str] = mapped_column(String, nullable=False)
    partition_pair_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("partition_pairs.id"), nullable=True
    )
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    expected_margin_pct: Mapped[float] = mapped_column(Float, nullable=False)
    expected_profit: Mapped[float] = mapped_column(Float, nullable=False)
    risk_confidence: Mapped[float | None] = mapped_column(Float)
    high_margin_warning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adaptive_threshold_pct: Mapped[float | None] = mapped_column(Float)
    garch_variance: Mapped[float | None] = mapped_column(Float)
    status: Mapped[OpportunityStatus] = mapped_column(
        Enum(
            OpportunityStatus,
            name="opportunity_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    status_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_reason: Mapped[str | None] = mapped_column(Text)

    placements: Mapped[list[Placement]] = relationship(
        back_populates="opportunity", passive_deletes=True
    )


class Placement(Base):
    __tablename__ = "placements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    leg: Mapped[str] = mapped_column(String(1), nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_odds: Mapped[float] = mapped_column(Float, nullable=False)
    actual_filled_odds: Mapped[float | None] = mapped_column(Float)
    target_stake: Mapped[float] = mapped_column(Float, nullable=False)
    actual_stake: Mapped[float | None] = mapped_column(Float)
    platform_bet_id: Mapped[str | None] = mapped_column(String)
    success: Mapped[bool | None] = mapped_column(Boolean)
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    opportunity: Mapped[Opportunity] = relationship(back_populates="placements")


class PartitionValidation(Base):
    """Audit log for every partition validation decision."""

    __tablename__ = "partition_validations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    desc_a: Mapped[str] = mapped_column(Text, nullable=False)
    desc_b: Mapped[str] = mapped_column(Text, nullable=False)
    match_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str | None] = mapped_column(String)
    result: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    reasoning: Mapped[str | None] = mapped_column(Text)
    edge_cases: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class OddsSnapshot(Base):
    """Raw odds tick — one row per recorded observation in the ``odds_snapshots``
    TimescaleDB hypertable.

    Hypertables have no real primary key; SQLAlchemy needs one to operate,
    so we declare a synthetic composite PK on ``(time, platform, platform_outcome_id)``
    for ORM purposes only (the DB table has no PK constraint).
    """

    __tablename__ = "odds_snapshots"

    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    platform: Mapped[str] = mapped_column(String, primary_key=True)
    platform_outcome_id: Mapped[str] = mapped_column(String, primary_key=True, server_default="")

    platform_event_id: Mapped[str] = mapped_column(String)
    canonical_outcome_id: Mapped[int | None] = mapped_column(BigInteger)
    raw_market_name: Mapped[str] = mapped_column(String)
    raw_outcome_name: Mapped[str] = mapped_column(String)
    decimal_odds: Mapped[float] = mapped_column(Float)
    max_stake: Mapped[float | None] = mapped_column(Float)
    # Lag-model columns (additive — all nullable or server-defaulted).
    platform_market_id: Mapped[str] = mapped_column(String, server_default="")
    raw_event_name: Mapped[str] = mapped_column(String, server_default="")
    raw_competition: Mapped[str] = mapped_column(String, server_default="")
    transport: Mapped[str] = mapped_column(String, server_default="poll")
    market_code: Mapped[str | None] = mapped_column(String)
    line: Mapped[float | None] = mapped_column(Float)
    cell: Mapped[str | None] = mapped_column(String)
    fixture_key: Mapped[str | None] = mapped_column(String)
    kickoff_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_change: Mapped[bool] = mapped_column(Boolean, server_default="true")
    prev_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorder_session_id: Mapped[str] = mapped_column(String, server_default="")
    session_fixture_id: Mapped[str | None] = mapped_column(String)
