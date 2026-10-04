"""Event models: the explanation layer of the monitor.

Every event answers four questions without needing a follow-up query:
what happened (``event_type``/``title``), when (``timestamp``), where
(``interface_name`` / ``pid`` / ``process_name`` / ``source``) and why
(``description`` + structured ``evidence``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from .network import from_iso, to_iso


class EventType(str, Enum):
    """All event types the deterministic rule set can raise."""

    HIGH_DOWNLOAD = "HIGH_DOWNLOAD"
    HIGH_UPLOAD = "HIGH_UPLOAD"
    INTERFACE_ERROR = "INTERFACE_ERROR"
    NEW_NETWORK_PROCESS = "NEW_NETWORK_PROCESS"
    CONNECTION_SPIKE = "CONNECTION_SPIKE"
    COLLECTOR_FAILURE = "COLLECTOR_FAILURE"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


class EventSeverity(str, Enum):
    """Severity levels, ordered from least to most urgent."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 1, "warning": 2, "critical": 3}[self.value]

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


class EventStatus(str, Enum):
    """Lifecycle of an event as the user sees it in the dashboard."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


@dataclass
class Event:
    """A detected condition, together with the evidence behind it."""

    timestamp: datetime
    event_type: EventType
    severity: EventSeverity
    title: str
    description: str
    source: str = "detection-engine"
    pid: Optional[int] = None
    process_name: Optional[str] = None
    interface_name: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    status: EventStatus = EventStatus.OPEN
    id: Optional[int] = None

    def dedupe_key(self, window_seconds: float) -> tuple[Any, ...]:
        """Key used to suppress duplicate events within a cooldown window.

        Time is bucketed, so two identical conditions one second apart collapse
        to the same key while the same condition an hour later does not.
        """
        bucket = int(self.timestamp.timestamp() // max(window_seconds, 1))
        return (
            self.event_type.value,
            self.interface_name,
            self.pid,
            bucket,
        )

    def to_row(self) -> Dict[str, Any]:
        return {
            "timestamp": to_iso(self.timestamp),
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "pid": self.pid,
            "process_name": self.process_name,
            "interface_name": self.interface_name,
            "evidence": json.dumps(self.evidence, default=str),
            "status": self.status.value,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Event":
        evidence: Dict[str, Any] = {}
        raw_evidence = row["evidence"]
        if raw_evidence:
            try:
                loaded = json.loads(raw_evidence)
                if isinstance(loaded, dict):
                    evidence = loaded
            except (TypeError, ValueError):
                evidence = {"raw": raw_evidence}

        return cls(
            id=row["id"],
            timestamp=from_iso(row["timestamp"]),
            event_type=EventType(row["event_type"]),
            severity=EventSeverity(row["severity"]),
            title=row["title"],
            description=row["description"],
            source=row["source"],
            pid=row["pid"],
            process_name=row["process_name"],
            interface_name=row["interface_name"],
            evidence=evidence,
            status=EventStatus(row["status"]),
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = to_iso(self.timestamp)
        data["event_type"] = self.event_type.value
        data["severity"] = self.severity.value
        data["status"] = self.status.value
        return data

    def summary_line(self, local_time: bool = True) -> str:
        """Compact single-line rendering used by notifications."""
        stamp = self.timestamp
        if local_time:
            stamp = stamp.astimezone()
        return (
            f"[{self.severity.value.upper()}] {self.timestamp.strftime('%H:%M:%S')} "
            f"{self.title}: {self.description}"
        )
