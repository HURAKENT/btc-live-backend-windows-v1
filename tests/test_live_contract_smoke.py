from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from datetime import datetime
from pathlib import Path, PurePath

import run_backend
from src.app import (
    ALREADY_RUNNING_EXIT,
    CLEAN_STOP_EXIT,
    CONFIG_FAILURE_EXIT,
    DATABASE_INTEGRITY_EXIT,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TASK_13_OFFLINE_COMMIT = "812628afa5afb5a020dceed4a44d0a3fc8170543"
EXPECTED_TASK_13_PROVIDER_BASE = "46e8f3ded26c31f77a79b5bbdb02a290e97ca45c"
EXPECTED_TASK_14_COMMIT = "a6348f6e0c4e0eee4bd529d35d360a99e8d455fb"
OFFLINE_REPORT = PROJECT_ROOT / "reports" / "C1_OFFLINE_VERIFICATION.json"
PROVIDER_REPORT = PROJECT_ROOT / "reports" / "C1_PROVIDER_CAPABILITY_SMOKE.json"
DOWNTIME_REPORT = PROJECT_ROOT / "reports" / "C1_DOWNTIME_ACCEPTANCE.json"
FINAL_ACCEPTANCE_REPORT = PROJECT_ROOT / "reports" / "C1_FINAL_ACCEPTANCE.json"
ACCEPTANCE_PACK = PROJECT_ROOT / "artifacts" / "C1_ACCEPTANCE_PACK.zip"
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

    def test_acceptance_launcher_runs_acceptance_tool_directly(self):
        text = self._launcher(LAUNCHERS[2])
        self.assertIn("System.Diagnostics.ProcessStartInfo", text)
        self.assertIn("Invoke-NativePython", text)
        self.assertEqual(text.count("ReadToEndAsync()"), 2)
        self.assertIn(
            '@("tools\\simulate_downtime.py")',
            text,
        )
        self.assertIn(
            '[ValidateSet("Run", "Preflight", "Offline")]',
            text,
        )
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

    def test_c1_closure_state_is_consistent(self):
        progress = (
            PROJECT_ROOT / "docs" / "C1_GOAL_PROGRESS.md"
        ).read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        start_here = (
            PROJECT_ROOT / "START_HERE.md"
        ).read_text(encoding="utf-8")

        self.assertIn("C1_AUTONOMOUS_GOAL_COMPLETE", progress)
        self.assertIn("BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS", progress)
        self.assertIn("Current Blocker", progress)
        self.assertIn("`NONE`", progress)
        self.assertIn(
            "C1-ACCEPTANCE-20260730T145425Z-F5722878",
            progress,
        )
        self.assertIn("Manual Action Required", progress)
        self.assertIn("No.", progress)
        markers = (
            "C1.1 hardening: PENDING_WINDOWS_OFFLINE_VERIFICATION",
            "C1.1 hardening: READY_TO_COMMIT",
        )
        self.assertEqual(sum(marker in progress for marker in markers), 1)
        self.assertNotIn("C1_1_CLOSURE_HARDENING_COMPLETE", progress)

        for document in (readme, start_here):
            self.assertIn("C0–C1", document)
            self.assertIn("COMPLETE", document)
            self.assertIn("trading approval", document.lower())
            self.assertIn("NORMAL OPERATION / SIGNAL ONLY", document)
            self.assertIn("OPTIONAL / DEFERRED", document)
            self.assertNotIn("remain mandatory", document)
            self.assertNotIn("still mandatory", document)

    def test_manual_task14_receipt_remains_outside_repository(self):
        text = (
            PROJECT_ROOT / "scripts" / "RUN_TASK14_MANUAL.ps1"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("LATEST_TASK14_LAUNCHER_RECEIPT.json", text)
        self.assertIn("task14_manual_logs", text)
        self.assertNotIn("C1_LAUNCHER_RECEIPT.json", text)

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
        self.assertEqual(
            offline["commit_sha"],
            EXPECTED_TASK_13_OFFLINE_COMMIT,
        )
        self.assertEqual(
            provider["commit_sha"],
            EXPECTED_TASK_13_PROVIDER_BASE,
        )
        for report in (offline, provider):
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
        elif provider["status"] == "WAIT_EXTERNAL_INVENTORY":
            gamma = provider["checks"]["gamma_discovery"]
            self.assertEqual(gamma["asset_ids"], 0)
            self.assertEqual(gamma["unique_asset_ids"], 0)
            targeted = provider["targeted_discovery"]
            self.assertEqual(targeted["manual_structural_match_count"], 0)
            self.assertEqual(
                targeted["root_cause"],
                "NO_DISCOVERABLE_ACTIVE_CANONICAL_EVENT",
            )
        else:
            self.assertIn(
                provider["status"],
                {
                    "BLOCKED_DISCOVERY_SCHEMA",
                    "BLOCKED_AMBIGUOUS_DISCOVERY",
                    "BLOCKED_PROVIDER_NETWORK",
                    "BLOCKED_PROVIDER_SCHEMA",
                },
            )
            self.assertTrue(provider["blocking_failures"])

        if provider["status"] in {
            "WAIT_EXTERNAL_INVENTORY",
            "BLOCKED_DISCOVERY_SCHEMA",
            "BLOCKED_AMBIGUOUS_DISCOVERY",
            "BLOCKED_PROVIDER_NETWORK",
        }:
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
        elif provider["status"] == "BLOCKED_PROVIDER_SCHEMA":
            self.assertEqual(
                provider["checks"]["gamma_discovery"]["status"],
                "PASS",
            )
            self.assertEqual(provider["checks"]["clob_books"]["requested"], 22)
            for name in (
                "clob_books",
                "clob_price_history",
                "polymarket_websocket",
            ):
                self.assertEqual(
                    provider["checks"][name]["status"],
                    "BLOCKED_PROVIDER_SCHEMA",
                )

    def test_task_13_keyset_diagnostic_contract_is_exact(self):
        _, provider = self._task_13_reports()
        diagnostic = provider["gamma_discovery_diagnostic"]
        self.assertEqual(diagnostic["pagination_mode"], "KEYSET")
        self.assertEqual(
            diagnostic["offset_limit_evidence"],
            {
                "last_successful_offset": 2000,
                "next_offset": 2100,
                "http_status": 422,
                "provider_message": (
                    "offset too large, use /events/keyset for deeper pagination"
                ),
            },
        )
        contract = diagnostic["keyset_contract"]
        self.assertEqual(
            contract["endpoint"],
            "https://gamma-api.polymarket.com/events/keyset",
        )
        self.assertEqual(contract["top_level_type"], "dict")
        self.assertEqual(
            contract["top_level_fields"],
            ["$schema", "events", "next_cursor"],
        )
        self.assertEqual(contract["event_container_field"], "events")
        self.assertEqual(contract["event_container_type"], "list")
        self.assertEqual(contract["cursor_fields"], ["next_cursor"])
        self.assertEqual(contract["response_cursor_field"], "next_cursor")
        self.assertEqual(
            contract["request_cursor_parameter"],
            "after_cursor",
        )
        self.assertEqual(
            contract["incorrect_parameter_previously_tested"],
            "cursor",
        )
        self.assertTrue(contract["official_contract_confirmed"])
        self.assertIn("timestamp_representation", contract)
        self.assertFalse(contract["auth_used"])
        self.assertIs(type(diagnostic["pages_scanned"]), int)
        self.assertIs(type(diagnostic["unique_objects_scanned"]), int)
        self.assertIn(
            diagnostic["root_cause"],
            {
                "CANONICAL_MARKET_FOUND",
                "EXTERNAL_INVENTORY_ABSENT",
                "DISCOVERY_SCHEMA_MISMATCH",
                "AMBIGUOUS_DISCOVERY",
                "SCAN_INCOMPLETE",
            },
        )

    def test_task_13_keyset_page_evidence_has_hashes_without_raw_pages(self):
        _, provider = self._task_13_reports()
        diagnostic = provider["gamma_discovery_diagnostic"]
        self.assertEqual(
            len(diagnostic["page_summaries"]),
            diagnostic["pages_scanned"],
        )
        required = {
            "page_index",
            "events_count",
            "new_unique_events",
            "latency_ms",
            "payload_sha256",
            "input_after_cursor_sha256",
            "response_next_cursor_sha256",
            "http_status",
        }
        forbidden_raw = {"events", "raw_page", "raw_payload", "payload"}
        for page in diagnostic["page_summaries"]:
            self.assertEqual(set(page), required)
            self.assertFalse(set(page) & forbidden_raw)
            for name in (
                "payload_sha256",
                "input_after_cursor_sha256",
                "response_next_cursor_sha256",
            ):
                value = page[name]
                if value is not None:
                    self.assertRegex(value, r"^[0-9a-f]{64}$")

        cumulative_unique = 0
        previous_cumulative = 0
        for index, page in enumerate(diagnostic["page_summaries"]):
            cumulative_unique += page["new_unique_events"]
            self.assertGreaterEqual(cumulative_unique, previous_cumulative)
            previous_cumulative = cumulative_unique
            if index == 0:
                self.assertIsNone(page["input_after_cursor_sha256"])
            else:
                self.assertEqual(
                    page["input_after_cursor_sha256"],
                    diagnostic["page_summaries"][index - 1][
                        "response_next_cursor_sha256"
                    ],
                )
        self.assertEqual(
            cumulative_unique,
            diagnostic["unique_objects_scanned"],
        )

    def test_task_13_repeated_page_is_never_successful_pagination(self):
        _, provider = self._task_13_reports()
        diagnostic = provider["gamma_discovery_diagnostic"]
        if diagnostic["termination_reason"] in {
            "REPEATED_PAGE",
            "REPEATED_NEXT_CURSOR",
        }:
            self.assertNotEqual(provider["status"], "PASS")
            self.assertFalse(diagnostic["scan_complete_within_bound"])
        self.assertEqual(
            diagnostic["keyset_contract"]["request_cursor_parameter"],
            "after_cursor",
        )

    def test_task_13_keyset_status_evidence_is_fail_closed(self):
        _, provider = self._task_13_reports()
        targeted = provider["targeted_discovery"]
        status = provider["status"]
        if status == "PASS":
            gamma = provider["checks"]["gamma_discovery"]
            self.assertEqual(gamma["markets"], 11)
            self.assertEqual(gamma["asset_ids"], 22)
            self.assertEqual(gamma["unique_asset_ids"], 22)
            self.assertEqual(provider["checks"]["clob_books"]["requested"], 22)
            self.assertEqual(provider["checks"]["clob_books"]["status"], "PASS")
            self.assertEqual(
                provider["checks"]["clob_price_history"]["status"],
                "PASS",
            )
            self.assertEqual(
                provider["checks"]["polymarket_websocket"]["status"],
                "PASS",
            )
        elif status == "WAIT_EXTERNAL_INVENTORY":
            self.assertEqual(targeted["manual_structural_match_count"], 0)
            self.assertEqual(
                targeted["root_cause"],
                "NO_DISCOVERABLE_ACTIVE_CANONICAL_EVENT",
            )
        elif status == "BLOCKED_DISCOVERY_SCHEMA":
            self.assertGreaterEqual(
                targeted["manual_structural_match_count"],
                1,
            )
            rejected = [
                candidate
                for candidate in targeted["candidate_slices"]
                if candidate["manual_structural_match"]
                and not candidate["adapter_result"]["accepted"]
            ]
            self.assertTrue(rejected)
            for candidate in rejected:
                self.assertIn("error", candidate["adapter_result"])
        elif status == "BLOCKED_AMBIGUOUS_DISCOVERY":
            self.assertGreaterEqual(
                targeted["manual_structural_match_count"],
                2,
            )
            self.assertEqual(targeted["root_cause"], "AMBIGUOUS_DISCOVERY")
        elif status == "BLOCKED_PROVIDER_NETWORK":
            self.assertEqual(status, "BLOCKED_PROVIDER_NETWORK")
            self.assertEqual(
                targeted["root_cause"],
                "NETWORK_POLICY_OR_GEO_RESTRICTION",
            )
            self.assertTrue(provider["blocking_failures"])
        else:
            self.assertEqual(status, "BLOCKED_PROVIDER_SCHEMA")
            remediation = provider["discovery_remediation"]
            self.assertEqual(
                remediation["selection_policy"],
                "NEAREST_FUTURE_RESOLUTION",
            )
            self.assertEqual(
                provider["checks"]["gamma_discovery"]["status"],
                "PASS",
            )
            self.assertTrue(provider["blocking_failures"])

        self.assertFalse(provider["task_14_started"])
        self.assertFalse(provider["trading_approval"])
        self.assertNotEqual(provider["status"], "C1_PASS")

    def test_task_13_candidate_slice_hashes_are_reproducible(self):
        _, provider = self._task_13_reports()
        candidate_groups = (
            provider["gamma_discovery_diagnostic"]["candidate_slices"],
            provider["targeted_discovery"]["candidate_slices"],
        )
        for candidates in candidate_groups:
            self.assertLessEqual(len(candidates), 20)
            for candidate in candidates:
                expected = candidate["slice_sha256"]
                unhashed = dict(candidate)
                del unhashed["slice_sha256"]
                encoded = json.dumps(
                    unhashed,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(
                    hashlib.sha256(encoded).hexdigest(),
                    expected,
                )

    def test_task_13_targeted_discovery_contract_is_exact(self):
        _, provider = self._task_13_reports()
        targeted = provider["targeted_discovery"]
        self.assertEqual(
            targeted["strategy"],
            "KEYSET_TITLE_SEARCH_THEN_PUBLIC_SEARCH",
        )
        window = targeted["date_window"]
        end_min = datetime.fromisoformat(
            window["end_date_min"].replace("Z", "+00:00")
        )
        end_max = datetime.fromisoformat(
            window["end_date_max"].replace("Z", "+00:00")
        )
        self.assertIsNotNone(end_min.tzinfo)
        self.assertIsNotNone(end_max.tzinfo)
        self.assertLess(end_min, end_max)
        self.assertLessEqual(len(targeted["keyset_queries"]), 3)
        self.assertLessEqual(len(targeted["public_search_queries"]), 3)

        allowed_titles = {"Bitcoin price on", "Bitcoin", "BTC"}
        for query in targeted["keyset_queries"]:
            self.assertIn(query["title_search"], allowed_titles)
            parameter_names = set(query["parameter_names"])
            self.assertIn("title_search", parameter_names)
            self.assertNotIn("active", parameter_names)
            self.assertNotIn("live", parameter_names)
            self.assertNotIn("offset", parameter_names)
            self.assertNotIn("cursor", parameter_names)
            self.assertLessEqual(len(query["pages"]), 3)
            for page in query["pages"]:
                self.assertRegex(
                    page["payload_sha256"],
                    r"^[0-9a-f]{64}$",
                )

        allowed_searches = {
            "Bitcoin price on",
            "Bitcoin",
            "BTC daily range",
        }
        for query in targeted["public_search_queries"]:
            self.assertIn(query["q"], allowed_searches)
            self.assertIn("q", query["parameter_names"])
            self.assertFalse(query["auth_used"])

    def test_task_13_targeted_candidates_are_deduplicated(self):
        _, provider = self._task_13_reports()
        targeted = provider["targeted_discovery"]
        candidate_ids = [
            candidate["event_id"]
            for candidate in targeted["candidate_slices"]
        ]
        self.assertEqual(len(candidate_ids), len(set(candidate_ids)))
        self.assertGreaterEqual(
            targeted["unique_candidate_count"],
            len(candidate_ids),
        )
        manual_matches = sum(
            candidate["manual_structural_match"]
            for candidate in targeted["candidate_slices"]
        )
        self.assertEqual(
            manual_matches,
            targeted["manual_structural_match_count"],
        )

    def test_task_13_targeted_client_security_is_fail_closed(self):
        _, provider = self._task_13_reports()
        targeted = provider["targeted_discovery"]
        security = targeted["client_security"]
        self.assertEqual(security["cookie_jar"], "DummyCookieJar")
        self.assertFalse(security["trust_env"])
        self.assertLessEqual(security["connect_timeout_seconds"], 10)
        self.assertLessEqual(security["request_timeout_seconds"], 20)
        self.assertLessEqual(security["transport_retries_max"], 2)
        self.assertFalse(security["vpn_or_proxy_bypass_used"])
        self.assertFalse(security["authentication_used"])

        allowed_exact = {
            "https://gamma-api.polymarket.com/events/keyset",
            "https://gamma-api.polymarket.com/public-search",
            "https://clob.polymarket.com/book",
            "https://clob.polymarket.com/prices-history",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        }
        detail = re.compile(
            r"^https://gamma-api\.polymarket\.com/events/[A-Za-z0-9_-]+$"
        )
        for request in targeted["network_request_evidence"]:
            endpoint = request["endpoint"]
            self.assertTrue(
                endpoint in allowed_exact or detail.fullmatch(endpoint)
            )
            self.assertEqual(request["method"], "GET")
            self.assertFalse(request["auth_used"])

        if provider["status"] == "BLOCKED_PROVIDER_NETWORK":
            self.assertEqual(
                targeted["root_cause"],
                "NETWORK_POLICY_OR_GEO_RESTRICTION",
            )
            restriction_evidence = [
                failure
                for failure in provider["blocking_failures"]
                if isinstance(failure, dict)
                and failure.get("http_status") in {403, 451}
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    failure.get("payload_sha256", ""),
                )
                and failure.get("subreason")
                == "NETWORK_POLICY_OR_GEO_RESTRICTION"
            ]
            self.assertTrue(restriction_evidence)

    def test_task_13_discovery_remediation_contract_is_exact(self):
        _, provider = self._task_13_reports()
        remediation = provider["discovery_remediation"]
        self.assertEqual(
            remediation["root_cause"],
            "LEGACY_IDENTIFIER_ONLY_AND_NO_NEAREST_FUTURE_POLICY",
        )
        self.assertTrue(remediation["legacy_identifier_supported"])
        self.assertTrue(remediation["current_identifier_supported"])
        self.assertEqual(
            remediation["current_identifier_pattern"],
            "bitcoin-price-on-YYYY-MM-DD",
        )
        self.assertEqual(
            remediation["selection_policy"],
            "NEAREST_FUTURE_RESOLUTION",
        )
        self.assertEqual(
            remediation["tie_policy"],
            "AMBIGUOUS_ONLY_AT_EQUAL_NEAREST_RESOLUTION",
        )
        self.assertTrue(remediation["red_test_confirmed"])
        self.assertEqual(remediation["targeted_query_events"], 6)
        self.assertEqual(remediation["valid_future_candidates"], 5)
        self.assertEqual(remediation["later_valid_events_ignored"], 4)
        self.assertEqual(remediation["markets"], 11)
        self.assertEqual(remediation["unique_market_ids"], 11)
        self.assertEqual(remediation["asset_ids"], 22)
        self.assertEqual(remediation["unique_asset_ids"], 22)

    def test_task_13_remediation_pass_requires_all_clob_checks(self):
        _, provider = self._task_13_reports()
        checks = provider["checks"]
        if provider["status"] == "PASS":
            books = checks["clob_books"]
            websocket = checks["polymarket_websocket"]
            self.assertEqual(books["successful"], 22)
            self.assertEqual(books["failed"], 0)
            self.assertEqual(checks["clob_price_history"]["status"], "PASS")
            self.assertEqual(websocket["handshake"], "PASS")
            self.assertTrue(websocket["ping_sent"])
            self.assertTrue(websocket["pong_received"])
            self.assertTrue(websocket["event_received"])
            self.assertEqual(provider["gate"], "C1_PROVIDER_CAPABILITY_PASS")
        else:
            self.assertNotEqual(
                provider.get("gate"),
                "C1_PROVIDER_CAPABILITY_PASS",
            )
            self.assertTrue(provider["blocking_failures"])

    def test_task_13_clob_boundary_remediation_contract_is_exact(self):
        _, provider = self._task_13_reports()
        remediation = provider["clob_boundary_remediation"]
        self.assertEqual(
            remediation["root_cause"],
            "WIRE_TYPES_DID_NOT_MATCH_STRICT_PROVIDER_BOUNDARY",
        )
        self.assertEqual(
            remediation["book_timestamp_normalization"],
            "POSITIVE_ASCII_DIGITS_TO_INT",
        )
        self.assertTrue(remediation["rest_and_ws_share_timestamp_policy"])
        self.assertEqual(
            remediation["history_json_fractional_number_decoder"],
            "DECIMAL",
        )
        self.assertFalse(remediation["binary_float_price_allowed"])
        self.assertTrue(remediation["red_test_confirmed"])
        self.assertEqual(
            set(remediation["network_allowlist"]),
            {
                "https://clob.polymarket.com/book",
                "https://clob.polymarket.com/prices-history",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            },
        )
        self.assertEqual(remediation["cookie_jar"], "DummyCookieJar")
        self.assertFalse(remediation["trust_env"])
        self.assertFalse(remediation["gamma_rerun"])
        self.assertFalse(remediation["binance_rerun"])
        self.assertFalse(remediation["vpn_or_proxy_bypass_used"])
        self.assertFalse(remediation["authentication_used"])

        if provider["status"] == "PASS":
            books = provider["checks"]["clob_books"]
            history = provider["checks"]["clob_price_history"]
            websocket = provider["checks"]["polymarket_websocket"]
            self.assertEqual(books["requested"], 22)
            self.assertEqual(books["http_200"], 22)
            self.assertEqual(books["parser_successful"], 22)
            self.assertEqual(books["failed"], 0)
            self.assertEqual(history["float_prices_observed"], 0)
            self.assertEqual(history["status"], "PASS")
            self.assertEqual(websocket["handshake"], "PASS")
            self.assertTrue(websocket["subscription_sent"])
            self.assertTrue(websocket["ping_sent"])
            self.assertTrue(websocket["pong_received"])
            self.assertTrue(websocket["canonical_event_accepted"])
            self.assertEqual(provider["blocking_failures"], [])
            self.assertEqual(provider["gate"], "C1_PROVIDER_CAPABILITY_PASS")
        self.assertFalse(provider["task_14_started"])
        self.assertFalse(provider["trading_approval"])
        self.assertFalse(provider["authentication_used"])
        self.assertFalse(provider["secrets_used"])

    def test_task_13_clob_schema_blocker_is_exact_and_hashed(self):
        _, provider = self._task_13_reports()
        if provider["status"] != "BLOCKED_PROVIDER_SCHEMA":
            self.skipTest("provider schema blocker is not the current result")
        books = provider["checks"]["clob_books"]
        history = provider["checks"]["clob_price_history"]
        websocket = provider["checks"]["polymarket_websocket"]
        self.assertEqual(books["requested"], 22)
        self.assertEqual(books["successful"], 0)
        self.assertEqual(books["failed"], 22)
        self.assertEqual(books["timestamp_observed_type"], "str")
        self.assertEqual(books["timestamp_expected_type"], "int")
        self.assertEqual(len(books["per_asset_payload_sha256"]), 22)
        for payload_hash in books["per_asset_payload_sha256"]:
            self.assertRegex(payload_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(history["http_status"], 200)
        self.assertEqual(history["points_count"], 31)
        self.assertEqual(history["timestamp_observed_type"], "int")
        self.assertEqual(history["price_observed_type"], "float")
        self.assertEqual(history["price_expected_type"], "str")
        self.assertEqual(websocket["handshake"], "PASS")
        self.assertTrue(websocket["subscription_sent"])
        self.assertTrue(websocket["ping_sent"])
        self.assertFalse(websocket["pong_received"])

    def test_task_13_report_contains_no_raw_provider_dumps(self):
        _, provider = self._task_13_reports()
        keys = set(self._nested_keys(provider))
        self.assertFalse(
            keys
            & {
                "raw_book",
                "raw_books",
                "raw_gamma_page",
                "raw_payload",
                "raw_websocket_log",
            }
        )

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
            "https://gamma-api.polymarket.com/events/keyset",
            "https://gamma-api.polymarket.com/public-search",
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

    def test_task_14_acceptance_evidence_exists(self):
        expected = (
            PROJECT_ROOT / "tools" / "simulate_downtime.py",
            DOWNTIME_REPORT,
            FINAL_ACCEPTANCE_REPORT,
            ACCEPTANCE_PACK,
        )
        self.assertEqual(
            [path for path in expected if path.is_file()],
            list(expected),
        )

    def test_task_14_report_contract_and_run_identity_are_exact(self):
        downtime, final = self._task_14_reports()
        self.assertEqual(
            downtime["schema_version"],
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_DOWNTIME_ACCEPTANCE_V1",
        )
        self.assertEqual(
            final["schema_version"],
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_FINAL_ACCEPTANCE_V1",
        )
        self.assertEqual(downtime["run_id"], final["run_id"])
        self.assertRegex(
            downtime["run_id"],
            r"^C1-ACCEPTANCE-\d{8}T\d{6}Z-[0-9A-F]{8}$",
        )
        if downtime["status"] == "PASS":
            self.assertRegex(downtime["commit_sha"], r"^[0-9a-f]{40}$")
            self.assertEqual(
                downtime["runtime_baseline_commit"],
                "eec05197f05c3d6887bbc49a5edd125ad784cd46",
            )
            self.assertRegex(
                downtime["acceptance_harness_commit"],
                r"^[0-9a-f]{40}$",
            )
            self.assertEqual(
                final["acceptance_harness_commit"],
                downtime["acceptance_harness_commit"],
            )
            self.assertEqual(
                downtime["source_commit"],
                downtime["commit_sha"],
            )
        else:
            self.assertEqual(
                downtime["commit_sha"],
                EXPECTED_TASK_14_COMMIT,
            )
        self.assertEqual(final["commit_sha"], downtime["commit_sha"])
        self.assertEqual(
            downtime["commit_under_test"],
            downtime["commit_sha"],
        )
        self.assertEqual(final["commit_under_test"], downtime["commit_sha"])

    def test_task_14_runtime_path_contract_passed_before_live_ready_blocker(self):
        downtime, final = self._task_14_reports()
        if downtime["status"] == "PASS":
            self.assertEqual(
                final["gate"],
                "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS",
            )
            self.assertTrue(
                downtime["database_path_contract"][
                    "same_path_for_initial_and_restart"
                ]
            )
            self.assertTrue(
                downtime["database_path_contract"][
                    "default_database_unchanged"
                ]
            )
            return
        self.assertEqual(
            downtime["status"],
            "BLOCKED_INITIAL_LIVE_READY",
        )
        self.assertEqual(final["status"], downtime["status"])
        self.assertEqual(final["gate"], "NOT_REACHED")
        contract = downtime["database_path_contract"]
        self.assertTrue(contract["supported"])
        self.assertTrue(contract["custom_database_path_used"])
        self.assertEqual(
            contract["configured_database_path"],
            "data/runtime/btc_live_backend.sqlite3",
        )
        self.assertEqual(
            contract["supported_cli_path_parameters"],
            ["--database-path"],
        )
        self.assertEqual(contract["supported_environment_path_variables"], [])
        self.assertEqual(
            contract["initial_database_path_sha256"],
            contract["restart_database_path_sha256"],
        )
        self.assertTrue(contract["same_path_for_initial_and_restart"])
        self.assertTrue(contract["default_database_unchanged"])
        self.assertEqual(
            downtime["blocking_failures"][0]["code"],
            "BLOCKED_INITIAL_LIVE_READY",
        )

    def test_task_14_initial_live_ready_blocker_stopped_before_fake_downtime(self):
        downtime, _ = self._task_14_reports()
        if downtime["status"] == "PASS":
            self.assertTrue(downtime["initial_state"]["live_ready"])
            self.assertEqual(
                downtime["downtime"]["initial_backend_exit_code"],
                0,
            )
            return
        self.assertTrue(downtime["task_14_started"])
        self.assertTrue(downtime["task_14_completed"])
        self.assertTrue(downtime["initial_state"]["backend_process_started"])
        observation = downtime["initial_state"]["observation"]
        self.assertFalse(observation["live_ready"])
        self.assertEqual(observation["health_status"], "PASS")
        self.assertIsNone(observation["startup_state"])
        self.assertEqual(observation["source_names"], [])
        self.assertFalse(observation["current_market_identity_present"])
        self.assertFalse(downtime["security"]["network_used"])
        self.assertFalse(downtime["security"]["forced_termination_used"])
        self.assertIsNone(downtime["downtime"]["downtime_duration_ms"])
        self.assertIsInstance(
            downtime["downtime"]["process_stopped_at_ms"],
            int,
        )
        self.assertIsNone(downtime["downtime"]["restart_requested_at_ms"])
        self.assertNotEqual(
            downtime["downtime"]["initial_backend_exit_code"],
            0,
        )
        self.assertEqual(
            [item["code"] for item in downtime["blocking_failures"]],
            [
                "BLOCKED_INITIAL_LIVE_READY",
                "BLOCKED_GRACEFUL_SHUTDOWN",
            ],
        )
        self.assertEqual(
            downtime["final_state"],
            {
                "backend_absent": True,
                "mutex_free": True,
                "port_8767_free": True,
            },
        )

    def test_task_14_isolated_database_integrity_is_preserved(self):
        downtime, _ = self._task_14_reports()
        if downtime["status"] == "PASS":
            integrity = downtime["database_integrity"]
            self.assertEqual(integrity["status"], "PASS")
            self.assertEqual(integrity["quick_check"], "ok")
            self.assertEqual(integrity["integrity_check"], "ok")
            self.assertEqual(integrity["journal_mode"], "wal")
            self.assertEqual(integrity["synchronous"], 2)
            self.assertEqual(integrity["foreign_keys"], 1)
            self.assertEqual(integrity["migration_version"], 2)
            self.assertEqual(integrity["table_count"], 9)
            return
        self.assertEqual(
            downtime["database_integrity"],
            {
                "foreign_keys": 1,
                "integrity_check": "ok",
                "journal_mode": "wal",
                "quick_check": "ok",
                "status": "PASS",
                "synchronous": 2,
            },
        )

    def test_task_14_pass_contract_is_complete_when_status_is_pass(self):
        downtime, final = self._task_14_reports()
        if downtime["status"] != "PASS":
            self.assertNotEqual(
                final["gate"],
                "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS",
            )
            return
        duration = downtime["downtime"]["downtime_duration_ms"]
        self.assertGreaterEqual(duration, 600_000)
        self.assertLessEqual(duration, 630_000)
        self.assertEqual(
            downtime["binance_continuity"][
                "missing_binance_closed_minutes"
            ],
            0,
        )
        self.assertEqual(
            downtime["binance_continuity"][
                "duplicate_binance_natural_keys"
            ],
            0,
        )
        self.assertTrue(downtime["polymarket_reconciliation"]["reconciled"])
        self.assertNotEqual(
            downtime["polymarket_reconciliation"][
                "historical_depth_classification"
            ],
            "",
        )
        self.assertTrue(downtime["outbox_replay"]["proved"])
        self.assertEqual(
            final["gate"],
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS",
        )

    def test_task_14_acceptance_pack_is_safe_and_self_verified(self):
        from tools import simulate_downtime

        _, final = self._task_14_reports()
        evidence = simulate_downtime.validate_acceptance_pack(ACCEPTANCE_PACK)
        self.assertEqual(evidence, final["acceptance_pack"])
        self.assertEqual(
            hashlib.sha256(ACCEPTANCE_PACK.read_bytes()).hexdigest(),
            evidence["sha256"],
        )
        self.assertTrue(evidence["crc_pass"])
        self.assertTrue(evidence["internal_sha256s_verified"])
        self.assertEqual(evidence["path_traversal_entries"], 0)
        self.assertEqual(evidence["duplicate_entries"], 0)
        self.assertEqual(evidence["missing_sha_entries"], 0)
        self.assertEqual(evidence["sha_mismatch"], 0)

    def test_task_14_acceptance_pack_has_only_sanitized_evidence(self):
        with zipfile.ZipFile(ACCEPTANCE_PACK, "r") as archive:
            names = archive.namelist()
        required = {
            "MANIFEST.json",
            "SHA256SUMS",
            "contract/STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json",
            "reports/EXECUTABLE_RULE_GAP_REPORT.json",
            "reports/C1_OFFLINE_VERIFICATION.json",
            "reports/C1_PROVIDER_CAPABILITY_SMOKE.json",
            "reports/C1_DOWNTIME_ACCEPTANCE.json",
            "reports/C1_FINAL_ACCEPTANCE.json",
            "evidence/database_integrity.json",
            "evidence/source_continuity.json",
            "evidence/polymarket_reconciliation.json",
            "evidence/canary_evaluation.json",
            "evidence/outbox_replay.json",
            "evidence/incident_summary.json",
        }
        self.assertTrue(required.issubset(names))
        for name in names:
            lowered = name.lower()
            with self.subTest(name=name):
                self.assertFalse(Path(name).is_absolute())
                self.assertNotIn("..", PurePath(name).parts)
                self.assertFalse(
                    lowered.endswith(
                        (
                            ".sqlite",
                            ".sqlite3",
                            ".sqlite-wal",
                            ".sqlite3-wal",
                            ".sqlite-shm",
                            ".sqlite3-shm",
                        )
                    )
                )
                self.assertNotIn("raw_ws", lowered)
                self.assertNotIn("raw_websocket", lowered)

    def test_task_14_security_and_scope_remain_disabled(self):
        downtime, final = self._task_14_reports()
        self.assertFalse(final["trading_approval"])
        self.assertFalse(final["security_assertions"]["credentials_used"])
        self.assertFalse(
            final["security_assertions"]["real_order_submission"]
        )
        self.assertFalse(final["security_assertions"]["wallet_or_signing"])
        self.assertEqual(
            final["scope_assertions"],
            {
                "c2_started": False,
                "c3_rollover_started": False,
                "c4_started": False,
                "c5_started": False,
                "paper_execution": False,
                "registry_47_execution": False,
            },
        )
        self.assertFalse(downtime["security"]["paper_execution"])
        self.assertFalse(downtime["security"]["registry_47_execution"])
        serialized = json.dumps((downtime, final), sort_keys=True).lower()
        for forbidden in (
            "api_key",
            "private_key",
            "sign_order",
            "place_order",
            "cancel_order",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    @staticmethod
    def _launcher(path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    @staticmethod
    def _task_13_reports() -> tuple[dict, dict]:
        return (
            json.loads(OFFLINE_REPORT.read_text(encoding="utf-8")),
            json.loads(PROVIDER_REPORT.read_text(encoding="utf-8")),
        )

    @staticmethod
    def _task_14_reports() -> tuple[dict, dict]:
        return (
            json.loads(DOWNTIME_REPORT.read_text(encoding="utf-8")),
            json.loads(FINAL_ACCEPTANCE_REPORT.read_text(encoding="utf-8")),
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


class DowntimeAcceptanceToolTests(unittest.TestCase):
    @staticmethod
    def _tool():
        return importlib.import_module("tools.simulate_downtime")

    @staticmethod
    def _passing_evidence() -> dict:
        return {
            "downtime_duration_ms": 600_000,
            "forced_termination_used": False,
            "initial_backend_exit_code": 0,
            "final_backend_exit_code": 0,
            "post_restart_live_ready": True,
            "missing_binance_closed_minutes": 0,
            "duplicate_binance_natural_keys": 0,
            "polymarket_reconciled": True,
            "historical_depth_classification": "NOT_REQUIRED",
            "current_post_restart_canary": True,
            "recovered_execution_eligible": False,
            "outbox_replay_proved": True,
            "database_integrity_pass": True,
            "second_instance_exit_code": 20,
        }

    def test_acceptance_tool_exists_and_import_has_no_network_side_effect(self):
        module = self._tool()
        self.assertTrue(callable(module.main))
        self.assertFalse(module.IMPORT_PERFORMED_NETWORK_IO)

    def test_acceptance_tool_requires_windows_runtime(self):
        module = self._tool()
        with self.assertRaisesRegex(
            module.AcceptanceBlocked,
            "BLOCKED_UNSUPPORTED_PLATFORM",
        ):
            module.require_windows("posix")
        module.require_windows("nt")

    def test_acceptance_tool_uses_current_python_executable(self):
        module = self._tool()
        self.assertEqual(
            module.backend_command(),
            [sys.executable, "run_backend.py"],
        )

    def test_acceptance_tool_has_no_powershell_child_process(self):
        text = (
            PROJECT_ROOT / "tools" / "simulate_downtime.py"
        ).read_text(encoding="utf-8").lower()
        for forbidden in ("powershell.exe", "pwsh", "start-process"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_acceptance_tool_has_no_order_wallet_or_auth_functionality(self):
        text = (
            PROJECT_ROOT / "tools" / "simulate_downtime.py"
        ).read_text(encoding="utf-8").lower()
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

    def test_downtime_duration_uses_monotonic_nanoseconds(self):
        module = self._tool()
        self.assertEqual(
            module.monotonic_duration_ms(
                10_000_000_000,
                610_000_000_000,
            ),
            600_000,
        )

    def test_integrity_marker_keeps_wall_clock_separate_from_duration(self):
        module = self._tool()
        marker = module.build_integrity_marker(
            {
                "run_id": "C1-ACCEPTANCE-20260729T000000Z-1234ABCD",
                "stop_requested_at_ms": 1_000,
                "process_stopped_at_ms": 2_000,
                "restart_requested_at_ms": 602_000,
                "downtime_duration_ms": 600_000,
            }
        )
        self.assertEqual(marker["downtime_duration_ms"], 600_000)
        self.assertEqual(marker["restart_requested_at_ms"], 602_000)
        self.assertRegex(marker["marker_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(module.verify_integrity_marker(marker))

    def test_pass_is_blocked_when_downtime_is_short(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["downtime_duration_ms"] = 599_999
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_DOWNTIME_TOO_SHORT",
        )

    def test_pass_is_blocked_when_downtime_is_long(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["downtime_duration_ms"] = 630_001
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_DOWNTIME_WINDOW",
        )

    def test_pass_is_blocked_after_forced_termination(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["forced_termination_used"] = True
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_GRACEFUL_SHUTDOWN",
        )

    def test_pass_is_blocked_for_nonzero_backend_exit(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["final_backend_exit_code"] = 1
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_GRACEFUL_SHUTDOWN",
        )

    def test_pass_is_blocked_without_post_restart_live_ready(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["post_restart_live_ready"] = False
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_RECOVERY",
        )

    def test_pass_is_blocked_for_missing_binance_minute(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["missing_binance_closed_minutes"] = 1
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_RECOVERY",
        )

    def test_pass_is_blocked_for_duplicate_binance_key(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["duplicate_binance_natural_keys"] = 1
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_RECOVERY",
        )

    def test_pass_is_blocked_without_polymarket_reconciliation(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["polymarket_reconciled"] = False
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_RECOVERY",
        )

    def test_pass_is_blocked_without_depth_classification(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["historical_depth_classification"] = ""
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_RECOVERY",
        )

    def test_pass_is_blocked_without_current_canary(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["current_post_restart_canary"] = False
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_CANARY_SEMANTICS",
        )

    def test_pass_is_blocked_for_execution_eligible_recovered_record(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["recovered_execution_eligible"] = True
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_CANARY_SEMANTICS",
        )

    def test_pass_is_blocked_without_outbox_replay(self):
        module = self._tool()
        evidence = self._passing_evidence()
        evidence["outbox_replay_proved"] = False
        self.assertEqual(
            module.calculate_acceptance_status(evidence),
            "BLOCKED_OUTBOX_REPLAY",
        )

    def test_zip_manifest_rejects_path_traversal(self):
        module = self._tool()
        with self.assertRaisesRegex(ValueError, "UNSAFE_ZIP_PATH"):
            module.safe_zip_manifest({"../escape.json": b"{}"})

    def test_acceptance_zip_has_crc_and_verified_sha256s(self):
        module = self._tool()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.zip"
            module.build_acceptance_pack(
                path,
                {"reports/evidence.json": b'{"status":"BLOCKED"}\n'},
                status="BLOCKED_RUNTIME_PATH_CONTRACT",
            )
            result = module.validate_acceptance_pack(path)
            self.assertTrue(result["crc_pass"])
            self.assertTrue(result["internal_sha256s_verified"])
            self.assertEqual(result["path_traversal_entries"], 0)

    def test_acceptance_zip_rejects_raw_runtime_data(self):
        module = self._tool()
        for path in (
            "runtime/db.sqlite3",
            "runtime/db.sqlite3-wal",
            "runtime/db.sqlite3-shm",
            "logs/raw_ws.log",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "FORBIDDEN_PACK_ENTRY"):
                    module.safe_zip_manifest({path: b"forbidden"})

    def test_final_report_never_grants_trading_approval(self):
        module = self._tool()
        report = module.build_final_report(
            run_id="C1-ACCEPTANCE-20260729T000000Z-1234ABCD",
            commit_sha="a" * 40,
            status="PASS",
            pack_evidence={},
        )
        self.assertFalse(report["trading_approval"])
        self.assertIn(
            "C1 PASS is not trading approval.",
            report["limitations"],
        )

    def test_task_14_pass_does_not_activate_later_scopes(self):
        module = self._tool()
        report = module.build_final_report(
            run_id="C1-ACCEPTANCE-20260729T000000Z-1234ABCD",
            commit_sha="a" * 40,
            status="PASS",
            pack_evidence={},
        )
        self.assertEqual(
            report["scope_assertions"],
            {
                "c2_started": False,
                "c3_rollover_started": False,
                "c4_started": False,
                "c5_started": False,
                "paper_execution": False,
                "registry_47_execution": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
