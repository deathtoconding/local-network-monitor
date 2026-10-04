"""SQLite schema for Local Network Monitor.

The schema is created on first start (no manual setup, see Definition of Done)
and versioned through ``PRAGMA user_version`` so future migrations have a
starting point. Statements are idempotent (``IF NOT EXISTS``).

Indexes are chosen for the actual query patterns:

* ``timestamp`` descending - history and "latest" queries
* ``interface_name + timestamp`` - per-interface history
* ``event_type + timestamp``  - event filtering and cooldown lookups
"""

from __future__ import annotations

SCHEMA_VERSION = 1

CREATE_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS interface_measurements (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT    NOT NULL,
        interface_name    TEXT    NOT NULL,
        bytes_sent        INTEGER NOT NULL,
        bytes_received    INTEGER NOT NULL,
        packets_sent      INTEGER NOT NULL DEFAULT 0,
        packets_received  INTEGER NOT NULL DEFAULT 0,
        errors_in         INTEGER NOT NULL DEFAULT 0,
        errors_out        INTEGER NOT NULL DEFAULT 0,
        drops_in          INTEGER NOT NULL DEFAULT 0,
        drops_out         INTEGER NOT NULL DEFAULT 0,
        upload_rate       REAL,
        download_rate     REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS connections (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       TEXT    NOT NULL,
        protocol        TEXT    NOT NULL DEFAULT 'TCP',
        local_address   TEXT    NOT NULL,
        local_port      INTEGER NOT NULL DEFAULT 0,
        remote_address  TEXT    NOT NULL,
        remote_port     INTEGER NOT NULL DEFAULT 0,
        state           TEXT    NOT NULL DEFAULT 'UNKNOWN',
        pid             INTEGER,
        process_name    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processes (
        pid               INTEGER PRIMARY KEY,
        name              TEXT    NOT NULL,
        executable        TEXT,
        created_at        TEXT,
        last_seen         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp        TEXT    NOT NULL,
        event_type       TEXT    NOT NULL,
        severity         TEXT    NOT NULL,
        title            TEXT    NOT NULL,
        description      TEXT    NOT NULL,
        source           TEXT    NOT NULL DEFAULT 'detection-engine',
        pid              INTEGER,
        process_name     TEXT,
        interface_name   TEXT,
        evidence         TEXT,
        status           TEXT    NOT NULL DEFAULT 'open'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collector_health (
        name          TEXT    PRIMARY KEY,
        state         TEXT    NOT NULL DEFAULT 'unknown',
        last_success  TEXT,
        last_attempt  TEXT,
        last_error    TEXT,
        updated_at    TEXT    NOT NULL
    )
    """,
)

CREATE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_measurements_ts ON interface_measurements (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_measurements_nic_ts "
    "ON interface_measurements (interface_name, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_connections_ts ON connections (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_connections_pid ON connections (pid)",
    "CREATE INDEX IF NOT EXISTS idx_connections_remote ON connections (remote_address, remote_port)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (event_type, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_events_severity ON events (severity)",
)

#: Tables whose rows are pruned by the retention policy.
TIME_SERIES_TABLES: tuple[tuple[str, str], ...] = (
    ("interface_measurements", "timestamp"),
    ("connections", "timestamp"),
    ("events", "timestamp"),
)
