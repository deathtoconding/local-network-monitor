"""Collector tests: Windows output parsing and the process resolver.

These run on any platform on purpose - the parsing of
``Get-NetTCPConnection``/``netstat`` output is pure string handling, and the
resolver is exercised against real PIDs of the test process itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import psutil
import pytest

from network_monitor.collectors import (
    ConnectionCollector,
    ProcessResolver,
    parse_netstat_output,
    parse_powershell_output,
)
from network_monitor.collectors.base import CollectorError
from network_monitor.collectors.connections import (
    PS_SCRIPT,
    normalize_psutil_connection,
    parse_csv_connections,
    split_endpoint,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

NETSTAT_OK = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1156
  TCP    192.168.1.20:51234     142.250.27.100:443     ESTABLISHED     8420
  TCP    127.0.0.1:50201        127.0.0.1:8000         TIME_WAIT       0
  TCP    [::1]:50202            [2606:4700::1111]:443  ESTABLISHED     9312
"""

NETSTAT_MALFORMED = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    garbage that will not parse
  UDP    127.0.0.1:1900         *:*                    -----           4321
"""


class TestNetstatParsing:
    def test_parses_valid_windows_output(self):
        connections = parse_netstat_output(NETSTAT_OK, T0)
        assert len(connections) == 4
        established = connections[1]
        assert established.pid == 8420
        assert established.state == "ESTABLISHED"
        assert established.remote_address == "142.250.27.100"
        assert established.remote_port == 443
        assert connections[0].state == "LISTEN"

    def test_parses_ipv6_endpoints(self):
        connection = parse_netstat_output(NETSTAT_OK, T0)[3]
        assert connection.local_address == "::1"
        assert connection.remote_address == "2606:4700::1111"
        assert connection.remote_port == 443

    def test_empty_output_is_not_an_error(self):
        assert parse_netstat_output("", T0) == []
        assert parse_netstat_output("Active Connections\n\n", T0) == []

    def test_malformed_rows_are_skipped(self):
        connections = parse_netstat_output(NETSTAT_MALFORMED, T0)
        assert all(connection.pid != 4321 for connection in connections)

    def test_time_wait_row_with_pid_zero(self):
        connection = parse_netstat_output(NETSTAT_OK, T0)[2]
        assert connection.pid == 0
        assert connection.state == "TIME_WAIT"

    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("142.250.27.100:443", ("142.250.27.100", 443)),
            ("[2606:4700::1111]:443", ("2606:4700::1111", 443)),
            ("0.0.0.0:0", ("0.0.0.0", 0)),
            ("*:*", ("*", 0)),
        ],
    )
    def test_split_endpoint(self, endpoint, expected):
        assert split_endpoint(endpoint) == expected


class TestPowerShellParsing:
    def test_script_uses_get_nettcpconnection_and_owning_process(self):
        assert "Get-NetTCPConnection" in PS_SCRIPT
        assert "OwningProcess" in PS_SCRIPT

    def test_parses_a_json_array(self):
        payload = json.dumps(
            [
                {
                    "local_address": "192.168.1.20",
                    "local_port": 51234,
                    "remote_address": "142.250.27.100",
                    "remote_port": 443,
                    "state": "Established",
                    "pid": 8420,
                }
            ]
        )
        connections = parse_powershell_output(payload, T0)
        assert len(connections) == 1
        assert connections[0].pid == 8420
        assert connections[0].state == "ESTABLISHED"

    def test_parses_a_single_object(self):
        payload = json.dumps(
            {
                "local_address": "10.0.0.5",
                "local_port": 80,
                "remote_address": "10.0.0.9",
                "remote_port": 51000,
                "state": "TimeWait",
                "pid": 1,
            }
        )
        assert len(parse_powershell_output(payload, T0)) == 1

    def test_empty_payload_returns_empty_list(self):
        assert parse_powershell_output("", T0) == []
        assert parse_powershell_output("null", T0) == []

    def test_invalid_json_raises_collector_error(self):
        with pytest.raises(CollectorError):
            parse_powershell_output("<html>access denied</html>", T0)

    def test_whitespace_payload_is_empty_not_invalid(self):
        assert parse_powershell_output("   \n", T0) == []

    def test_malformed_entries_are_skipped(self):
        payload = json.dumps([{"local_address": "1.2.3.4"}, {"local_port": "not-a-number"}])
        assert parse_powershell_output(payload, T0) == []


class TestConnectionCollector:
    def test_uses_injected_source(self):
        payload = json.dumps(
            [
                {
                    "local_address": "192.168.1.20",
                    "local_port": 5000,
                    "remote_address": "1.1.1.1",
                    "remote_port": 443,
                    "state": "Established",
                    "pid": 999,
                }
            ]
        )
        collector = ConnectionCollector(
            tcp_table_reader=lambda: [],
            ps_provider=lambda: payload,
            netstat_provider=lambda: NETSTAT_OK,
            platform_name="win32",
        )
        connections = collector.collect()
        assert connections[0].pid == 999

    def test_empty_powershell_result_is_a_valid_empty_snapshot(self):
        """No connections is a normal answer, not a failure to fall back from."""
        collector = ConnectionCollector(
            tcp_table_reader=lambda: [_ for _ in ()],  # would raise if used
            ps_provider=lambda: "",
            netstat_provider=lambda: NETSTAT_OK,
            platform_name="win32",
        )
        assert collector.collect() == []
        assert collector.last_source == "powershell"

    def test_psutil_is_used_when_powershell_is_unavailable(self):
        collector = ConnectionCollector(
            tcp_table_reader=lambda: [],
            ps_provider=lambda: (_ for _ in ()).throw(CollectorError("no powershell")),
            netstat_provider=lambda: NETSTAT_OK,
            platform_name="win32",
        )
        assert collector.collect() == []
        assert collector.last_source == "psutil"

    def test_all_sources_failing_raises_collector_error(self):
        def boom():
            raise OSError("no powershell")

        collector = ConnectionCollector(
            tcp_table_reader=boom,
            ps_provider=boom,
            netstat_provider=boom,
            platform_name="win32",
        )
        with pytest.raises(CollectorError):
            collector.collect()

    def test_command_failure_is_reported_not_swallowed(self):
        with pytest.raises(CollectorError):
            parse_powershell_output("not json", T0)

    def test_falls_back_to_psutil_when_powershell_fails(self):
        def failing_powershell():
            raise CollectorError("powershell blocked by policy")

        collector = ConnectionCollector(
            tcp_table_reader=lambda: [],
            ps_provider=failing_powershell,
            netstat_provider=lambda: NETSTAT_OK,
            platform_name="win32",
        )
        assert collector.collect() == []
        assert collector.last_source == "psutil"  # psutil tried before netstat

    def test_netstat_is_the_last_resort_on_windows(self):
        collector = ConnectionCollector(
            tcp_table_reader=lambda: (_ for _ in ()).throw(CollectorError("denied")),
            ps_provider=lambda: (_ for _ in ()).throw(CollectorError("blocked")),
            netstat_provider=lambda: NETSTAT_OK,
            platform_name="win32",
        )
        connections = collector.collect()
        assert collector.last_source == "netstat"
        assert len(connections) == 4

    def test_normalize_psutil_connection(self):
        entry = type(
            "Conn",
            (),
            {
                "laddr": type("Addr", (), {"ip": "192.168.1.5", "port": 50000})(),
                "raddr": type("Addr", (), {"ip": "1.1.1.1", "port": 443})(),
                "status": "ESTABLISHED",
                "pid": 1234,
                "family": "AddressFamily.AF_INET",
            },
        )()
        connection = normalize_psutil_connection(entry, T0)
        assert connection.local_port == 50000
        assert connection.remote_endpoint == "1.1.1.1:443"

    def test_normalize_psutil_connection_without_remote(self):
        entry = type(
            "Conn",
            (),
            {"laddr": ("0.0.0.0", 135), "raddr": (), "status": "LISTEN", "pid": 4},
        )()
        connection = normalize_psutil_connection(entry, T0)
        assert connection.remote_address == ""
        assert connection.state == "LISTEN"

    def test_csv_parsing_utility(self):
        payload = "LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess\n1.2.3.4,5000,5.6.7.8,443,Established,4242\n"
        connections = parse_csv_connections(payload, T0)
        assert connections[0].pid == 4242


class TestProcessResolver:
    def test_resolves_the_current_process(self):
        resolver = ProcessResolver()
        info = resolver.resolve(os.getpid())
        assert info.pid == os.getpid()
        assert info.name  # "python" / "pytest"
        assert info.resolved
        assert info.error is None

    def test_invalid_pid_is_reported_cleanly(self):
        resolver = ProcessResolver()
        info = resolver.resolve(-1)
        assert info.error == "no_such_process"
        assert info.name is None

    def test_terminated_process_is_reported_cleanly(self):
        """A PID that certainly does not exist must not raise."""
        resolver = ProcessResolver()
        info = resolver.resolve(999_999)
        assert info.error in {"no_such_process", "access_denied"}
        assert not info.resolved

    def test_process_that_exits_between_steps(self):
        # sys.executable keeps this cross-platform: Windows has no `sleep` binary.
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
        pid = process.pid
        process.wait()
        info = ProcessResolver().resolve(pid)
        assert info.name is None
        assert info.error == "no_such_process"

    def test_access_denied_is_reported_cleanly(self, monkeypatch):
        resolver = ProcessResolver()

        def deny(pid):
            raise psutil.AccessDenied(pid=pid)

        monkeypatch.setattr("psutil.Process", deny)
        info = resolver.resolve(1234)
        assert info.error == "access_denied"
        assert info.name is None

    def test_resolved_processes_are_cached(self, monkeypatch):
        resolver = ProcessResolver(cache_ttl_seconds=60)
        calls = {"count": 0}
        original = psutil.Process

        def counting_process(pid):
            calls["count"] += 1
            return original(pid)

        monkeypatch.setattr("psutil.Process", counting_process)
        resolver.resolve(os.getpid())
        resolver.resolve(os.getpid())
        assert calls["count"] == 1

    def test_cache_ttl_expiry(self, monkeypatch):
        resolver = ProcessResolver(cache_ttl_seconds=0.0)
        original = psutil.Process
        calls = {"count": 0}

        def counting_process(pid):
            calls["count"] += 1
            return original(pid)

        monkeypatch.setattr("psutil.Process", counting_process)
        resolver.resolve(os.getpid())
        resolver.resolve(os.getpid())
        assert calls["count"] == 2

    def test_resolve_many_counts_connections_per_process(self):
        resolver = ProcessResolver()
        infos = resolver.resolve_many([os.getpid(), os.getpid(), os.getpid()])
        assert len(infos) == 1
        assert infos[0].connection_count == 3
