"""Detection engine.

The engine owns everything stateful about detection so the rules stay pure:

* the rolling connection baseline (mean of the last N cycles)
* the set of PIDs already observed on the network
* the cooldown bookkeeping that stops a sustained condition from creating one
  event per second

It receives a :class:`RuleContext`, runs every enabled rule, suppresses events
that duplicate a recent one and returns what is left for storage, notification
and display.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Set

from ..config import DetectionSection
from ..models.events import Event, EventType
from ..models.health import CollectorHealthRegistry
from ..models.network import InterfaceMeasurement, NetworkConnection
from ..models.process import ProcessInfo
from .rules import Rule, RuleContext, default_rules

logger = logging.getLogger(__name__)

#: How many cycles feed the connection baseline (~2 minutes at 1 Hz).
BASELINE_WINDOW = 120


class DetectionEngine:
    """Runs the rule set and manages detection state."""

    def __init__(
        self,
        config: DetectionSection | None = None,
        rules: Optional[Sequence[Rule]] = None,
        baseline_window: int = BASELINE_WINDOW,
    ) -> None:
        self.config = config or DetectionSection()
        self.rules: List[Rule] = list(rules) if rules is not None else default_rules(self.config)
        self._connection_history: Deque[int] = deque(maxlen=max(1, baseline_window))
        self._seen_pids: Set[int] = set()
        self._last_emitted: Dict[tuple, datetime] = {}
        self._started_at: Optional[datetime] = None

    # ---- lifecycle ---------------------------------------------------
    def seed_processes(self, pids: Iterable[int]) -> None:
        """Mark PIDs as already known (used on startup)."""
        self._seen_pids.update(int(pid) for pid in pids if pid)
        logger.debug("seeded %d known network PIDs", len(self._seen_pids))

    def seed_processes_from_connections(self, connections: Iterable[NetworkConnection]) -> None:
        """Seed the baseline from the current connection table."""
        self.seed_processes(c.pid for c in connections if c.pid is not None)

    @property
    def seen_pids(self) -> Set[int]:
        return set(self._seen_pids)

    @property
    def baseline(self) -> float:
        if not self._connection_history:
            return 0.0
        return sum(self._connection_history) / len(self._connection_history)

    def reset(self) -> None:
        self._connection_history.clear()
        self._seen_pids.clear()
        self._last_emitted.clear()
        self._started_at = None

    # ---- evaluation --------------------------------------------------
    def evaluate(
        self,
        timestamp: datetime,
        measurements: Sequence[InterfaceMeasurement] = (),
        connections: Sequence[NetworkConnection] = (),
        processes: Sequence[ProcessInfo] = (),
        collector_health: Optional[CollectorHealthRegistry] = None,
    ) -> List[Event]:
        """Run every rule for one cycle and return the events to act on."""
        if self._started_at is None:
            self._started_at = timestamp

        statuses = (
            {name: status for name, status in collector_health.statuses.items()}
            if collector_health is not None
            else {}
        )
        context = RuleContext(
            timestamp=timestamp,
            measurements=list(measurements),
            connections=list(connections),
            processes=list(processes),
            collector_status=statuses,
            connection_baseline=self.baseline,
            seen_pids=set(self._seen_pids),
        )

        candidates: List[Event] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            try:
                candidates.extend(rule.evaluate(context))
            except Exception:  # noqa: BLE001 - one broken rule must not stop others
                logger.exception("detection rule %s failed", rule.name)

        # A PID is "seen" as soon as it has been evaluated, whether or not the
        # event survived the cooldown filter, otherwise suppression would make
        # the same process look new on every cycle.
        self._seen_pids.update(int(c.pid) for c in context.connections if c.pid is not None)
        self._connection_history.append(len(context.connections))

        events = self._apply_cooldown(candidates, timestamp)
        if events:
            logger.info(
                "detection produced %d event(s): %s",
                len(events),
                ", ".join(e.event_type.value for e in events),
            )
        return events

    # ---- cooldown ----------------------------------------------------
    def _apply_cooldown(self, events: Sequence[Event], timestamp: datetime) -> List[Event]:
        window = self.config.event_cooldown_seconds
        if window <= 0:
            return list(events)

        cutoff = timestamp - timedelta(seconds=window)
        # Drop bookkeeping for keys that expired long ago.
        self._last_emitted = {
            key: seen
            for key, seen in self._last_emitted.items()
            if seen >= cutoff - timedelta(seconds=window)
        }

        emitted: List[Event] = []
        for event in events:
            key = (
                event.event_type.value,
                event.interface_name,
                event.pid,
                event.source,
            )
            last = self._last_emitted.get(key)
            if last is not None and (timestamp - last).total_seconds() < window:
                logger.debug("suppressed duplicate %s event (cooldown)", event.event_type.value)
                continue
            self._last_emitted[key] = timestamp
            emitted.append(event)
        return emitted

    # ---- introspection -----------------------------------------------
    def rule_summary(self) -> List[Dict[str, object]]:
        """Rule list for logs and tests (thresholds included for display)."""
        return [
            {
                "name": rule.name,
                "event_type": rule.event_type.value,
                "severity": rule.severity.value,
                "enabled": rule.enabled,
            }
            for rule in self.rules
        ]

    def event_types(self) -> List[EventType]:
        return [rule.event_type for rule in self.rules]
