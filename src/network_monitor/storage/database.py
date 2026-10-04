"""SQLite connection management.

Design notes:

* The database is a single file (``data/monitor.db`` by default) created on
  first start - nothing to install, nothing to configure.
* The monitoring loop runs in a background thread, FastAPI serves requests in
  worker threads, so every connection is opened with ``check_same_thread=False``
  and access is serialised with a re-entrant lock plus WAL journal mode.
* Row factory is ``sqlite3.Row`` so the repositories can hand rows straight to
  the model ``from_row`` constructors.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from .schema import CREATE_INDEXES, CREATE_TABLES, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class Database:
    """Thin, thread-safe wrapper around a SQLite file."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path).expanduser()
        self._timeout = timeout
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None

    # ---- lifecycle ---------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Open (or reuse) the connection and make sure the schema exists."""
        with self._lock:
            if self._connection is not None:
                return self._connection

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                timeout=self._timeout,
                check_same_thread=False,
                isolation_level=None,  # explicit transaction control
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._connection = connection
            self._create_schema(connection)
            logger.info("database ready at %s (schema v%d)", self.path, SCHEMA_VERSION)
            return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        for statement in CREATE_TABLES:
            connection.execute(statement)
        for statement in CREATE_INDEXES:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                finally:
                    self._connection = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- access ------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        return self.connect()

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.execute(sql, parameters)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.executemany(sql, list(rows))

    def query(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self.connection.execute(sql, parameters)
            try:
                return cursor.fetchall()
            finally:
                cursor.close()

    def query_one(
        self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()
    ) -> Optional[sqlite3.Row]:
        rows = self.query(sql, parameters)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside a single transaction (rollback on error)."""
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def insert_many(self, sql: str, rows: list[Sequence[Any]]) -> int:
        """Insert many rows in one transaction; returns the number written."""
        if not rows:
            return 0
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN")
            try:
                connection.executemany(sql, rows)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(rows)

    def scalar(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.query_one(sql, parameters)
        return row[0] if row is not None else None

    # ---- maintenance -------------------------------------------------
    def prune(self, table: str, timestamp_column: str, older_than_iso: str) -> int:
        """Delete rows older than ``older_than_iso``; returns rows deleted."""
        cursor = self.execute(
            f"DELETE FROM {table} WHERE {timestamp_column} < ?", (older_than_iso,)
        )
        deleted = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        try:
            cursor.close()
        except Exception:  # pragma: no cover - cursor already closed
            pass
        return deleted

    def vacuum(self) -> None:
        with self._lock:
            self.connection.execute("VACUUM")

    def table_counts(self) -> dict[str, int]:
        """Row counts per table - handy for /api/status and tests."""
        tables = ("interface_measurements", "connections", "processes", "events")
        return {
            table: int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0) for table in tables
        }

    def schema_version(self) -> int:
        return int(self.scalar("PRAGMA user_version") or 0)
