"""Tests for the execution recovery seam."""

from __future__ import annotations

from src.execution.recovery import HumanRecoveryHandler, RecoveryOutcome


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


async def test_human_recovery_alerts_and_returns_unresolved() -> None:
    n = _FakeNotifier()
    out = await HumanRecoveryHandler(n).recover("session expired")
    assert out is RecoveryOutcome.UNRESOLVED
    assert any("manual recovery" in t.lower() for t in n.sent)
    assert any("session expired" in t for t in n.sent)
