"""Deterministic detection rules.

Every rule is a small, pure function of the current :class:`RuleContext`:
measurements in, :class:`Event` objects out. No rule touches the database, the
network or the clock (the timestamp comes from the context), which is what makes
Milestone 6's acceptance criteria testable with plain unit tests.

Rules implemented (spec section 8):

====  ==========================  =========================================
#     Rule                        Trigger
====  ==========================  =========================================
1     HIGH_DOWNLOAD               download_rate > threshold
2     HIGH_UPLOAD                 upload_rate > threshold
3     INTERFACE_ERROR             error counter delta > 0
4     NEW_NETWORK_PROCESS         unseen PID owns a connection
5     CONNECTION_SPIKE            count > baseline * multiplier
6     COLLECTOR_FAILURE           no successful collection within timeout
====  ==========================  =========================================

A NEW_NETWORK_PROCESS event is an *observation*, never an accusation: the
wording deliberately says "may be worth reviewing" instead of implying malice.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set

from ..config import DetectionSection
from ..models.events import Event, EventSeverity, EventType
from ..models.health import CollectorStatus
from ..models.network import InterfaceMeasurement, NetworkConnection
from ..models.process import ProcessInfo


@dataclass
class RuleContext:
    """Everything every rule may look at for one collection cycle."""

    timestamp: datetime
    measurements: Sequence[InterfaceMeasurement] = field(default_factory=list)
    connections: Sequence[NetworkConnection] = field(default_factory=list)
    processes: Sequence[ProcessInfo] = field(default_factory=list)
    collector_status: Dict[str, CollectorStatus] = field(default_factory=dict)
    #: Rolling average of connections over previous cycles (0 = unknown).
    connection_baseline: float = 0.0
    #: PIDs that were already known to use the network.
    seen_pids: Set[int] = field(default_factory=set)

    def connections_for_pid(self, pid: int) -> List[NetworkConnection]:
        return [c for c in self.connections if c.pid == pid]


class Rule(abc.ABC):
    """Base class for detection rules."""

    #: Stable identifier used in events, logs and tests.
    name: str = "rule"
    event_type: EventType = EventType.NEW_NETWORK_PROCESS
    severity: EventSeverity = EventSeverity.INFO
    enabled: bool = True

    def __init__(self, config: DetectionSection | None = None) -> None:
        self.config = config or DetectionSection()

    @abc.abstractmethod
    def evaluate(self, context: RuleContext) -> List[Event]:
        """Return zero or more events for this cycle."""

    # ---- helpers -----------------------------------------------------
    def make_event(
        self,
        context: RuleContext,
        *,
        title: str,
        description: str,
        evidence: Dict[str, object],
        severity: Optional[EventSeverity] = None,
        pid: Optional[int] = None,
        process_name: Optional[str] = None,
        interface_name: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Event:
        return Event(
            timestamp=context.timestamp,
            event_type=self.event_type,
            severity=severity or self.severity,
            title=title,
            description=description,
            source=source or f"rule:{self.name}",
            pid=pid,
            process_name=process_name,
            interface_name=interface_name,
            evidence=evidence,
        )


class BandwidthRule(Rule):
    """Shared implementation for the download and upload threshold rules."""

    direction = "download"

    def threshold_bytes_per_second(self) -> float:
        raise NotImplementedError

    def rate_for(self, measurement: InterfaceMeasurement) -> Optional[float]:
        raise NotImplementedError

    def evaluate(self, context: RuleContext) -> List[Event]:
        threshold = self.threshold_bytes_per_second()
        if threshold <= 0:  # 0 disables the rule
            return []

        events: List[Event] = []
        for measurement in context.measurements:
            rate = self.rate_for(measurement)
            if rate is None or rate <= threshold:
                continue

            megabit_rate = rate / DetectionSection.BYTES_PER_MEGABIT
            threshold_mbps = threshold / DetectionSection.BYTES_PER_MEGABIT
            events.append(
                self.make_event(
                    context,
                    title=f"High {self.direction} traffic on {measurement.interface_name}",
                    description=(
                        f"{self.direction.capitalize()} traffic reached "
                        f"{megabit_rate:.1f} Mb/s, above the configured threshold of "
                        f"{threshold_mbps:.1f} Mb/s."
                    ),
                    interface_name=measurement.interface_name,
                    evidence={
                        "interface": measurement.interface_name,
                        f"{self.direction}_rate_bytes_per_second": round(rate, 2),
                        f"{self.direction}_rate_mbps": round(megabit_rate, 3),
                        "threshold_bytes_per_second": threshold,
                        "threshold_mbps": threshold_mbps,
                        "timestamp": measurement.timestamp.isoformat(),
                    },
                )
            )
        return events


class HighDownloadRule(BandwidthRule):
    name = "high_download"
    event_type = EventType.HIGH_DOWNLOAD
    severity = EventSeverity.WARNING
    direction = "download"

    def threshold_bytes_per_second(self) -> float:
        return self.config.download_threshold_bytes_per_second

    def rate_for(self, measurement: InterfaceMeasurement) -> Optional[float]:
        return measurement.download_rate


class HighUploadRule(BandwidthRule):
    name = "high_upload"
    event_type = EventType.HIGH_UPLOAD
    severity = EventSeverity.WARNING
    direction = "upload"

    def threshold_bytes_per_second(self) -> float:
        return self.config.upload_threshold_bytes_per_second

    def rate_for(self, measurement: InterfaceMeasurement) -> Optional[float]:
        return measurement.upload_rate


class InterfaceErrorRule(Rule):
    """Rule 3 - any increase in interface errors or drops.

    Only *deltas* matter. The absolute counters are cumulative since boot, so a
    machine that saw 12 errors last month would otherwise raise an event on
    every single cycle. Deltas come from comparing against the previous sample
    of the same interface, remembered by the engine between cycles.
    """

    name = "interface_error"
    event_type = EventType.INTERFACE_ERROR
    severity = EventSeverity.WARNING

    def __init__(self, config: DetectionSection | None = None) -> None:
        super().__init__(config)
        self._previous: Dict[str, Dict[str, int]] = {}
        self._primed = False

    def evaluate(self, context: RuleContext) -> List[Event]:
        events: List[Event] = []
        for measurement in context.measurements:
            previous = self._previous.get(measurement.interface_name)
            current = {
                "errors_in": measurement.errors_in,
                "errors_out": measurement.errors_out,
                "drops_in": measurement.drops_in,
                "drops_out": measurement.drops_out,
            }
            if previous is not None:
                deltas = {
                    key: max(0, current[key] - previous.get(key, current[key])) for key in current
                }
                total = sum(deltas.values())
                if total > 0:
                    events.append(
                        self.make_event(
                            context,
                            title=f"Interface errors on {measurement.interface_name}",
                            description=(
                                f"{total} new error(s)/drop(s) observed on "
                                f"{measurement.interface_name} in the last interval."
                            ),
                            interface_name=measurement.interface_name,
                            evidence={
                                "interface": measurement.interface_name,
                                "deltas": deltas,
                                "total_delta": total,
                                "cumulative": current,
                            },
                        )
                    )
            self._previous[measurement.interface_name] = current
        self._primed = True
        return events

    def reset(self) -> None:
        """Forget baselines (used in tests and when the loop restarts)."""
        self._previous.clear()
        self._primed = False


class NewNetworkProcessRule(Rule):
    """Rule 4 - a PID that was not seen using the network before.

    The engine owns the ``seen_pids`` set, so the rule stays stateless and the
    baseline can be seeded on startup (``seed_processes_on_start``) to avoid a
    flood of events for already-running software.
    """

    name = "new_network_process"
    event_type = EventType.NEW_NETWORK_PROCESS
    severity = EventSeverity.INFO

    def evaluate(self, context: RuleContext) -> List[Event]:
        if not self.config.new_process_rule_enabled:
            return []

        events: List[Event] = []
        by_pid: Dict[int, List[NetworkConnection]] = {}
        for connection in context.connections:
            if connection.pid is None:
                continue
            by_pid.setdefault(int(connection.pid), []).append(connection)

        process_index = {p.pid: p for p in context.processes}
        for pid, connections in sorted(by_pid.items()):
            if pid in context.seen_pids:
                continue
            process = process_index.get(pid)
            name = process.name if process else (connections[0].process_name or f"PID {pid}")
            endpoints = sorted({c.remote_endpoint for c in connections if c.remote_address})
            states = sorted({c.state for c in connections})
            events.append(
                self.make_event(
                    context,
                    title=f"New network process: {name}",
                    description=(
                        f"Process {name} (PID {pid}) started using the network: "
                        f"{len(connections)} connection(s). This is an observation, "
                        "not an indication of malicious behaviour."
                    ),
                    pid=pid,
                    process_name=name,
                    evidence={
                        "pid": pid,
                        "process_name": name,
                        "executable": process.executable if process else None,
                        "connection_count": len(connections),
                        "remote_endpoints": endpoints[:20],
                        "states": states,
                        "first_seen": context.timestamp.isoformat(),
                    },
                )
            )
        return events


class ConnectionSpikeRule(Rule):
    """Rule 5 - connection count far above the rolling baseline."""

    name = "connection_spike"
    event_type = EventType.CONNECTION_SPIKE
    severity = EventSeverity.WARNING

    def evaluate(self, context: RuleContext) -> List[Event]:
        current = len(context.connections)
        baseline = context.connection_baseline
        multiplier = self.config.connection_spike_multiplier

        if baseline < self.config.connection_spike_min_baseline:
            return []
        if multiplier <= 0 or current <= baseline * multiplier:
            return []

        # Attribute the spike to the processes that grew the most, if known.
        per_process: Dict[str, int] = {}
        for connection in context.connections:
            key = connection.process_name or (
                f"PID {connection.pid}" if connection.pid else "unknown"
            )
            per_process[key] = per_process.get(key, 0) + 1
        top = sorted(per_process.items(), key=lambda item: item[1], reverse=True)[:5]

        return [
            self.make_event(
                context,
                title="Connection spike detected",
                description=(
                    f"{current} active TCP connections, {current / baseline:.1f}x the "
                    f"rolling baseline of {baseline:.1f} (threshold {multiplier:.1f}x)."
                ),
                evidence={
                    "current_connections": current,
                    "baseline_connections": round(baseline, 2),
                    "multiplier": multiplier,
                    "observed_ratio": round(current / baseline, 2),
                    "top_processes": [
                        {"process": name, "connections": count} for name, count in top
                    ],
                },
            )
        ]


class CollectorFailureRule(Rule):
    """Rule 6 - a collector has not succeeded within the timeout."""

    name = "collector_failure"
    event_type = EventType.COLLECTOR_FAILURE
    severity = EventSeverity.CRITICAL

    def evaluate(self, context: RuleContext) -> List[Event]:
        timeout = self.config.collector_failure_timeout_seconds
        events: List[Event] = []
        for name, status in sorted(context.collector_status.items()):
            if status.last_success is None:
                # Never succeeded: only report once the process has been running
                # for longer than the timeout (handled by the engine's clock).
                age = _age_seconds(context.timestamp, status.last_attempt)
                if age is None or age < timeout:
                    continue
            else:
                age = _age_seconds(context.timestamp, status.last_success)
                if age is None or age < timeout:
                    continue

            events.append(
                self.make_event(
                    context,
                    title=f"Collector failure: {name}",
                    description=(
                        f"The '{name}' collector has not produced data for "
                        f"{age:.0f}s (timeout {timeout:.0f}s)."
                        + (f" Last error: {status.last_error}" if status.last_error else "")
                    ),
                    source=f"collector:{name}",
                    evidence={
                        "collector": name,
                        "seconds_since_success": round(age, 1),
                        "timeout_seconds": timeout,
                        "consecutive_failures": status.consecutive_failures,
                        "last_error": status.last_error,
                        "state": status.state,
                    },
                )
            )
        return events


def _age_seconds(now: datetime, moment: Optional[datetime]) -> Optional[float]:
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds())


def default_rules(config: DetectionSection | None = None) -> List[Rule]:
    """The rule set used by the application, in evaluation order."""
    return [
        HighDownloadRule(config),
        HighUploadRule(config),
        InterfaceErrorRule(config),
        NewNetworkProcessRule(config),
        ConnectionSpikeRule(config),
        CollectorFailureRule(config),
    ]
