"""Repository layer.

Every SQL statement lives here; collectors, the detection engine and the API
never touch ``sqlite3`` directly. That keeps the storage schema an
implementation detail and makes each repository independently testable with an
in-memory database, which is exactly what ``tests/test_storage.py`` does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models.events import Event, EventStatus, EventType
from ..models.health import CollectorStatus
from ..models.network import (
    InterfaceMeasurement,
    NetworkConnection,
    TrafficHistoryPoint,
    from_iso,
    to_iso,
    utc_now,
)
from ..models.process import ProcessInfo
from .database import Database
from .schema import TIME_SERIES_TABLES

logger = logging.getLogger(__name__)


class InterfaceMeasurementRepository:
    """Reads and writes interface counter samples."""

    INSERT_SQL = """
        INSERT INTO interface_measurements (
            timestamp, interface_name, bytes_sent, bytes_received, packets_sent,
            packets_received, errors_in, errors_out, drops_in, drops_out,
            upload_rate, download_rate
        ) VALUES (:timestamp, :interface_name, :bytes_sent, :bytes_received,
                  :packets_sent, :packets_received, :errors_in, :errors_out,
                  :drops_in, :drops_out, :upload_rate, :download_rate)
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def add(self, measurement: InterfaceMeasurement) -> int:
        cursor = self._db.execute(self.INSERT_SQL, measurement.to_row())
        lastrowid = int(cursor.lastrowid or 0)
        cursor.close()
        return lastrowid

    def add_many(self, measurements: Iterable[InterfaceMeasurement]) -> int:
        rows = [m.to_row() for m in measurements]
        return self._db.insert_many(self.INSERT_SQL, rows)

    def latest_per_interface(self) -> List[InterfaceMeasurement]:
        """Most recent sample of every interface that has ever reported."""
        rows = self._db.query(
            """
            SELECT m.* FROM interface_measurements m
            JOIN (
                SELECT interface_name, MAX(timestamp) AS ts
                FROM interface_measurements GROUP BY interface_name
            ) latest
              ON latest.interface_name = m.interface_name AND latest.ts = m.timestamp
            ORDER BY m.interface_name
            """
        )
        return [InterfaceMeasurement.from_row(row) for row in rows]

    def last_row(self) -> Optional[InterfaceMeasurement]:
        row = self._db.query_one(
            "SELECT * FROM interface_measurements ORDER BY timestamp DESC LIMIT 1"
        )
        return InterfaceMeasurement.from_row(row) if row else None

    def history(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        interface: Optional[str] = None,
        limit: int = 500,
        since_id: int = 0,
    ) -> List[TrafficHistoryPoint]:
        """Return traffic history ordered oldest-first for charting."""
        conditions: List[str] = []
        parameters: List[Any] = []
        if start is not None:
            conditions.append("timestamp >= ?")
            parameters.append(to_iso(start))
        if end is not None:
            conditions.append("timestamp <= ?")
            parameters.append(to_iso(end))
        if interface:
            conditions.append("interface_name = ?")
            parameters.append(interface)
        if since_id:
            conditions.append("id > ?")
            parameters.append(since_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(max(1, min(limit, 10_000)))
        rows = self._db.query(
            f"""
            SELECT timestamp, interface_name, upload_rate, download_rate
            FROM interface_measurements
            {where}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            parameters,
        )
        # Reverse so consumers plot left-to-right in time order.
        return [
            TrafficHistoryPoint(
                timestamp=from_iso(row["timestamp"]),
                interface_name=row["interface_name"],
                upload_rate=float(row["upload_rate"] or 0.0),
                download_rate=float(row["download_rate"] or 0.0),
            )
            for row in reversed(rows)
        ]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM interface_measurements") or 0)

    def latest_timestamp(self) -> Optional[datetime]:
        value = self._db.scalar("SELECT MAX(timestamp) FROM interface_measurements")
        return from_iso(value) if value else None


class ConnectionRepository:
    """Stores TCP snapshots.

    A snapshot replaces the previous collection cycle's rows for that timestamp;
    history is kept for the retention window so the dashboard can show "what
    happened" rather than only "what is happening".
    """

    INSERT_SQL = """
        INSERT INTO connections (
            timestamp, protocol, local_address, local_port, remote_address,
            remote_port, state, pid, process_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def __init__(self, database: Database, keep_snapshots: int = 2) -> None:
        self._db = database
        self._keep_snapshots = max(1, keep_snapshots)

    def add_snapshot(self, connections: Sequence[NetworkConnection]) -> int:
        """Persist one cycle of connections and drop very old snapshots."""
        rows = [
            (
                to_iso(c.timestamp),
                c.protocol,
                c.local_address,
                int(c.local_port),
                c.remote_address,
                int(c.remote_port),
                c.state,
                c.pid,
                c.process_name,
            )
            for c in connections
        ]
        written = self._db.insert_many(self.INSERT_SQL, rows)
        return written

    def latest_snapshot(self, limit: int = 1_000) -> List[NetworkConnection]:
        """Connections of the most recent collection cycle."""
        rows = self._db.query(
            """
            SELECT * FROM connections
            WHERE timestamp = (SELECT MAX(timestamp) FROM connections)
            ORDER BY remote_address, remote_port
            LIMIT ?
            """,
            (max(1, limit),),
        )
        return [NetworkConnection.from_row(row) for row in rows]

    def latest_snapshot_timestamp(self) -> Optional[datetime]:
        value = self._db.scalar("SELECT MAX(timestamp) FROM connections")
        return from_iso(value) if value else None

    def search(
        self,
        process: Optional[str] = None,
        pid: Optional[int] = None,
        state: Optional[str] = None,
        remote: Optional[str] = None,
        limit: int = 1_000,
    ) -> List[NetworkConnection]:
        """Filtered view of the latest snapshot (Should-Have: filtering)."""
        conditions = ["timestamp = (SELECT MAX(timestamp) FROM connections)"]
        parameters: List[Any] = []
        if process:
            conditions.append("LOWER(process_name) LIKE ?")
            parameters.append(f"%{process.lower()}%")
        if pid is not None:
            conditions.append("pid = ?")
            parameters.append(pid)
        if state:
            conditions.append("UPPER(state) = ?")
            parameters.append(state.upper())
        if remote:
            conditions.append("(remote_address LIKE ? OR CAST(remote_port AS TEXT) LIKE ?)")
            parameters.extend([f"%{remote}%", f"%{remote}%"])

        parameters.append(max(1, limit))
        rows = self._db.query(
            f"SELECT * FROM connections WHERE {' AND '.join(conditions)} "
            "ORDER BY remote_address, remote_port LIMIT ?",
            parameters,
        )
        return [NetworkConnection.from_row(row) for row in rows]

    def latest_count(self) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM connections "
                "WHERE timestamp = (SELECT MAX(timestamp) FROM connections)"
            )
            or 0
        )

    def count_since(self, since: datetime) -> int:
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM connections WHERE timestamp >= ?", (to_iso(since),)
            )
            or 0
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM connections") or 0)


class ProcessRepository:
    """Upserts the process directory (PID -> name/exe/created)."""

    UPSERT_SQL = """
        INSERT INTO processes (pid, name, executable, created_at, last_seen)
        VALUES (:pid, :name, :executable, :created_at, :last_seen)
        ON CONFLICT(pid) DO UPDATE SET
            name       = excluded.name,
            executable = COALESCE(excluded.executable, processes.executable),
            created_at = COALESCE(excluded.created_at, processes.created_at),
            last_seen  = excluded.last_seen
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert_many(self, processes: Iterable[ProcessInfo]) -> int:
        rows = [p.to_row() for p in processes if p.resolved]
        return self._db.insert_many(self.UPSERT_SQL, rows)

    def all(self, limit: int = 1_000) -> List[ProcessInfo]:
        rows = self._db.query(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM connections c
                     WHERE c.pid = p.pid
                       AND c.timestamp = (SELECT MAX(timestamp) FROM connections)
                   ) AS connection_count
            FROM processes p
            ORDER BY connection_count DESC, p.name ASC
            LIMIT ?
            """,
            (max(1, limit),),
        )
        return [ProcessInfo.from_row(row) for row in rows]

    def get(self, pid: int) -> Optional[ProcessInfo]:
        row = self._db.query_one("SELECT * FROM processes WHERE pid = ?", (pid,))
        return ProcessInfo.from_row(row) if row else None

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM processes") or 0)


class EventRepository:
    """Stores and queries detected events."""

    INSERT_SQL = """
        INSERT INTO events (
            timestamp, event_type, severity, title, description, source, pid,
            process_name, interface_name, evidence, status
        ) VALUES (:timestamp, :event_type, :severity, :title, :description,
                  :source, :pid, :process_name, :interface_name, :evidence, :status)
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def add(self, event: Event) -> int:
        cursor = self._db.execute(self.INSERT_SQL, event.to_row())
        lastrowid = int(cursor.lastrowid or 0)
        cursor.close()
        event.id = lastrowid
        return lastrowid

    def add_many(self, events: Iterable[Event]) -> List[int]:
        """Insert several events and return their ids (in the given order)."""
        events = list(events)
        ids: List[int] = []
        if not events:
            return ids
        with self._db.transaction():
            for event in events:
                ids.append(self.add(event))
        return ids

    def get(self, event_id: int) -> Optional[Event]:
        row = self._db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
        return Event.from_row(row) if row else None

    def list(
        self,
        limit: int = 100,
        offset: int = 0,
        severity: Optional[str] = None,
        event_type: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[Event]:
        conditions: List[str] = []
        parameters: List[Any] = []
        if severity:
            conditions.append("severity = ?")
            parameters.append(severity.lower())
        if event_type:
            conditions.append("event_type = ?")
            parameters.append(event_type.upper())
        if status:
            conditions.append("status = ?")
            parameters.append(status.lower())
        if since:
            conditions.append("timestamp >= ?")
            parameters.append(to_iso(since))
        if until:
            conditions.append("timestamp <= ?")
            parameters.append(to_iso(until))

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([max(1, min(limit, 5_000)), max(0, offset)])
        rows = self._db.query(
            f"SELECT * FROM events {where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            parameters,
        )
        return [Event.from_row(row) for row in rows]

    def recent_by_type(self, event_type: EventType | str, since: datetime) -> List[Event]:
        value = event_type.value if isinstance(event_type, EventType) else str(event_type)
        rows = self._db.query(
            "SELECT * FROM events WHERE event_type = ? AND timestamp >= ? "
            "ORDER BY timestamp DESC",
            (value, to_iso(since)),
        )
        return [Event.from_row(row) for row in rows]

    def count_since(self, since: datetime, severity: Optional[str] = None) -> int:
        if severity:
            return int(
                self._db.scalar(
                    "SELECT COUNT(*) FROM events WHERE timestamp >= ? AND severity = ?",
                    (to_iso(since), severity.lower()),
                )
                or 0
            )
        return int(
            self._db.scalar(
                "SELECT COUNT(*) FROM events WHERE timestamp >= ?", (to_iso(since),)
            )
            or 0
        )

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM events") or 0)

    def update_status(self, event_id: int, status: EventStatus | str) -> bool:
        value = status.value if isinstance(status, EventStatus) else str(status).lower()
        cursor = self._db.execute(
            "UPDATE events SET status = ? WHERE id = ?", (value, event_id)
        )
        changed = cursor.rowcount > 0
        cursor.close()
        return changed

    def severity_counts(self, since: datetime) -> Dict[str, int]:
        rows = self._db.query(
            "SELECT severity, COUNT(*) AS total FROM events WHERE timestamp >= ? "
            "GROUP BY severity",
            (to_iso(since),),
        )
        return {row["severity"]: int(row["total"]) for row in rows}

    def latest(self, limit: int = 10) -> List[Event]:
        return self.list(limit=limit)


class CollectorHealthRepository:
    """Persists collector health so restarts do not erase the last known state."""

    UPSERT_SQL = """
        INSERT INTO collector_health (
            name, state, last_success, last_attempt, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            state        = excluded.state,
            last_success = excluded.last_success,
            last_attempt = excluded.last_attempt,
            last_error   = excluded.last_error,
            updated_at   = excluded.updated_at
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert_many(self, statuses: Iterable[CollectorStatus]) -> int:
        now = to_iso(utc_now())
        rows = [
            (
                status.name,
                status.state,
                to_iso(status.last_success) if status.last_success else None,
                to_iso(status.last_attempt) if status.last_attempt else None,
                status.last_error,
                now,
            )
            for status in statuses
        ]
        return self._db.insert_many(self.UPSERT_SQL, rows)

    def load(self) -> Dict[str, CollectorStatus]:
        rows = self._db.query("SELECT * FROM collector_health ORDER BY name")
        statuses: Dict[str, CollectorStatus] = {}
        for row in rows:
            statuses[row["name"]] = CollectorStatus(
                name=row["name"],
                state=row["state"],
                last_success=from_iso(row["last_success"]) if row["last_success"] else None,
                last_attempt=from_iso(row["last_attempt"]) if row["last_attempt"] else None,
                last_error=row["last_error"],
            )
        return statuses


__all__ = [
    "CollectorHealthRepository",
    "ConnectionRepository",
    "EventRepository",
    "InterfaceMeasurementRepository",
    "ProcessRepository",
    "TIME_SERIES_TABLES",
    "utc_now",
    "timedelta",
]
