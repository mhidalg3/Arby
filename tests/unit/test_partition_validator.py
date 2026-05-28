"""Unit tests for the composed pre-filter → LLM partition validator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.semantic.llm_validator import LLMValidator, LLMVerdict
from src.semantic.partition_filter import MatchContext, Verdict
from src.semantic.partition_validator import PartitionDecision, PartitionValidator

REGULAR_SEASON = MatchContext("Liga Profesional", "regular_season", is_knockout=False)


@pytest.fixture
def llm() -> MagicMock:
    """A mocked LLMValidator whose `validate` returns a configurable record."""
    mock = MagicMock(spec=LLMValidator)
    mock.validate = AsyncMock()
    return mock


class TestPreFilterPath:
    async def test_pre_filter_valid_short_circuits(self, llm: MagicMock) -> None:
        """When the pre-filter returns VALID the LLM must not be called."""
        validator = PartitionValidator(llm_validator=llm)
        # This pair matches the yes/no complement rule (a VALID pre-filter rule).
        result = await validator.validate("BTTS Yes", "BTTS No", REGULAR_SEASON)
        assert result.verdict is Verdict.VALID
        assert result.source == "pre_filter"
        assert result.llm_verdict is None
        llm.validate.assert_not_called()

    async def test_pre_filter_reason_passes_through(self, llm: MagicMock) -> None:
        validator = PartitionValidator(llm_validator=llm)
        result = await validator.validate(
            "Boca gana al medio tiempo",
            "Boca no gana al medio tiempo",
            REGULAR_SEASON,
        )
        assert result.source == "pre_filter"
        assert "complement" in result.reason


class TestLLMEscalation:
    async def test_unknown_falls_through_to_llm(self, llm: MagicMock) -> None:
        """Pre-filter UNKNOWN must escalate."""
        llm.validate.return_value = LLMVerdict(
            verdict=Verdict.INVALID,
            confidence="high",
            reasoning="misses the draw",
            trap_pattern="win_loss_without_draw",
        )
        validator = PartitionValidator(llm_validator=llm)
        # No rule fires on this pair — pre-filter returns UNKNOWN, LLM decides.
        result = await validator.validate("Gana Boca Juniors", "Gana River Plate", REGULAR_SEASON)
        assert result.verdict is Verdict.INVALID
        assert result.source == "llm"
        assert result.reason == "misses the draw"
        assert result.llm_verdict is not None
        assert result.llm_verdict.trap_pattern == "win_loss_without_draw"
        llm.validate.assert_awaited_once_with(
            "Gana Boca Juniors", "Gana River Plate", REGULAR_SEASON
        )

    async def test_llm_valid_passes_through(self, llm: MagicMock) -> None:
        llm.validate.return_value = LLMVerdict(
            verdict=Verdict.VALID,
            confidence="medium",
            reasoning="clean partition",
            trap_pattern="none",
        )
        validator = PartitionValidator(llm_validator=llm)
        result = await validator.validate(
            "Gana Boca Juniors", "Empate o gana River Plate", REGULAR_SEASON
        )
        assert result == PartitionDecision(
            verdict=Verdict.VALID,
            source="llm",
            reason="clean partition",
            llm_verdict=llm.validate.return_value,
        )


class TestDecisionShape:
    """The PartitionDecision shape is what feeds the partition_validations
    audit log; lock the surface."""

    async def test_pre_filter_decision_has_no_llm_record(self, llm: MagicMock) -> None:
        validator = PartitionValidator(llm_validator=llm)
        result = await validator.validate("BTTS Yes", "BTTS No", REGULAR_SEASON)
        assert result.llm_verdict is None

    async def test_llm_decision_has_full_llm_record(self, llm: MagicMock) -> None:
        llm.validate.return_value = LLMVerdict(
            verdict=Verdict.INVALID,
            confidence="low",
            reasoning="unclear scope",
            trap_pattern="scope_mismatch",
        )
        validator = PartitionValidator(llm_validator=llm)
        result = await validator.validate("Vague A", "Vague B", REGULAR_SEASON)
        assert result.llm_verdict is not None
        assert result.llm_verdict.confidence == "low"
        assert result.llm_verdict.trap_pattern == "scope_mismatch"
