"""Milestone 5 & 14 tests: REST API contracts.

Every endpoint named in the specification is asserted to answer 200 with valid
JSON, both with an empty database and with seeded data.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from network_monitor.models.events import Event, EventSeverity, EventType
from network_monitor.models.network import utc_now
from network_monitor.models.process import ProcessInfo

from .conftest import make_connection, make_history, make_measurement

SPEC_ENDPOINTS = [
    "/api/status",
    "/api/interfaces",
    "/api/traffic",
    "/api/connections",
    "/api/processes",
    "/api/events",
]


@pytest.mark.parametrize("endpoint", SPEC_ENDPOINTS)
def test_endpoint_returns_200_on_empty_database(client, endpoint):
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


class TestStatusEndpoint:
    def test_shape(self, client):
        payload = client.get("/api/status").json()
        assert payload["status"] in {"starting", "running", "degraded"}
        assert "uptime_seconds" in payload
        assert payload["collectors"] == {}
        assert {rule["name"] for rule in payload["detection"]["rules"]} >= {
            "high_download",
            "high_upload",
            "interface_error",
            "new_network_process",
            "connection_spike",
            "collector_failure",
        }
        assert payload["storage"]["database"]

    def test_reports_degraded_when_a_collector_fails(self, client, state):
        state.health.record_failure("connections", utc_now(), "access denied")
        state.health.record_failure("connections", utc_now(), "access denied")
        payload = client.get("/api/status").json()
        assert payload["status"] == "degraded"
        assert payload["collectors"]["connections"]["state"] == "failed"
        assert payload["collectors"]["connections"]["last_error"] == "access denied"

    def test_health_probe(self, client):
        assert client.get("/api/health").json() == {"status": "ok"}


class TestTrafficEndpoints:
    def test_traffic_without_data_is_zeroed_not_missing(self, client):
        payload = client.get("/api/traffic").json()
        assert payload["download_bytes_per_second"] == 0.0
        assert payload["upload_bytes_per_second"] == 0.0
        assert payload["interfaces"] == {}

    def test_traffic_aggregates_interfaces(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[
                make_measurement(interface="Ethernet", download_rate=18_400_000.0, upload_rate=2_100_000.0),
                make_measurement(interface="Wi-Fi", download_rate=600_000.0, upload_rate=100_000.0),
            ],
            connections=[],
            processes=[],
            duration_ms=12.0,
        )
        payload = client.get("/api/traffic").json()
        assert payload["download_bytes_per_second"] == pytest.approx(19_000_000.0)
        assert payload["upload_bytes_per_second"] == pytest.approx(2_200_000.0)
        assert payload["download_mbps"] == pytest.approx(152.0)
        assert set(payload["interfaces"]) == {"Ethernet", "Wi-Fi"}

    def test_interfaces_endpoint(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[make_measurement(interface="Ethernet", download_rate=1000.0)],
            connections=[],
            processes=[],
            duration_ms=1.0,
        )
        payload = client.get("/api/interfaces").json()
        assert isinstance(payload, list)
        assert payload[0]["name"] == "Ethernet"
        assert payload[0]["download_bytes_per_second"] == pytest.approx(1000.0)

    def test_history_endpoint(self, client, state):
        make_history(state.database, "Ethernet", [1000.0, 2000.0, 3000.0])
        payload = client.get("/api/traffic/history?limit=10").json()
        assert payload["count"] == 3
        assert [point["download_bytes_per_second"] for point in payload["points"]] == [
            1000.0,
            2000.0,
            3000.0,
        ]

    def test_history_filters_by_interface(self, client, state):
        make_history(state.database, "Ethernet", [1000.0])
        make_history(state.database, "Wi-Fi", [2000.0])
        payload = client.get("/api/traffic/history?interface=Wi-Fi").json()
        assert payload["count"] == 1
        assert payload["points"][0]["interface_name"] == "Wi-Fi"

    def test_history_rejects_bad_timestamps(self, client):
        assert client.get("/api/traffic/history?from=not-a-date").status_code == 422

    def test_history_from_to_window(self, client, state):
        now = utc_now()
        make_history(state.database, "Ethernet", [1.0, 2.0], start=now - timedelta(hours=2))
        make_history(state.database, "Ethernet", [3.0], start=now)
        start = (now - timedelta(minutes=30)).isoformat()
        payload = client.get("/api/traffic/history", params={"from": start}).json()
        assert payload["count"] == 1

    def test_history_tolerates_an_unencoded_offset(self, client, state):
        """A '+' in a query string arrives as a space; the API must cope."""
        make_history(state.database, "Ethernet", [1.0])
        space_form = utc_now().strftime("%Y-%m-%dT%H:%M:%S+00:00").replace("+", " ")
        response = client.get(f"/api/traffic/history?from={space_form}")
        assert response.status_code == 200


class TestConnectionEndpoints:
    def test_connections_from_latest_snapshot(self, client, state):
        now = utc_now()
        state.publish_cycle(
            timestamp=now,
            measurements=[],
            connections=[
                make_connection(pid=8420, process_name="chrome.exe", timestamp=now),
                make_connection(pid=9312, process_name="python.exe", timestamp=now, remote_port=8443),
            ],
            processes=[],
            duration_ms=1.0,
        )
        payload = client.get("/api/connections").json()
        assert payload["count"] == 2
        assert payload["states"] == {"ESTABLISHED": 2}
        assert payload["connections"][0]["remote_endpoint"]

    def test_connection_filters(self, client, state):
        now = utc_now()
        state.publish_cycle(
            timestamp=now,
            measurements=[],
            connections=[
                make_connection(pid=8420, process_name="chrome.exe", timestamp=now),
                make_connection(pid=9312, process_name="python.exe", state_name="LISTEN", timestamp=now),
            ],
            processes=[],
            duration_ms=1.0,
        )
        assert client.get("/api/connections?process=chrom").json()["count"] == 1
        assert client.get("/api/connections?pid=9312").json()["count"] == 1
        assert client.get("/api/connections?state=LISTEN").json()["count"] == 1
        assert client.get("/api/connections?remote=443").json()["count"] == 2

    def test_missing_pid_cannot_crash_the_endpoint(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[],
            connections=[make_connection(pid=None, process_name=None)],
            processes=[],
            duration_ms=1.0,
        )
        assert client.get("/api/connections").status_code == 200


class TestProcessEndpoints:
    def test_processes_include_connection_counts(self, client, state):
        now = utc_now()
        state.processes.upsert_many([ProcessInfo(pid=8420, name="chrome.exe", executable="C:/chrome.exe", last_seen=now)])
        state.publish_cycle(
            timestamp=now,
            measurements=[],
            connections=[
                make_connection(pid=8420, process_name="chrome.exe", timestamp=now),
                make_connection(pid=8420, process_name="chrome.exe", timestamp=now, remote_port=8443),
            ],
            processes=[],
            duration_ms=1.0,
        )
        payload = client.get("/api/processes").json()
        entry = next(item for item in payload["processes"] if item["pid"] == 8420)
        assert entry["name"] == "chrome.exe"
        assert entry["connection_count"] == 2

    def test_unresolved_pid_still_appears(self, client, state):
        state.publish_cycle(
            timestamp=utc_now(),
            measurements=[],
            connections=[make_connection(pid=7777, process_name="<access_denied>")],
            processes=[],
            duration_ms=1.0,
        )
        payload = client.get("/api/processes").json()
        entry = next(item for item in payload["processes"] if item["pid"] == 7777)
        assert entry["resolved"] is False


class TestEventEndpoints:
    def _seed_events(self, state):
        now = utc_now()
        return state.events.add_many(
            [
                Event(
                    timestamp=now,
                    event_type=EventType.HIGH_UPLOAD,
                    severity=EventSeverity.WARNING,
                    title="High upload traffic on Ethernet",
                    description="Upload traffic exceeded the configured threshold.",
                    interface_name="Ethernet",
                    evidence={"interface": "Ethernet", "upload_rate": 9_000_000, "threshold": 5_000_000},
                ),
                Event(
                    timestamp=now,
                    event_type=EventType.NEW_NETWORK_PROCESS,
                    severity=EventSeverity.INFO,
                    title="New network process: chrome.exe",
                    description="observed",
                    pid=8420,
                    process_name="chrome.exe",
                ),
            ]
        )

    def test_list_events(self, client, state):
        self._seed_events(state)
        payload = client.get("/api/events").json()
        assert payload["count"] == 2
        assert payload["counts_24h"]["total"] == 2

    def test_event_details_include_evidence(self, client, state):
        ids = self._seed_events(state)
        event_id = ids[0]
        payload = client.get(f"/api/events/{event_id}").json()
        assert payload["event_type"] == "HIGH_UPLOAD"
        assert payload["evidence"]["threshold"] == 5_000_000
        assert payload["title"].startswith("High upload")

    def test_event_filters(self, client, state):
        self._seed_events(state)
        assert client.get("/api/events?severity=info").json()["count"] == 1
        assert client.get("/api/events?event_type=HIGH_UPLOAD").json()["count"] == 1
        assert client.get("/api/events?event_type=NONSENSE").status_code == 422

    def test_unknown_event_returns_404(self, client):
        assert client.get("/api/events/99999").status_code == 404

    def test_status_can_be_updated(self, client, state):
        event_id = self._seed_events(state)[0]
        response = client.patch(f"/api/events/{event_id}", json={"status": "acknowledged"})
        assert response.status_code == 200
        assert response.json()["event"]["status"] == "acknowledged"
        assert client.patch("/api/events/12345", json={"status": "open"}).status_code == 404

    def test_invalid_status_value_is_rejected(self, client, state):
        event_id = self._seed_events(state)[0]
        assert client.patch(f"/api/events/{event_id}", json={"status": "nonsense"}).status_code == 422


class TestMiscEndpoints:
    def test_system_endpoint(self, client):
        payload = client.get("/api/system").json()
        assert "uptime_seconds" in payload
        assert "storage_rows" in payload

    def test_openapi_schema_is_served(self, client):
        assert client.get("/api/openapi.json").status_code == 200

    def test_info_endpoint_lists_api_routes(self, client):
        payload = client.get("/api/info").json()
        for endpoint in SPEC_ENDPOINTS:
            assert endpoint in payload["endpoints"]
