"""Tests for the CDP debug-port gating on InSessionTransport.

Off by default — ``port_base=None`` must produce no launch args (identical behavior
for dry-run, tests, or production runs that don't opt in via Settings.cdp_port_base).
When set, allocates a deterministic per-platform port so an external read-only client
can attach to each platform's window separately.
"""

from __future__ import annotations

from src.execution.session import _cdp_debug_args


def test_no_port_means_no_args() -> None:
    # The default path: every existing test and dry-run hits this branch.
    assert _cdp_debug_args("betano", None) == []
    assert _cdp_debug_args("betsson", None) == []
    assert _cdp_debug_args("betwarrior", None) == []


def test_port_allocates_per_platform_ports() -> None:
    # Base 9222 → betano 9222, betsson 9223, betwarrior 9224. Three distinct
    # ports so each CDP attach reaches exactly one logged-in window.
    assert _cdp_debug_args("betano", 9222) == ["--remote-debugging-port=9222"]
    assert _cdp_debug_args("betsson", 9222) == ["--remote-debugging-port=9223"]
    assert _cdp_debug_args("betwarrior", 9222) == ["--remote-debugging-port=9224"]


def test_unknown_platform_falls_back_to_base_port() -> None:
    # Defensive: a typo or a future platform name must not crash startup —
    # it attaches at the base port instead (offset 0).
    assert _cdp_debug_args("bplay", 9222) == ["--remote-debugging-port=9222"]
