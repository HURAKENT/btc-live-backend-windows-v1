from __future__ import annotations

import hashlib
import json
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_ROOT = (
    ROOT.parent.parent if ROOT.parent.name == ".worktrees" else ROOT
)
C3_REPORT = ROOT / "reports" / "C3_MARKET_ROLLOVER_ACCEPTANCE.json"
C3_MATRIX = ROOT / "reports" / "C3_MARKET_ROLLOVER_TEST_MATRIX.md"
FINAL_REPORT = ROOT / "reports" / "C2_C3_FINAL_ACCEPTANCE.json"
PACK = AUTHORITY_ROOT / "artifacts" / "C2_C3_ACCEPTANCE_PACK.zip"
IMPLEMENTATION_COMMIT = "cec9e29255a207859006dfdffbc27aa140f273ad"


class C3MarketRolloverAcceptanceContractTests(unittest.TestCase):
    def report(self) -> dict[str, object]:
        return json.loads(C3_REPORT.read_text(encoding="utf-8"))

    def test_reports_matrix_and_pack_exist(self):
        for path in (C3_REPORT, C3_MATRIX, FINAL_REPORT, PACK):
            self.assertTrue(path.is_file(), path)

    def test_c3_gate_is_exact_and_non_trading(self):
        report = self.report()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["gate"], "C3_MARKET_ROLLOVER_PASS")
        self.assertEqual(report["source_commit"], IMPLEMENTATION_COMMIT)
        self.assertFalse(report["trading_approval"])

    def test_current_next_and_cutover_are_complete(self):
        rollover = self.report()["rollover"]
        self.assertEqual(rollover["market_catalog_count"], 2)
        self.assertEqual(rollover["current_market_count"], 11)
        self.assertEqual(rollover["current_asset_count"], 22)
        self.assertEqual(rollover["next_market_count"], 11)
        self.assertEqual(rollover["next_asset_count"], 22)
        self.assertEqual(rollover["next_current_book_count"], 22)
        self.assertEqual(rollover["next_history_completed_assets"], 22)
        self.assertTrue(rollover["identity_switched_after_reconciliation"])
        self.assertTrue(rollover["post_cutover_live_event_committed"])

    def test_subscription_lifecycle_and_failure_boundary_are_explicit(self):
        rollover = self.report()["rollover"]
        self.assertEqual(rollover["active_subscription_count_after_cutover"], 1)
        self.assertEqual(rollover["writer_consumer_count"], 1)
        self.assertEqual(
            rollover["lifecycle"],
            [
                "NEXT_DISCOVERED",
                "NEXT_BUFFERING",
                "NEXT_RECONCILED",
                "CUTOVER_COMMITTED",
                "CURRENT_LIVE",
            ],
        )
        self.assertTrue(rollover["invalid_next_keeps_current_live"])
        self.assertEqual(rollover["invalid_next_rows_committed"], 0)

    def test_same_database_restart_is_bounded_and_idempotent(self):
        restart = self.report()["restart"]
        self.assertTrue(restart["same_database"])
        self.assertEqual(restart["initial_history_requests"], 44)
        self.assertEqual(restart["restart_history_requests_added"], 0)
        self.assertEqual(restart["duplicate_natural_keys"], 0)
        self.assertEqual(restart["c3_pass_incidents"], 1)
        self.assertTrue(restart["outbox_ids_unique_and_increasing"])

    def test_verification_and_protected_c1_evidence_are_exact(self):
        report = self.report()
        verification = report["verification"]
        self.assertEqual(verification["process_integration_tests"], 11)
        self.assertEqual(verification["full_suite_tests"], 625)
        self.assertEqual(verification["documented_skips"], 1)
        self.assertEqual(verification["offline_launcher_exit"], 0)
        self.assertTrue(verification["compileall_pass"])
        self.assertTrue(verification["pip_check_pass"])
        self.assertTrue(report["protected_c1_artifacts"]["unchanged"])

    def test_forbidden_scope_is_zero(self):
        scope = self.report()["scope"]
        for key in (
            "external_provider_requests",
            "task14_runs",
            "registry_executions",
            "orders",
            "paper_fills",
            "authentication",
            "wallet_or_signing",
        ):
            self.assertIn(scope[key], (0, False))
        self.assertFalse(scope["c4_started"])

    def test_combined_gate_requires_both_c2_and_c3(self):
        report = json.loads(FINAL_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            report["gate"], "BTC_LIVE_BACKEND_WINDOWS_V1_C2_C3_PASS"
        )
        self.assertEqual(report["c2_gate"], "C2_RECOVERY_HARDENING_PASS")
        self.assertEqual(report["c3_gate"], "C3_MARKET_ROLLOVER_PASS")
        self.assertFalse(report["trading_approval"])
        self.assertFalse(report["c4_started"])

    def test_pack_crc_and_internal_hashes_pass(self):
        with zipfile.ZipFile(PACK) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertFalse(any(".." in Path(name).parts for name in names))
            checksums = archive.read("SHA256SUMS").decode("ascii").splitlines()
            expected = {}
            for line in checksums:
                digest, name = line.split("  ", 1)
                expected[name] = digest
            self.assertEqual(set(expected), set(names) - {"SHA256SUMS"})
            for name, digest in expected.items():
                self.assertEqual(
                    hashlib.sha256(archive.read(name)).hexdigest(), digest
                )

    def test_matrix_records_load_bearing_boundaries(self):
        matrix = C3_MATRIX.read_text(encoding="utf-8")
        for marker in (
            "C3_MARKET_ROLLOVER_PASS",
            "Current/next discovery",
            "Atomic next reconciliation",
            "Subscription migration",
            "Post-cutover live ingress",
            "Same-database restart",
            "Historical C1 artifacts",
        ):
            self.assertIn(marker, matrix)


if __name__ == "__main__":
    unittest.main()
