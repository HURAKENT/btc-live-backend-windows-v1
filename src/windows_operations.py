from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from types import MethodType
from typing import Any

from src.storage import SqliteStore


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_REQUIRED_TABLES = frozenset(
    {
        "canonical_state",
        "data_import_run_events",
        "data_import_runs",
        "data_source_range_assessments",
        "data_source_ranges",
        "incidents",
        "market_catalog",
        "outbox_events",
        "paper_accounts",
        "paper_execution_readiness",
        "paper_fills",
        "paper_intents",
        "paper_positions",
        "schema_migrations",
        "signals",
        "source_cursors",
        "source_events",
        "strategy_checkpoint_schedules",
        "strategy_evaluations",
    }
)
_REPRESENTATIVE_TABLES = (
    "source_events",
    "market_catalog",
    "canonical_state",
    "strategy_evaluations",
    "signals",
    "paper_intents",
    "paper_fills",
    "paper_positions",
    "paper_accounts",
)


class DatabaseOperationsError(RuntimeError):
    pass


class StartupValidatedSqliteStore:
    """Factory preserving the accepted runtime's exact SqliteStore type."""

    @staticmethod
    def open(path: Path) -> SqliteStore:
        store = SqliteStore.open(path)
        store.integrity_report = MethodType(_startup_validation_report, store)
        return store


def _startup_validation_report(store: SqliteStore) -> dict[str, Any]:
    quick_check = store.scalar("PRAGMA quick_check(1)")
    foreign_keys = store.scalar("PRAGMA foreign_keys")
    journal_mode = str(store.scalar("PRAGMA journal_mode")).lower()
    schema = _schema_report(store._connection)
    status = (
        "PASS"
        if quick_check == "ok"
        and foreign_keys == 1
        and journal_mode == "wal"
        and schema["schema_status"] == "PASS"
        else "FAIL"
    )
    return {
        "status": status,
        "quick_check": quick_check,
        "foreign_keys": foreign_keys,
        "journal_mode": journal_mode,
        **schema,
    }


def create_online_backup(
    source_path: Path,
    backup_path: Path,
) -> dict[str, Any]:
    source = _existing_database_path(source_path)
    target = _new_database_target(backup_path, source)
    partial = _partial_path(target)
    source_connection = None
    destination_connection = None
    try:
        source_connection = _open_read_only(source)
        destination_connection = sqlite3.connect(partial)
        source_connection.backup(destination_connection, pages=256, sleep=0.01)
        destination_connection.close()
        destination_connection = None
        report = validate_database_copy(partial)
        os.replace(partial, target)
        return report
    except (OSError, sqlite3.Error, DatabaseOperationsError) as error:
        raise DatabaseOperationsError("DATABASE_BACKUP_FAILED") from error
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        partial.unlink(missing_ok=True)


def restore_backup_to_copy(
    backup_path: Path,
    restored_path: Path,
) -> dict[str, Any]:
    source = _existing_database_path(backup_path)
    source_report = validate_database_copy(source)
    target = _new_database_target(restored_path, source)
    partial = _partial_path(target)
    source_connection = None
    destination_connection = None
    try:
        source_connection = _open_read_only(source)
        destination_connection = sqlite3.connect(partial)
        source_connection.backup(destination_connection, pages=256, sleep=0.01)
        destination_connection.close()
        destination_connection = None
        restored_report = validate_database_copy(partial)
        if (
            restored_report["representative_rows"]
            != source_report["representative_rows"]
        ):
            raise DatabaseOperationsError("RESTORE_STATE_MISMATCH")
        os.replace(partial, target)
        return restored_report
    except (OSError, sqlite3.Error, DatabaseOperationsError) as error:
        raise DatabaseOperationsError("DATABASE_RESTORE_FAILED") from error
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        partial.unlink(missing_ok=True)


def validate_database_copy(path: Path) -> dict[str, Any]:
    database_path = _existing_database_path(path)
    connection = None
    try:
        connection = _open_read_only(database_path)
        quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
        schema = _schema_report(connection)
        representative_rows = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in _REPRESENTATIVE_TABLES
        }
        status = (
            "PASS"
            if quick_check == "ok" and schema["schema_status"] == "PASS"
            else "FAIL"
        )
        if status != "PASS":
            raise DatabaseOperationsError("DATABASE_COPY_INVALID")
        return {
            "status": status,
            "quick_check": quick_check,
            **schema,
            "representative_rows": representative_rows,
        }
    except (OSError, sqlite3.Error, TypeError, DatabaseOperationsError) as error:
        raise DatabaseOperationsError("DATABASE_COPY_INVALID") from error
    finally:
        if connection is not None:
            connection.close()


def _schema_report(connection: sqlite3.Connection) -> dict[str, Any]:
    expected_migrations = tuple(
        (int(path.name[:4]), path.name)
        for path in sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    )
    applied_migrations = tuple(
        connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    )
    actual_tables = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    )
    missing_tables = tuple(sorted(_REQUIRED_TABLES - actual_tables))
    schema_status = (
        "PASS"
        if expected_migrations
        and applied_migrations == expected_migrations
        and not missing_tables
        else "FAIL"
    )
    return {
        "schema_status": schema_status,
        "migration_version": (
            applied_migrations[-1][0] if applied_migrations else 0
        ),
        "missing_tables": missing_tables,
    }


def _existing_database_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise DatabaseOperationsError("INVALID_DATABASE_PATH_TYPE")
    resolved = path.resolve(strict=False)
    if not resolved.is_file():
        raise DatabaseOperationsError("DATABASE_SOURCE_NOT_FOUND")
    return resolved


def _new_database_target(path: Path, source: Path) -> Path:
    if not isinstance(path, Path):
        raise DatabaseOperationsError("INVALID_DATABASE_PATH_TYPE")
    resolved = path.resolve(strict=False)
    if resolved == source or resolved.exists():
        raise DatabaseOperationsError("DATABASE_TARGET_EXISTS")
    if resolved.suffix != ".sqlite3":
        raise DatabaseOperationsError("DATABASE_TARGET_SUFFIX")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _partial_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
