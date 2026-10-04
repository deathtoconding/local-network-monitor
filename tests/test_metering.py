"""Milestone 1 tests: rate calculation and the interface collector.

The acceptance criterion that matters most here is the counter-reset rule:
after an interface counter goes backwards the monitor must report 0 B/s for that
interval - never a negative rate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from network_monitor.collectors import InterfaceCollector, RateCalculator, summarise_rates
from network_monitor.collectors.base import CollectorError
from network_monitor.config import MonitorSection

from .conftest import make_measurement

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestRateCalculator:
    def test_returns_none_without_a_previous_sample(self):
        calculator = RateCalculator()
        assert calculator.update("eth0", T0, 1_000) is None

    def test_rate_is_delta_over_elapsed_seconds(self):
        calculator = RateCalculator()
        calculator.update("eth0", T0, 1_000)
        rate = calculator.update("eth0", T0 + timedelta(seconds=2), 3_000)
        assert rate == pytest.approx(1_000.0)  # (3000 - 1000) / 2

    def test_counter_reset_is_discarded_not_negative(self):
        calculator = RateCalculator()
        calculator.update("eth0", T0, 5_000_000)
        rate = calculator.update("eth0", T0 + timedelta(seconds=1), 100)
        assert rate == 0.0
        assert calculator.reset_count == 1

    def test_baseline_is_reestablished_after_a_reset(self):
        calculator = RateCalculator()
        calculator.update("eth0", T0, 5_000_000)
        calculator.update("eth0", T0 + timedelta(seconds=1), 100)  # reset
        rate = calculator.update("eth0", T0 + timedelta(seconds=2), 1_100)
        assert rate == pytest.approx(1_000.0)

    def test_zero_elapsed_time_does_not_divide_by_zero(self):
        calculator = RateCalculator()
        calculator.update("eth0", T0, 10)
        assert calculator.update("eth0", T0, 20) is None

    def test_counters_are_tracked_per_key(self):
        calculator = RateCalculator()
        calculator.update("eth0", T0, 0)
        calculator.update("wlan0", T0, 0)
        assert calculator.update("eth0", T0 + timedelta(seconds=1), 100) == pytest.approx(100)
        assert calculator.update("wlan0", T0 + timedelta(seconds=1), 50) == pytest.approx(50)

    def test_reset_clears_baselines(self):
        calculator = RateCalculator()
        calculator.update("eth0", T0, 1)
        assert calculator.has_baseline("eth0")
        calculator.reset()
        assert not calculator.has_baseline("eth0")


def fake_counter(bytes_sent: int, bytes_recv: int, **extra) -> SimpleNamespace:
    defaults = dict(
        bytes_sent=bytes_sent,
        bytes_recv=bytes_recv,
        packets_sent=10,
        packets_recv=20,
        errin=0,
        errout=0,
        dropin=0,
        dropout=0,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


class TestInterfaceCollector:
    def _collector(self, monkeypatch, samples):
        """Build a collector whose counter source yields the given samples."""
        collector = InterfaceCollector(MonitorSection(exclude_interfaces=[]))
        iterator = iter(samples)

        def fake_net_io_counters(pernic=True):
            return next(iterator)

        monkeypatch.setattr("psutil.net_io_counters", fake_net_io_counters)
        return collector

    def test_first_cycle_establishes_baseline_without_rates(self, monkeypatch):
        collector = self._collector(monkeypatch, [{"eth0": fake_counter(100, 200)}])
        measurements = collector.collect()
        assert len(measurements) == 1
        assert measurements[0].upload_rate is None
        assert measurements[0].download_rate is None
        assert measurements[0].bytes_sent == 100

    def test_second_cycle_produces_rates(self, monkeypatch):
        timestamps = iter([T0, T0 + timedelta(seconds=2)])
        collector = self._collector(
            monkeypatch,
            [{"eth0": fake_counter(1_000, 2_000)}, {"eth0": fake_counter(3_000, 6_000)}],
        )
        monkeypatch.setattr(
            "network_monitor.collectors.interface.utc_now", lambda: next(timestamps)
        )
        collector.collect()
        measurements = collector.collect()
        assert measurements[0].upload_rate == pytest.approx(1_000.0)
        assert measurements[0].download_rate == pytest.approx(2_000.0)

    def test_counter_reset_never_yields_a_negative_rate(self, monkeypatch):
        timestamps = iter([T0, T0 + timedelta(seconds=1)])
        collector = self._collector(
            monkeypatch,
            [{"eth0": fake_counter(9_000, 9_000)}, {"eth0": fake_counter(10, 20)}],
        )
        monkeypatch.setattr(
            "network_monitor.collectors.interface.utc_now", lambda: next(timestamps)
        )
        collector.collect()
        measurements = collector.collect()
        assert measurements[0].upload_rate == 0.0
        assert measurements[0].download_rate == 0.0

    def test_errors_and_drops_are_carried_through(self, monkeypatch):
        collector = self._collector(
            monkeypatch,
            [{"eth0": fake_counter(1, 2, errin=3, errout=4, dropin=5, dropout=6)}],
        )
        measurement = collector.collect()[0]
        assert (measurement.errors_in, measurement.errors_out) == (3, 4)
        assert (measurement.drops_in, measurement.drops_out) == (5, 6)

    def test_excluded_interfaces_are_skipped(self, monkeypatch):
        collector = InterfaceCollector(MonitorSection(exclude_interfaces=["lo"]))
        monkeypatch.setattr(
            "psutil.net_io_counters",
            lambda pernic=True: {"lo": fake_counter(1, 1), "eth0": fake_counter(2, 2)},
        )
        measurements = collector.collect()
        assert [m.interface_name for m in measurements] == ["eth0"]

    def test_no_interfaces_raises_collector_error(self, monkeypatch):
        collector = InterfaceCollector(MonitorSection(exclude_interfaces=["lo"]))
        monkeypatch.setattr(
            "psutil.net_io_counters", lambda pernic=True: {"lo": fake_counter(1, 1)}
        )
        with pytest.raises(CollectorError):
            collector.collect()

    def test_failure_is_returned_as_a_value_by_safe_collect(self, monkeypatch):
        collector = InterfaceCollector()
        monkeypatch.setattr(
            "psutil.net_io_counters",
            lambda pernic=True: (_ for _ in ()).throw(OSError("boom")),
        )
        data, error = collector.safe_collect()
        assert data is None
        assert error and error.startswith("CollectorError")
        assert "boom" in error


def test_summarise_rates_ignores_missing_rates():
    measurements = [
        make_measurement(interface="eth0", upload_rate=100.0, download_rate=200.0),
        make_measurement(interface="wlan0", upload_rate=None, download_rate=None),
    ]
    upload, download = summarise_rates(measurements)
    assert (upload, download) == (100.0, 200.0)
