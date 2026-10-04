"""Logging configuration for Local Network Monitor.

One call to :func:`configure_logging` wires up a rotating file handler plus an
optional console handler. Collectors, storage, detection, API and notifications
all log through the standard library, so ``logs/monitor.log`` is the single
place to look when something misbehaves.

Two layouts are supported:

``text``
    Human-readable single lines, the default for interactive use.

``json``
    One JSON object per line with stable keys (``ts``, ``level``, ``logger``,
    ``message``, ``exception`` plus any contextual fields passed via
    ``extra=``). This is what a log shipper, a SIEM or a cron-based alert would
    consume, and it is why the formatter is part of the product rather than an
    afterthought: an operational tool nobody can observe is not operable.

Contextual fields are attached by the code that knows them, e.g.::

    logger.warning("collector failed", extra={"collector": "connections"})

and land in the JSON output as ``"collector": "connections"``.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import LoggingSection

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-38s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Attributes present on every LogRecord; anything else was passed via `extra`.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def __init__(self, *, timestamp_key: str = "ts") -> None:
        super().__init__()
        self.timestamp_key = timestamp_key

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            self.timestamp_key: datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRIBUTES or key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def build_formatter(section: LoggingSection) -> logging.Formatter:
    """Return the formatter matching the configured layout."""
    if section.format == "json":
        return JsonFormatter()
    return logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def configure_logging(section: LoggingSection) -> logging.Logger:
    """Configure the root logger. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(section.level)

    # Remove handlers from previous calls (important for tests and --reload).
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = build_formatter(section)

    log_path = Path(section.file).expanduser()
    if section.file:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=section.max_bytes,
                backupCount=section.backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - depends on filesystem
            # Logging must never prevent the monitor from starting.
            print(f"warning: cannot open log file {log_path}: {exc}", file=sys.stderr)

    if section.console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # Uvicorn's access log duplicates our own request logging; keep it quieter.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logging.getLogger("network_monitor")
