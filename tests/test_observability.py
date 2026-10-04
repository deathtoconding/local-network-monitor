"""Observability tests: structured logging, metrics exposition and readiness.

These are the SRE-facing contracts of the product. If logging stops emitting
machine-readable lines, or the metrics endpoint produces something a scraper
cannot parse, or readiness reports "ready" while the collection loop is dead,
then nobody can operate the monitor - regardless of how well the collectors
work.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

import pytest

from network_monitor.api.metrics import CONTENT_TYPE, render_metrics
from network_monitor.config import LoggingSection
from network_monitor.logging_setup import JsonFormatter, build_formatter, configure_logging
from network_monitor.models.network import utc_now

from .conftest import make_connection, make_history, make_measurement


class TestJsonLogging:
    def _record(self, **extra) -> logging.LogRecord:
        record = logging.LogRecord(
            name="network_monitor.monitor",
            level=logging.WARNING,
            pathname=__file__,
            lineno=42,
            msg="collector failed: %s",
            args=("access denied",),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_every_line_is_valid_json(self):
        formatter = JsonFormatter()
        payload = json.loads(formatter.format(self._record()))
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "network_monitor.monitor"
        assert payload["message"] == "collector failed: access denied"

    def test_timestamp_is_utc_iso8601(self):
        payload = json.loads(JsonFormatter().format(self._record()))
        assert payload["ts"].endswith("+00:00")
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", payload["ts"])

    def test_contextual_fields_are_preserved(self):
        """`extra=` fields are what make logs queryable after the fact."""
        formatter = JsonFormatter()
        payload = json.loads(
            formatter.format(self._record(collector="connections", event_type="COLLECTOR_FAILURE"))
        )
        assert payload["collector"] == "connections"
        assert payload["event_type"] == "COLLECTOR_FAILURE"

    def test_unserialisable_extra_is_stringified_not_dropped(self):
        payload = json.loads(JsonFormatter().format(self._record(blob=object())))
        assert isinstance(payload["blob"], str)

    def test_exceptions_are_rendered(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = self._record()
            record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["exception"]

    def test_formatter_selection_follows_configuration(self):
        assert isinstance(build_formatter(LoggingSection(format="json")), JsonFormatter)
        assert not isinstance(build_formatter(LoggingSection(format="text")), JsonFormatter)

    def test_configure_logging_writes_json_to_disk(self, tmp_path):
        section = LoggingSection(
            level="INFO",
            format="json",
            file=str(tmp_path / "logs" / "monitor.log"),
            console=False,
        )
        configure_logging(section)
        logging.getLogger("network_monitor.test").info("hello", extra={"collector": "interface"})

        content = (tmp_path / "logs" / "monitor.log").read_text(encoding="utf-8").strip()
        payload = json.loads(content.splitlines()[-1])
        assert payload["message"] == "hello"
        assert payload["collector"] == "interface"

    def test_configure_logging_is_idempotent(self, tmp_path):
        """Repeated calls (tests, --reload) must not duplicate handlers."""
        section = LoggingSection(file=str(tmp_path / "monitor.log"), console=False)
        configure_logging(section)
        first = len(logging.getLogger().handlers)
        configure_logging(section)
        assert len(logging.getLogger().handlers) == first

    def test_unwritable_log_path_does_not_break_startup(self, tmp_path, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(
            "network_monitor.logging_setup.logging.handlers.RotatingFileHandler", boom
        )
        configure_logging(LoggingSection(file=str(tmp_path / "x.log"), console=False))
        # The application must still be usable: logging is not allowed to be a
        # single point of failure.
        assert logging.getLogger("network_monitor") is not None


class TestMetricsEndpoint:
    def test_content_type_is_prometheus(self, client):
        response = client.get("/api/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "version=0.0.4" in CONTENT_TYPE

    def test_build_info_and_up_are_always_present(self, client):
        body = client.get("/api/metrics").text
        assert "# TYPE lnm_up gauge" in body
        assert "lnm_up 1" in body
        assert 'lnm_build_info{version="' in body

    def test_every_sample_line_parses(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[make_measurement(interface="Ethernet", download_rate=1234.0)],
            connections=[make_connection(pid=1, state_name="ESTABLISHED")],
            processes=[],
            duration_ms=12.5,
        )
        for line in client.get("/api/metrics").text.splitlines():
            if not line or line.startswith("#"):
                continue
            assert re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?\d", line), line

    def test_help_and_type_precede_every_metric_family(self, client):
        families = set()
        typed = set()
        for line in client.get("/api/metrics").text.splitlines():
            if line.startswith("# TYPE"):
                typed.add(line.split()[2])
            elif line and not line.startswith("#"):
                families.add(line.split("{")[0].split(" ")[0])
        assert families <= typed, families - typed

    def test_collector_series_are_per_collector(self, client, state):
        state.health.record_success("interface", utc_now(), 1.0)
        state.health.record_failure("connections", utc_now(), "access denied")
        body = client.get("/api/metrics").text
        assert 'lnm_collector_up{collector="interface"} 1' in body
        assert 'lnm_collector_up{collector="connections"} 0' in body
        assert 'lnm_collector_consecutive_failures{collector="connections"} 1' in body

    def test_interface_rates_are_exported(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[make_measurement(interface="Wi-Fi", download_rate=2500.0)],
            connections=[],
            processes=[],
            duration_ms=1.0,
        )
        body = client.get("/api/metrics").text
        assert 'lnm_interface_receive_bytes_per_second{interface="Wi-Fi"} 2500' in body

    def test_event_counts_are_labelled_by_type_and_severity(self, client, state):
        make_history(state.database, "Ethernet", [1.0])
        from network_monitor.models.events import Event, EventSeverity, EventType

        state.events.add(
            Event(
                timestamp=utc_now(),
                event_type=EventType.HIGH_DOWNLOAD,
                severity=EventSeverity.WARNING,
                title="t",
                description="d",
            )
        )
        body = client.get("/api/metrics").text
        assert 'lnm_events_total{event_type="HIGH_DOWNLOAD",severity="warning"} 1' in body
        assert 'lnm_events_24h{severity="warning"} 1' in body

    def test_storage_and_notification_series_exist(self, client):
        body = client.get("/api/metrics").text
        assert 'lnm_storage_rows{table="events"}' in body
        assert "lnm_storage_bytes" in body

    def test_label_values_are_escaped(self, state):
        """A quote in an interface name must not break the exposition format."""
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[make_measurement(interface='we"ird\\nic')],
            connections=[],
            processes=[],
            duration_ms=1.0,
        )
        body = render_metrics(state)
        line = next(
            line
            for line in body.splitlines()
            if line.startswith("lnm_interface_receive_bytes_per_second")
        )
        assert '\\"' in line and "\\n" in line

    def test_cardinality_stays_bounded(self, client, state):
        """Process names and remote addresses must never become labels."""
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[make_measurement()],
            connections=[
                make_connection(pid=index, process_name=f"proc-{index}.exe") for index in range(50)
            ],
            processes=[],
            duration_ms=1.0,
        )
        body = client.get("/api/metrics").text
        assert "proc-1.exe" not in body
        labels = set(re.findall(r'([a-z_]+)="', body))
        assert labels <= {
            "version",
            "python",
            "platform",
            "collector",
            "interface",
            "state",
            "table",
            "event_type",
            "severity",
            "check",
        }


class TestReadinessEndpoint:
    def test_fresh_monitor_is_ready(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(), measurements=[], connections=[], processes=[], duration_ms=1.0
        )
        payload = client.get("/api/ready").json()
        assert payload["ready"] is True
        assert payload["checks"]["database"] is True

    def test_api_only_mode_is_ready(self, client):
        payload = client.get("/api/ready").json()
        assert payload["ready"] is True
        assert payload["checks"]["collection_loop"] is True

    def test_stalled_loop_is_not_ready(self, client, state):
        """A stalled collector loop must fail readiness, not report healthy."""
        state.publish_cycle(
            timestamp=utc_now() - timedelta(minutes=5),
            measurements=[],
            connections=[],
            processes=[],
            duration_ms=1.0,
        )
        response = client.get("/api/ready")
        assert response.status_code == 503
        payload = response.json()
        assert payload["ready"] is False
        assert payload["checks"]["collection_loop"] is False

    def test_liveness_stays_200_while_readiness_fails(self, client, state):
        """Liveness and readiness must not be conflated."""
        state.publish_cycle(
            timestamp=utc_now() - timedelta(minutes=5),
            measurements=[],
            connections=[],
            processes=[],
            duration_ms=1.0,
        )
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 503

    def test_unwritable_database_fails_readiness(self, client, state, monkeypatch):
        monkeypatch.setattr(state.database, "is_writable", lambda: False)
        payload = client.get("/api/ready").json()
        assert payload["ready"] is False
        assert payload["checks"]["database"] is False

    def test_readiness_metric_tracks_the_endpoint(self, client, state):
        state.publish_cycle(
            timestamp=utc_now() - timedelta(minutes=5),
            measurements=[],
            connections=[],
            processes=[],
            duration_ms=1.0,
        )
        assert "lnm_ready 0" in client.get("/api/metrics").text
        assert 'lnm_readiness_check{check="collection_loop"} 0' in client.get("/api/metrics").text


class TestStatusObservability:
    def test_status_exposes_cycle_health_and_success_ratio(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[],
            connections=[],
            processes=[],
            duration_ms=3.0,
        )
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[],
            connections=[],
            processes=[],
            duration_ms=4.0,
            errors=["connections: access denied"],
        )
        payload = client.get("/api/status").json()
        assert payload["cycles"] == 2
        assert payload["failed_cycles"] == 1
        assert payload["collection_success_ratio"] == pytest.approx(0.5)
        assert payload["max_cycle_duration_ms"] == pytest.approx(4.0)
        assert payload["last_errors"] == ["connections: access denied"]

    def test_upstream_errors_are_visible_not_silent(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[],
            connections=[],
            processes=[],
            duration_ms=1.0,
            errors=["storage: OperationalError: database or disk is full"],
        )
        payload = client.get("/api/status").json()
        assert payload["status"] in {"running", "degraded"}
        assert "disk is full" in payload["last_errors"][0]
