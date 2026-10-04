"""Notification manager.

Responsibility: decide *whether* an event should be dispatched and hand it to
the registered notifiers. It never knows how a notification is delivered, and
detection never knows that notifications exist at all.

Delivery happens on a background worker thread fed by a bounded queue, so a slow
SMTP server can never stall the monitoring loop - the requirement from
Milestone 8 ("generate an email without blocking the monitoring loop").
"""

from __future__ import annotations

import abc
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from ..config import NotificationsSection
from ..models.events import Event, EventSeverity

logger = logging.getLogger(__name__)


class Notifier(abc.ABC):
    """Anything that can deliver an event."""

    name: str = "notifier"

    @abc.abstractmethod
    def send(self, event: Event) -> bool:
        """Deliver one event. Return ``True`` on success; never raise."""

    def close(self) -> None:  # noqa: B027 - optional hook, not a requirement
        """Release resources (sockets, sessions).

        Deliberately concrete and empty: most notifiers have nothing to close,
        and forcing them to implement a no-op would be noise.
        """


@dataclass
class NotificationRecord:
    """Outcome of one delivery attempt, kept in memory for /api/status."""

    timestamp: datetime
    event_id: Optional[int]
    event_type: str
    severity: str
    notifier: str
    delivered: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_id": self.event_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "notifier": self.notifier,
            "delivered": self.delivered,
            "detail": self.detail,
        }


class NotificationManager:
    """Filters, deduplicates and asynchronously dispatches events."""

    def __init__(
        self,
        config: NotificationsSection | None = None,
        notifiers: Optional[Sequence[Notifier]] = None,
        queue_size: int = 200,
    ) -> None:
        self.config = config or NotificationsSection()
        self.notifiers: List[Notifier] = list(notifiers or [])
        self._queue: "queue.Queue[Optional[Event]]" = queue.Queue(maxsize=queue_size)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cooldowns: Dict[tuple, datetime] = {}
        self.history: List[NotificationRecord] = []
        self._history_limit = 200
        self.dispatched = 0
        self.suppressed = 0
        self.dropped = 0

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        """Start the delivery worker (no-op when notifications are disabled)."""
        if not self.config.enabled or not self.notifiers:
            logger.info(
                "notifications disabled or no notifier configured (enabled=%s, notifiers=%d)",
                self.config.enabled,
                len(self.notifiers),
            )
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="notifications", daemon=True
        )
        self._worker.start()
        logger.info("notification worker started (%d notifier(s))", len(self.notifiers))

    def stop(self, timeout: float = 2.0) -> None:
        if self._worker is None:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # wake the worker
        except queue.Full:  # pragma: no cover - queue full at shutdown
            pass
        self._worker.join(timeout=timeout)
        self._worker = None
        for notifier in self.notifiers:
            try:
                notifier.close()
            except Exception:  # noqa: BLE001 - shutdown best effort
                logger.debug("notifier %s failed to close", notifier.name, exc_info=True)

    # ---- dispatch ----------------------------------------------------
    def handle_events(self, events: Sequence[Event]) -> int:
        """Queue the events that pass the policy filter. Returns the count queued.

        Always returns quickly: this is called from the monitoring loop.
        """
        if not self.config.enabled:
            return 0
        min_rank = EventSeverity(self.config.min_severity).rank
        queued = 0
        for event in events:
            if event.severity.rank < min_rank:
                continue
            if self._is_cooled_down(event):
                self.suppressed += 1
                continue
            try:
                self._queue.put_nowait(event)
                queued += 1
                self.dispatched += 1
            except queue.Full:
                self.dropped += 1
                logger.warning(
                    "notification queue full; dropped %s event %s",
                    event.severity.value,
                    event.event_type.value,
                )
        return queued

    def _is_cooled_down(self, event: Event) -> bool:
        window = self.config.cooldown_seconds
        if window <= 0:
            return False
        key = (event.event_type.value, event.interface_name, event.pid)
        now = event.timestamp
        last = self._cooldowns.get(key)
        if last is not None and (now - last).total_seconds() < window:
            return True
        self._cooldowns[key] = now
        # Bound the map so a long-running monitor does not leak memory.
        if len(self._cooldowns) > 1_000:
            cutoff = now - timedelta(seconds=window * 2)
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v >= cutoff}
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if event is None:
                    break
                self._deliver(event)
            finally:
                self._queue.task_done()

    def _deliver(self, event: Event) -> None:
        for notifier in self.notifiers:
            delivered = False
            detail = ""
            try:
                delivered = bool(notifier.send(event))
            except Exception as exc:  # noqa: BLE001 - notifiers must never raise
                detail = f"{type(exc).__name__}: {exc}"
                logger.exception("notifier %s raised while sending event %s", notifier.name, event.id)
            record = NotificationRecord(
                timestamp=event.timestamp,
                event_id=event.id,
                event_type=event.event_type.value,
                severity=event.severity.value,
                notifier=notifier.name,
                delivered=delivered,
                detail=detail,
            )
            self.history.append(record)
            del self.history[: max(0, len(self.history) - self._history_limit)]

    # ---- introspection -----------------------------------------------
    def status(self) -> Dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "min_severity": self.config.min_severity,
            "notifiers": [notifier.name for notifier in self.notifiers],
            "queued": self._queue.qsize(),
            "dispatched": self.dispatched,
            "suppressed": self.suppressed,
            "dropped": self.dropped,
            "worker_running": bool(self._worker and self._worker.is_alive()),
            "recent": [record.to_dict() for record in self.history[-10:]],
        }
