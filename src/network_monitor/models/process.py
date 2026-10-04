"""Process models.

A :class:`ProcessInfo` is a point-in-time observation of an operating system
process resolved from a PID. Missing fields are represented as ``None`` rather
than fabricated values, because "access denied" and "process exited" are
meaningful outcomes the dashboard must be able to show.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from .network import from_iso, to_iso


@dataclass
class ProcessInfo:
    """Information about a process owning one or more network connections."""

    pid: int
    name: Optional[str] = None
    executable: Optional[str] = None
    created_at: Optional[datetime] = None
    status: Optional[str] = None
    last_seen: Optional[datetime] = None
    #: Why the process could not be fully resolved: "no_such_process",
    #: "access_denied", "zombie", "error". ``None`` means fully resolved.
    error: Optional[str] = None
    connection_count: int = 0

    @property
    def resolved(self) -> bool:
        return self.name is not None

    def to_row(self) -> Dict[str, Any]:
        return {
            "pid": int(self.pid),
            "name": self.name or "unknown",
            "executable": self.executable,
            "created_at": to_iso(self.created_at) if self.created_at else None,
            "last_seen": to_iso(self.last_seen) if self.last_seen else None,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ProcessInfo":
        keys = row.keys() if hasattr(row, "keys") else row
        return cls(
            pid=row["pid"],
            name=row["name"],
            executable=row["executable"],
            created_at=from_iso(row["created_at"]) if row["created_at"] else None,
            last_seen=from_iso(row["last_seen"]) if row["last_seen"] else None,
            # ``connection_count`` is a derived column: it is only present in
            # the joined query used by ProcessRepository.all().
            connection_count=(row["connection_count"] or 0) if "connection_count" in keys else 0,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["created_at"] = to_iso(self.created_at) if self.created_at else None
        data["last_seen"] = to_iso(self.last_seen) if self.last_seen else None
        data["resolved"] = self.resolved
        return data
