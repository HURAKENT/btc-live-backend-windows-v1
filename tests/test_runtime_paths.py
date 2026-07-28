from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import run_backend
from src.app import DEFAULT_MUTEX_NAME
from tools import simulate_downtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "data" / "runtime" / "btc_live_backend.sqlite3"
)


class RuntimePathTests(unittest.TestCase):
    def test_no_argument_launch_preserves_exact_default_database_path(self):
        self.assertEqual(
            run_backend.resolve_database_path(None),
            DEFAULT_DATABASE_PATH,
        )

    def test_absolute_database_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            requested = Path(directory) / "acceptance.sqlite3"
            self.assertEqual(
                run_backend.resolve_database_path(str(requested)),
                requested.resolve(strict=False),
            )

    def test_relative_database_path_returns_config_exit_30(self):
        self.assertEqual(
            self._run_main(
                ["--database-path", "relative.sqlite3"],
                expect_run=False,
            )[0],
            30,
        )

    def test_directory_is_rejected_as_database_path(self):
        with tempfile.TemporaryDirectory(suffix=".sqlite3") as directory:
            with self.assertRaisesRegex(
                run_backend.DatabasePathError,
                "DATABASE_PATH_IS_DIRECTORY",
            ):
                run_backend.resolve_database_path(directory)

    def test_non_sqlite3_suffix_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.db"
            with self.assertRaisesRegex(
                run_backend.DatabasePathError,
                "DATABASE_PATH_SUFFIX",
            ):
                run_backend.resolve_database_path(str(path))

    def test_parent_directory_is_created_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "acceptance.sqlite3"
            resolved = run_backend.resolve_database_path(str(path))
            self.assertTrue(resolved.parent.is_dir())
            self.assertFalse(resolved.exists())

    def test_resolved_path_is_passed_to_backend_application(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite3"
            exit_code, backend_class = self._run_main(
                ["--database-path", str(path)]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                backend_class.call_args.kwargs["database_path"],
                path.resolve(strict=False),
            )

    def test_sequential_bootstraps_use_same_acceptance_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite3"
            observed = []
            for _ in range(2):
                exit_code, backend_class = self._run_main(
                    ["--database-path", str(path)]
                )
                self.assertEqual(exit_code, 0)
                observed.append(backend_class.call_args.kwargs["database_path"])
            self.assertEqual(observed, [path.resolve(), path.resolve()])

    def test_acceptance_command_uses_current_python_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite3"
            command = simulate_downtime.backend_command(path)
            self.assertEqual(command[0], simulate_downtime.sys.executable)

    def test_initial_and_restart_commands_use_one_exact_database_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite3"
            initial = simulate_downtime.backend_command(path)
            restart = simulate_downtime.backend_command(path)
            self.assertEqual(initial, restart)
            self.assertEqual(
                initial[-1],
                str(path.resolve(strict=False)),
            )

    def test_acceptance_tool_rejects_default_database(self):
        data_root = PROJECT_ROOT.parent / "acceptance-data"
        with self.assertRaisesRegex(
            simulate_downtime.AcceptanceBlocked,
            "BLOCKED_RUNTIME_PATH_CONTRACT",
        ):
            simulate_downtime.validate_acceptance_database_path(
                DEFAULT_DATABASE_PATH,
                data_root,
            )

    def test_acceptance_tool_rejects_path_outside_run_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data-root"
            outside = Path(directory) / "outside" / "acceptance.sqlite3"
            with self.assertRaisesRegex(
                simulate_downtime.AcceptanceBlocked,
                "BLOCKED_RUNTIME_PATH_CONTRACT",
            ):
                simulate_downtime.validate_acceptance_database_path(
                    outside,
                    data_root,
                )

    def test_acceptance_tool_accepts_precreated_empty_run_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "run"
            data_root.mkdir()
            simulate_downtime.prepare_run_data_root(data_root)
            self.assertEqual(list(data_root.iterdir()), [])

    def test_acceptance_tool_rejects_nonempty_run_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "run"
            data_root.mkdir()
            (data_root / "unexpected.txt").write_text(
                "unexpected",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                simulate_downtime.AcceptanceBlocked,
                "BLOCKED_ACCEPTANCE_DATA_ROOT_NOT_EMPTY",
            ):
                simulate_downtime.prepare_run_data_root(data_root)

    def test_default_database_is_unchanged_by_isolated_bootstrap(self):
        before = simulate_downtime.path_fingerprint(DEFAULT_DATABASE_PATH)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite3"
            exit_code, _ = self._run_main(["--database-path", str(path)])
            self.assertEqual(exit_code, 0)
        self.assertEqual(
            simulate_downtime.path_fingerprint(DEFAULT_DATABASE_PATH),
            before,
        )

    def test_invalid_path_does_not_create_partial_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.db"
            exit_code, _ = self._run_main(
                ["--database-path", str(path)],
                expect_run=False,
            )
            self.assertEqual(exit_code, 30)
            self.assertFalse(path.exists())

    def test_path_contract_does_not_change_mutex_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.sqlite3"
            exit_code, backend_class = self._run_main(
                ["--database-path", str(path)]
            )
            self.assertEqual(exit_code, 0)
            self.assertNotIn("mutex_name", backend_class.call_args.kwargs)
            self.assertEqual(DEFAULT_MUTEX_NAME, "BTC_LIVE_BACKEND_WINDOWS_V1")

    def test_path_contract_adds_no_auth_order_or_wallet_scope(self):
        text = "\n".join(
            path.read_text(encoding="utf-8").lower()
            for path in (
                PROJECT_ROOT / "run_backend.py",
                PROJECT_ROOT / "tools" / "simulate_downtime.py",
            )
        )
        for forbidden in (
            "authorization",
            "private_key",
            "sign_order",
            "place_order",
            "cancel_order",
            "wallet_address",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    @staticmethod
    def _run_main(arguments: list[str], *, expect_run: bool = True):
        backend_class = patch.object(run_backend, "LiveBackend").start()
        run_mock = patch.object(
            run_backend,
            "run_backend",
            new=AsyncMock(return_value=0),
        ).start()
        try:
            exit_code = run_backend.main(arguments)
            if expect_run:
                run_mock.assert_awaited_once()
            else:
                run_mock.assert_not_awaited()
            return exit_code, backend_class
        finally:
            patch.stopall()


if __name__ == "__main__":
    unittest.main()
