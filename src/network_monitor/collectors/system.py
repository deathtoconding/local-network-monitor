"""System collector.

Provides the ambient context that makes traffic numbers interpretable: CPU and
memory pressure of the monitor host, plus a summary of how many sockets are in
which state. Kept deliberately small - it must stay cheap enough to poll once a
second without becoming the reason the monitor is expensive.
"""

from __future__ import annotations

import platform
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

import psutil

from ..models.network import utc_now
from .base import Collector, CollectorError

STARTUP = utc_now()


@dataclass
class SystemSnapshot:
    """Host-level information sampled once per collection cycle."""

    timestamp: datetime
    hostname: str
    platform: str
    python_version: str
    cpu_percent: float
    memory_percent: float
    process_count: int
    connection_states: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "hostname": self.hostname,
            "platform": self.platform,
            "python_version": self.python_version,
            "cpu_percent": round(self.cpu_percent, 2),
            "memory_percent": round(self.memory_percent, 2),
            "process_count": self.process_count,
            "connection_states": self.connection_states,
        }


class SystemCollector(Collector[SystemSnapshot]):
    """Collects cheap host metrics.

    Deliberately cheap enough to poll every cycle: it must never become the
    reason the monitor is expensive. TCP state distribution is *not* gathered
    here - the connection collector already has that data, so the monitoring
    loop attaches it via :func:`attach_connection_states` instead of walking
    the socket table a second time.
    """

    name = "system"

    def collect(self) -> SystemSnapshot:
        try:
            memory = psutil.virtual_memory()
            snapshot = SystemSnapshot(
                timestamp=utc_now(),
                hostname=socket.gethostname(),
                platform=f"{platform.system()} {platform.release()}",
                python_version=platform.python_version(),
                cpu_percent=psutil.cpu_percent(interval=None),
                memory_percent=memory.percent,
                process_count=len(psutil.pids()),
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            raise CollectorError(f"system snapshot failed: {exc}", collector=self.name) from exc
        return snapshot


def attach_connection_states(snapshot: SystemSnapshot, connections: List[Any]) -> SystemSnapshot:
    """Fill in TCP state counts from data already collected this cycle."""
    snapshot.connection_states = connection_state_summary(connections)
    return snapshot


def uptime_seconds() -> float:
    """Seconds since the process was imported (used by /api/status)."""
    return (utc_now() - STARTUP).total_seconds()


def connection_state_summary(connections: List[Any]) -> Dict[str, int]:
    """Count connections per state without touching psutil."""
    states: Dict[str, int] = {}
    for connection in connections:
        state = str(getattr(connection, "state", "UNKNOWN")).upper()
        states[state] = states.get(state, 0) + 1
    return dict(sorted(states.items()))
