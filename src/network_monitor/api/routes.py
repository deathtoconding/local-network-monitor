"""REST API routes.

The API is a read-mostly view over the state written by the monitoring loop.
Every endpoint degrades gracefully: when the monitor has not produced data yet
the response is an empty list or a zeroed snapshot with HTTP 200, because an
empty honest answer beats a 500 for the dashboard.

Timestamps are always ISO-8601 UTC. Rates are always bytes/second; megabit
values are provided next to them where a human is likely to read the number.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..models.events import EventSeverity, EventStatus, EventType
from ..models.network import from_iso, utc_now
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render_metrics
from .state import MonitorState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["monitoring"])


def get_state(request: Request) -> MonitorState:
    """Fetch the shared state object attached to the FastAPI app."""
    state = getattr(request.app.state, "monitor", None)
    if state is None:  # pragma: no cover - only reachable if app built wrongly
        raise HTTPException(status_code=503, detail="monitor state is not initialised")
    return state


def parse_datetime_param(value: Optional[str], parameter: str) -> Optional[datetime]:
    """Parse an ISO-8601 query parameter.

    Tolerates the shapes that turn up in practice: a trailing ``Z`` and an
    unencoded ``+`` offset, which a query string delivers as a space
    (``...T12:00:00 00:00``) because ``+`` means "space" in that context.
    """
    if value is None or value == "":
        return None
    candidate = value.strip()
    try:
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        elif re.search(r"\s\d{2}:\d{2}$", candidate):
            candidate = re.sub(r"\s(\d{2}:\d{2})$", r"+\1", candidate)
        parsed = from_iso(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid '{parameter}' timestamp: {value}"
        ) from exc
    return parsed


def default_window(hours: float = 1.0) -> datetime:
    return utc_now() - timedelta(hours=hours)


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------
@router.get("/status", summary="Monitor and collector status")
def get_status(request: Request) -> Dict[str, Any]:
    return get_state(request).status_payload()


@router.get("/health", summary="Liveness probe")
def health() -> Dict[str, str]:
    """Process-level liveness: is the API answering at all.

    Deliberately shallow - a liveness probe that fails when a collector hiccups
    would restart a healthy monitor. Use ``/api/ready`` for depth.
    """
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe")
def ready(request: Request) -> JSONResponse:
    """Deep readiness: database writable, loop fresh, notifier alive."""
    state = get_state(request)
    is_ready, checks = state.readiness()
    payload = {
        "ready": is_ready,
        "checks": checks,
        "cycles": state.cycle_count,
        "last_cycle_at": state.last_cycle_at.isoformat() if state.last_cycle_at else None,
    }
    return JSONResponse(status_code=200 if is_ready else 503, content=payload)


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}}},
)
def metrics(request: Request) -> PlainTextResponse:
    """Metrics in the Prometheus text exposition format."""
    return PlainTextResponse(render_metrics(get_state(request)), media_type=METRICS_CONTENT_TYPE)


# ----------------------------------------------------------------------
# Interfaces and traffic
# ----------------------------------------------------------------------
@router.get("/interfaces", summary="Detected network interfaces")
def get_interfaces(request: Request) -> List[Dict[str, Any]]:
    state = get_state(request)
    measurements = {m.interface_name: m for m in state.current_measurements()}
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for name, info in sorted(state.interface_info.items()):
        seen.add(name)
        measurement = measurements.get(name)
        entry = info.to_dict()
        entry.update(_measurement_view(measurement))
        result.append(entry)

    # Interfaces that have measurements but no inventory entry (e.g. the
    # inventory call failed) are still reported.
    for name, measurement in sorted(measurements.items()):
        if name in seen:
            continue
        entry = {"name": name, "is_up": None, "speed_mbps": 0, "mtu": 0, "addresses": []}
        entry.update(_measurement_view(measurement))
        result.append(entry)

    return result


def _measurement_view(measurement: Optional[Any]) -> Dict[str, Any]:
    if measurement is None:
        return {
            "bytes_sent": 0,
            "bytes_received": 0,
            "upload_bytes_per_second": 0.0,
            "download_bytes_per_second": 0.0,
            "errors_in": 0,
            "drops_in": 0,
            "timestamp": None,
        }
    upload = float(measurement.upload_rate or 0.0)
    download = float(measurement.download_rate or 0.0)
    return {
        "timestamp": measurement.timestamp.isoformat(),
        "bytes_sent": measurement.bytes_sent,
        "bytes_received": measurement.bytes_received,
        "packets_sent": measurement.packets_sent,
        "packets_received": measurement.packets_received,
        "errors_in": measurement.errors_in,
        "errors_out": measurement.errors_out,
        "drops_in": measurement.drops_in,
        "drops_out": measurement.drops_out,
        "upload_bytes_per_second": round(upload, 2),
        "download_bytes_per_second": round(download, 2),
        "upload_mbps": round(upload / 125_000, 3),
        "download_mbps": round(download / 125_000, 3),
    }


@router.get("/traffic", summary="Current aggregate traffic")
def get_traffic(request: Request) -> Dict[str, Any]:
    snapshot = get_state(request).traffic_snapshot()
    payload = snapshot.to_dict()
    payload["upload_mbps"] = round(snapshot.upload_bytes_per_second / 125_000, 3)
    payload["download_mbps"] = round(snapshot.download_bytes_per_second / 125_000, 3)
    return payload


@router.get("/traffic/history", summary="Historical traffic rates")
def get_traffic_history(
    request: Request,
    from_: Optional[str] = Query(None, alias="from", description="ISO-8601 UTC start"),
    to: Optional[str] = Query(None, description="ISO-8601 UTC end"),
    interface: Optional[str] = Query(None, description="Interface name filter"),
    limit: int = Query(500, ge=1, le=10_000),
) -> Dict[str, Any]:
    state = get_state(request)
    start = parse_datetime_param(from_, "from")
    end = parse_datetime_param(to, "to")
    points = state.traffic_history(start=start, end=end, interface=interface, limit=limit)
    return {
        "from": (start or points[0].timestamp if points else start).isoformat()
        if (start or points)
        else None,
        "to": (end or points[-1].timestamp if points else end).isoformat()
        if (end or points)
        else None,
        "interface": interface,
        "count": len(points),
        "points": [point.to_dict() for point in points],
    }


# ----------------------------------------------------------------------
# Connections and processes
# ----------------------------------------------------------------------
@router.get("/connections", summary="Active TCP connections of the latest cycle")
def get_connections(
    request: Request,
    process: Optional[str] = Query(None, description="Substring match on process name"),
    pid: Optional[int] = Query(None, description="Exact PID filter"),
    state_filter: Optional[str] = Query(None, alias="state", description="TCP state filter"),
    remote: Optional[str] = Query(None, description="Remote address or port substring"),
    limit: int = Query(1000, ge=1, le=10_000),
) -> Dict[str, Any]:
    monitor = get_state(request)
    connections = monitor.current_connections()

    if process or pid is not None or state_filter or remote:
        connections = [
            c
            for c in connections
            if (
                not process
                or (c.process_name or "").lower().find(process.lower()) >= 0
                or f"pid {c.pid}".find(process.lower()) >= 0
            )
            and (pid is None or c.pid == pid)
            and (not state_filter or c.state.upper() == state_filter.upper())
            and (
                not remote
                or remote.lower() in c.remote_address.lower()
                or str(c.remote_port).startswith(remote)
            )
        ]

    connections = connections[:limit]
    states: Dict[str, int] = {}
    for connection in connections:
        states[connection.state] = states.get(connection.state, 0) + 1

    return {
        "timestamp": monitor.last_cycle_at.isoformat() if monitor.last_cycle_at else None,
        "count": len(connections),
        "states": dict(sorted(states.items())),
        "connections": [connection.to_dict() for connection in connections],
    }


@router.get("/processes", summary="Processes currently owning network connections")
def get_processes(
    request: Request,
    limit: int = Query(500, ge=1, le=5_000),
) -> Dict[str, Any]:
    monitor = get_state(request)
    processes = monitor.current_processes()
    connections = monitor.current_connections()

    counts: Dict[int, int] = {}
    for connection in connections:
        if connection.pid is not None:
            counts[connection.pid] = counts.get(connection.pid, 0) + 1

    entries: List[Dict[str, Any]] = []
    for process in processes:
        payload = process.to_dict()
        payload["connection_count"] = counts.get(process.pid, process.connection_count)
        entries.append(payload)

    # A PID can own connections while being unresolvable right now (access
    # denied, process mid-exit). Fall back to what the process directory knows
    # before declaring it unknown: an explanatory name beats a blank one.
    known = {entry["pid"] for entry in entries}
    for pid, count in sorted(counts.items()):
        if pid in known:
            continue
        stored = monitor.processes.get(pid)
        entries.append(
            {
                "pid": pid,
                "name": stored.name if stored else None,
                "executable": stored.executable if stored else None,
                "created_at": stored.created_at.isoformat()
                if stored and stored.created_at
                else None,
                "last_seen": stored.last_seen.isoformat() if stored and stored.last_seen else None,
                "error": None if stored else "unresolved",
                "resolved": bool(stored),
                "connection_count": count,
            }
        )

    entries.sort(key=lambda entry: (-entry["connection_count"], str(entry.get("name") or "")))
    return {"count": len(entries), "processes": entries[:limit]}


@router.get("/system", summary="Host metrics collected with the monitor")
def get_system(request: Request) -> Dict[str, Any]:
    state = get_state(request)
    payload: Dict[str, Any] = {
        "uptime_seconds": round(state.uptime_seconds(), 1),
        "connection_states": {},
        "interfaces_tracked": len(state.interface_info),
        "storage_rows": state.database.table_counts(),
    }
    if state.latest_system is not None:
        payload.update(state.latest_system.to_dict())
        payload["uptime_seconds"] = round(state.uptime_seconds(), 1)
        payload["interfaces_tracked"] = len(state.interface_info)
        payload["storage_rows"] = state.database.table_counts()
    elif state.latest_connections:
        states: Dict[str, int] = {}
        for connection in state.latest_connections:
            states[connection.state] = states.get(connection.state, 0) + 1
        payload["connection_states"] = dict(sorted(states.items()))
    return payload


# ----------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------
@router.get("/events", summary="Detected events, newest first")
def get_events(
    request: Request,
    limit: int = Query(100, ge=1, le=1_000),
    offset: int = Query(0, ge=0),
    severity: Optional[str] = Query(None, pattern="^(info|warning|critical)$"),
    event_type: Optional[str] = Query(None, description="e.g. HIGH_DOWNLOAD"),
    status: Optional[str] = Query(None, pattern="^(open|acknowledged|resolved)$"),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
) -> Dict[str, Any]:
    state = get_state(request)
    if event_type:
        valid = {item.value for item in EventType}
        normalised = event_type.upper()
        if normalised not in valid:
            raise HTTPException(
                status_code=422,
                detail=f"unknown event_type '{event_type}'; expected one of {sorted(valid)}",
            )
        event_type = normalised

    events = state.events.list(
        limit=limit,
        offset=offset,
        severity=severity,
        event_type=event_type,
        status=status,
        since=parse_datetime_param(from_, "from"),
        until=parse_datetime_param(to, "to"),
    )
    return {
        "count": len(events),
        "limit": limit,
        "offset": offset,
        "counts_24h": state.event_counts(),
        "events": [event.to_dict() for event in events],
    }


@router.get("/events/{event_id}", summary="One event with its evidence")
def get_event(request: Request, event_id: int) -> Dict[str, Any]:
    event = get_state(request).events.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    return event.to_dict()


class EventStatusUpdate(BaseModel):
    """Payload for acknowledging / resolving an event."""

    status: EventStatus = Field(..., description="open | acknowledged | resolved")


@router.patch("/events/{event_id}", summary="Update the status of an event")
def update_event_status(
    request: Request, event_id: int, update: EventStatusUpdate
) -> Dict[str, Any]:
    repository = get_state(request).events
    if not repository.update_status(event_id, update.status):
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")
    event = repository.get(event_id)
    return {"updated": True, "event": event.to_dict() if event else None}


@router.get("/events/types", include_in_schema=False)
def event_types(request: Request) -> Dict[str, List[str]]:
    """Rule catalogue, handy for building filters in the dashboard."""
    return {
        "event_types": [item.value for item in EventType],
        "severities": [item.value for item in EventSeverity],
        "statuses": [item.value for item in EventStatus],
    }
