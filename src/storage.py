from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from src.models import SourceEvent


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_COUNTABLE_TABLES = frozenset(
    {
        "schema_migrations",
        "source_events",
        "source_cursors",
        "market_catalog",
        "canonical_state",
        "strategy_evaluations",
        "signals",
        "outbox_events",
        "incidents",
    }
)


@dataclass(frozen=True, slots=True)
class AppendResult:
    event_id: int
    inserted: bool


@dataclass(frozen=True, slots=True)
class WriteCommand:
    operation: str
    event: SourceEvent | None
    future: asyncio.Future[AppendResult] | None

    @classmethod
    def append_source_event(
        cls,
        event: SourceEvent,
        future: asyncio.Future[AppendResult],
    ) -> Self:
        return cls("APPEND_SOURCE_EVENT", event, future)

    @classmethod
    def stop(cls) -> Self:
        return cls("STOP", None, None)


class SqliteStore:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self._path = path
        self._connection = connection
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> Self:
        if not isinstance(path, Path):
            raise ValueError("INVALID_DATABASE_PATH_TYPE")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return cls(path, connection)

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def migrate(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at_ms INTEGER NOT NULL
            )
            """
        )
        migration_paths = sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if not migration_paths:
            raise ValueError("NO_DATABASE_MIGRATIONS")

        for migration_path in migration_paths:
            version = int(migration_path.name[:4])
            already_applied = self.scalar(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (version,),
            )
            if already_applied == 1:
                continue

            script = migration_path.read_text(encoding="utf-8-sig")
            escaped_name = migration_path.name.replace("'", "''")
            applied_at_ms = time.time_ns() // 1_000_000
            transactional_script = (
                "BEGIN IMMEDIATE;\n"
                f"{script.rstrip()}\n"
                "INSERT INTO schema_migrations(version, name, applied_at_ms) "
                f"VALUES ({version}, '{escaped_name}', {applied_at_ms});\n"
                "COMMIT;\n"
            )
            try:
                self._connection.executescript(transactional_script)
            except sqlite3.Error:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise

    def append_source_event(self, event: SourceEvent) -> AppendResult:
        if type(event) is not SourceEvent:
            raise ValueError("INVALID_SOURCE_EVENT_TYPE")

        committed_at_ms = time.time_ns() // 1_000_000
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT INTO source_events(
                    source,
                    natural_key,
                    source_timestamp_ms,
                    received_timestamp_ms,
                    event_type,
                    payload_json,
                    payload_sha256,
                    recovery_origin,
                    committed_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(natural_key) DO NOTHING
                """,
                (
                    event.source,
                    event.natural_key,
                    event.source_timestamp_ms,
                    event.received_timestamp_ms,
                    event.event_type,
                    event.payload_json,
                    event.payload_sha256,
                    event.recovery_origin,
                    committed_at_ms,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._connection.execute(
                """
                SELECT
                    event_id,
                    source,
                    source_timestamp_ms,
                    received_timestamp_ms,
                    event_type,
                    payload_json,
                    payload_sha256,
                    recovery_origin
                FROM source_events
                WHERE natural_key = ?
                """,
                (event.natural_key,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("SOURCE_EVENT_INSERT_MISSING")

            expected = (
                event.source,
                event.source_timestamp_ms,
                event.received_timestamp_ms,
                event.event_type,
                event.payload_json,
                event.payload_sha256,
                event.recovery_origin,
            )
            if tuple(stored[1:]) != expected:
                raise ValueError("SOURCE_EVENT_CONFLICT")

            self._connection.commit()
            return AppendResult(event_id=stored[0], inserted=inserted)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def open_read_only(self) -> sqlite3.Connection:
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def integrity_report(self) -> dict[str, Any]:
        quick_check = self.scalar("PRAGMA quick_check")
        integrity_check = self.scalar("PRAGMA integrity_check")
        foreign_key_violations = len(self.rows("PRAGMA foreign_key_check"))
        foreign_keys = self.scalar("PRAGMA foreign_keys")
        journal_mode = self.scalar("PRAGMA journal_mode").lower()
        migration_version = self.scalar(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        status = (
            "PASS"
            if (
                quick_check == "ok"
                and integrity_check == "ok"
                and foreign_key_violations == 0
                and foreign_keys == 1
                and journal_mode == "wal"
                and migration_version == 2
            )
            else "FAIL"
        )
        return {
            "status": status,
            "quick_check": quick_check,
            "integrity_check": integrity_check,
            "foreign_keys": foreign_keys,
            "foreign_key_violations": foreign_key_violations,
            "journal_mode": journal_mode,
            "migration_version": migration_version,
        }

    def scalar(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> Any:
        row = self._connection.execute(sql, parameters).fetchone()
        return None if row is None else row[0]

    def rows(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        return self._connection.execute(sql, parameters).fetchall()

    def count(self, table: str) -> int:
        if table not in _COUNTABLE_TABLES:
            raise ValueError("INVALID_COUNT_TABLE")
        return self.scalar(f"SELECT COUNT(*) FROM {table}")


class SqliteWriter:
    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError("INVALID_DATABASE_PATH_TYPE")
        self._path = path

    async def run(self, queue: asyncio.Queue[WriteCommand]) -> None:
        store = SqliteStore.open(self._path)
        try:
            store.migrate()
            while True:
                command = await queue.get()
                try:
                    if type(command) is not WriteCommand:
                        raise ValueError("INVALID_WRITE_COMMAND")
                    if command.operation == "STOP":
                        return
                    if command.operation != "APPEND_SOURCE_EVENT":
                        raise ValueError("UNKNOWN_WRITE_COMMAND")
                    if command.event is None or command.future is None:
                        raise ValueError("INCOMPLETE_WRITE_COMMAND")
                    try:
                        result = store.append_source_event(command.event)
                    except BaseException as error:
                        if not command.future.done():
                            command.future.set_exception(error)
                    else:
                        if not command.future.done():
                            command.future.set_result(result)
                finally:
                    queue.task_done()
        finally:
            store.close()
