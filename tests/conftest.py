"""Shared pytest fixtures.

Every test runs against a temporary SQLite file (or an in-memory database) and
never touches the real ``data/monitor.db``, so the suite is safe to run on a
machine that is actively monitoring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from network_monitor.api import create_app
from network_monitor.api.state import build_state
from network_monitor.config import Config
from network_monitor.detection import DetectionEngine
from network_monitor.models.network import InterfaceMeasurement, NetworkConnection, utc_now
from network_monitor.storage import Database


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """Configuration pointing at throwaway paths."""
    cfg = Config()
    cfg.database.path = str(tmp_path / "monitor.db")
    cfg.logging.file = str(tmp_path / "monitor.log")
    cfg.logging.console = False
    cfg.notifications.enabled = False
    return cfg


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.connect()
    yield db
    db.close()


@pytest.fixture()
def memory_database() -> Database:
    db = Database(":memory:")
    db.connect()
    yield db
    db.close()


@pytest.fixture()
def state(config: Config):
    monitor_state = build_state(config, with_notifications=False)
    yield monitor_state
    monitor_state.database.close()


@pytest.fixture()
def client(state):
    """FastAPI test client wired to an isolated monitor state."""
    from fastapi.testclient import TestClient

    app = create_app(state=state, web_dir=Path(__file__).parent / "static_stub")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_measurement(
    interface: str = "Ethernet",
    timestamp: datetime | None = None,
    bytes_sent: int = 1_000_000,
    bytes_received: int = 5_000_000,
    upload_rate: float | None = 1_000.0,
    download_rate: float | None = 20_000.0,
    **overrides,
) -> InterfaceMeasurement:
    """Helper for building measurements with sensible defaults."""
    payload = dict(
        timestamp=timestamp or utc_now(),
        interface_name=interface,
        bytes_sent=bytes_sent,
        bytes_received=bytes_received,
        packets_sent=100,
        packets_received=200,
        errors_in=0,
        errors_out=0,
        drops_in=0,
        drops_out=0,
        upload_rate=upload_rate,
        download_rate=download_rate,
    )
    payload.update(overrides)
    return InterfaceMeasurement(**payload)


def make_connection(
    pid: int = 4242,
    process_name: str | None = "chrome.exe",
    state_name: str = "ESTABLISHED",
    remote_address: str = "142.250.27.100",
    remote_port: int = 443,
    timestamp: datetime | None = None,
) -> NetworkConnection:
    return NetworkConnection(
        timestamp=timestamp or utc_now(),
        protocol="TCP",
        local_address="192.168.1.20",
        local_port=51234,
        remote_address=remote_address,
        remote_port=remote_port,
        state=state_name,
        pid=pid,
        process_name=process_name,
    )


def make_history(
    database: Database, interface: str, rates: list[float], start: datetime | None = None
):
    """Insert a series of measurements (one per second) into the database."""
    from network_monitor.storage import InterfaceMeasurementRepository

    repository = InterfaceMeasurementRepository(database)
    base = start or (utc_now() - timedelta(seconds=len(rates)))
    measurements = [
        make_measurement(
            interface=interface,
            timestamp=base + timedelta(seconds=index),
            download_rate=rate,
            upload_rate=rate / 10,
        )
        for index, rate in enumerate(rates)
    ]
    repository.add_many(measurements)
    return measurements


@pytest.fixture()
def engine() -> DetectionEngine:
    return DetectionEngine(Config().detection)
