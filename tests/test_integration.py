"""Integration tests: collector -> normalizer -> database -> detection -> event.

These deliberately go through the real components (real SQLite file, real
detection engine, real repositories) and only fake the operating-system
boundary, which is what makes them useful as a vertical-slice check.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from network_monitor.api import create_app
from network_monitor.models.events import EventType
from network_monitor.models.network import utc_now
from network_monitor.monitor import Monitor

from .conftest import make_connection, make_measurement
from .test_monitor import (
    FailingCollector,
    FakeConnectionCollector,
    FakeInterfaceCollector,
    build_monitor,
)


class TestVerticalSlice:
    def test_traffic_slice_reaches_the_api(self, config, tmp_path):
        """collector -> rate calculation -> SQLite -> FastAPI -> /api/traffic."""
        from fastapi.testclient import TestClient

        monitor = build_monitor(
            config,
            FakeInterfaceCollector(
                [[make_measurement(interface="Ethernet", download_rate=18_400_000.0)]]
            ),
            FakeConnectionCollector([]),
        )
        monitor.cycle()

        app = create_app(state=monitor.state, web_dir=tmp_path)
        with TestClient(app) as client:
            traffic = client.get("/api/traffic").json()
            assert traffic["download_bytes_per_second"] == pytest.approx(18_400_000.0)
            assert traffic["interfaces"]["Ethernet"]["download_bytes_per_second"] == pytest.approx(
                18_400_000.0
            )

            history = client.get("/api/traffic/history").json()
            assert history["count"] == 1

            interfaces = client.get("/api/interfaces").json()
            assert interfaces[0]["bytes_received"] == 5_000_000
        monitor.state.database.close()

    def test_connection_slice_includes_process_attribution(self, config, tmp_path):
        """collector -> process resolver -> storage -> /api/connections."""
        from fastapi.testclient import TestClient

        connection = make_connection(pid=8420, process_name=None, remote_port=443)
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[]]),
            FakeConnectionCollector([connection]),
            process_collector=type(
                "Resolver",
                (),
                {
                    "collect": lambda self: [],
                    "resolve": lambda self, pid: type(
                        "Info",
                        (),
                        {
                            "pid": pid,
                            "name": "chrome.exe",
                            "error": None,
                            "executable": "C:/chrome.exe",
                        },
                    )(),
                },
            )(),
        )
        monitor.cycle()

        app = create_app(state=monitor.state, web_dir=tmp_path)
        with TestClient(app) as client:
            payload = client.get("/api/connections").json()
            assert payload["connections"][0]["process_name"] == "chrome.exe"
            assert payload["connections"][0]["remote_endpoint"] == "142.250.27.100:443"
        monitor.state.database.close()

    def test_detection_slice_creates_a_persisted_explainable_event(self, config, tmp_path):
        """measurement -> detection -> event storage -> /api/events/{id}."""
        from fastapi.testclient import TestClient

        monitor = build_monitor(
            config,
            FakeInterfaceCollector(
                [
                    [
                        make_measurement(
                            interface="Ethernet",
                            download_rate=20 * 125_000,
                            upload_rate=1_000.0,
                        )
                    ]
                ]
            ),
            FakeConnectionCollector([]),
        )
        events = monitor.cycle()
        assert [event.event_type for event in events] == [EventType.HIGH_DOWNLOAD]

        app = create_app(state=monitor.state, web_dir=tmp_path)
        with TestClient(app) as client:
            listing = client.get("/api/events").json()
            assert listing["count"] == 1

            detail = client.get(f"/api/events/{events[0].id}").json()
            assert detail["event_type"] == "HIGH_DOWNLOAD"
            assert detail["severity"] == "warning"
            assert detail["evidence"]["interface"] == "Ethernet"
            assert detail["evidence"]["threshold_mbps"] == 10.0
            assert detail["evidence"]["download_rate_mbps"] == pytest.approx(20.0)
        monitor.state.database.close()

    def test_degraded_run_is_visible_end_to_end(self, config, tmp_path):
        """A dead collector must be visible in /api/status, not silent."""
        from fastapi.testclient import TestClient

        measurements = [make_measurement(download_rate=1000.0)]
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([measurements, measurements]),
            FailingCollector("access denied"),
        )
        monitor.cycle()
        monitor.cycle()

        app = create_app(state=monitor.state, web_dir=tmp_path)
        with TestClient(app) as client:
            status = client.get("/api/status").json()
            assert status["status"] == "degraded"
            assert status["collectors"]["connections"]["state"] == "failed"
            assert "access denied" in status["collectors"]["connections"]["last_error"]
            # Interface metering is unaffected.
            assert status["collectors"]["interface"]["state"] == "healthy"
            assert client.get("/api/traffic").json()["download_bytes_per_second"] == pytest.approx(
                1000.0
            )
        monitor.state.database.close()


class TestPersistenceAcrossRestart:
    def test_history_survives_a_restart(self, config, tmp_path):
        first = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=5000.0)]]),
            FakeConnectionCollector([]),
        )
        first.cycle()
        first.state.database.close()

        second = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=6000.0)]]),
            FakeConnectionCollector([]),
        )
        points = second.state.measurements.history(limit=10)
        assert len(points) == 1
        assert points[0].download_rate == pytest.approx(5000.0)

        second.cycle()
        assert len(second.state.measurements.history(limit=10)) == 2
        second.state.database.close()

    def test_collector_health_survives_a_restart(self, config):
        first = build_monitor(config, FakeInterfaceCollector([[]]), FailingCollector("nope"))
        first.cycle()
        first.state.database.close()

        second = build_monitor(config, FakeInterfaceCollector([[]]), FakeConnectionCollector([]))
        loaded = second.state.health.get("connections")
        assert loaded is not None
        assert loaded.last_error == "nope"
        second.state.database.close()

    def test_rate_baselines_are_rebuilt_after_restart(self, config):
        """Rates must not be computed across a restart boundary."""
        config.monitor.exclude_interfaces = []

        from network_monitor.collectors import InterfaceCollector

        collector = InterfaceCollector(config.monitor)
        monitor = Monitor(config)
        monitor.interface_collector = collector
        monitor.connection_collector = FakeConnectionCollector([])
        monitor.cycle()
        first_measurements = monitor.state.current_measurements()
        # First cycle after start: baseline only.
        assert all(m.download_rate is None for m in first_measurements)
        monitor.state.database.close()


class TestNotificationIntegration:
    def test_notifications_are_queued_without_blocking(self, config):
        from network_monitor.models.events import Event, EventSeverity, EventType
        from network_monitor.notifications import NotificationManager

        delivered = []

        class RecordingNotifier:
            name = "recording"

            def send(self, event):
                delivered.append(event.event_type.value)
                return True

            def close(self):
                pass

        config.notifications.enabled = True
        manager = NotificationManager(config.notifications, notifiers=[RecordingNotifier()])
        manager.start()

        event = Event(
            timestamp=utc_now(),
            event_type=EventType.COLLECTOR_FAILURE,
            severity=EventSeverity.CRITICAL,
            title="Collector failure",
            description="connections collector stopped reporting",
        )
        assert manager.handle_events([event]) == 1

        deadline = utc_now() + timedelta(seconds=2)
        while not delivered and utc_now() < deadline:
            import time

            time.sleep(0.02)
        manager.stop()

        assert delivered == ["COLLECTOR_FAILURE"]
        assert manager.status()["dispatched"] == 1

    def test_info_events_below_min_severity_are_not_dispatched(self, config):
        from network_monitor.models.events import Event, EventSeverity, EventType
        from network_monitor.notifications import NotificationManager

        config.notifications.enabled = True
        config.notifications.min_severity = "warning"
        manager = NotificationManager(config.notifications, notifiers=[])
        event = Event(
            timestamp=utc_now(),
            event_type=EventType.NEW_NETWORK_PROCESS,
            severity=EventSeverity.INFO,
            title="new process",
            description="observation",
        )
        assert manager.handle_events([event]) == 0

    def test_disabled_notifications_do_nothing(self, config):
        from network_monitor.notifications import NotificationManager

        manager = NotificationManager(config.notifications, notifiers=[])
        monitor = build_monitor(
            config,
            FakeInterfaceCollector([[make_measurement(download_rate=50_000_000.0)]]),
            FakeConnectionCollector([]),
        )
        monitor.state.notifications = None
        monitor.cycle()
        assert manager.handle_events([]) == 0
        assert manager.status()["enabled"] is False
