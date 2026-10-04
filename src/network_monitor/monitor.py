"""The monitoring loop.

This is the only module that knows the full shape of a collection cycle:

    COLLECT -> NORMALIZE -> STORE -> DETECT -> EXPLAIN -> NOTIFY -> DISPLAY

Two properties matter more than anything else here:

1. **Failure isolation.** Every collector runs inside its own try/except. A
   broken ``Get-NetTCPConnection`` on a locked-down machine degrades the
   connection features; it does not stop interface metering.
2. **Honest separation.** Interface measurements answer "how much traffic",
   connections answer "who is talking". The loop never derives per-process byte
   counts, because no reliable per-process accounting exists in this design.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from .api.state import MonitorState, build_state
from .collectors import (
    CollectorError,
    ConnectionCollector,
    InterfaceCollector,
    ProcessResolver,
    SystemCollector,
    attach_connection_states,
)
from .config import Config
from .models.events import Event
from .models.network import NetworkConnection, utc_now
from .models.process import ProcessInfo

logger = logging.getLogger(__name__)


class Monitor:
    """Owns the collectors, the storage writes and the detection cycle."""

    def __init__(self, config: Config, state: Optional[MonitorState] = None) -> None:
        self.config = config
        self.state = state or build_state(config)

        self.interface_collector = InterfaceCollector(config.monitor)
        self.connection_collector = ConnectionCollector()
        self.process_resolver = ProcessResolver()
        self.system_collector = SystemCollector()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_prune: Optional[datetime] = None
        #: Identity of the last stored connection snapshot. Snapshots are only
        #: written when the set of connections actually changes, which keeps an
        #: idle machine from writing thousands of identical rows per hour.
        self._last_connection_keys: Optional[frozenset] = None

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        """Start the background loop and the notification worker."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        if self.state.notifications:
            self.state.notifications.start()
        self._thread = threading.Thread(target=self.run_forever, name="monitor-loop", daemon=True)
        self._thread.start()
        logger.info("monitoring loop started (interval %.2fs)", self.config.monitor.collection_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self.state.notifications:
            self.state.notifications.stop()
        logger.info("monitoring loop stopped")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_forever(self) -> None:
        """Blocking loop; normally executed on the background thread."""
        self._initialise()
        interval = self.config.monitor.collection_interval
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("unexpected error in monitoring cycle")
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.0, interval - elapsed))

    def _initialise(self) -> None:
        """One-time setup: interface inventory and detection baselines."""
        try:
            interfaces = self.interface_collector.list_interfaces()
            self.state.set_interface_info(interfaces)
            logger.info(
                "detected %d interface(s): %s",
                len(interfaces),
                ", ".join(item.name for item in interfaces) or "none",
            )
        except CollectorError as exc:
            logger.warning("interface inventory failed: %s", exc)
            self.state.health.record_failure("interface_inventory", utc_now(), str(exc))

        if self.config.detection.seed_processes_on_start:
            connections, error = self._safe_collect_connections()
            if error is None and connections:
                self.state.detection.seed_processes_from_connections(connections)
                logger.info(
                    "seeded detection baseline with %d already-running network process(es)",
                    len(self.state.detection.seen_pids),
                )

    # ---- one cycle ---------------------------------------------------
    def cycle(self) -> List[Event]:
        """Execute one full collection/detection cycle. Returns the events."""
        cycle_started = time.monotonic()
        timestamp = utc_now()
        errors: List[str] = []

        # 1. COLLECT ---------------------------------------------------
        measurements, error = self._safe_collect(
            "interface", self.interface_collector.collect
        )
        if error:
            errors.append(f"interface: {error}")
        measurements = measurements or []

        connections, error = self._safe_collect(
            "connections", self.connection_collector.collect
        )
        if error:
            errors.append(f"connections: {error}")
        connections = connections or []

        processes, error = self._safe_collect("processes", self.process_resolver.collect)
        if error:
            errors.append(f"processes: {error}")
        processes = processes or []

        # The system collector is cheap and runs every cycle, so that a healthy
        # monitor never looks stale to the COLLECTOR_FAILURE rule.
        system, sys_error = self._safe_collect("system", self.system_collector.collect)
        if sys_error:
            errors.append(f"system: {sys_error}")
        elif system is not None:
            attach_connection_states(system, connections)
            self.state.latest_system = system

        # 2. NORMALIZE -------------------------------------------------
        self._attach_process_names(connections, processes)

        # 3. STORE -----------------------------------------------------
        try:
            if measurements:
                self.state.measurements.add_many(measurements)
            self._store_connection_snapshot(connections)
            resolved = [p for p in processes if p.resolved]
            if resolved:
                self.state.processes.upsert_many(resolved)
        except Exception as exc:  # noqa: BLE001 - storage failure is recorded, not fatal
            logger.exception("failed to persist cycle data")
            errors.append(f"storage: {type(exc).__name__}: {exc}")

        # 4. DETECT ----------------------------------------------------
        events: List[Event] = []
        try:
            events = self.state.detection.evaluate(
                timestamp=timestamp,
                measurements=measurements,
                connections=connections,
                processes=processes,
                collector_health=self.state.health,
            )
        except Exception:  # noqa: BLE001 - detection must not kill the loop
            logger.exception("detection engine failed")

        # 5. STORE EVENTS + NOTIFY -------------------------------------
        if events:
            try:
                self.state.events.add_many(events)  # sets event.id in place
            except Exception:  # noqa: BLE001
                logger.exception("failed to persist events")
            for event in events:
                logger.info("event: %s", event.summary_line())
            if self.state.notifications:
                self.state.notifications.handle_events(events)

        # 6. PUBLISH ---------------------------------------------------
        duration_ms = (time.monotonic() - cycle_started) * 1000
        self.state.publish_cycle(
            timestamp=timestamp,
            measurements=measurements,
            connections=connections,
            processes=processes,
            duration_ms=duration_ms,
            errors=errors,
        )
        self._persist_health()
        self._maybe_prune(timestamp)

        logger.debug(
            "cycle #%d: %d measurement(s), %d connection(s), %d process(es), %d event(s) in %.1fms",
            self.state.cycle_count,
            len(measurements),
            len(connections),
            len(processes),
            len(events),
            duration_ms,
        )
        return events

    # ---- helpers -----------------------------------------------------
    def _safe_collect(self, name: str, func) -> tuple[Optional[list], Optional[str]]:
        """Run a collector, recording success/failure in the health registry."""
        started = time.monotonic()
        timestamp = utc_now()
        try:
            data = func()
        except Exception as exc:  # noqa: BLE001 - this is the isolation boundary
            duration_ms = (time.monotonic() - started) * 1000
            message = str(exc) if isinstance(exc, CollectorError) else f"{type(exc).__name__}: {exc}"
            self.state.health.record_failure(name, timestamp, message, duration_ms)
            logger.warning("collector '%s' failed: %s", name, message)
            return None, message

        duration_ms = (time.monotonic() - started) * 1000
        self.state.health.record_success(name, timestamp, duration_ms)
        return list(data) if isinstance(data, (list, tuple)) else data, None

    def _store_connection_snapshot(self, connections: Sequence[NetworkConnection]) -> None:
        """Persist the connection table only when it changed.

        The in-memory snapshot served to the dashboard is always live; the
        database is history. Storing an identical set of connections every
        second would add no information and would swamp the file, so an
        unchanged set is skipped and the previous snapshot remains the record
        of "these connections were open".
        """
        keys = frozenset(connection.dedupe_key() for connection in connections)
        if not connections:
            # Nothing to write: the live view already reports zero connections,
            # and the earlier snapshot stays in history as what was observed.
            self._last_connection_keys = keys
            return

        if keys == self._last_connection_keys:
            logger.debug("connection snapshot unchanged; not stored again")
            return
        self._last_connection_keys = keys
        self.state.connections.add_snapshot(connections)

    def _safe_collect_connections(self) -> tuple[List[NetworkConnection], Optional[str]]:
        data, error = self._safe_collect("connections", self.connection_collector.collect)
        return (data or []), error

    def _attach_process_names(
        self, connections: Sequence[NetworkConnection], processes: Sequence[ProcessInfo]
    ) -> None:
        """Fill ``process_name`` on connections from resolved process info."""
        index = {process.pid: process for process in processes}
        for connection in connections:
            if connection.pid is None:
                connection.process_name = connection.process_name or "unknown"
                continue
            process = index.get(connection.pid)
            if process is None:
                resolved = self.process_resolver.resolve(connection.pid)
                process = resolved
                index[connection.pid] = resolved
            if process.name:
                connection.process_name = process.name
            elif process.error:
                connection.process_name = f"<{process.error}>"
            else:
                connection.process_name = "unknown"

    def _persist_health(self) -> None:
        if self.state.health_repository is None:
            return
        try:
            self.state.health_repository.upsert_many(self.state.health.statuses.values())
        except Exception:  # noqa: BLE001
            logger.debug("could not persist collector health", exc_info=True)

    def _maybe_prune(self, timestamp: datetime) -> None:
        """Apply the retention policy at most once per prune interval.

        Two windows are used: interface measurements and events live for
        ``retention_days``, connection snapshots - which are far more numerous -
        for ``connection_retention_hours``.
        """
        settings = self.config.database
        if settings.retention_days <= 0 and settings.connection_retention_hours <= 0:
            return
        interval = timedelta(seconds=settings.prune_interval_seconds)
        if self._last_prune is not None and timestamp - self._last_prune < interval:
            return
        self._last_prune = timestamp

        windows = []
        if settings.retention_days > 0:
            long_cutoff = timestamp - timedelta(days=settings.retention_days)
            windows.append((("interface_measurements", "timestamp"), long_cutoff))
            windows.append((("events", "timestamp"), long_cutoff))
        if settings.connection_retention_hours > 0:
            short_cutoff = timestamp - timedelta(hours=settings.connection_retention_hours)
            windows.append((("connections", "timestamp"), short_cutoff))

        deleted = 0
        for (table, column), cutoff in windows:
            try:
                deleted += self.state.database.prune(
                    table, column, cutoff.isoformat(timespec="milliseconds")
                )
            except Exception:  # noqa: BLE001
                logger.debug("prune of %s failed", table, exc_info=True)
        if deleted:
            logger.info("retention policy removed %d stale row(s)", deleted)

