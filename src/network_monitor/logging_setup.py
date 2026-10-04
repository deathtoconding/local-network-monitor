"""Logging configuration for Local Network Monitor.

One call to :func:`configure_logging` wires up a rotating file handler plus an
optional console handler. Collectors, storage, detection, API and notifications
all log through the standard library, so ``logs/monitor.log`` is the single
place to look when something misbehaves.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingSection

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-38s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(section: LoggingSection) -> logging.Logger:
    """Configure the root logger. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(section.level)

    # Remove handlers from previous calls (important for tests and --reload).
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

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
