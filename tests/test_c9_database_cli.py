from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import windows_database
from src.storage import SqliteStore


class C9DatabaseCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "runtime.sqlite3"
        store = SqliteStore.open(self.database_path)
        try:
            store.migrate()
        finally:
            store.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_validate_and_restore_drill_commands_emit_json(self):
        backup_path = self.root / "backup.sqlite3"
        restored_path = self.root / "restored.sqlite3"

        backup = self._run("backup", self.database_path, backup_path)
        validated = self._run("validate", backup_path)
        restored = self._run("restore-drill", backup_path, restored_path)

        self.assertEqual(backup["status"], "PASS")
        self.assertEqual(validated["status"], "PASS")
        self.assertEqual(restored["status"], "PASS")
        self.assertTrue(backup_path.is_file())
        self.assertTrue(restored_path.is_file())

    def test_corrupted_backup_command_returns_nonzero_without_restore(self):
        corrupt_path = self.root / "corrupt.sqlite3"
        restored_path = self.root / "restored.sqlite3"
        corrupt_path.write_bytes(b"corrupted")
        stderr = StringIO()

        with redirect_stderr(stderr):
            exit_code = windows_database.main(
                ["restore-drill", str(corrupt_path), str(restored_path)]
            )

        self.assertEqual(exit_code, 40)
        self.assertIn("DATABASE_COPY_INVALID", stderr.getvalue())
        self.assertFalse(restored_path.exists())

    def _run(self, operation: str, *paths: Path):
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = windows_database.main(
                [operation, *(str(path) for path in paths)]
            )
        self.assertEqual(exit_code, 0)
        return json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
