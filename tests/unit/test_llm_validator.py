"""Unit tests for the LLM partition validator.

These mock the Anthropic client and verify the request shape — model,
adaptive thinking, system-prompt caching, strict-tool-forced output — and
the response-parsing contract. They do NOT exercise the real model; the
LLM's classification accuracy is verified separately via a fixture-driven
integration test (next slice).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.semantic.llm_validator import (
    PARTITION_TOOL,
    SYSTEM_PROMPT,
    TOOL_NAME,
    LLMValidator,
    LLMVerdict,
)
from src.semantic.partition_filter import MatchContext, Verdict

REGULAR_SEASON = MatchContext(
    competition="Liga Profesional", stage="regular_season", is_knockout=False
)


def make_response(
    verdict: str = "valid",
    confidence: str = "high",
    reasoning: str = "Clean BTTS yes/no binary.",
    trap_pattern: str = "none",
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    """Build a mock anthropic.Message with a single forced tool_use block."""
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.name = TOOL_NAME
    tool_use.input = {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": reasoning,
        "trap_pattern": trap_pattern,
    }
    response = MagicMock()
    response.content = [tool_use]
    response.stop_reason = "tool_use"
    response.usage = MagicMock()
    response.usage.cache_read_input_tokens = cache_read
    response.usage.cache_creation_input_tokens = cache_write
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    return response


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock()
    return client


@pytest.fixture
def call_kwargs(mock_client: MagicMock) -> dict[str, Any]:
    """Helper that runs one validate() call and returns the kwargs passed
    to messages.create. Test bodies that don't need a particular response
    can use this directly."""

    async def _capture() -> dict[str, Any]:
        mock_client.messages.create.return_value = make_response()
        validator = LLMValidator(client=mock_client)
        await validator.validate("BTTS Yes", "BTTS No", REGULAR_SEASON)
        return mock_client.messages.create.call_args.kwargs

    return _capture  # type: ignore[return-value]


class TestRequestShape:
    async def test_uses_opus_4_7_by_default(self, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = make_response()
        await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        assert mock_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"

    async def test_model_is_overridable(self, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = make_response()
        await LLMValidator(client=mock_client, model="claude-haiku-4-5").validate(
            "a", "b", REGULAR_SEASON
        )
        assert mock_client.messages.create.call_args.kwargs["model"] == "claude-haiku-4-5"

    async def test_uses_adaptive_thinking(self, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = make_response()
        await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["thinking"] == {"type": "adaptive"}

    async def test_effort_is_high(self, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = make_response()
        await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        assert mock_client.messages.create.call_args.kwargs["output_config"] == {"effort": "high"}

    async def test_system_prompt_is_cached(self, mock_client: MagicMock) -> None:
        """The system prompt is the cacheable prefix; an ephemeral
        cache_control marker on it is what triggers the price reduction
        on repeat calls."""
        mock_client.messages.create.return_value = make_response()
        await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        system = mock_client.messages.create.call_args.kwargs["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == SYSTEM_PROMPT

    async def test_strict_tool_use_is_forced(self, mock_client: MagicMock) -> None:
        """tool_choice forces the partition tool — the model cannot
        respond with free-form text, which guarantees a parseable shape."""
        mock_client.messages.create.return_value = make_response()
        await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["tools"] == [PARTITION_TOOL]
        assert PARTITION_TOOL["strict"] is True
        assert kwargs["tool_choice"] == {"type": "tool", "name": TOOL_NAME}

    async def test_user_message_includes_descriptions_and_context(
        self, mock_client: MagicMock
    ) -> None:
        mock_client.messages.create.return_value = make_response()
        ctx = MatchContext("Copa Libertadores", "round_of_16", is_knockout=True)
        await LLMValidator(client=mock_client).validate("Avanza River", "Eliminado River", ctx)
        messages = mock_client.messages.create.call_args.kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        body = messages[0]["content"]
        assert "Avanza River" in body
        assert "Eliminado River" in body
        assert "Copa Libertadores" in body
        assert "round_of_16" in body
        assert "True" in body  # is_knockout serialized


class TestSchemaInvariants:
    """The tool schema must satisfy strict-mode constraints. These tests
    catch a regression in the schema shape (a missing field in `required`,
    accidentally permissive `additionalProperties`, etc.) before it lands
    in production."""

    def test_all_properties_are_required(self) -> None:
        schema = PARTITION_TOOL["input_schema"]
        props = set(schema["properties"].keys())
        required = set(schema["required"])
        assert props == required, (
            f"strict mode requires every property in `required`; missing: {props - required}"
        )

    def test_additional_properties_is_false(self) -> None:
        assert PARTITION_TOOL["input_schema"]["additionalProperties"] is False

    def test_verdict_enum_matches_verdict_values(self) -> None:
        """The schema's `verdict` enum and the `Verdict` enum's string
        values must stay in sync — otherwise parsing a valid LLM response
        will raise on `Verdict(...)`."""
        schema_values = set(PARTITION_TOOL["input_schema"]["properties"]["verdict"]["enum"])
        # The LLM is only allowed to commit to a definite verdict.
        assert schema_values == {"valid", "invalid"}
        assert all(Verdict(v) in (Verdict.VALID, Verdict.INVALID) for v in schema_values)


class TestResponseParsing:
    async def test_returns_valid_verdict(self, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = make_response(
            verdict="valid", confidence="high", reasoning="ok", trap_pattern="none"
        )
        result = await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        assert result == LLMVerdict(
            verdict=Verdict.VALID,
            confidence="high",
            reasoning="ok",
            trap_pattern="none",
        )

    async def test_returns_invalid_verdict_with_trap_name(self, mock_client: MagicMock) -> None:
        mock_client.messages.create.return_value = make_response(
            verdict="invalid",
            confidence="medium",
            reasoning="misses draw",
            trap_pattern="win_loss_without_draw",
        )
        result = await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        assert result.verdict == Verdict.INVALID
        assert result.confidence == "medium"
        assert result.trap_pattern == "win_loss_without_draw"

    async def test_raises_when_no_tool_use_block(self, mock_client: MagicMock) -> None:
        """Defensive: should be unreachable with forced tool_choice, but
        if the API ever returned plain text (e.g., due to a refusal stop
        reason), parsing must fail loudly rather than silently drop the
        decision."""
        empty = MagicMock()
        empty.content = []
        empty.stop_reason = "refusal"
        mock_client.messages.create.return_value = empty

        with pytest.raises(ValueError, match="tool_use"):
            await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)

    async def test_ignores_non_matching_tool_blocks(self, mock_client: MagicMock) -> None:
        """If the response somehow contains a different tool_use plus the
        expected one, the parser should pick the expected one."""
        decoy = MagicMock()
        decoy.type = "tool_use"
        decoy.name = "some_other_tool"
        decoy.input = {"verdict": "valid"}  # would mis-classify if used

        wanted = MagicMock()
        wanted.type = "tool_use"
        wanted.name = TOOL_NAME
        wanted.input = {
            "verdict": "invalid",
            "confidence": "high",
            "reasoning": "ok",
            "trap_pattern": "integer_push",
        }

        response = MagicMock()
        response.content = [decoy, wanted]
        response.stop_reason = "tool_use"
        response.usage = MagicMock(
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            input_tokens=10,
            output_tokens=10,
        )
        mock_client.messages.create.return_value = response

        result = await LLMValidator(client=mock_client).validate("a", "b", REGULAR_SEASON)
        assert result.verdict == Verdict.INVALID
