"""Domain models for network measurements.

The models are deliberately plain dataclasses with ``to_row``/``from_row``
helpers: they travel between collectors, the SQLite repositories, the detection
engine and the REST API without any framework-specific coupling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


def utc_now() -> datetime:
    """Timezone-aware "now"; all timestamps in the system are UTC."""
    return datetime.now(timezone.utc)


def to_iso(timestamp: datetime) -> str:
    """Serialise a timestamp in a sortable, timezone-explicit form."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def from_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp produced by :func:`to_iso`."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class InterfaceInfo:
    """Static description of a network interface."""

    name: str
    is_up: bool = False
    speed_mbps: int = 0
    mtu: int = 0
    addresses: list[str] = field(default_factory=list)
    is_loopback: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InterfaceMeasurement:
    """One sample of the cumulative counters of one interface.

    ``upload_rate`` / ``download_rate`` are bytes per second, derived from the
    delta between two consecutive samples. They are ``None`` until a second
    sample exists (the first tick establishes the baseline).
    """

    timestamp: datetime
    interface_name: str
    bytes_sent: int
    bytes_received: int
    packets_sent: int = 0
    packets_received: int = 0
    errors_in: int = 0
    errors_out: int = 0
    drops_in: int = 0
    drops_out: int = 0
    upload_rate: Optional[float] = None
    download_rate: Optional[float] = None
    id: Optional[int] = None

    # ---- conversions -------------------------------------------------
    def to_row(self) -> Dict[str, Any]:
        return {
            "timestamp": to_iso(self.timestamp),
            "interface_name": self.interface_name,
            "bytes_sent": int(self.bytes_sent),
            "bytes_received": int(self.bytes_received),
            "packets_sent": int(self.packets_sent),
            "packets_received": int(self.packets_received),
            "errors_in": int(self.errors_in),
            "errors_out": int(self.errors_out),
            "drops_in": int(self.drops_in),
            "drops_out": int(self.drops_out),
            "upload_rate": self.upload_rate,
            "download_rate": self.download_rate,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "InterfaceMeasurement":
        return cls(
            id=row["id"],
            timestamp=from_iso(row["timestamp"]),
            interface_name=row["interface_name"],
            bytes_sent=row["bytes_sent"],
            bytes_received=row["bytes_received"],
            packets_sent=row["packets_sent"],
            packets_received=row["packets_received"],
            errors_in=row["errors_in"],
            errors_out=row["errors_out"],
            drops_in=row["drops_in"],
            drops_out=row["drops_out"],
            upload_rate=row["upload_rate"],
            download_rate=row["download_rate"],
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = to_iso(self.timestamp)
        return data


@dataclass
class NetworkConnection:
    """A TCP endpoint observed on this machine, with its owning PID.

    ``process_name`` is filled in by the process resolver and is ``None`` when
    the PID has already exited or cannot be inspected.
    """

    timestamp: datetime
    protocol: str = "TCP"
    local_address: str = ""
    local_port: int = 0
    remote_address: str = ""
    remote_port: int = 0
    state: str = "UNKNOWN"
    pid: Optional[int] = None
    process_name: Optional[str] = None
    id: Optional[int] = None

    @property
    def remote_endpoint(self) -> str:
        if not self.remote_address:
            return "-"
        return f"{self.remote_address}:{self.remote_port}"

    @property
    def local_endpoint(self) -> str:
        if not self.local_address:
            return "-"
        return f"{self.local_address}:{self.local_port}"

    def dedupe_key(self) -> tuple[Any, ...]:
        """Identity of a connection within one collection cycle."""
        return (
            self.protocol,
            self.local_address,
            self.local_port,
            self.remote_address,
            self.remote_port,
            self.state,
            self.pid,
        )

    def to_row(self) -> Dict[str, Any]:
        return {
            "timestamp": to_iso(self.timestamp),
            "protocol": self.protocol,
            "local_address": self.local_address,
            "local_port": int(self.local_port),
            "remote_address": self.remote_address,
            "remote_port": int(self.remote_port),
            "state": self.state,
            "pid": self.pid,
            "process_name": self.process_name,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "NetworkConnection":
        return cls(
            id=row["id"],
            timestamp=from_iso(row["timestamp"]),
            protocol=row["protocol"],
            local_address=row["local_address"],
            local_port=row["local_port"],
            remote_address=row["remote_address"],
            remote_port=row["remote_port"],
            state=row["state"],
            pid=row["pid"],
            process_name=row["process_name"],
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = to_iso(self.timestamp)
        data["local_endpoint"] = self.local_endpoint
        data["remote_endpoint"] = self.remote_endpoint
        return data


@dataclass
class TrafficSnapshot:
    """Aggregated current throughput, used by /api/traffic and the dashboard."""

    timestamp: datetime
    download_bytes_per_second: float = 0.0
    upload_bytes_per_second: float = 0.0
    interfaces: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": to_iso(self.timestamp),
            "download_bytes_per_second": self.download_bytes_per_second,
            "upload_bytes_per_second": self.upload_bytes_per_second,
            "interfaces": self.interfaces,
        }


@dataclass
class TrafficHistoryPoint:
    """A single point of the historical traffic series."""

    timestamp: datetime
    interface_name: str
    upload_rate: float
    download_rate: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": to_iso(self.timestamp),
            "interface_name": self.interface_name,
            "upload_bytes_per_second": self.upload_rate,
            "download_bytes_per_second": self.download_rate,
        }
