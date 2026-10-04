"""Interface collector: cumulative counters + rate calculation.

This is the measurement backbone of the MVP. It reads
``psutil.net_io_counters(pernic=True)`` once per collection cycle and turns the
cumulative counters into bytes-per-second rates.

Rate calculation rules (see Milestone 1 acceptance criteria):

* the first sample for an interface only establishes a baseline (rate ``None``)
* ``rate = (current - previous) / elapsed_seconds``
* a *decreasing* counter means the OS counter was reset or the interface was
  restarted: the negative delta is discarded, ``0.0`` is reported for that
  cycle, and a new baseline is established
* rates are never negative
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import psutil

from ..config import MonitorSection
from ..models.network import InterfaceInfo, InterfaceMeasurement, utc_now
from .base import Collector, CollectorError

logger = logging.getLogger(__name__)


@dataclass
class _Baseline:
    """Previous counter value and the moment it was read."""

    timestamp: datetime
    value: float


class RateCalculator:
    """Turns monotonically increasing counters into per-second rates.

    One instance per counter domain (bytes sent, bytes received, ...), or one
    instance keyed by ``(interface, metric)`` as used by the interface
    collector. The class is intentionally free of psutil and datetime.now()
    side effects so it can be unit tested with controlled inputs.
    """

    def __init__(self, minimum_elapsed: float = 0.001) -> None:
        self._baselines: Dict[str, _Baseline] = {}
        self._minimum_elapsed = minimum_elapsed
        self.reset_count = 0

    def update(self, key: str, timestamp: datetime, value: float) -> Optional[float]:
        """Feed a counter reading and get the rate in units/second.

        Returns ``None`` when there is no previous reading to compare against.
        """
        previous = self._baselines.get(key)
        self._baselines[key] = _Baseline(timestamp=timestamp, value=value)

        if previous is None:
            return None

        elapsed = (timestamp - previous.timestamp).total_seconds()
        if elapsed < self._minimum_elapsed:
            # Two samples inside the same clock tick: keep the new baseline but
            # do not report a bogus rate.
            return None

        delta = value - previous.value
        if delta < 0:
            # Counter reset / interface restart: discard the negative delta and
            # continue from the new baseline. This is the documented behaviour
            # for a counter that went backwards.
            self.reset_count += 1
            logger.info(
                "counter reset detected for %s (%.0f -> %.0f); establishing new baseline",
                key,
                previous.value,
                value,
            )
            return 0.0

        return delta / elapsed

    def reset(self, key: str | None = None) -> None:
        """Forget baselines (all of them, or a single key)."""
        if key is None:
            self._baselines.clear()
        else:
            self._baselines.pop(key, None)

    def has_baseline(self, key: str) -> bool:
        return key in self._baselines


class InterfaceCollector(Collector[List[InterfaceMeasurement]]):
    """Collects per-interface counters and derives upload/download rates."""

    name = "interface"

    def __init__(self, monitor_config: MonitorSection | None = None) -> None:
        self._config = monitor_config or MonitorSection()
        self._rates = RateCalculator()

    # ---- interface inventory ----------------------------------------
    def list_interfaces(self) -> List[InterfaceInfo]:
        """Describe the interfaces known to the operating system."""
        try:
            stats = psutil.net_if_stats()
            addresses = psutil.net_if_addrs()
        except Exception as exc:  # pragma: no cover - platform dependent
            raise CollectorError(
                f"cannot enumerate interfaces: {exc}", collector=self.name
            ) from exc

        interfaces: List[InterfaceInfo] = []
        for name in sorted(stats.keys() | addresses.keys()):
            stat = stats.get(name)
            addr_list = addresses.get(name, [])
            addresses_str = [entry.address for entry in addr_list]
            interfaces.append(
                InterfaceInfo(
                    name=name,
                    is_up=bool(stat.isup) if stat else False,
                    speed_mbps=int(stat.speed) if stat and stat.speed else 0,
                    mtu=int(getattr(stat, "mtu", 0) or 0) if stat else 0,
                    addresses=addresses_str,
                    is_loopback=is_loopback_interface(name, addresses_str),
                )
            )
        return interfaces

    # ---- collection --------------------------------------------------
    def collect(self) -> List[InterfaceMeasurement]:
        """Return one measurement per interface, rates included."""
        timestamp = utc_now()
        counters = self._read_counters()

        measurements: List[InterfaceMeasurement] = []
        for name, counter in sorted(counters.items()):
            if self._config.is_excluded(name):
                continue

            measurement = InterfaceMeasurement(
                timestamp=timestamp,
                interface_name=name,
                bytes_sent=int(counter.bytes_sent),
                bytes_received=int(counter.bytes_recv),
                packets_sent=int(counter.packets_sent),
                packets_received=int(counter.packets_recv),
                errors_in=int(counter.errin),
                errors_out=int(counter.errout),
                drops_in=int(counter.dropin),
                drops_out=int(counter.dropout),
                upload_rate=self._rates.update(f"{name}:bytes_sent", timestamp, counter.bytes_sent),
                download_rate=self._rates.update(
                    f"{name}:bytes_recv", timestamp, counter.bytes_recv
                ),
            )
            measurements.append(measurement)

        if not measurements:
            raise CollectorError("no usable network interfaces found", collector=self.name)
        return measurements

    def _read_counters(self) -> Dict[str, "psutil._common.snetio"]:  # type: ignore[name-defined]
        try:
            counters = psutil.net_io_counters(pernic=True)
        except Exception as exc:  # pragma: no cover - platform dependent
            raise CollectorError(f"net_io_counters failed: {exc}", collector=self.name) from exc
        if not counters:
            raise CollectorError("net_io_counters returned no interfaces", collector=self.name)
        return dict(counters)

    # ---- helpers -----------------------------------------------------
    def reset_baselines(self) -> None:
        """Drop all rate baselines (used when the loop restarts)."""
        self._rates.reset()


def is_loopback_interface(name: str, addresses: Iterable[str] = ()) -> bool:
    """Heuristic used to label and filter loopback interfaces on Windows/Linux."""
    lowered = name.strip().lower()
    if lowered in {"lo", "lo0"} or "loopback" in lowered:
        return True
    return any(addr.startswith("127.") or addr in {"::1"} for addr in addresses)


def socket_family(addr: object) -> str:
    """Best-effort name of the address family of a psutil address entry."""
    family = getattr(addr, "family", None)
    return getattr(family, "name", str(family))


def summarise_rates(measurements: Iterable[InterfaceMeasurement]) -> Tuple[float, float]:
    """Sum upload/download rates across measurements, ignoring ``None``."""
    upload = sum(m.upload_rate or 0.0 for m in measurements)
    download = sum(m.download_rate or 0.0 for m in measurements)
    return upload, download
