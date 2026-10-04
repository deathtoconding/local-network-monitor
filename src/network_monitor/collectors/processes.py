"""Process resolver.

Turns the PIDs found in the TCP table into human-readable process information
and never lets a dead, inaccessible or weird process break a collection cycle.
Every failure mode in the spec is handled explicitly:

* process disappeared between collection steps -> ``error="no_such_process"``
* invalid / out-of-range PID               -> ``error="no_such_process"``
* access denied                            -> ``error="access_denied"``
* anything else                            -> ``error="error"``

Resolved entries are cached briefly (default 5 seconds) because the same PID
usually owns several sockets and ``psutil.Process(...).exe()`` is comparatively
expensive.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional

import psutil

from ..models.network import utc_now
from ..models.process import ProcessInfo
from .base import Collector, CollectorError

logger = logging.getLogger(__name__)

#: Default time-to-live of a cached process entry.
DEFAULT_CACHE_TTL_SECONDS = 5.0


class ProcessResolver(Collector[List[ProcessInfo]]):
    """Resolves PIDs to :class:`ProcessInfo` objects."""

    name = "processes"

    def __init__(self, cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._cache_ttl = cache_ttl_seconds
        self._cache: Dict[int, tuple[float, ProcessInfo]] = {}

    # ---- collection --------------------------------------------------
    def collect(self) -> List[ProcessInfo]:
        """Resolve every process that currently owns a network connection.

        Uses ``psutil.net_connections`` only to discover the PID set; the
        per-process details come from :meth:`resolve`.
        """
        try:
            connections = psutil.net_connections(kind="tcp")
        except psutil.AccessDenied as exc:
            raise CollectorError(
                "access denied while enumerating connection owners", collector=self.name
            ) from exc
        except Exception as exc:  # pragma: no cover - platform dependent
            raise CollectorError(f"cannot enumerate connection owners: {exc}", collector=self.name) from exc

        pids = sorted({c.pid for c in connections if c.pid})
        return self.resolve_many(pids)

    def resolve_many(self, pids: Iterable[int]) -> List[ProcessInfo]:
        """Resolve a collection of PIDs, counting connections per process."""
        counts: Dict[int, int] = {}
        for pid in pids:
            counts[pid] = counts.get(pid, 0) + 1
        return [self.resolve(pid, connection_count=count) for pid, count in sorted(counts.items())]

    # ---- single process ---------------------------------------------
    def resolve(self, pid: int, connection_count: int = 0) -> ProcessInfo:
        """Resolve one PID, using the cache when possible."""
        cached = self._from_cache(pid)
        if cached is not None:
            cached.last_seen = utc_now()
            cached.connection_count = connection_count
            return cached

        info = self._inspect(pid, connection_count)
        if info.name:  # only cache successful resolutions
            self._cache[pid] = (time.monotonic(), info)
        return info

    def _inspect(self, pid: int, connection_count: int) -> ProcessInfo:
        info = ProcessInfo(pid=pid, last_seen=utc_now(), connection_count=connection_count)
        if not isinstance(pid, int) or pid <= 0:
            info.error = "no_such_process"
            return info

        try:
            process = psutil.Process(pid)
            with process.oneshot():
                info.name = process.name()
                info.status = process.status()
                info.pid = process.pid
                try:
                    info.executable = process.exe()
                except (psutil.AccessDenied, OSError):
                    info.executable = None
                try:
                    info.created_at = process.create_time() and _from_epoch(process.create_time())
                except (psutil.AccessDenied, OSError):
                    info.created_at = None
        except psutil.NoSuchProcess:
            info.error = "no_such_process"
        except psutil.AccessDenied:
            info.error = "access_denied"
            info.name = None
        except psutil.ZombieProcess:
            info.error = "zombie"
        except (ValueError, TypeError, OSError) as exc:
            logger.debug("cannot resolve pid %s: %s", pid, exc)
            info.error = "error"
        return info

    # ---- cache -------------------------------------------------------
    def _from_cache(self, pid: int) -> Optional[ProcessInfo]:
        entry = self._cache.get(pid)
        if entry is None:
            return None
        stored_at, info = entry
        if time.monotonic() - stored_at > self._cache_ttl:
            self._cache.pop(pid, None)
            return None
        return ProcessInfo(**{**info.__dict__})

    def clear_cache(self) -> None:
        self._cache.clear()

    def invalidate(self, pid: int) -> None:
        self._cache.pop(pid, None)

    # ---- helpers -----------------------------------------------------
    def name_for(self, pid: Optional[int]) -> Optional[str]:
        """Convenience wrapper returning just the process name (or ``None``)."""
        if pid is None:
            return None
        return self.resolve(pid).name


def _from_epoch(epoch_seconds: float):
    """Convert a POSIX timestamp into an aware datetime."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def attach_process_names(
    connections: List["object"], resolver: ProcessResolver
) -> List["object"]:
    """Fill ``process_name`` on every connection in place, returning the list."""
    for connection in connections:
        pid = getattr(connection, "pid", None)
        if pid is None:
            continue
        name = resolver.name_for(pid)
        if name:
            connection.process_name = name  # type: ignore[attr-defined]
        else:
            info = resolver.resolve(pid)
            connection.process_name = f"<{info.error}>" if info.error else None  # type: ignore[attr-defined]
    return connections
