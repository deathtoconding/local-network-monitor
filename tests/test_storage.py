"""Milestone 2 tests: SQLite persistence.

The headline acceptance criterion is that restarting the application does not
delete previous measurements, so several tests reopen the same database file.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from network_monitor.models.events import Event, EventSeverity, EventType
from network_monitor.models.health import CollectorHealthRegistry
from network_monitor.models.network import utc_now
from network_monitor.models.process import ProcessInfo
from network_monitor.storage import (
    SCHEMA_VERSION,
    CollectorHealthRepository,
    ConnectionRepository,
    Database,
    EventRepository,
    InterfaceMeasurementRepository,
    ProcessRepository,
)

from .conftest import make_connection, make_history, make_measurement


class TestSchema:
    def test_schema_is_created_automatically(self, database):
        tables = {
            row["name"]
            for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "interface_measurements",
            "connections",
            "processes",
            "events",
            "collector_health",
        } <= tables

    def test_schema_version_is_recorded(self, database):
        assert database.schema_version() == SCHEMA_VERSION

    def test_connect_is_idempotent(self, tmp_path):
        db = Database(tmp_path / "nested" / "deep" / "monitor.db")
        db.connect()
        db.connect()  # second call must reuse the connection
        assert db.path.exists()
        assert db.schema_version() == SCHEMA_VERSION
        db.close()

    def test_in_memory_database_works(self, memory_database):
        assert memory_database.table_counts()["events"] == 0


class TestInterfaceMeasurementRepository:
    def test_add_and_read_back(self, database):
        repository = InterfaceMeasurementRepository(database)
        measurement = make_measurement(interface="Ethernet", download_rate=18_400_000.0)
        measurement_id = repository.add(measurement)
        assert measurement_id > 0

        stored = repository.latest_per_interface()
        assert len(stored) == 1
        assert stored[0].interface_name == "Ethernet"
        assert stored[0].download_rate == pytest.approx(18_400_000.0)
        # Timestamps are stored with millisecond precision, so compare with a
        # tolerance rather than for exact equality.
        assert abs((stored[0].timestamp - measurement.timestamp).total_seconds()) < 0.001

    def test_latest_per_interface_returns_newest_row_only(self, database):
        repository = InterfaceMeasurementRepository(database)
        now = utc_now()
        make_history(database, "Ethernet", [1000.0, 2000.0, 3000.0], start=now - timedelta(seconds=3))
        latest = repository.latest_per_interface()
        assert len(latest) == 1
        assert latest[0].download_rate == pytest.approx(3000.0)

    def test_history_is_returned_in_time_order(self, database):
        now = utc_now()
        make_history(database, "Ethernet", [1.0, 2.0, 3.0], start=now - timedelta(seconds=3))
        points = InterfaceMeasurementRepository(database).history(limit=10)
        assert [point.download_rate for point in points] == [1.0, 2.0, 3.0]

    def test_history_filters_by_interface_and_window(self, database):
        repository = InterfaceMeasurementRepository(database)
        now = utc_now()
        make_history(database, "Ethernet", [1.0, 2.0], start=now - timedelta(minutes=10))
        make_history(database, "Wi-Fi", [3.0, 4.0], start=now - timedelta(minutes=10))

        wifi = repository.history(interface="Wi-Fi", limit=10)
        assert {point.interface_name for point in wifi} == {"Wi-Fi"}

        recent = repository.history(start=now - timedelta(minutes=5), limit=10)
        assert recent == []

    def test_data_survives_a_restart(self, tmp_path):
        path = tmp_path / "persist.db"

        first = Database(path)
        first.connect()
        InterfaceMeasurementRepository(first).add(make_measurement(interface="Ethernet"))
        first.close()

        second = Database(path)
        second.connect()
        stored = InterfaceMeasurementRepository(second).latest_per_interface()
        second.close()

        assert len(stored) == 1
        assert stored[0].interface_name == "Ethernet"


class TestConnectionRepository:
    def test_add_and_read_latest_snapshot(self, database):
        repository = ConnectionRepository(database)
        now = utc_now()
        repository.add_snapshot(
            [
                make_connection(pid=1, process_name="chrome.exe", timestamp=now),
                make_connection(pid=2, process_name="python.exe", timestamp=now),
            ]
        )
        stored = repository.latest_snapshot()
        assert len(stored) == 2
        assert {connection.process_name for connection in stored} == {"chrome.exe", "python.exe"}

    def test_latest_snapshot_only_returns_newest_cycle(self, database):
        repository = ConnectionRepository(database)
        now = utc_now()
        repository.add_snapshot([make_connection(pid=1, timestamp=now)])
        repository.add_snapshot([make_connection(pid=2, timestamp=now + timedelta(seconds=1))])
        assert [connection.pid for connection in repository.latest_snapshot()] == [2]

    def test_search_filters(self, database):
        repository = ConnectionRepository(database)
        now = utc_now()
        repository.add_snapshot(
            [
                make_connection(pid=10, process_name="chrome.exe", state_name="ESTABLISHED", timestamp=now),
                make_connection(pid=11, process_name="svchost.exe", state_name="LISTEN", timestamp=now),
            ]
        )
        assert len(repository.search(process="chrome")) == 1
        assert len(repository.search(state="listen")) == 1
        assert len(repository.search(pid=11)) == 1
        assert len(repository.search(remote="443")) == 2
        assert repository.latest_count() == 2


class TestProcessRepository:
    def test_upsert_inserts_then_updates(self, database):
        repository = ProcessRepository(database)
        now = utc_now()
        repository.upsert_many([ProcessInfo(pid=100, name="chrome.exe", last_seen=now)])
        repository.upsert_many(
            [ProcessInfo(pid=100, name="chrome.exe", executable="C:/chrome.exe", last_seen=now)]
        )
        stored = repository.get(100)
        assert stored.name == "chrome.exe"
        assert stored.executable == "C:/chrome.exe"
        assert repository.count() == 1

    def test_unresolved_processes_are_not_stored(self, database):
        repository = ProcessRepository(database)
        repository.upsert_many([ProcessInfo(pid=101, name=None, error="no_such_process")])
        assert repository.count() == 0

    def test_all_orders_by_connection_count(self, database):
        now = utc_now()
        ProcessRepository(database).upsert_many(
            [
                ProcessInfo(pid=1, name="a.exe", last_seen=now),
                ProcessInfo(pid=2, name="b.exe", last_seen=now),
            ]
        )
        ConnectionRepository(database).add_snapshot(
            [
                make_connection(pid=2, timestamp=now),
                make_connection(pid=2, timestamp=now, remote_port=8443),
                make_connection(pid=1, timestamp=now),
            ]
        )
        entries = ProcessRepository(database).all()
        assert entries[0].pid == 2
        assert entries[0].connection_count == 2


class TestEventRepository:
    def test_add_list_and_get(self, database):
        repository = EventRepository(database)
        event = Event(
            timestamp=utc_now(),
            event_type=EventType.HIGH_DOWNLOAD,
            severity=EventSeverity.WARNING,
            title="High network usage",
            description="Download traffic exceeded configured threshold.",
            evidence={"interface": "Ethernet", "download_rate": 18_400_000, "threshold": 10_000_000},
        )
        event_id = repository.add(event)
        assert event.id == event_id

        stored = repository.get(event_id)
        assert stored.event_type is EventType.HIGH_DOWNLOAD
        assert stored.evidence["interface"] == "Ethernet"

        listed = repository.list()
        assert len(listed) == 1

    def test_add_many_returns_ids(self, database):
        repository = EventRepository(database)
        events = [
            Event(
                timestamp=utc_now(),
                event_type=EventType.NEW_NETWORK_PROCESS,
                severity=EventSeverity.INFO,
                title=f"process {index}",
                description="observed",
            )
            for index in range(3)
        ]
        ids = repository.add_many(events)
        assert len(ids) == 3
        assert all(event.id is not None for event in events)

    def test_filters_and_severity_counts(self, database):
        repository = EventRepository(database)
        now = utc_now()
        repository.add_many(
            [
                Event(now, EventType.HIGH_DOWNLOAD, EventSeverity.WARNING, "a", "d"),
                Event(now, EventType.NEW_NETWORK_PROCESS, EventSeverity.INFO, "b", "d"),
                Event(now, EventType.COLLECTOR_FAILURE, EventSeverity.CRITICAL, "c", "d"),
            ]
        )
        assert len(repository.list(severity="warning")) == 1
        assert len(repository.list(event_type="NEW_NETWORK_PROCESS")) == 1
        assert repository.severity_counts(now - timedelta(hours=1)) == {
            "info": 1,
            "warning": 1,
            "critical": 1,
        }
        assert repository.count_since(now - timedelta(hours=1)) == 3

    def test_status_update(self, database):
        repository = EventRepository(database)
        event_id = repository.add(
            Event(utc_now(), EventType.HIGH_UPLOAD, EventSeverity.WARNING, "t", "d")
        )
        assert repository.update_status(event_id, "acknowledged")
        assert repository.get(event_id).status.value == "acknowledged"
        assert repository.update_status(999, "resolved") is False

    def test_corrupt_evidence_does_not_break_reading(self, database):
        repository = EventRepository(database)
        event_id = repository.add(
            Event(utc_now(), EventType.HIGH_UPLOAD, EventSeverity.WARNING, "t", "d")
        )
        database.execute("UPDATE events SET evidence = ? WHERE id = ?", ("not json", event_id))
        stored = repository.get(event_id)
        assert stored.evidence == {"raw": "not json"}


class TestCollectorHealthRepository:
    def test_health_round_trip(self, database):
        repository = CollectorHealthRepository(database)
        registry = CollectorHealthRegistry()
        now = utc_now()
        registry.record_success("interface", now, 1.5)
        registry.record_failure("connections", now, "access denied", 2.5)

        repository.upsert_many(registry.statuses.values())
        loaded = repository.load()

        assert loaded["interface"].state == "healthy"
        assert loaded["connections"].state == "degraded"
        assert loaded["connections"].last_error == "access denied"


class TestRetention:
    def test_prune_removes_old_rows_only(self, database):
        repository = InterfaceMeasurementRepository(database)
        now = utc_now()
        make_history(database, "Ethernet", [1.0], start=now - timedelta(days=30))
        make_history(database, "Ethernet", [2.0], start=now)

        cutoff = (now - timedelta(days=7)).isoformat()
        deleted = database.prune("interface_measurements", "timestamp", cutoff)

        assert deleted == 1
        assert repository.count() == 1
