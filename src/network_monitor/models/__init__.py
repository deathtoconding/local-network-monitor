"""Domain models for Local Network Monitor."""

from .events import Event, EventSeverity, EventStatus, EventType
from .health import (
    DEGRADED,
    FAILED,
    HEALTHY,
    UNKNOWN,
    CollectorHealthRegistry,
    CollectorStatus,
)
from .network import (
    InterfaceInfo,
    InterfaceMeasurement,
    NetworkConnection,
    TrafficHistoryPoint,
    TrafficSnapshot,
    from_iso,
    to_iso,
    utc_now,
)
from .process import ProcessInfo

__all__ = [
    "CollectorHealthRegistry",
    "CollectorStatus",
    "DEGRADED",
    "Event",
    "EventSeverity",
    "EventStatus",
    "EventType",
    "FAILED",
    "HEALTHY",
    "InterfaceInfo",
    "InterfaceMeasurement",
    "NetworkConnection",
    "ProcessInfo",
    "TrafficHistoryPoint",
    "TrafficSnapshot",
    "UNKNOWN",
    "from_iso",
    "to_iso",
    "utc_now",
]
