"""Escalation recovery for the execution pipeline (cold path → frozen path).

When the deterministic hot path can't proceed (session expiry, 2FA challenge,
a novel/unexpected state) it escalates to a `RecoveryHandler` rather than
improvising on real money:

- ``RESOLVED``   — the out-of-band condition was fixed; the hot path may retry.
- ``UNRESOLVED`` — recovery failed; the executor FREEZES (halt + alert + human
  takeover).

Today's handler is `HumanRecoveryHandler`: it alerts the operator over Telegram
and returns ``UNRESOLVED`` — a human resolves the condition out-of-band and
restarts the system (there is no auto-resume channel yet, so the cold path
currently collapses into the frozen path). The `RecoveryHandler` protocol is
the seam where a future **fully-agentic handler (openclaw)** that performs
autonomous re-auth and returns ``RESOLVED`` can be dropped in — recovery only,
never the time-critical hot path. See ``docs/architecture.md`` Layer 4.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

import structlog

from src.execution.notify import Notifier

log = structlog.get_logger(__name__)


class RecoveryOutcome(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


@runtime_checkable
class RecoveryHandler(Protocol):
    async def recover(self, reason: str) -> RecoveryOutcome:
        """Attempt to resolve an out-of-band condition. Returns RESOLVED if the
        hot path may retry, UNRESOLVED if execution must freeze."""
        ...


class HumanRecoveryHandler:
    """Cold-path recovery via a human. Alerts over Telegram and returns
    UNRESOLVED — the operator fixes the condition out-of-band and restarts.
    Until an auto-resume channel exists this is effectively the frozen path."""

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def recover(self, reason: str) -> RecoveryOutcome:
        await self._notifier.send(f"⚠️ Execution paused — manual recovery needed: {reason}")
        log.warning("recovery.human_required", reason=reason)
        return RecoveryOutcome.UNRESOLVED
