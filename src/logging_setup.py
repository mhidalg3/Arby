"""Structured logging configuration.

Import and call `configure_logging()` once at process startup. Use
`structlog.get_logger(__name__)` everywhere else.
"""

import io
import logging
import sys
from pathlib import Path
from typing import TextIO, cast

import structlog

from src.config import get_settings


class _Tee(io.TextIOBase):
    """Write-only stream duplicating every write to several underlying streams.

    Mirrors the hot loop's log output to both the terminal and the bot log file
    so the session viewer sees events no matter how the process was launched
    (shell redirection is no longer required — see docs/hot_loop_runbook.md).
    """

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self._streams:
            st.flush()


def configure_logging(tee_path: str | Path | None = None) -> None:
    """Configure stdlib + structlog output.

    tee_path: when set, every log line is written BOTH to stdout and to this
    file. The file is TRUNCATED on configure (a redeploy starts a clean log —
    the session viewer replays it from offset 0 and must not re-see a previous
    run's kill-switch events) and opened line-buffered so the viewer's
    size-polling tail sees each event immediately.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    stream: TextIO = sys.stdout
    if tee_path is not None:
        # Process-lifetime handle — deliberately not context-managed.
        tee_file = open(tee_path, "w", buffering=1, encoding="utf-8")  # noqa: SIM115
        stream = cast(TextIO, _Tee(sys.stdout, tee_file))

    logging.basicConfig(
        format="%(message)s",
        stream=stream,
        level=level,
        force=True,
    )

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]

    if settings.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=True,
    )
