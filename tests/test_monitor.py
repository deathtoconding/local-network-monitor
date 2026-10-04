"""Runtime loop tests: the resilience requirements.

The most important assertion in the whole suite lives here: when one collector
blows up, the cycle still completes, the healthy data is still stored, and the
failure is recorded in the collector health registry.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from network_monitor.collectors.base import CollectorError
from network_monitor.models.network import utc_now
from network_monitor.monitor import Monitor

from .conftest import make_connection, make_measurement


class FakeInterfaceCollector:
    name = "interface"

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def collect(self):
        self.calls += 1
        if not self.batches:
            return []
        return self.batches.pop(0)

    def list_interfaces(self):
        return []


class FailingCollector:
    name = "connections"

    def __init__(self, message="Get-NetTCPConnection timed out after 10s"):
        self.message = message
        self.calls = 0

    def collect(self):
        self.calls += 1
        raise CollectorError(self.message)


class FakeConnectionCollector:
    name = "connections"

    def __init__(self, connections):
        self.connections = connections

    def collect(self):
        return list(self.connections)


class FakeProcessCollector:
    name = "processes"

    def __init__(self, processes=()):
        self.processes = list(processes)

    def collect(self):
        return list(self.processes)

    def resolve(self, pid, connection_count=0):
        """Mirrors ProcessResolver.resolve for PIDs missing from the fixture."""
        from network_monitor.models.process import ProcessInfo

        return ProcessInfo(pid=pid, name=f"process-{pid}", connection_count=connection_count)


class FakeSystemCollector:
    name = "system"

    def collect(self):
        from network_monitor.collectors import SystemSnapshot
        from network_monitor.models.network import utc_now

        return SystemSnapshot(
            timestamp=utc_now(),
            hostname="test-host",
            platform="TestOS",
            python_version="3.11",
            cpu_percent=1.0,
            memory_percent=20.0,
            process_count=42,
        )


def build_monitor(config, interface_collector, connection_collector, process_collector=None):
    monitor = Monitor(config)
    monitor.interface_collector = interface_collector
    monitor.connection_collector = connection_collector
    monitor.process_resolver = process_collector or FakeProcessCollector()
    monitor.system_collector = FakeSystemCollector()
    return monitor


class TestCycle:
    def test_cycle_stores_measurements_connections_and_events(self, config):
        measurement = make_measurement(interface="Ethernet", download_rate=50_000_000.0)
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[measurement]]),
            FakeConnectionCollector([make_connection(pid=4242)]),
        )

        events = monitor.cycle()

        assert monitor.state.measurements.count() == 1
        assert monitor.state.connections.latest_count() == 1
        assert [event.event_type.value for event in events] == ["HIGH_DOWNLOAD", "NEW_NETWORK_PROCESS"]
        assert monitor.state.events.count() == 2
        # The event id is assigned during persistence, so notifications can
        # reference a stored event.
        assert all(event.id is not None for event in events)
        assert monitor.state.cycle_count == 1
        assert monitor.state.health.states()["interface"] == "healthy"

    def test_state_is_published_for_the_api(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(upload_rate=1_000.0)]]),
            FakeConnectionCollector([]),
        )
        monitor.cycle()
        snapshot = monitor.state.traffic_snapshot()
        assert snapshot.upload_bytes_per_second == pytest.approx(1_000.0)
        assert monitor.state.last_cycle_at is not None
        assert monitor.state.last_cycle_duration_ms is not None

    def test_no_events_when_traffic_is_calm(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=1024.0, upload_rate=512.0)]]),
            FakeConnectionCollector([]),
        )
        assert monitor.cycle() == []


class TestResilience:
    def test_failing_connection_collector_does_not_stop_the_cycle(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=1000.0)]]),
            FailingCollector(),
        )
        events = monitor.cycle()

        # Interface data still stored...
        assert monitor.state.measurements.count() == 1
        # ...the failure is recorded...
        status = monitor.state.health.get("connections")
        assert status.state == "degraded"
        assert status.last_error.startswith("Get-NetTCPConnection")
        # ...and the cycle kept going.
        assert monitor.state.cycle_count == 1
        assert events == []

    def test_repeated_failures_escalate_and_raise_an_event(self, config):
        config.detection.collector_failure_timeout_seconds = 15
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[], [], []]),
            FailingCollector(),
        )
        # Pretend the connection collector last succeeded well beyond the timeout.
        all_events = []
        for _ in range(3):
            monitor.state.health.ensure("connections").last_success = utc_now() - timedelta(minutes=5)
            all_events.extend(monitor.cycle())

        assert monitor.state.health.get("connections").state == "failed"
        failure_events = [
            event for event in all_events if event.event_type.value == "COLLECTOR_FAILURE"
        ]
        assert failure_events, "a sustained collector failure must raise an event"
        assert failure_events[0].evidence["consecutive_failures"] >= 1
        # Interface collector kept running the whole time.
        assert monitor.state.health.get("interface").state == "healthy"

    def test_one_broken_rule_does_not_break_detection(self, config):
        class ExplodingRule:
            name = "exploding"
            enabled = True
            event_type = "X"
            severity = "info"

            def evaluate(self, context):
                raise RuntimeError("boom")

        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=50_000_000.0)]]),
            FakeConnectionCollector([]),
        )
        monitor.state.detection.rules.insert(0, ExplodingRule())
        events = monitor.cycle()
        assert [event.event_type.value for event in events] == ["HIGH_DOWNLOAD"]

    def test_loop_can_be_started_and_stopped(self, config):
        config.monitor.collection_interval = 0.05
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 50),
            FakeConnectionCollector([]),
        )
        monitor.start()
        assert monitor.running
        for _ in range(50):
            if monitor.state.cycle_count >= 2:
                break
            import time

            time.sleep(0.05)
        monitor.stop()
        assert not monitor.running
        assert monitor.state.cycle_count >= 1


class TestRetention:
    def test_prune_runs_after_the_interval(self, config):
        config.database.retention_days = 1
        config.database.prune_interval_seconds = 60
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[]]),
            FakeConnectionCollector([]),
        )
        old = utc_now() - timedelta(days=30)
        monitor.state.measurements.add(make_measurement(timestamp=old))
        monitor.state.measurements.add(make_measurement())
        assert monitor.state.measurements.count() == 2

        monitor.cycle()

        assert monitor.state.measurements.count() == 1


class TestConnectionSnapshotStorage:
    """A snapshot is history: identical sets are not written again and again."""

    def test_identical_snapshot_is_stored_once(self, config):
        connection = make_connection(pid=4242, process_name="chrome.exe")
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 5),
            FakeConnectionCollector([connection]),
        )
        for _ in range(5):
            monitor.cycle()
        assert monitor.state.connections.count() == 1
        # ...but the live view still reports the connection every cycle.
        assert len(monitor.state.current_connections()) == 1

    def test_changed_snapshot_is_stored_again(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 2),
            FakeConnectionCollector([make_connection(pid=1)]),
        )
        monitor.cycle()
        monitor.connection_collector = FakeConnectionCollector(
            [make_connection(pid=1), make_connection(pid=2)]
        )
        monitor.cycle()
        assert monitor.state.connections.count() == 3  # 1 + 2 rows

    def test_empty_snapshot_is_not_written_and_history_is_kept(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 2),
            FakeConnectionCollector([make_connection(pid=1)]),
        )
        monitor.cycle()
        monitor.connection_collector = FakeConnectionCollector([])
        monitor.cycle()

        # The live view reports zero connections...
        assert monitor.state.current_connections() == []
        # ...while the database keeps the outcome of the earlier cycle as history.
        assert monitor.state.connections.latest_count() == 1


class TestCollectorHealthCadence:
    def test_system_collector_runs_every_cycle(self, config):
        """Regression: a once-only collector looked 'failed' after 15 seconds."""
        config.detection.collector_failure_timeout_seconds = 15
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 3),
            FakeConnectionCollector([]),
        )
        events = []
        for _ in range(3):
            events.extend(monitor.cycle())
        assert monitor.state.health.get("system").state == "healthy"
        assert monitor.state.health.get("system").total_runs == 3
        assert not [event for event in events if event.event_type.value == "COLLECTOR_FAILURE"]

    def test_system_snapshot_is_published_with_connection_states(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]]),
            FakeConnectionCollector([make_connection(pid=1, state_name="ESTABLISHED")]),
        )
        monitor.cycle()
        snapshot = monitor.state.latest_system
        assert snapshot is not None
        assert snapshot.connection_states == {"ESTABLISHED": 1}


class TestMixedRetention:
    def test_connections_use_the_shorter_window(self, config):
        config.database.retention_days = 7
        config.database.connection_retention_hours = 1
        config.database.prune_interval_seconds = 60
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[]]),
            FakeConnectionCollector([]),
        )
        old_connection = make_connection(pid=1, timestamp=utc_now() - timedelta(hours=6))
        old_measurement = make_measurement(timestamp=utc_now() - timedelta(hours=6))
        monitor.state.connections.add_snapshot([old_connection])
        monitor.state.measurements.add(old_measurement)

        monitor.cycle()

        assert monitor.state.connections.latest_count() == 0
        assert monitor.state.measurements.count() == 1  # still inside 7 days
