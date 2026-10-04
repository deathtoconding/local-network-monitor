"""Prometheus metrics endpoint.

An operational tool that cannot be observed is not operable. Everything the
monitor knows about itself is exported here in the Prometheus text exposition
format (v0.0.4), so a scraping agent, a Grafana dashboard or a text-mode check
can consume it without any dependency on this process.

Design rules:

* **No client library.** The format is twelve lines of string building; adding
  ``prometheus_client`` to a single-machine tool would be dependencies for
  nothing.
* **Bounded cardinality.** Labels are only ever interface names, collector
  names, TCP states, the six event types and three severities. Process names and
  remote addresses are deliberately *not* labels: they are unbounded and belong
  in the events table, not in a time series.
* **Honest naming.** Counters end in ``_total``, gauges do not, timestamps end
  in ``_timestamp_seconds``, and rates are exported as gauges with ``_per_second``
  in the name because they are already rates.
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from .. import __version__
from .state import MonitorState

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    """Escape a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: Iterable[Tuple[str, Any]]) -> str:
    rendered = [f'{key}="{_escape(str(value))}"' for key, value in pairs]
    return "{" + ",".join(rendered) + "}" if rendered else ""


def _timestamp_seconds(moment: datetime | None) -> float:
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


class _Exposition:
    """Small helper that emits valid HELP/TYPE/value blocks."""

    def __init__(self) -> None:
        self._lines: List[str] = []

    def metric(
        self, name: str, kind: str, help_text: str, samples: Iterable[Tuple[str, float]]
    ) -> None:
        samples = [(labels, value) for labels, value in samples]
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            self._lines.append(f"{name}{labels} {_format_number(value)}")

    def scalar(self, name: str, kind: str, help_text: str, value: float) -> None:
        self.metric(name, kind, help_text, [("", value)])

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


def _format_number(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if value != value:  # NaN
        return "NaN"
    if value in (float("inf"), float("-inf")):  # pragma: no cover - defensive
        return "+Inf" if value > 0 else "-Inf"
    return f"{value:.6g}"


def render_metrics(state: MonitorState) -> str:
    """Render the full exposition text for the current state."""
    exposition = _Exposition()
    now = datetime.now(timezone.utc)

    # ---- build & process -------------------------------------------------
    exposition.metric(
        "lnm_build_info",
        "gauge",
        "Build metadata; always 1.",
        [
            (
                _labels(
                    [
                        ("version", __version__),
                        ("python", platform.python_version()),
                        ("platform", platform.system()),
                    ]
                ),
                1,
            )
        ],
    )
    exposition.scalar("lnm_up", "gauge", "1 while the API process is serving.", 1)
    exposition.scalar(
        "lnm_uptime_seconds",
        "gauge",
        "Seconds since the monitor state was created.",
        state.uptime_seconds(),
    )

    # ---- collection loop -------------------------------------------------
    exposition.scalar(
        "lnm_cycles_total", "counter", "Completed collection cycles.", state.cycle_count
    )
    exposition.scalar(
        "lnm_failed_cycles_total",
        "counter",
        "Collection cycles that recorded at least one collector or storage error.",
        state.failed_cycles,
    )
    exposition.scalar(
        "lnm_last_cycle_timestamp_seconds",
        "gauge",
        "Unix time of the last completed collection cycle.",
        _timestamp_seconds(state.last_cycle_at),
    )
    if state.last_cycle_duration_ms is not None:
        exposition.scalar(
            "lnm_cycle_duration_seconds",
            "gauge",
            "Duration of the most recent collection cycle.",
            state.last_cycle_duration_ms / 1000.0,
        )
    if state.max_cycle_duration_ms is not None:
        exposition.scalar(
            "lnm_cycle_duration_seconds_max",
            "gauge",
            "Longest collection cycle observed since start.",
            state.max_cycle_duration_ms / 1000.0,
        )

    # ---- collectors ------------------------------------------------------
    statuses = list(state.health.statuses.values())
    if statuses:
        exposition.metric(
            "lnm_collector_up",
            "gauge",
            "1 when the collector's last attempt succeeded, 0 otherwise.",
            [
                (_labels([("collector", s.name)]), 1 if s.state == "healthy" else 0)
                for s in statuses
            ],
        )
        exposition.metric(
            "lnm_collector_consecutive_failures",
            "gauge",
            "Consecutive failures of the last collection attempt.",
            [(_labels([("collector", s.name)]), s.consecutive_failures) for s in statuses],
        )
        exposition.metric(
            "lnm_collector_runs_total",
            "counter",
            "Collection attempts per collector.",
            [(_labels([("collector", s.name)]), s.total_runs) for s in statuses],
        )
        exposition.metric(
            "lnm_collector_failures_total",
            "counter",
            "Failed collection attempts per collector.",
            [(_labels([("collector", s.name)]), s.total_failures) for s in statuses],
        )
        exposition.metric(
            "lnm_collector_last_success_timestamp_seconds",
            "gauge",
            "Unix time of the last successful collection per collector.",
            [
                (_labels([("collector", s.name)]), _timestamp_seconds(s.last_success))
                for s in statuses
            ],
        )

    # ---- interfaces ------------------------------------------------------
    measurements = state.current_measurements()
    if measurements:
        exposition.metric(
            "lnm_interface_receive_bytes_per_second",
            "gauge",
            "Measured download rate per interface.",
            [
                (_labels([("interface", m.interface_name)]), float(m.download_rate or 0.0))
                for m in measurements
            ],
        )
        exposition.metric(
            "lnm_interface_transmit_bytes_per_second",
            "gauge",
            "Measured upload rate per interface.",
            [
                (_labels([("interface", m.interface_name)]), float(m.upload_rate or 0.0))
                for m in measurements
            ],
        )
        exposition.metric(
            "lnm_interface_receive_bytes_total",
            "counter",
            "Cumulative bytes received per interface (OS counter).",
            [(_labels([("interface", m.interface_name)]), m.bytes_received) for m in measurements],
        )
        exposition.metric(
            "lnm_interface_transmit_bytes_total",
            "counter",
            "Cumulative bytes sent per interface (OS counter).",
            [(_labels([("interface", m.interface_name)]), m.bytes_sent) for m in measurements],
        )
        exposition.metric(
            "lnm_interface_receive_errors_total",
            "counter",
            "Cumulative receive errors per interface (OS counter).",
            [(_labels([("interface", m.interface_name)]), m.errors_in) for m in measurements],
        )
        exposition.metric(
            "lnm_interface_transmit_errors_total",
            "counter",
            "Cumulative transmit errors per interface (OS counter).",
            [(_labels([("interface", m.interface_name)]), m.errors_out) for m in measurements],
        )
        exposition.metric(
            "lnm_interface_receive_drops_total",
            "counter",
            "Cumulative dropped inbound packets per interface (OS counter).",
            [(_labels([("interface", m.interface_name)]), m.drops_in) for m in measurements],
        )
        exposition.metric(
            "lnm_interface_transmit_drops_total",
            "counter",
            "Cumulative dropped outbound packets per interface (OS counter).",
            [(_labels([("interface", m.interface_name)]), m.drops_out) for m in measurements],
        )

    # ---- connections -----------------------------------------------------
    connections = state.current_connections()
    exposition.scalar(
        "lnm_connections_active",
        "gauge",
        "TCP connections seen in the latest collection cycle.",
        len(connections),
    )
    if connections:
        states: Dict[str, int] = {}
        for connection in connections:
            states[connection.state] = states.get(connection.state, 0) + 1
        exposition.metric(
            "lnm_connections_by_state",
            "gauge",
            "TCP connections of the latest cycle by state.",
            [(_labels([("state", name)]), count) for name, count in sorted(states.items())],
        )

    # ---- detection -------------------------------------------------------
    exposition.scalar(
        "lnm_detection_baseline_connections",
        "gauge",
        "Rolling baseline used by the connection-spike rule.",
        round(state.detection.baseline, 3),
    )
    exposition.scalar(
        "lnm_detection_known_processes",
        "gauge",
        "Processes known to use the network (NEW_NETWORK_PROCESS baseline).",
        len(state.detection.seen_pids),
    )
    event_counts = state.events.counts_by_type_and_severity()
    if event_counts:
        exposition.metric(
            "lnm_events_total",
            "counter",
            "Detected events by type and severity (lifetime).",
            [
                (
                    _labels([("event_type", row["event_type"]), ("severity", row["severity"])]),
                    row["total"],
                )
                for row in event_counts
            ],
        )
    window = state.event_counts()
    exposition.metric(
        "lnm_events_24h",
        "gauge",
        "Detected events in the last 24 hours by severity.",
        [
            (_labels([("severity", severity)]), window[severity])
            for severity in ("info", "warning", "critical")
        ],
    )

    # ---- storage ---------------------------------------------------------
    exposition.metric(
        "lnm_storage_rows",
        "gauge",
        "Rows per table.",
        [
            (_labels([("table", table)]), count)
            for table, count in state.database.table_counts().items()
        ],
    )
    exposition.scalar(
        "lnm_storage_bytes",
        "gauge",
        "On-disk size of the SQLite database including WAL.",
        state.database.size_bytes(),
    )
    exposition.scalar(
        "lnm_processes_tracked",
        "gauge",
        "Processes recorded in the process directory.",
        state.processes.count(),
    )

    # ---- notifications ---------------------------------------------------
    if state.notifications is not None:
        status = state.notifications.status()
        exposition.scalar(
            "lnm_notifications_dispatched_total",
            "counter",
            "Notifications handed to the delivery worker.",
            status["dispatched"],
        )
        exposition.scalar(
            "lnm_notifications_suppressed_total",
            "counter",
            "Notifications suppressed by the cooldown policy.",
            status["suppressed"],
        )
        exposition.scalar(
            "lnm_notifications_dropped_total",
            "counter",
            "Notifications dropped because the queue was full.",
            status["dropped"],
        )
        exposition.scalar(
            "lnm_notifications_queued",
            "gauge",
            "Notifications waiting in the delivery queue.",
            status["queued"],
        )

    # ---- readiness -------------------------------------------------------
    ready, checks = state.readiness()
    exposition.scalar(
        "lnm_ready",
        "gauge",
        "1 when /api/ready would answer 200, 0 otherwise.",
        1 if ready else 0,
    )
    exposition.metric(
        "lnm_readiness_check",
        "gauge",
        "Individual readiness checks (1 = passing).",
        [
            (_labels([("check", name)]), 1 if passed else 0)
            for name, passed in sorted(checks.items())
        ],
    )
    exposition.scalar(
        "lnm_scrape_timestamp_seconds",
        "gauge",
        "Unix time at which this exposition was rendered.",
        now.timestamp(),
    )

    return exposition.text()
