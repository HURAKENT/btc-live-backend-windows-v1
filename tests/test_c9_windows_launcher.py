from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

import run_windows_backend
from src.windows_operations import StartupValidatedSqliteStore


class C9WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "runtime.sqlite3"
        self.log_path = self.root / "backend.log"

    def tearDown(self):
        run_windows_backend.close_backend_logger()
        self.temp.cleanup()

    def test_rotating_log_is_bounded_by_size_and_count(self):
        logger = run_windows_backend.configure_backend_logger(
            self.log_path,
            max_bytes=256,
            backup_count=2,
        )

        for sequence in range(100):
            logger.info("rotation-fixture sequence=%03d payload=abcdefghij", sequence)
        run_windows_backend.close_backend_logger()

        files = sorted(self.root.glob("backend.log*"))
        self.assertGreater(len(files), 1)
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 256 for path in files))
        combined = "\n".join(path.read_text(encoding="utf-8") for path in files)
        self.assertRegex(combined, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_launcher_uses_cheap_startup_store_and_logs_clean_lifecycle(self):
        captured = {}

        class FakeBackend:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        async def clean_runner(backend):
            return 0

        exit_code = run_windows_backend.main(
            [
                "--database-path",
                str(self.database_path.resolve()),
                "--log-path",
                str(self.log_path.resolve()),
            ],
            backend_factory=FakeBackend,
            backend_runner=clean_runner,
        )
        run_windows_backend.close_backend_logger()

        self.assertEqual(exit_code, 0)
        self.assertIs(captured["store_factory"], StartupValidatedSqliteStore.open)
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("backend startup requested", log_text)
        self.assertIn("backend clean shutdown", log_text)

    def test_launcher_logs_non_clean_exit_as_error(self):
        class FakeBackend:
            def __init__(self, **kwargs):
                pass

        async def refused_runner(backend):
            return 20

        exit_code = run_windows_backend.main(
            [
                "--database-path",
                str(self.database_path.resolve()),
                "--log-path",
                str(self.log_path.resolve()),
            ],
            backend_factory=FakeBackend,
            backend_runner=refused_runner,
        )
        run_windows_backend.close_backend_logger()

        self.assertEqual(exit_code, 20)
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("ERROR", log_text)
        self.assertIn("backend stopped with exit_code=20", log_text)
        self.assertIn("reason=BACKEND_ALREADY_RUNNING", log_text)

    def test_invalid_database_argument_is_logged_and_returns_config_exit(self):
        exit_code = run_windows_backend.main(
            [
                "--database-path",
                "relative.sqlite3",
                "--log-path",
                str(self.log_path.resolve()),
            ]
        )
        run_windows_backend.close_backend_logger()

        self.assertEqual(exit_code, 30)
        self.assertIn(
            "DATABASE_PATH_NOT_ABSOLUTE",
            self.log_path.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
