from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import AsyncMock, patch

import run_backend
import run_windows_backend
from src import runtime_paths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
C9_LAUNCHER = PROJECT_ROOT / "scripts" / "C9_RUN_BACKEND.ps1"
MANUAL_LAUNCHER = PROJECT_ROOT / "scripts" / "RUN_BACKEND_SAFE.ps1"


class ExternalRuntimePathContractTests(unittest.TestCase):
    def test_cli_data_root_wins_over_environment(self):
        explicit = Path(tempfile.gettempdir()) / "explicit root"
        environmental = Path(tempfile.gettempdir()) / "environment root"

        resolved = runtime_paths.resolve_data_root(
            str(explicit),
            environ={"BTC_DAILY_RANGE_DATA_ROOT": str(environmental)},
        )

        self.assertEqual(resolved, explicit.resolve(strict=False))

    def test_environment_data_root_is_used_without_cli(self):
        environmental = Path(tempfile.gettempdir()) / "environment root"

        resolved = runtime_paths.resolve_data_root(
            environ={"BTC_DAILY_RANGE_DATA_ROOT": str(environmental)},
        )

        self.assertEqual(resolved, environmental.resolve(strict=False))

    def test_windows_default_uses_local_user_documents(self):
        resolved = runtime_paths.resolve_data_root(
            environ={},
            platform_name="win32",
            home=PureWindowsPath("C:/Users/alice"),
        )

        self.assertEqual(
            resolved,
            PureWindowsPath("C:/Users/alice/Documents/BTC Daily Range"),
        )

    def test_linux_default_uses_absolute_xdg_data_home(self):
        resolved = runtime_paths.resolve_data_root(
            environ={"XDG_DATA_HOME": "/srv/alice data"},
            platform_name="linux",
            home=PurePosixPath("/home/alice"),
        )

        self.assertEqual(resolved, PurePosixPath("/srv/alice data/btc_daily_range"))

    def test_linux_default_falls_back_when_xdg_is_relative(self):
        resolved = runtime_paths.resolve_data_root(
            environ={"XDG_DATA_HOME": "relative/data"},
            platform_name="linux",
            home=PurePosixPath("/home/alice"),
        )

        self.assertEqual(
            resolved,
            PurePosixPath("/home/alice/.local/share/btc_daily_range"),
        )

    def test_explicit_path_with_spaces_is_preserved(self):
        requested = Path(tempfile.gettempdir()) / "BTC Daily Range Test"

        resolved = runtime_paths.resolve_data_root(str(requested), environ={})

        self.assertEqual(resolved, requested.resolve(strict=False))

    def test_relative_data_root_is_rejected(self):
        with self.assertRaisesRegex(
            runtime_paths.RuntimePathError,
            "^DATA_ROOT_NOT_ABSOLUTE$",
        ):
            runtime_paths.resolve_data_root("relative/data", environ={})

    def test_windows_unc_data_root_is_rejected(self):
        with self.assertRaisesRegex(
            runtime_paths.RuntimePathError,
            "^DATA_ROOT_UNC_NOT_SUPPORTED$",
        ):
            runtime_paths.resolve_data_root(
                r"\\server\share\btc",
                environ={},
                platform_name="win32",
            )

    def test_legacy_absolute_database_path_remains_accepted(self):
        legacy = Path(tempfile.gettempdir()) / "legacy.sqlite3"

        resolved = runtime_paths.resolve_runtime_paths(
            database_path=str(legacy),
            environ={},
        )

        self.assertEqual(resolved.database_path, legacy.resolve(strict=False))

    def test_consistent_data_root_and_database_path_are_accepted(self):
        root = Path(tempfile.gettempdir()) / "consistent root"
        database = root / "runtime" / "btc_daily_range.sqlite3"

        resolved = runtime_paths.resolve_runtime_paths(
            data_root=str(root),
            database_path=str(database),
            environ={},
        )

        self.assertEqual(resolved.database_path, database.resolve(strict=False))

    def test_conflicting_data_root_and_database_path_fail_closed(self):
        root = Path(tempfile.gettempdir()) / "configured root"
        database = Path(tempfile.gettempdir()) / "other" / "legacy.sqlite3"

        with self.assertRaisesRegex(
            runtime_paths.RuntimePathError,
            "^DATA_ROOT_DATABASE_PATH_CONFLICT$",
        ):
            runtime_paths.resolve_runtime_paths(
                data_root=str(root),
                database_path=str(database),
                environ={},
            )

    def test_explicit_legacy_database_path_wins_over_environment_root(self):
        root = Path(tempfile.gettempdir()) / "environment root"
        database = Path(tempfile.gettempdir()) / "other" / "legacy.sqlite3"

        resolved = runtime_paths.resolve_runtime_paths(
            database_path=str(database),
            environ={"BTC_DAILY_RANGE_DATA_ROOT": str(root)},
        )

        self.assertEqual(resolved.database_path, database.resolve(strict=False))

    def test_all_derived_paths_share_one_root(self):
        root = Path(tempfile.gettempdir()) / "contract root"

        resolved = runtime_paths.resolve_runtime_paths(
            data_root=str(root),
            environ={},
        )

        expected_root = root.resolve(strict=False)
        self.assertEqual(resolved.runtime_root, expected_root / "runtime")
        self.assertEqual(
            resolved.database_path,
            expected_root / "runtime" / "btc_daily_range.sqlite3",
        )
        self.assertEqual(resolved.backup_root, expected_root / "backups")
        self.assertEqual(resolved.log_root, expected_root / "logs")
        self.assertEqual(resolved.diagnostics_root, expected_root / "diagnostics")

    def test_resolver_does_not_mutate_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "not created"

            runtime_paths.resolve_runtime_paths(data_root=str(root), environ={})

            self.assertFalse(root.exists())

    def test_standard_startup_creates_database_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "startup root"
            with patch.dict(os.environ, {}, clear=True):
                exit_code, _ = self._run_standard(["--data-root", str(root)])

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / "runtime").is_dir())

    def test_python_entrypoints_resolve_equivalent_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "equivalent root"
            log_path = Path(directory) / "launcher.log"
            with patch.dict(os.environ, {}, clear=True):
                standard_exit, standard_backend = self._run_standard(
                    ["--data-root", str(root)]
                )
                windows_exit, captured = self._run_windows(
                    [
                        "--data-root",
                        str(root),
                        "--log-path",
                        str(log_path),
                    ]
                )

            self.assertEqual(standard_exit, 0)
            self.assertEqual(windows_exit, 0)
            self.assertEqual(
                standard_backend.call_args.kwargs["database_path"],
                captured["database_path"],
            )

    def test_unpinned_windows_entrypoint_keeps_external_os_default(self):
        with tempfile.TemporaryDirectory() as directory:
            user_profile = Path(directory) / "user profile"
            with patch.dict(
                os.environ,
                {"USERPROFILE": str(user_profile)},
                clear=True,
            ):
                exit_code, captured = self._run_windows([])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                captured["database_path"],
                user_profile
                / "Documents"
                / "BTC Daily Range"
                / "runtime"
                / "btc_daily_range.sqlite3",
            )

    def test_all_five_security_guards_remain_false(self):
        config = json.loads(
            (PROJECT_ROOT / "config" / "mvp_runtime_v1.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIs(config["trading_approval"], False)
        self.assertIs(config["real_orders_enabled"], False)
        self.assertIs(config["wallet_enabled"], False)
        self.assertIs(config["signing"], False)
        self.assertIs(config["authenticated_clob_writes"], False)

    @staticmethod
    def _run_standard(arguments: list[str]):
        backend_class = patch.object(run_backend, "LiveBackend").start()
        runner = patch.object(
            run_backend,
            "run_backend",
            new=AsyncMock(return_value=0),
        ).start()
        try:
            exit_code = run_backend.main(arguments)
            runner.assert_awaited_once()
            return exit_code, backend_class
        finally:
            patch.stopall()

    @staticmethod
    def _run_windows(arguments: list[str]):
        captured = {}

        class FakeBackend:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        async def clean_runner(backend):
            return 0

        exit_code = run_windows_backend.main(
            arguments,
            backend_factory=FakeBackend,
            backend_runner=clean_runner,
        )
        return exit_code, captured


class WindowsTransitionalLauncherContractTests(unittest.TestCase):
    def test_c9_launcher_pins_pre_a1_database_and_log(self):
        text = C9_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn(
            '$DatabasePath = Join-Path $ProjectRoot "data\\runtime\\btc_live_backend.sqlite3"',
            text,
        )
        self.assertIn(
            '$LogPath = Join-Path $ProjectRoot "data\\runtime\\backend.log"',
            text,
        )
        self.assertIn('"--database-path" $DatabasePath', text)
        self.assertIn('"--log-path" $LogPath', text)

    def test_manual_launcher_matches_c9_transitional_paths_and_entrypoint(self):
        c9_text = C9_LAUNCHER.read_text(encoding="utf-8")
        manual_text = MANUAL_LAUNCHER.read_text(encoding="utf-8")

        for text in (c9_text, manual_text):
            self.assertIn("run_windows_backend.py", text)
            self.assertIn(
                '$DatabasePath = Join-Path $ProjectRoot "data\\runtime\\btc_live_backend.sqlite3"',
                text,
            )
            self.assertIn(
                '$LogPath = Join-Path $ProjectRoot "data\\runtime\\backend.log"',
                text,
            )
            self.assertIn('"--database-path" $DatabasePath', text)
            self.assertIn('"--log-path" $LogPath', text)


if __name__ == "__main__":
    unittest.main()
