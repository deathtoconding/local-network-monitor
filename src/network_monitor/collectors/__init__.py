"""Collectors: each one observes one aspect of the machine.

All collectors implement the same contract (see :mod:`network_monitor.collectors.base`)
and are isolated from each other by the monitoring loop: one failing collector
degrades a feature, it does not stop the monitor.
"""

from .base import Collector, CollectorError, MultiCollector
from .connections import ConnectionCollector, parse_netstat_output, parse_powershell_output
from .interface import InterfaceCollector, RateCalculator, summarise_rates
from .processes import ProcessResolver, attach_process_names
from .system import (
    SystemCollector,
    SystemSnapshot,
    attach_connection_states,
    connection_state_summary,
    uptime_seconds,
)

__all__ = [
    "Collector",
    "CollectorError",
    "ConnectionCollector",
    "InterfaceCollector",
    "MultiCollector",
    "ProcessResolver",
    "RateCalculator",
    "SystemCollector",
    "SystemSnapshot",
    "attach_connection_states",
    "attach_process_names",
    "connection_state_summary",
    "parse_netstat_output",
    "parse_powershell_output",
    "summarise_rates",
    "uptime_seconds",
]
