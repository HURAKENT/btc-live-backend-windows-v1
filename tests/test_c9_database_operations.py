from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.storage import SqliteStore
from src.windows_operations import (
    DatabaseOperationsError,
    StartupValidatedSqliteStore,
    create_online_backup,
    restore_backup_to_copy,
    validate_database_copy,
)


class C9DatabaseOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "runtime.sqlite3"
        self.store = SqliteStore.open(self.database_path)
        self.store.migrate()
        self.store._connection.execute(
            """
            INSERT INTO source_events(
                source, natural_key, source_timestamp_ms,
                received_timestamp_ms, event_type, payload_json,
                payload_sha256, recovery_origin, committed_at_ms
            ) VALUES ('fixture', 'fixture:1', 1, 2, 'BOOK', '{}', 'abc',
                      'LIVE', 3)
            """
        )
        self.store._connection.execute(
            """
            INSERT INTO paper_accounts(
                account_key, schema_version, starting_bankroll_usd_micros,
                cash_usd_micros, open_cost_basis_usd_micros,
                equity_usd_micros, realized_pnl_usd_micros,
                unrealized_pnl_usd_micros, updated_at_ms
            ) VALUES ('default', 'PAPER_ACCOUNT_V1', 1000000000,
                      1000000000, 0, 1000000000, 0, 0, 4)
            """
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_startup_validation_is_quick_and_checks_exact_schema_version(self):
        self.store.close()
        store = StartupValidatedSqliteStore.open(self.database_path)
        self.assertIs(type(store), SqliteStore)
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        try:
            report = store.integrity_report()
        finally:
            store.close()

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["quick_check"], "ok")
        self.assertEqual(report["migration_version"], 5)
        self.assertNotIn(
            "integrity_check",
            "\n".join(statements).lower(),
        )

    def test_startup_validation_rejects_forged_migration_name(self):
        self.store._connection.execute(
            "UPDATE schema_migrations SET name = 'forged.sql' WHERE version = 5"
        )
        self.store.close()
        store = StartupValidatedSqliteStore.open(self.database_path)
        try:
            report = store.integrity_report()
        finally:
            store.close()

        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["schema_status"], "FAIL")

    def test_startup_validation_rejects_missing_required_table(self):
        self.store._connection.execute("DROP TABLE paper_fills")
        self.store.close()
        store = StartupValidatedSqliteStore.open(self.database_path)
        try:
            report = store.integrity_report()
        finally:
            store.close()

        self.assertEqual(report["status"], "FAIL")
        self.assertIn("paper_fills", report["missing_tables"])

    def test_online_backup_captures_committed_live_wal_state(self):
        backup_path = self.root / "backup.sqlite3"

        report = create_online_backup(self.database_path, backup_path)

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["representative_rows"]["source_events"], 1)
        self.assertEqual(report["representative_rows"]["paper_accounts"], 1)
        self.assertEqual(
            validate_database_copy(backup_path)["representative_rows"],
            report["representative_rows"],
        )

    def test_restore_drill_uses_copy_and_preserves_representative_state(self):
        backup_path = self.root / "backup.sqlite3"
        restored_path = self.root / "restore-drill.sqlite3"
        create_online_backup(self.database_path, backup_path)

        report = restore_backup_to_copy(backup_path, restored_path)

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(restored_path.is_file())
        self.assertEqual(report["representative_rows"]["source_events"], 1)
        self.assertEqual(report["representative_rows"]["paper_accounts"], 1)
        with closing(sqlite3.connect(self.database_path)) as working:
            self.assertEqual(
                working.execute("SELECT COUNT(*) FROM source_events").fetchone()[0],
                1,
            )

    def test_corrupted_backup_is_refused_before_restore_target_is_created(self):
        corrupt_path = self.root / "corrupt.sqlite3"
        restored_path = self.root / "must-not-exist.sqlite3"
        corrupt_path.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(
            DatabaseOperationsError,
            "DATABASE_COPY_INVALID",
        ):
            restore_backup_to_copy(corrupt_path, restored_path)

        self.assertFalse(restored_path.exists())

    def test_backup_and_restore_refuse_overwrite_or_live_database_target(self):
        with self.assertRaisesRegex(
            DatabaseOperationsError,
            "DATABASE_TARGET_EXISTS",
        ):
            create_online_backup(self.database_path, self.database_path)

        backup_path = self.root / "backup.sqlite3"
        create_online_backup(self.database_path, backup_path)
        with self.assertRaisesRegex(
            DatabaseOperationsError,
            "DATABASE_TARGET_EXISTS",
        ):
            restore_backup_to_copy(backup_path, self.database_path)


if __name__ == "__main__":
    unittest.main()
