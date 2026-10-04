"""TCP connection collector.

Primary source on Windows is PowerShell's ``Get-NetTCPConnection`` because it
exposes the owning process id directly (``OwningProcess``). Two fallbacks keep
the monitor useful everywhere:

* ``psutil.net_connections(kind="tcp")`` - cross-platform, needs privileges to
  see other users' sockets on Linux/macOS
* the ``netstat -ano`` text parser - present on every Windows install

All three produce the same normalised :class:`NetworkConnection` objects, so the
rest of the system never learns which source answered.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

import psutil

from ..models.network import NetworkConnection, utc_now
from .base import Collector, CollectorError

logger = logging.getLogger(__name__)

#: Hard timeout for any subprocess a collector spawns.
SUBPROCESS_TIMEOUT_SECONDS = 10

#: PowerShell script: emit normalised JSON for every TCP endpoint.
PS_SCRIPT = (
    "Get-NetTCPConnection | "
    "Select-Object @{n='local_address';e={$_.LocalAddress}},"
    "@{n='local_port';e={$_.LocalPort}},"
    "@{n='remote_address';e={$_.RemoteAddress}},"
    "@{n='remote_port';e={$_.RemotePort}},"
    "@{n='state';e={[string]$_.State}},"
    "@{n='pid';e={[int]$_.OwningProcess}} | "
    "ConvertTo-Json -Compress"
)


class ConnectionCollector(Collector[List[NetworkConnection]]):
    """Collects active TCP connections together with their owning PID.

    Constructor parameters exist so tests can inject fake sources instead of
    depending on the host operating system:

    ``tcp_table_reader``  - callable returning psutil connection tuples
    ``ps_provider``       - callable returning the PowerShell JSON payload
    ``netstat_provider``  - callable returning ``netstat -ano`` text
    """

    name = "connections"

    def __init__(
        self,
        tcp_table_reader: Callable[[], List[Any]] | None = None,
        ps_provider: Callable[[], str] | None = None,
        netstat_provider: Callable[[], str] | None = None,
        include_listening: bool = True,
        platform_name: str | None = None,
    ) -> None:
        self._tcp_table_reader = tcp_table_reader or self._read_tcp_table
        self._ps_provider = ps_provider or self._read_get_nettcpconnection
        self._netstat_provider = netstat_provider or self._read_netstat
        self._include_listening = include_listening
        #: Overridable so tests can exercise the Windows code paths on Linux.
        self.platform_name = platform_name or sys.platform
        #: Where the data of the last successful collection came from.
        self.last_source: Optional[str] = None

    @property
    def is_windows(self) -> bool:
        return self.platform_name.startswith("win")

    # ---- collection --------------------------------------------------
    def collect(self) -> List[NetworkConnection]:
        timestamp = utc_now()
        errors: List[str] = []
        sources: List[tuple[str, Callable[[], List[NetworkConnection]]]] = []

        if self.is_windows:
            sources.append(("powershell", lambda: self._collect_via_powershell(timestamp)))
        sources.append(("psutil", lambda: self._collect_via_psutil(timestamp)))
        if self.is_windows:
            sources.append(("netstat", lambda: self._collect_via_netstat(timestamp)))

        for source_name, source in sources:
            try:
                connections = source()
            except Exception as exc:  # noqa: BLE001 - try the next source
                message = f"{source_name}: {type(exc).__name__}: {exc}"
                logger.warning("connection source failed -> %s", message)
                errors.append(message)
                continue

            self.last_source = source_name
            logger.debug("collected %d connections via %s", len(connections), source_name)
            return connections

        raise CollectorError(
            "all connection sources failed: " + "; ".join(errors) if errors else "no source available",
            collector=self.name,
        )

    # ---- source: PowerShell ------------------------------------------
    def _collect_via_powershell(self, timestamp) -> List[NetworkConnection]:
        payload = self._ps_provider()
        if not payload.strip():
            return []
        return self.parse_powershell_output(payload, timestamp)

    def _read_get_nettcpconnection(self) -> str:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise CollectorError("powershell executable not found", collector=self.name)
        try:
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    PS_SCRIPT,
                ],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
                shell=False,  # never invoke through a shell
            )
        except subprocess.TimeoutExpired as exc:
            raise CollectorError(
                f"Get-NetTCPConnection timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
                collector=self.name,
            ) from exc
        except OSError as exc:
            raise CollectorError(f"cannot run powershell: {exc}", collector=self.name) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip().splitlines()
            detail = stderr[-1] if stderr else f"exit code {completed.returncode}"
            raise CollectorError(f"Get-NetTCPConnection failed: {detail}", collector=self.name)
        return completed.stdout or ""

    @staticmethod
    def parse_powershell_output(payload: str, timestamp) -> List[NetworkConnection]:
        """Parse ``Get-NetTCPConnection | ConvertTo-Json`` output."""
        return parse_powershell_output(payload, timestamp)

    # ---- source: psutil ----------------------------------------------
    def _collect_via_psutil(self, timestamp) -> List[NetworkConnection]:
        raw = self._tcp_table_reader()
        connections: List[NetworkConnection] = []
        for entry in raw:
            connection = normalize_psutil_connection(entry, timestamp)
            if connection is not None:
                connections.append(connection)
        if not self._include_listening:
            connections = [c for c in connections if c.state != "LISTEN"]
        return connections

    @staticmethod
    def _read_tcp_table() -> List[Any]:
        try:
            return list(psutil.net_connections(kind="tcp"))
        except psutil.AccessDenied as exc:
            raise CollectorError(
                "access denied reading the TCP table (run as administrator "
                "or enable the interface collector only)",
                collector="connections",
            ) from exc
        except Exception as exc:  # pragma: no cover - platform dependent
            raise CollectorError(f"net_connections failed: {exc}", collector="connections") from exc

    # ---- source: netstat ---------------------------------------------
    def _collect_via_netstat(self, timestamp) -> List[NetworkConnection]:
        return parse_netstat_output(self._netstat_provider(), timestamp)

    @staticmethod
    def _read_netstat() -> str:
        netstat = shutil.which("netstat")
        if not netstat:
            raise CollectorError("netstat executable not found", collector="connections")
        try:
            completed = subprocess.run(
                [netstat, "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise CollectorError(f"netstat failed: {exc}", collector="connections") from exc
        if completed.returncode != 0:
            raise CollectorError(
                f"netstat exited with code {completed.returncode}", collector="connections"
            )
        return completed.stdout or ""


def parse_powershell_output(payload: str, timestamp) -> List[NetworkConnection]:
    """Parse ``Get-NetTCPConnection | ConvertTo-Json`` output.

    Handles the three shapes PowerShell can emit: a JSON array (the normal
    case), a single object (when exactly one connection matches) and an empty
    document (no connections at all). Malformed entries are skipped rather than
    failing the whole collection - a single odd socket must not blind the
    dashboard.
    """
    if not payload or not payload.strip():
        return []

    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise CollectorError(f"cannot parse Get-NetTCPConnection JSON: {exc}") from exc

    if parsed is None or parsed == "":
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise CollectorError("unexpected Get-NetTCPConnection payload shape")

    connections: List[NetworkConnection] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        # An entry without an owning process or a local address is not usable
        # as a connection: skip it instead of inventing placeholder values.
        if entry.get("pid") is None or not entry.get("local_address"):
            continue
        try:
            connections.append(
                NetworkConnection(
                    timestamp=timestamp,
                    protocol="TCP",
                    local_address=str(entry.get("local_address") or ""),
                    local_port=int(entry.get("local_port") or 0),
                    remote_address=str(entry.get("remote_address") or ""),
                    remote_port=int(entry.get("remote_port") or 0),
                    state=str(entry.get("state") or "UNKNOWN").upper(),
                    pid=int(entry["pid"]) if entry.get("pid") is not None else None,
                )
            )
        except (TypeError, ValueError) as exc:
            logger.debug("skipping malformed connection entry %r: %s", entry, exc)
    return connections


#: Windows netstat state names -> the names psutil/Get-NetTCPConnection use.
NETSTAT_STATE_MAP = {
    "LISTENING": "LISTEN",
    "ESTABLISHED": "ESTABLISHED",
    "TIME_WAIT": "TIME_WAIT",
    "CLOSE_WAIT": "CLOSE_WAIT",
    "SYN_SENT": "SYN_SENT",
    "SYN_RECEIVED": "SYN_RECV",
    "FIN_WAIT_1": "FIN_WAIT1",
    "FIN_WAIT_2": "FIN_WAIT2",
    "LAST_ACK": "LAST_ACK",
    "CLOSING": "CLOSING",
    "CLOSED": "CLOSE",
    "DELETE_TCB": "DELETE_TCB",
}

_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"
_IPV6 = r"[0-9A-Fa-f:]+"
_ENDPOINT = re.compile(rf"^(?P<address>{_IPV4}|{_IPV6}|\[{_IPV6}\]|\*):(?P<port>\d+|\*)$")
_NETSTAT_ROW = re.compile(
    r"^\s*(?P<protocol>TCP)\s+(?P<local>\S+)\s+(?P<remote>\S+)\s+"
    r"(?P<state>[A-Z_]+)\s+(?P<pid>\d+)\s*$",
    re.IGNORECASE,
)


def split_endpoint(value: str) -> tuple[str, int]:
    """Split ``"142.250.27.100:443"`` / ``"[::1]:443"`` into address and port."""
    match = _ENDPOINT.match(value.strip())
    if not match:
        return value.strip(), 0
    address = match.group("address").strip("[]")
    port = match.group("port")
    return address, int(port) if port != "*" else 0


def parse_netstat_output(payload: str, timestamp) -> List[NetworkConnection]:
    """Parse the output of ``netstat -ano -p TCP``.

    Empty, header-only and malformed payloads all yield an empty list (with a
    debug log) - a collector returning *no connections* is not an error.
    """
    connections: List[NetworkConnection] = []
    skipped = 0
    for line in payload.splitlines():
        if not line.strip() or line.lstrip().upper().startswith("PROTO"):
            continue
        match = _NETSTAT_ROW.match(line)
        if not match:
            if "TCP" in line.upper():
                skipped += 1
            continue
        local_address, local_port = split_endpoint(match.group("local"))
        remote_address, remote_port = split_endpoint(match.group("remote"))
        state = match.group("state").upper()
        connections.append(
            NetworkConnection(
                timestamp=timestamp,
                protocol="TCP",
                local_address=local_address,
                local_port=local_port,
                remote_address=remote_address,
                remote_port=remote_port,
                state=NETSTAT_STATE_MAP.get(state, state),
                pid=int(match.group("pid")),
            )
        )
    if skipped:
        logger.debug("netstat parser skipped %d unrecognised line(s)", skipped)
    return connections


def normalize_psutil_connection(entry: Any, timestamp) -> Optional[NetworkConnection]:
    """Convert a ``psutil`` connection namedtuple into our model."""
    try:
        local = getattr(entry, "laddr", None)
        remote = getattr(entry, "raddr", None)
        if local is None:
            local_address, local_port = "", 0
        elif isinstance(local, tuple):  # some platforms expose tuples
            local_address, local_port = local[0], local[1]
        else:
            local_address, local_port = local.ip, local.port

        if not remote:  # None or () - a listening socket has no peer
            remote_address, remote_port = "", 0
        elif isinstance(remote, tuple) and len(remote) == 2:
            remote_address, remote_port = remote[0], remote[1]
        else:
            remote_address, remote_port = remote.ip, remote.port
        family = getattr(entry, "family", None)
        protocol = "TCP6" if str(family) in {"AddressFamily.AF_INET6", "2"} else "TCP"
        return NetworkConnection(
            timestamp=timestamp,
            protocol=protocol,
            local_address=str(local_address),
            local_port=int(local_port),
            remote_address=str(remote_address),
            remote_port=int(remote_port),
            state=str(getattr(entry, "status", "UNKNOWN")).upper(),
            pid=getattr(entry, "pid", None),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        logger.debug("skipping malformed psutil connection %r: %s", entry, exc)
        return None


def parse_csv_connections(payload: str, timestamp) -> List[NetworkConnection]:
    """Parse ``Get-NetTCPConnection | ConvertTo-Csv`` output (utility/tests)."""
    reader = csv.DictReader(io.StringIO(payload))
    connections: List[NetworkConnection] = []
    for row in reader:
        normalised: Dict[str, Any] = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        try:
            connections.append(
                NetworkConnection(
                    timestamp=timestamp,
                    local_address=normalised.get("LocalAddress", ""),
                    local_port=int(normalised.get("LocalPort") or 0),
                    remote_address=normalised.get("RemoteAddress", ""),
                    remote_port=int(normalised.get("RemotePort") or 0),
                    state=normalised.get("State", "UNKNOWN").upper(),
                    pid=int(normalised["OwningProcess"]) if normalised.get("OwningProcess") else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return connections
