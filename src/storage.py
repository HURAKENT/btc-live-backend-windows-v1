from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from src.models import OutboxEvent, SignalRecord, SourceEvent


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


class CommittedPublisher(Protocol):
    def publish_committed(self, event_id: int) -> None: ...


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

    def commit_signal_and_outbox(
        self,
        signal: SignalRecord,
        *,
        topic: str,
        broker: CommittedPublisher | None = None,
    ) -> tuple[int, int]:
        if type(signal) is not SignalRecord:
            raise ValueError("INVALID_SIGNAL_TYPE")
        if type(topic) is not str:
            raise ValueError("INVALID_OUTBOX_TOPIC_TYPE")
        if not topic:
            raise ValueError("INVALID_OUTBOX_TOPIC")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            signal_cursor = self._connection.execute(
                """
                INSERT INTO signals(
                    identity_key,
                    evaluation_key,
                    strategy_id,
                    signal_type,
                    payload_json,
                    created_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO NOTHING
                """,
                (
                    signal.identity_key,
                    signal.evaluation_key,
                    signal.strategy_id,
                    signal.signal_type,
                    signal.payload_json,
                    signal.created_at_ms,
                ),
            )
            signal_inserted = signal_cursor.rowcount == 1
            stored_signal = self._connection.execute(
                """
                SELECT
                    signal_id,
                    evaluation_key,
                    strategy_id,
                    signal_type,
                    payload_json,
                    created_at_ms
                FROM signals
                WHERE identity_key = ?
                """,
                (signal.identity_key,),
            ).fetchone()
            if stored_signal is None:
                raise RuntimeError("SIGNAL_INSERT_MISSING")
            expected_signal = (
                signal.evaluation_key,
                signal.strategy_id,
                signal.signal_type,
                signal.payload_json,
                signal.created_at_ms,
            )
            if tuple(stored_signal[1:]) != expected_signal:
                raise ValueError("SIGNAL_IDENTITY_CONFLICT")

            signal_id = stored_signal[0]
            outbox_cursor = self._connection.execute(
                """
                INSERT INTO outbox_events(
                    signal_id,
                    topic,
                    payload_json,
                    created_at_ms
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(signal_id) DO NOTHING
                """,
                (
                    signal_id,
                    topic,
                    signal.payload_json,
                    signal.created_at_ms,
                ),
            )
            outbox_inserted = outbox_cursor.rowcount == 1
            stored_outbox = self._connection.execute(
                """
                SELECT event_id, topic, payload_json, created_at_ms
                FROM outbox_events
                WHERE signal_id = ?
                """,
                (signal_id,),
            ).fetchone()
            if stored_outbox is None:
                raise RuntimeError("OUTBOX_INSERT_MISSING")
            expected_outbox = (
                topic,
                signal.payload_json,
                signal.created_at_ms,
            )
            if tuple(stored_outbox[1:]) != expected_outbox:
                raise ValueError("OUTBOX_SIGNAL_CONFLICT")
            if signal_inserted != outbox_inserted:
                raise RuntimeError("SIGNAL_OUTBOX_ATOMICITY_VIOLATION")

            outbox_id = stored_outbox[0]
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

        if outbox_inserted and broker is not None:
            broker.publish_committed(outbox_id)
        return signal_id, outbox_id

    def read_outbox_after(
        self,
        event_id: int,
        limit: int,
    ) -> list[OutboxEvent]:
        if type(event_id) is not int:
            raise ValueError("INVALID_OUTBOX_EVENT_ID_TYPE")
        if event_id < 0:
            raise ValueError("INVALID_OUTBOX_EVENT_ID")
        if type(limit) is not int:
            raise ValueError("INVALID_OUTBOX_LIMIT_TYPE")
        if not 1 <= limit <= 1000:
            raise ValueError("INVALID_OUTBOX_LIMIT")

        rows = self.rows(
            """
            SELECT event_id, topic, payload_json, created_at_ms
            FROM outbox_events
            WHERE event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (event_id, limit),
        )
        return [
            OutboxEvent(
                event_id=row[0],
                topic=row[1],
                payload_json=row[2],
                created_at_ms=row[3],
            )
            for row in rows
        ]

    def open_read_only(self) -> sqlite3.Connection:
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def open_read_store(self) -> SqliteReadStore:
        return SqliteReadStore(self.open_read_only())

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


class SqliteReadStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

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

    def health(self) -> dict[str, Any]:
        quick_check = self.scalar("PRAGMA quick_check")
        foreign_keys = self.scalar("PRAGMA foreign_keys")
        journal_mode = self.scalar("PRAGMA journal_mode").lower()
        status = (
            "PASS"
            if quick_check == "ok"
            and foreign_keys == 1
            and journal_mode == "wal"
            else "FAIL"
        )
        return {
            "database": {
                "foreign_keys": foreign_keys,
                "journal_mode": journal_mode,
                "quick_check": quick_check,
            },
            "status": status,
        }

    def sources(self) -> list[dict[str, Any]]:
        rows = self.rows(
            """
            SELECT
                source,
                COUNT(*),
                MAX(source_timestamp_ms),
                MAX(received_timestamp_ms)
            FROM source_events
            GROUP BY source
            ORDER BY source ASC
            """
        )
        return [
            {
                "event_count": row[1],
                "last_received_timestamp_ms": row[3],
                "last_source_timestamp_ms": row[2],
                "source": row[0],
            }
            for row in rows
        ]

    def signals(self, *, limit: int) -> list[dict[str, Any]]:
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT
                signal_id,
                identity_key,
                evaluation_key,
                strategy_id,
                signal_type,
                payload_json,
                created_at_ms
            FROM signals
            ORDER BY signal_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "created_at_ms": row[6],
                "evaluation_key": row[2],
                "identity_key": row[1],
                "payload": _decode_stored_json(row[5]),
                "signal_id": row[0],
                "signal_type": row[4],
                "strategy_id": row[3],
            }
            for row in rows
        ]

    def incidents(self, *, limit: int) -> list[dict[str, Any]]:
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT
                incident_id,
                incident_key,
                severity,
                status,
                payload_json,
                created_at_ms
            FROM incidents
            ORDER BY incident_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "created_at_ms": row[5],
                "incident_id": row[0],
                "incident_key": row[1],
                "payload": _decode_stored_json(row[4]),
                "severity": row[2],
                "status": row[3],
            }
            for row in rows
        ]

    def current_market_identity(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT market_id, payload_json, updated_at_ms
            FROM market_catalog
            ORDER BY updated_at_ms DESC, market_id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "market_id": row[0],
            "payload": _decode_stored_json(row[1]),
            "updated_at_ms": row[2],
        }

    def last_event_id(self) -> int:
        return self.scalar(
            "SELECT COALESCE(MAX(event_id), 0) FROM outbox_events"
        )

    def read_outbox_after(
        self,
        event_id: int,
        limit: int,
    ) -> list[OutboxEvent]:
        if type(event_id) is not int:
            raise ValueError("INVALID_OUTBOX_EVENT_ID_TYPE")
        if event_id < 0:
            raise ValueError("INVALID_OUTBOX_EVENT_ID")
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT event_id, topic, payload_json, created_at_ms
            FROM outbox_events
            WHERE event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (event_id, limit),
        )
        return [
            OutboxEvent(
                event_id=row[0],
                topic=row[1],
                payload_json=row[2],
                created_at_ms=row[3],
            )
            for row in rows
        ]


def _validate_read_limit(limit: int) -> None:
    if type(limit) is not int:
        raise ValueError("INVALID_READ_LIMIT_TYPE")
    if not 1 <= limit <= 1000:
        raise ValueError("INVALID_READ_LIMIT")


def _decode_stored_json(value: str) -> Any:
    import json

    return json.loads(value)


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
