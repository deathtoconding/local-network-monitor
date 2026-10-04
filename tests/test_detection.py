"""Milestone 6 tests: the deterministic detection engine.

The acceptance criterion from the specification is asserted directly:
``download_rate = 20 MB/s`` with a ``10 MB/s`` threshold must produce
``HIGH_DOWNLOAD``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from network_monitor.config import DetectionSection
from network_monitor.detection import (
    CollectorFailureRule,
    ConnectionSpikeRule,
    DetectionEngine,
    HighDownloadRule,
    HighUploadRule,
    InterfaceErrorRule,
    NewNetworkProcessRule,
    RuleContext,
)
from network_monitor.models.events import EventSeverity, EventType
from network_monitor.models.health import CollectorHealthRegistry
from network_monitor.models.process import ProcessInfo

from .conftest import make_connection, make_measurement

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
MBPS = DetectionSection.BYTES_PER_MEGABIT  # 125_000 bytes/s per Mb/s


def context(**overrides) -> RuleContext:
    payload = dict(timestamp=T0)
    payload.update(overrides)
    return RuleContext(**payload)


class TestHighDownloadRule:
    def test_acceptance_criterion_20_mbps_above_10_mbps_threshold(self):
        rule = HighDownloadRule(DetectionSection(download_threshold_mbps=10))
        measurements = [make_measurement(download_rate=20 * MBPS)]
        events = rule.evaluate(context(measurements=measurements))
        assert len(events) == 1
        assert events[0].event_type is EventType.HIGH_DOWNLOAD
        assert events[0].severity is EventSeverity.WARNING
        assert events[0].evidence["download_rate_mbps"] == pytest.approx(20.0)
        assert events[0].evidence["interface"] == "Ethernet"

    def test_no_event_below_threshold(self):
        rule = HighDownloadRule(DetectionSection(download_threshold_mbps=10))
        assert rule.evaluate(context(measurements=[make_measurement(download_rate=5 * MBPS)])) == []

    def test_zero_threshold_disables_the_rule(self):
        rule = HighDownloadRule(DetectionSection(download_threshold_mbps=0))
        assert rule.evaluate(context(measurements=[make_measurement(download_rate=999 * MBPS)])) == []

    def test_missing_rate_is_ignored(self):
        rule = HighDownloadRule(DetectionSection(download_threshold_mbps=1))
        assert rule.evaluate(context(measurements=[make_measurement(download_rate=None)])) == []

    def test_event_explains_itself(self):
        rule = HighDownloadRule(DetectionSection(download_threshold_mbps=10))
        event = rule.evaluate(context(measurements=[make_measurement(download_rate=20 * MBPS)]))[0]
        assert "20.0 Mb/s" in event.description
        assert event.source == "rule:high_download"
        assert event.timestamp == T0


class TestHighUploadRule:
    def test_upload_spike_detected(self):
        rule = HighUploadRule(DetectionSection(upload_threshold_mbps=5))
        events = rule.evaluate(context(measurements=[make_measurement(upload_rate=6 * MBPS)]))
        assert events[0].event_type is EventType.HIGH_UPLOAD

    def test_managed_upload_is_silent(self):
        rule = HighUploadRule(DetectionSection(upload_threshold_mbps=5))
        assert rule.evaluate(context(measurements=[make_measurement(upload_rate=1 * MBPS)])) == []

    def test_download_traffic_does_not_trigger_upload_rule(self):
        rule = HighUploadRule(DetectionSection(upload_threshold_mbps=5))
        measurements = [make_measurement(upload_rate=0.0, download_rate=50 * MBPS)]
        assert rule.evaluate(context(measurements=measurements)) == []


class TestInterfaceErrorRule:
    def test_no_event_on_first_observation(self):
        rule = InterfaceErrorRule()
        measurements = [make_measurement(errors_in=5)]
        assert rule.evaluate(context(measurements=measurements)) == []

    def test_cumulative_counters_do_not_repeat_forever(self):
        rule = InterfaceErrorRule()
        first = [make_measurement(errors_in=5)]
        rule.evaluate(context(measurements=first))
        # same absolute value: no new errors, so no event
        assert rule.evaluate(context(measurements=[make_measurement(errors_in=5)])) == []

    def test_delta_raises_an_event(self):
        rule = InterfaceErrorRule()
        rule.evaluate(context(measurements=[make_measurement(errors_in=5, drops_out=0)]))
        events = rule.evaluate(
            context(measurements=[make_measurement(errors_in=8, drops_out=2)])
        )
        assert len(events) == 1
        assert events[0].event_type is EventType.INTERFACE_ERROR
        assert events[0].evidence["deltas"]["errors_in"] == 3
        assert events[0].evidence["deltas"]["drops_out"] == 2


class TestNewNetworkProcessRule:
    def test_observation_of_a_new_pid(self):
        rule = NewNetworkProcessRule(DetectionSection())
        events = rule.evaluate(
            context(
                connections=[make_connection(pid=8420, process_name="chrome.exe")],
                processes=[ProcessInfo(pid=8420, name="chrome.exe", executable="C:/chrome.exe")],
                seen_pids=set(),
            )
        )
        assert len(events) == 1
        event = events[0]
        assert event.event_type is EventType.NEW_NETWORK_PROCESS
        assert event.severity is EventSeverity.INFO
        assert event.pid == 8420
        assert "observation" in event.description.lower()

    def test_known_pid_is_silent(self):
        rule = NewNetworkProcessRule(DetectionSection())
        assert (
            rule.evaluate(
                context(connections=[make_connection(pid=8420)], seen_pids={8420})
            )
            == []
        )

    def test_rule_can_be_disabled(self):
        rule = NewNetworkProcessRule(DetectionSection(new_process_rule_enabled=False))
        assert rule.evaluate(context(connections=[make_connection(pid=1)])) == []

    def test_one_event_per_process_not_per_connection(self):
        rule = NewNetworkProcessRule(DetectionSection())
        connections = [
            make_connection(pid=8420, remote_port=443),
            make_connection(pid=8420, remote_port=8443),
        ]
        events = rule.evaluate(context(connections=connections))
        assert len(events) == 1
        assert events[0].evidence["connection_count"] == 2

    def test_connection_without_pid_is_skipped(self):
        rule = NewNetworkProcessRule(DetectionSection())
        assert rule.evaluate(context(connections=[make_connection(pid=None)])) == []


class TestConnectionSpikeRule:
    def test_spike_above_baseline_multiplier(self):
        rule = ConnectionSpikeRule(DetectionSection(connection_spike_multiplier=3))
        connections = [make_connection(pid=index) for index in range(1, 32)]
        events = rule.evaluate(context(connections=connections, connection_baseline=10.0))
        assert len(events) == 1
        assert events[0].event_type is EventType.CONNECTION_SPIKE
        assert events[0].evidence["baseline_connections"] == 10.0
        assert events[0].evidence["top_processes"]

    def test_small_absolute_numbers_do_not_trigger(self):
        rule = ConnectionSpikeRule(DetectionSection(connection_spike_multiplier=3))
        events = rule.evaluate(
            context(connections=[make_connection(pid=index) for index in range(1, 7)],
                    connection_baseline=2.0)
        )
        assert events == []

    def test_connection_drop_is_never_a_spike(self):
        rule = ConnectionSpikeRule(DetectionSection(connection_spike_multiplier=3))
        events = rule.evaluate(context(connections=[make_connection()], connection_baseline=50.0))
        assert events == []


class TestCollectorFailureRule:
    def _registry(self, timestamp, age_seconds, error="Access denied"):
        registry = CollectorHealthRegistry()
        registry.record_failure("connections", timestamp, error)
        status = registry.get("connections")
        status.last_success = timestamp - timedelta(seconds=age_seconds)
        return registry

    def test_failure_beyond_timeout_raises_event(self):
        rule = CollectorFailureRule(DetectionSection(collector_failure_timeout_seconds=15))
        registry = self._registry(T0 - timedelta(seconds=30), age_seconds=30)
        events = rule.evaluate(context(collector_status=registry.statuses))
        assert len(events) == 1
        assert events[0].event_type is EventType.COLLECTOR_FAILURE
        assert events[0].severity is EventSeverity.CRITICAL
        assert events[0].evidence["collector"] == "connections"

    def test_healthy_collector_is_silent(self):
        rule = CollectorFailureRule(DetectionSection(collector_failure_timeout_seconds=15))
        registry = CollectorHealthRegistry()
        registry.record_success("interface", T0, 1.0)
        assert rule.evaluate(context(collector_status=registry.statuses)) == []


class TestDetectionEngine:
    def test_engine_end_to_end_high_download(self):
        engine = DetectionEngine(DetectionSection(download_threshold_mbps=10))
        events = engine.evaluate(
            timestamp=T0,
            measurements=[make_measurement(download_rate=20 * MBPS)],
        )
        assert [event.event_type for event in events] == [EventType.HIGH_DOWNLOAD]

    def test_cooldown_suppresses_repeats_then_allows_them_again(self):
        engine = DetectionEngine(
            DetectionSection(download_threshold_mbps=10, event_cooldown_seconds=60)
        )
        hot = [make_measurement(download_rate=20 * MBPS)]
        first = engine.evaluate(timestamp=T0, measurements=hot)
        second = engine.evaluate(timestamp=T0 + timedelta(seconds=5), measurements=hot)
        later = engine.evaluate(timestamp=T0 + timedelta(seconds=120), measurements=hot)
        assert len(first) == 1
        assert second == []
        assert len(later) == 1

    def test_new_process_seen_once_across_cycles(self):
        engine = DetectionEngine(DetectionSection(event_cooldown_seconds=0))
        connections = [make_connection(pid=4242)]
        first = engine.evaluate(timestamp=T0, connections=connections)
        second = engine.evaluate(timestamp=T0 + timedelta(seconds=1), connections=connections)
        assert [event.event_type for event in first] == [EventType.NEW_NETWORK_PROCESS]
        assert second == []

    def test_seeding_prevents_startup_noise(self):
        engine = DetectionEngine(DetectionSection(event_cooldown_seconds=0))
        connections = [make_connection(pid=4242)]
        engine.seed_processes_from_connections(connections)
        assert engine.evaluate(timestamp=T0, connections=connections) == []

    def test_baseline_averages_previous_cycles(self):
        engine = DetectionEngine(DetectionSection(connection_spike_min_baseline=1))
        for count in (10, 20, 30):
            engine.evaluate(
                timestamp=T0,
                connections=[make_connection(pid=index) for index in range(count)],
            )
            assert engine.baseline > 0
        assert engine.baseline == pytest.approx(15.0, abs=10.0)

    def test_connection_spike_uses_the_rolling_baseline(self):
        engine = DetectionEngine(
            DetectionSection(
                connection_spike_multiplier=2.0,
                connection_spike_min_baseline=10,
                event_cooldown_seconds=0,
                new_process_rule_enabled=False,
            )
        )
        steady = [make_connection(pid=index) for index in range(10)]
        for tick in range(10):
            engine.seed_processes_from_connections(steady)
            engine.evaluate(timestamp=T0 + timedelta(seconds=tick), connections=steady)

        spike = [make_connection(pid=index) for index in range(40)]
        engine.seed_processes_from_connections(spike)
        events = engine.evaluate(timestamp=T0 + timedelta(seconds=20), connections=spike)
        assert EventType.CONNECTION_SPIKE in [event.event_type for event in events]

    def test_broken_rule_does_not_stop_the_engine(self):
        class ExplodingRule(HighDownloadRule):
            name = "exploding"

            def evaluate(self, context):  # noqa: D102 - deliberate failure
                raise RuntimeError("rule bug")

        engine = DetectionEngine(
            DetectionSection(download_threshold_mbps=10),
            rules=[ExplodingRule(), HighDownloadRule(DetectionSection(download_threshold_mbps=10))],
        )
        events = engine.evaluate(
            timestamp=T0, measurements=[make_measurement(download_rate=20 * MBPS)]
        )
        assert [event.event_type for event in events] == [EventType.HIGH_DOWNLOAD]

    def test_collector_failure_end_to_end(self):
        engine = DetectionEngine(DetectionSection(collector_failure_timeout_seconds=15))
        registry = CollectorHealthRegistry()
        registry.record_failure("interface", T0, "boom")
        registry.get("interface").last_success = T0 - timedelta(seconds=60)
        events = engine.evaluate(timestamp=T0, collector_health=registry)
        assert [event.event_type for event in events] == [EventType.COLLECTOR_FAILURE]

    def test_rule_summary_lists_all_rules(self):
        engine = DetectionEngine()
        names = {entry["name"] for entry in engine.rule_summary()}
        assert names == {
            "high_download",
            "high_upload",
            "interface_error",
            "new_network_process",
            "connection_spike",
            "collector_failure",
        }

    def test_reset_clears_state(self):
        engine = DetectionEngine(DetectionSection(event_cooldown_seconds=0))
        engine.evaluate(timestamp=T0, connections=[make_connection(pid=7)])
        engine.reset()
        assert engine.seen_pids == set()
        assert engine.baseline == 0.0
