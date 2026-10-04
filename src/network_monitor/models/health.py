"""Collector health models.

Collector health is first-class data, not a log line: /api/status exposes it and
the COLLECTOR_FAILURE rule consumes it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from .network import to_iso

HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "failed"
UNKNOWN = "unknown"


@dataclass
class CollectorStatus:
    """Last known health of a single collector."""

    name: str
    state: str = UNKNOWN
    last_success: Optional[datetime] = None
    last_attempt: Optional[datetime] = None
    last_duration_ms: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["last_success"] = to_iso(self.last_success) if self.last_success else None
        data["last_attempt"] = to_iso(self.last_attempt) if self.last_attempt else None
        return data


@dataclass
class CollectorHealthRegistry:
    """Mutable registry holding the current health of every collector."""

    statuses: Dict[str, CollectorStatus] = field(default_factory=dict)

    def ensure(self, name: str) -> CollectorStatus:
        if name not in self.statuses:
            self.statuses[name] = CollectorStatus(name=name)
        return self.statuses[name]

    def get(self, name: str) -> Optional[CollectorStatus]:
        return self.statuses.get(name)

    def record_success(self, name: str, timestamp: datetime, duration_ms: float) -> CollectorStatus:
        status = self.ensure(name)
        status.state = HEALTHY
        status.last_success = timestamp
        status.last_attempt = timestamp
        status.last_duration_ms = duration_ms
        status.last_error = None
        status.consecutive_failures = 0
        status.total_runs += 1
        return status

    def record_failure(
        self, name: str, timestamp: datetime, error: str, duration_ms: float | None = None
    ) -> CollectorStatus:
        status = self.ensure(name)
        status.last_attempt = timestamp
        status.last_error = error
        status.last_duration_ms = duration_ms
        status.consecutive_failures += 1
        status.total_runs += 1
        status.total_failures += 1
        status.state = FAILED if status.consecutive_failures >= 2 else DEGRADED
        return status

    def to_dict(self) -> Dict[str, Any]:
        return {name: status.to_dict() for name, status in self.statuses.items()}

    def states(self) -> Dict[str, str]:
        return {name: status.state for name, status in self.statuses.items()}
