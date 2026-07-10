"""Tests for the configure_logging tee (hot-loop log file contract)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog

from src.config import get_settings
from src.logging_setup import configure_logging


def test_tee_truncates_then_mirrors_json_events_to_file_and_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """configure_logging(tee_path=...) truncates the file, then structlog and
    stdlib logging events land there while still reaching stdout."""
    for k in ("DATABASE_URL", "DATABASE_URL_SYNC", "REDIS_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "test-value")
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    log_file = tmp_path / "bot.log"
    log_file.write_text('{"event": "stale.previous_run"}\n')
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    root.addHandler(logging.NullHandler())
    try:
        configure_logging(tee_path=log_file)
        structlog.get_logger("tee_smoke").info("orchestrator.arb_found", roi_pct=5.0)
        logging.getLogger("stdlib_tee_smoke").warning("stdlib.hot_loop_event")
        raw_lines = [ln for ln in log_file.read_text().splitlines() if ln]
        lines = [json.loads(ln) for ln in raw_lines if ln.startswith("{")]
        assert all(e["event"] != "stale.previous_run" for e in lines)  # truncated
        found = [e for e in lines if e["event"] == "orchestrator.arb_found"]
        assert found and found[0]["roi_pct"] == 5.0  # parseable JSON in the file
        assert "stdlib.hot_loop_event" in raw_lines  # stdlib/httpx path uses tee too
        stdout = capsys.readouterr().out
        assert "orchestrator.arb_found" in stdout  # stdout too
        assert "stdlib.hot_loop_event" in stdout
    finally:
        structlog.reset_defaults()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        get_settings.cache_clear()
