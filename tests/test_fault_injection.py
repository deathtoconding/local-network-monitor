"""Fault-injection tests.

The monitoring loop is the one component that must never die: if it stops, the
monitor silently becomes a dashboard showing stale data, which is worse than
crashing. These tests deliberately break dependencies one at a time - storage,
the operating system, a detection rule, the notifier - and assert that the loop
keeps cycling and that the failure is *visible* rather than swallowed.

They are the executable form of the resilience requirement in the specification
and of the failure modes listed in docs/RUNBOOK.md.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from network_monitor.models.network import utc_now
from network_monitor.notifications import NotificationManager
from network_monitor.notifications.manager import Notifier

from .conftest import make_connection, make_measurement
from .test_monitor import (
    FailingCollector,
    FakeConnectionCollector,
    FakeInterfaceCollector,
    build_monitor,
)


class TestStorageFailures:
    def test_disk_full_does_not_stop_the_loop(self, config, monkeypatch):
        """A failing write must degrade, be recorded, and keep the loop alive."""
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 3),
            FakeConnectionCollector([make_connection(pid=1)]),
        )

        def disk_full(*_args, **_kwargs):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(monitor.state.measurements, "add_many", disk_full)

        events = monitor.cycle()
        assert monitor.state.cycle_count == 1
        assert any("disk is full" in error for error in monitor.state.last_errors)
        # The failure is reported, not hidden: status turns degraded...
        assert monitor.state.status_payload()["status"] == "degraded"
        # ...and detection still ran on the data it had.
        assert isinstance(events, list)

    def test_recovery_after_a_storage_failure(self, config, monkeypatch):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 3),
            FakeConnectionCollector([]),
        )
        original = monitor.state.measurements.add_many
        calls = {"count": 0}

        def flaky(rows):
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(rows)

        monkeypatch.setattr(monitor.state.measurements, "add_many", flaky)
        monitor.cycle()
        assert monitor.state.last_errors  # failure recorded

        monitor.cycle()
        assert monitor.state.last_errors == []  # a clean cycle clears it
        assert monitor.state.measurements.count() >= 1

    def test_corrupt_database_fails_loudly_and_recovers(self, config):
        """Documented in docs/RUNBOOK.md: a corrupt file must never be silent.

        Starting against a corrupt database stops the process with sqlite's own
        message rather than appearing to run while discarding data. Recovery is
        to move the file aside (the schema is recreated on the next start).
        """
        from network_monitor.api import build_state

        state = build_state(config, with_notifications=False)
        state.database.close()
        database_path = Path(config.database.path)
        database_path.write_bytes(b"not a database at all")

        with pytest.raises(sqlite3.DatabaseError):
            build_state(config, with_notifications=False)

        # Recovery path used by the runbook: move the file aside. WAL companions
        # are removed too, otherwise a stale -wal would be replayed on the next
        # start. build_state() must already have released its handle here, which
        # is why this step also works on Windows.
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{database_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        recovered = build_state(config, with_notifications=False)
        assert recovered.database.table_counts()["events"] == 0
        recovered.database.close()


class TestCollectorFailures:
    def test_collector_exception_is_isolated_per_collector(self, config):
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=1000.0)]] * 2),
            FailingCollector("Get-NetTCPConnection timed out after 10s"),
        )
        for _ in range(2):
            monitor.cycle()

        states = monitor.state.health.states()
        assert states["connections"] == "failed"
        assert states["interface"] == "healthy"
        assert states["system"] == "healthy"
        # Traffic keeps flowing to the dashboard even though connections are dead.
        assert monitor.state.traffic_snapshot().download_bytes_per_second == pytest.approx(1000.0)

    def test_missing_platform_tool_degrades_only_one_feature(self, config):
        """The Windows-without-PowerShell case, expressed as a test."""

        class NoPowerShell:
            name = "connections"

            def collect(self):
                raise FileNotFoundError("powershell executable not found")

        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement()]] * 2),
            NoPowerShell(),
        )
        monitor.cycle()
        assert monitor.state.health.get("connections").state == "degraded"
        assert "not found" in monitor.state.health.get("connections").last_error


class TestDetectorFailures:
    def test_a_rule_that_always_raises_cannot_silence_the_others(self, config):
        class ExplodingRule:
            name = "exploding"
            enabled = True
            event_type = "X"
            severity = "info"

            def evaluate(self, context):
                raise ZeroDivisionError("bug in a user-supplied rule")

        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=50_000_000.0)]]),
            FakeConnectionCollector([]),
        )
        monitor.state.detection.rules.insert(0, ExplodingRule())
        events = monitor.cycle()
        assert [event.event_type.value for event in events] == ["HIGH_DOWNLOAD"]
        assert monitor.state.events.count() == 1


class TestNotificationFailures:
    class BrokenNotifier(Notifier):
        name = "broken"

        def send(self, event):
            raise ConnectionRefusedError("smtp server unreachable")

    def test_raising_notifier_does_not_kill_the_worker(self, config):
        config.notifications.enabled = True
        config.notifications.cooldown_seconds = 0
        manager = NotificationManager(config.notifications, notifiers=[self.BrokenNotifier()])
        manager.start()

        from network_monitor.models.events import Event, EventSeverity, EventType

        event = Event(
            timestamp=utc_now(),
            event_type=EventType.HIGH_UPLOAD,
            severity=EventSeverity.WARNING,
            title="t",
            description="d",
        )
        assert manager.handle_events([event]) == 1

        deadline = utc_now() + timedelta(seconds=2)
        while not manager.history and utc_now() < deadline:
            import time

            time.sleep(0.02)
        manager.stop()

        # The attempt is recorded as a failure, and the worker still exits cleanly.
        assert manager.history and manager.history[0].delivered is False
        assert "unreachable" in manager.history[0].detail

    def test_full_notification_queue_drops_instead_of_blocking(self, config):
        """Back-pressure must never reach the monitoring loop."""
        from network_monitor.models.events import Event, EventSeverity, EventType

        config.notifications.enabled = True
        config.notifications.cooldown_seconds = 0
        manager = NotificationManager(config.notifications, notifiers=[], queue_size=2)

        def event(index: int) -> Event:
            return Event(
                timestamp=utc_now(),
                event_type=EventType.CONNECTION_SPIKE,
                severity=EventSeverity.CRITICAL,
                title=f"spike {index}",
                description="d",
                pid=index,  # distinct subjects so the cooldown does not collapse them
            )

        queued = manager.handle_events([event(index) for index in range(5)])
        assert queued == 2  # the queue size
        assert manager.dropped == 3
        assert manager.status()["queued"] == 2


class TestLoopSurvival:
    def test_many_consecutive_broken_cycles_still_leave_the_loop_running(self, config):
        """Chaos run: every dependency is broken at once for 10 cycles."""
        config.detection.collector_failure_timeout_seconds = 15
        monitor = build_monitor(
            config,
            FailingCollector("interface syscall failed"),
            FailingCollector("TCP table unavailable"),
        )

        def storage_down(*_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monitor.state.measurements.add_many = storage_down
        monitor.state.connections.add_snapshot = storage_down

        for _ in range(10):
            monitor.cycle()

        assert monitor.state.cycle_count == 10
        assert monitor.state.failed_cycles == 10
        payload = monitor.state.status_payload()
        assert payload["status"] == "degraded"
        assert payload["collection_success_ratio"] == 0.0
        assert payload["last_errors"]
        # Readiness fails while the loop is broken, liveness would still pass.
        assert monitor.state.readiness()[0] is False
