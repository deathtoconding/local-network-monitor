"""Shared runtime state for the API.

The monitoring loop writes into this object; the FastAPI routes read from it.
That inversion keeps the HTTP layer completely decoupled from collectors: the
API can be started with no monitor running at all (as the API tests do) and
simply reports an empty, honest state.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from .. import __version__
from ..config import Config
from ..detection import DetectionEngine
from ..models.events import Event
from ..models.health import CollectorHealthRegistry
from ..models.network import (
    InterfaceInfo,
    InterfaceMeasurement,
    NetworkConnection,
    TrafficHistoryPoint,
    TrafficSnapshot,
    utc_now,
)
from ..models.process import ProcessInfo
from ..notifications import NotificationManager
from ..storage import (
    CollectorHealthRepository,
    ConnectionRepository,
    Database,
    EventRepository,
    InterfaceMeasurementRepository,
    ProcessRepository,
)

logger = logging.getLogger(__name__)

EVENTS_WINDOW_HOURS = 24


@dataclass
class MonitorState:
    """Everything the API needs to answer a request."""

    config: Config
    database: Database
    measurements: InterfaceMeasurementRepository
    connections: ConnectionRepository
    processes: ProcessRepository
    events: EventRepository
    health: CollectorHealthRegistry
    detection: DetectionEngine
    notifications: Optional[NotificationManager] = None
    health_repository: Optional[CollectorHealthRepository] = None
    interface_info: Dict[str, InterfaceInfo] = field(default_factory=dict)

    started_at: datetime = field(default_factory=utc_now)
    last_cycle_at: Optional[datetime] = None
    last_cycle_duration_ms: Optional[float] = None
    max_cycle_duration_ms: Optional[float] = None
    cycle_count: int = 0
    #: Cycles that recorded at least one collector/storage error - the SLO
    #: "collection success ratio" is derived from cycle_count and this value.
    failed_cycles: int = 0
    #: Latest in-memory snapshot (falls back to the database when empty).
    latest_measurements: List[InterfaceMeasurement] = field(default_factory=list)
    latest_connections: List[NetworkConnection] = field(default_factory=list)
    latest_processes: List[ProcessInfo] = field(default_factory=list)
    latest_system: Optional[Any] = None
    last_errors: List[str] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---- updates from the monitoring loop ----------------------------
    def publish_cycle(
        self,
        timestamp: datetime,
        measurements: Sequence[InterfaceMeasurement],
        connections: Sequence[NetworkConnection],
        processes: Sequence[ProcessInfo],
        duration_ms: float,
        errors: Sequence[str] = (),
    ) -> None:
        with self._lock:
            self.latest_measurements = list(measurements)
            self.latest_connections = list(connections)
            self.latest_processes = list(processes)
            self.last_cycle_at = timestamp
            self.last_cycle_duration_ms = duration_ms
            if self.max_cycle_duration_ms is None or duration_ms > self.max_cycle_duration_ms:
                self.max_cycle_duration_ms = duration_ms
            self.cycle_count += 1
            if errors:
                self.failed_cycles += 1
            self.last_errors = list(errors)[-10:]

    def set_interface_info(self, info: Sequence[InterfaceInfo]) -> None:
        with self._lock:
            self.interface_info = {item.name: item for item in info}

    # ---- reads used by the routes ------------------------------------
    @property
    def has_live_data(self) -> bool:
        """True once the monitoring loop has published at least one cycle.

        While the loop is running, the in-memory snapshot is the truth -
        including when it is empty ("there are no connections right now").
        Only in API-only mode, where nothing is collected in this process, do
        the reads fall back to what earlier runs stored in SQLite.
        """
        return self.cycle_count > 0

    def current_measurements(self) -> List[InterfaceMeasurement]:
        with self._lock:
            if self.has_live_data:
                return list(self.latest_measurements)
        return self.measurements.latest_per_interface()

    def current_connections(self) -> List[NetworkConnection]:
        with self._lock:
            if self.has_live_data:
                return list(self.latest_connections)
        return self.connections.latest_snapshot()

    def current_processes(self) -> List[ProcessInfo]:
        with self._lock:
            if self.has_live_data:
                return list(self.latest_processes)
        return self.processes.all()

    def traffic_snapshot(self) -> TrafficSnapshot:
        """Aggregate the newest rate of every interface."""
        measurements = self.current_measurements()
        interfaces: Dict[str, Dict[str, float]] = {}
        download = 0.0
        upload = 0.0
        timestamp = self.last_cycle_at or utc_now()
        for measurement in measurements:
            up = float(measurement.upload_rate or 0.0)
            down = float(measurement.download_rate or 0.0)
            interfaces[measurement.interface_name] = {
                "upload_bytes_per_second": up,
                "download_bytes_per_second": down,
                "bytes_sent": float(measurement.bytes_sent),
                "bytes_received": float(measurement.bytes_received),
                "packets_sent": float(measurement.packets_sent),
                "packets_received": float(measurement.packets_received),
            }
            upload += up
            download += down
            if measurement.timestamp > timestamp:
                timestamp = measurement.timestamp
        return TrafficSnapshot(
            timestamp=timestamp,
            download_bytes_per_second=download,
            upload_bytes_per_second=upload,
            interfaces=interfaces,
        )

    def traffic_history(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        interface: Optional[str] = None,
        limit: int = 500,
    ) -> List[TrafficHistoryPoint]:
        return self.measurements.history(start=start, end=end, interface=interface, limit=limit)

    def event_counts(self, window_hours: int = EVENTS_WINDOW_HOURS) -> Dict[str, int]:
        since = utc_now() - timedelta(hours=window_hours)
        counts = self.events.severity_counts(since)
        return {
            "info": counts.get("info", 0),
            "warning": counts.get("warning", 0),
            "critical": counts.get("critical", 0),
            "total": sum(counts.values()),
            "window_hours": window_hours,
        }

    def uptime_seconds(self) -> float:
        return max(0.0, (utc_now() - self.started_at).total_seconds())

    def collection_success_ratio(self) -> Optional[float]:
        """Fraction of cycles without collector/storage errors (``None`` = no data).

        This is the primary SLI of the monitor itself: see docs/SLO.md.
        """
        if self.cycle_count == 0:
            return None
        return (self.cycle_count - self.failed_cycles) / self.cycle_count

    def readiness(self) -> tuple[bool, Dict[str, bool]]:
        """Readiness probe used by ``/api/ready``.

        Deliberately stricter than liveness: an API that answers while the
        database is unwritable or the loop has stalled is *not* ready to serve
        trustworthy data. Checks are returned individually so a failure can be
        diagnosed from one response.
        """
        checks: Dict[str, bool] = {}
        checks["database"] = self.database.is_writable()

        if self.has_live_data:
            tolerance = max(5.0, self.config.monitor.collection_interval * 3)
            age = (utc_now() - self.last_cycle_at).total_seconds() if self.last_cycle_at else None
            checks["collection_loop"] = age is not None and age <= tolerance
            # Serving a snapshot assembled from failed collectors is not being
            # ready to serve trustworthy data, so the last cycle must be clean.
            checks["collection_errors"] = not self.last_errors
        else:
            # API-only mode: nothing to collect in this process.
            checks["collection_loop"] = True
            checks["collection_errors"] = True

        if self.notifications is not None and self.config.notifications.enabled:
            checks["notifications"] = bool(self.notifications.status().get("worker_running"))
        else:
            checks["notifications"] = True

        return all(checks.values()), checks

    def status_payload(self) -> Dict[str, Any]:
        """Body of ``GET /api/status``."""
        states = self.health.states()
        if any(state == "failed" for state in states.values()) or self.last_errors:
            # A degraded feature must be visible in the headline status, not
            # only in the per-collector detail below.
            overall = "degraded"
        elif not states and self.cycle_count == 0:
            overall = "starting"
        else:
            overall = "running"

        return {
            "status": overall,
            "version": __version__,
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "started_at": self.started_at.isoformat(),
            "collection_interval_seconds": self.config.monitor.collection_interval,
            "cycles": self.cycle_count,
            "failed_cycles": self.failed_cycles,
            "collection_success_ratio": (
                round(self.collection_success_ratio(), 4)
                if self.collection_success_ratio() is not None
                else None
            ),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_cycle_duration_ms": round(self.last_cycle_duration_ms, 2)
            if self.last_cycle_duration_ms is not None
            else None,
            "max_cycle_duration_ms": round(self.max_cycle_duration_ms, 2)
            if self.max_cycle_duration_ms is not None
            else None,
            "ready": self.readiness()[0],
            "collectors": {name: status.to_dict() for name, status in self.health.statuses.items()},
            "collector_states": states,
            "detection": {
                "rules": self.detection.rule_summary(),
                "baseline_connections": round(self.detection.baseline, 2),
                "known_processes": len(self.detection.seen_pids),
            },
            "notifications": self.notifications.status()
            if self.notifications
            else {"enabled": False},
            "storage": {
                "database": str(self.database.path),
                **self.database.table_counts(),
            },
            "events_24h": self.event_counts(),
            "last_errors": list(self.last_errors),
        }


def build_state(config: Config, *, with_notifications: bool = True) -> MonitorState:
    """Create a :class:`MonitorState` from a configuration object.

    The database is opened first and closed again if any later step fails, so a
    failed startup never holds the file open - that is what makes the "move the
    corrupt file aside" recovery in docs/RUNBOOK.md work on Windows.
    """
    database = Database(config.database.path_obj)
    try:
        database.connect()

        health_repository = CollectorHealthRepository(database)
        health = CollectorHealthRegistry()
        # Restore the last known health from disk so /api/status is informative
        # immediately after a restart.
        for name, status in health_repository.load().items():
            health.statuses[name] = status

        notifications: Optional[NotificationManager] = None
        if with_notifications:
            from ..notifications import EmailNotifier

            notifiers = [EmailNotifier(config.notifications.email)]
            notifications = NotificationManager(config.notifications, notifiers=notifiers)

        return MonitorState(
            config=config,
            database=database,
            measurements=InterfaceMeasurementRepository(database),
            connections=ConnectionRepository(database),
            processes=ProcessRepository(database),
            events=EventRepository(database),
            health=health,
            detection=DetectionEngine(config.detection),
            notifications=notifications,
            health_repository=health_repository,
        )
    except Exception:
        database.close()
        raise


def empty_state(config: Optional[Config] = None) -> MonitorState:
    """State with defaults, used by tests and by ``--api-only`` mode."""
    return build_state(config or Config(), with_notifications=False)


def event_to_dict(event: Event) -> Dict[str, Any]:
    return event.to_dict()
