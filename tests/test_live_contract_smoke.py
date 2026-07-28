from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import unittest
from pathlib import Path

import run_backend
from src.app import (
    ALREADY_RUNNING_EXIT,
    CLEAN_STOP_EXIT,
    CONFIG_FAILURE_EXIT,
    DATABASE_INTEGRITY_EXIT,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TASK_13_COMMIT = "812628afa5afb5a020dceed4a44d0a3fc8170543"
OFFLINE_REPORT = PROJECT_ROOT / "reports" / "C1_OFFLINE_VERIFICATION.json"
PROVIDER_REPORT = PROJECT_ROOT / "reports" / "C1_PROVIDER_CAPABILITY_SMOKE.json"
LAUNCHERS = (
    PROJECT_ROOT / "scripts" / "RUN_BACKEND_SAFE.ps1",
    PROJECT_ROOT / "scripts" / "RUN_TESTS_SAFE.ps1",
    PROJECT_ROOT / "scripts" / "RUN_C1_ACCEPTANCE_SAFE.ps1",
)
FORBIDDEN_POWERSHELL = (
    "executionpolicy",
    "bypass",
    "unblock-file",
    "powershell.exe",
    "pwsh",
    "start-process",
    ".bat",
    ".cmd",
)


class LiveContractSmokeTests(unittest.TestCase):
    def test_launchers_exist(self):
        self.assertEqual(
            [path.name for path in LAUNCHERS if path.is_file()],
            [path.name for path in LAUNCHERS],
        )

    def test_launchers_use_only_project_venv_python(self):
        for path in LAUNCHERS:
            with self.subTest(path=path.name):
                text = self._launcher(path)
                self.assertIn(r".\.venv\Scripts\python.exe", text)
                without_venv = text.lower().replace(
                    r".\.venv\scripts\python.exe",
                    "",
                )
                self.assertNotRegex(
                    without_venv,
                    r"\b(py|python|python3)\.exe\b",
                )

    def test_launchers_reject_missing_venv_and_wrong_python(self):
        for path in LAUNCHERS:
            with self.subTest(path=path.name):
                text = self._launcher(path)
                self.assertIn("Test-Path -LiteralPath $PythonPath", text)
                self.assertIn(r"^3\.12\.", text)

    def test_launchers_set_isolated_python_environment(self):
        for path in LAUNCHERS:
            with self.subTest(path=path.name):
                text = self._launcher(path)
                self.assertIn("$env:PYTHONNOUSERSITE = \"1\"", text)
                self.assertIn("$env:PYTHONDONTWRITEBYTECODE = \"1\"", text)

    def test_launchers_set_project_root(self):
        for path in LAUNCHERS:
            with self.subTest(path=path.name):
                text = self._launcher(path)
                self.assertIn("Set-Location -LiteralPath $ProjectRoot", text)

    def test_launchers_have_no_forbidden_powershell(self):
        for path in LAUNCHERS:
            text = self._launcher(path).lower()
            for token in FORBIDDEN_POWERSHELL:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_launchers_do_not_install_or_download(self):
        forbidden = (
            "pip install",
            "install-module",
            "invoke-webrequest",
            "invoke-restmethod",
            "curl ",
            "wget ",
        )
        for path in LAUNCHERS:
            text = self._launcher(path).lower()
            for token in forbidden:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_backend_launcher_checks_config_and_returns_backend_exit(self):
        text = self._launcher(LAUNCHERS[0])
        self.assertIn("c0_c1_frozen_config.json", text)
        self.assertIn("run_backend.py", text)
        self.assertIn("exit $BackendExitCode", text)
        self.assertEqual(text.count("Tee-Object -FilePath $RunLogPath"), 1)

    def test_test_launcher_runs_all_offline_commands(self):
        text = self._launcher(LAUNCHERS[1])
        self.assertIn("-m unittest discover -s tests -v", text)
        self.assertIn("-m compileall -q src tests tools run_backend.py", text)
        self.assertIn("-m pip check", text)

    def test_acceptance_launcher_is_offline_and_incomplete(self):
        text = self._launcher(LAUNCHERS[2])
        self.assertIn("-m unittest discover -s tests -v", text)
        self.assertIn("EXTERNAL_PROVIDER_CAPABILITY_NOT_RUN", text)
        self.assertIn("DOWNTIME_ACCEPTANCE_NOT_RUN", text)
        self.assertNotIn("C1 PASS", text)

    def test_python_requirement_is_exact(self):
        payload = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["project"]["requires-python"], "==3.12.4")

    def test_direct_runtime_dependency_is_only_aiohttp(self):
        payload = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["project"]["dependencies"], ["aiohttp==3.14.3"])
        requirements = (
            PROJECT_ROOT / "requirements.in"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(requirements, ["aiohttp==3.14.3"])

    def test_frozen_runtime_config_is_fail_closed(self):
        config = json.loads(
            (
                PROJECT_ROOT / "config" / "c0_c1_frozen_config.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(config["bind_host"], "127.0.0.1")
        self.assertEqual(config["bind_port"], 8767)
        self.assertFalse(config["real_orders_enabled"])
        self.assertFalse(config["wallet_enabled"])
        self.assertFalse(config["paper_enabled"])
        self.assertFalse(config["dashboard_enabled"])
        self.assertEqual(config["database_writer_count"], 1)

    def test_registry_47_remains_non_executable(self):
        contract = json.loads(
            (
                PROJECT_ROOT
                / "contract"
                / "BTC_LIVE_BACKEND_WINDOWS_V1_CONTRACT.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(contract["registry_lock"]["executable_rule_pack_complete"])
        self.assertFalse(contract["safety"]["real_order_submission"])
        self.assertFalse(contract["safety"]["wallet_or_private_key_support"])

    def test_forbidden_production_modules_and_actions_are_absent(self):
        forbidden_modules = (
            "wallet.py",
            "order_execution.py",
            "paper_execution.py",
        )
        source_files = tuple((PROJECT_ROOT / "src").glob("*.py"))
        self.assertFalse(
            [path for path in source_files if path.name in forbidden_modules]
        )
        forbidden_actions = (
            "private_key",
            "sign_order",
            "place_order",
            "cancel_order",
            "api_secret",
        )
        for path in (*source_files, PROJECT_ROOT / "run_backend.py"):
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden_actions:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_startup_entry_point_and_exit_mapping_are_exact(self):
        self.assertTrue(callable(run_backend.main))
        self.assertEqual(
            (
                CLEAN_STOP_EXIT,
                ALREADY_RUNNING_EXIT,
                CONFIG_FAILURE_EXIT,
                DATABASE_INTEGRITY_EXIT,
            ),
            (0, 20, 30, 40),
        )

    def test_readme_documents_safe_runtime_contract(self):
        text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        for expected in (
            r".\scripts\RUN_TESTS_SAFE.ps1",
            r".\scripts\RUN_BACKEND_SAFE.ps1",
            "127.0.0.1:8767",
            "Tasks 13–14",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertNotIn("C1 PASS", text)

    def test_start_here_documents_stop_and_exit_codes(self):
        text = (PROJECT_ROOT / "START_HERE.md").read_text(encoding="utf-8")
        for expected in (
            "Ctrl+C",
            "0",
            "20",
            "30",
            "40",
            "Registry 47",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertNotIn("C1 PASS", text)

    def test_launchers_parse_with_windows_powershell_51_when_available(self):
        executable = shutil.which("powershell.exe")
        if executable is None:
            self.skipTest("Windows PowerShell parser is unavailable")
        for path in LAUNCHERS:
            command = (
                "$Errors = $null; $Tokens = $null; "
                "[System.Management.Automation.Language.Parser]::ParseFile("
                f"'{path}', [ref]$Tokens, [ref]$Errors) | Out-Null; "
                "if ($Errors.Count -ne 0) { "
                "$Errors | ForEach-Object { Write-Error $_ }; exit 1 }"
            )
            result = subprocess.run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            with self.subTest(path=path.name):
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_task_13_reports_exist(self):
        self.assertTrue(OFFLINE_REPORT.is_file())
        self.assertTrue(PROVIDER_REPORT.is_file())

    def test_task_13_report_contracts_are_exact(self):
        offline, provider = self._task_13_reports()
        self.assertEqual(
            offline["schema_version"],
            "BTC_LIVE_BACKEND_C1_OFFLINE_VERIFICATION_V1",
        )
        self.assertEqual(
            provider["schema_version"],
            "BTC_LIVE_BACKEND_C1_PROVIDER_CAPABILITY_V1",
        )
        for report in (offline, provider):
            self.assertEqual(report["commit_sha"], EXPECTED_TASK_13_COMMIT)
            self.assertRegex(report["commit_sha"], r"^[0-9a-f]{40}$")

    def test_task_13_offline_gate_is_pass(self):
        offline, _ = self._task_13_reports()
        self.assertEqual(offline["status"], "PASS")
        self.assertEqual(offline["tests"]["total"], 157)
        self.assertEqual(offline["tests"]["failures"], 0)
        self.assertEqual(offline["tests"]["errors"], 0)
        self.assertEqual(offline["compileall"], "PASS")
        self.assertEqual(offline["pip_check"], "PASS")
        self.assertEqual(
            offline["sqlite"],
            {
                "quick_check": "ok",
                "integrity_check": "ok",
                "journal_mode": "wal",
                "synchronous": 2,
                "foreign_keys": 1,
            },
        )

    def test_task_13_provider_scope_is_public_read_only(self):
        _, provider = self._task_13_reports()
        self.assertEqual(provider["network_scope"], "PUBLIC_READ_ONLY")
        self.assertFalse(provider["authentication_used"])
        self.assertFalse(provider["secrets_used"])
        self.assertFalse(provider["trading_approval"])
        self.assertFalse(provider["task_14_started"])

    def test_task_13_provider_checks_are_complete_or_explicitly_blocked(self):
        _, provider = self._task_13_reports()
        required = {
            "binance_rest",
            "binance_websocket",
            "gamma_discovery",
            "clob_books",
            "clob_price_history",
            "polymarket_websocket",
        }
        self.assertEqual(set(provider["checks"]), required)
        self.assertEqual(provider["checks"]["binance_rest"]["status"], "PASS")
        self.assertEqual(
            provider["checks"]["binance_websocket"]["status"],
            "PASS",
        )

        if provider["status"] == "PASS":
            gamma = provider["checks"]["gamma_discovery"]
            books = provider["checks"]["clob_books"]
            self.assertIsNotNone(gamma["event_id"])
            self.assertEqual(gamma["markets"], 11)
            self.assertEqual(gamma["asset_ids"], 22)
            self.assertEqual(gamma["unique_asset_ids"], 22)
            self.assertEqual(len(set(gamma["canonical_asset_ids"])), 22)
            self.assertEqual(books["requested"], 22)
        else:
            self.assertEqual(
                provider["status"],
                "BLOCKED_EXTERNAL_INVENTORY",
            )
            gamma = provider["checks"]["gamma_discovery"]
            self.assertEqual(gamma["pages_scanned"], 5)
            self.assertEqual(gamma["objects_scanned"], 500)
            self.assertEqual(gamma["asset_ids"], 0)
            self.assertEqual(gamma["unique_asset_ids"], 0)
            for name in (
                "clob_books",
                "clob_price_history",
                "polymarket_websocket",
            ):
                self.assertEqual(
                    provider["checks"][name]["status"],
                    "SKIPPED_BLOCKED_UPSTREAM",
                )
            self.assertEqual(provider["checks"]["clob_books"]["requested"], 0)
            self.assertTrue(provider["blocking_failures"])

    def test_task_13_payload_hashes_are_lowercase_sha256(self):
        _, provider = self._task_13_reports()
        observed = 0
        for check in provider["checks"].values():
            payload_hash = check["payload_sha256"]
            if payload_hash is None:
                self.assertNotEqual(check["status"], "PASS")
                continue
            observed += 1
            self.assertRegex(payload_hash, r"^[0-9a-f]{64}$")
        self.assertGreaterEqual(observed, 2)

    def test_task_13_endpoints_are_allowlisted_and_unauthenticated(self):
        _, provider = self._task_13_reports()
        allowlist = {
            "https://data-api.binance.vision/api/v3/klines",
            "wss://stream.binance.com:9443/ws/btcusdt@kline_1m",
            "https://gamma-api.polymarket.com/events",
            "https://clob.polymarket.com/book",
            "https://clob.polymarket.com/prices-history",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        }
        for check in provider["checks"].values():
            self.assertIn(check["endpoint"], allowlist)
            self.assertFalse(check["auth_used"])
        serialized = json.dumps(provider, sort_keys=True).lower()
        for forbidden in (
            "authorization",
            "api_key",
            "private_key",
            "user channel",
            "place_order",
            "cancel_order",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_task_13_reports_have_no_secret_like_keys(self):
        for report in self._task_13_reports():
            keys = set(self._nested_keys(report))
            for forbidden in (
                "authorization",
                "cookie",
                "credentials",
                "password",
                "private_key",
                "secret",
                "token",
                "wallet_address",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, keys)

    def test_task_14_artifacts_do_not_exist(self):
        forbidden = (
            PROJECT_ROOT / "tools" / "simulate_downtime.py",
            PROJECT_ROOT / "reports" / "C1_DOWNTIME_ACCEPTANCE.json",
            PROJECT_ROOT / "reports" / "C1_FINAL_ACCEPTANCE.json",
            PROJECT_ROOT / "artifacts" / "C1_ACCEPTANCE_PACK.zip",
        )
        self.assertFalse([path for path in forbidden if path.exists()])

    @staticmethod
    def _launcher(path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    @staticmethod
    def _task_13_reports() -> tuple[dict, dict]:
        return (
            json.loads(OFFLINE_REPORT.read_text(encoding="utf-8")),
            json.loads(PROVIDER_REPORT.read_text(encoding="utf-8")),
        )

    @classmethod
    def _nested_keys(cls, value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key.lower()
                yield from cls._nested_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from cls._nested_keys(item)


if __name__ == "__main__":
    unittest.main()
