"""Collector contract.

Every collector implements the same tiny interface so the monitoring loop can
treat them uniformly and isolate their failures:

    collect()  ->  data            (raises CollectorError on failure)
    name       ->  stable string used for health reporting

A collector must never call ``sys.exit``, never block forever and never assume
that a previous call succeeded. The loop wraps every call in try/except and
records the outcome in the health registry, which is what makes the monitor
survive an individual collector failure.
"""

from __future__ import annotations

import abc
from typing import Any, Generic, List, TypeVar

T = TypeVar("T")


class CollectorError(RuntimeError):
    """Raised by a collector when it cannot produce data.

    The message is surfaced verbatim in logs, in the health registry and in
    COLLECTOR_FAILURE event evidence, so it should be short and human readable.
    """

    def __init__(self, message: str, *, collector: str | None = None) -> None:
        super().__init__(message)
        self.collector = collector


class Collector(abc.ABC, Generic[T]):
    """Base class for all collectors."""

    #: Stable identifier, e.g. "interface" or "connections".
    name: str = "collector"

    @abc.abstractmethod
    def collect(self) -> T:
        """Return the current data or raise :class:`CollectorError`."""

    def safe_collect(self) -> tuple[T | None, str | None]:
        """Collect without raising; returns ``(data, error_message)``.

        Convenience for callers (and tests) that want the failure as a value
        rather than an exception.
        """
        try:
            return self.collect(), None
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            return None, f"{type(exc).__name__}: {exc}"


class MultiCollector(Collector[List[Any]]):
    """Collector that aggregates several sub-collectors.

    Used nowhere in production yet, but part of the contract: it demonstrates
    that a composite collector still fails softly, returning whatever the
    healthy sub-collectors produced.
    """

    def __init__(self, collectors: List[Collector[Any]]) -> None:
        self._collectors = collectors

    @property
    def name(self) -> str:  # type: ignore[override]
        return "multi"

    def collect(self) -> List[Any]:
        results: List[Any] = []
        errors: List[str] = []
        for collector in self._collectors:
            data, error = collector.safe_collect()
            if error:
                errors.append(f"{collector.name}: {error}")
            elif data is not None:
                results.extend(data if isinstance(data, list) else [data])
        if errors and not results:
            raise CollectorError("; ".join(errors), collector=self.name)
        return results
