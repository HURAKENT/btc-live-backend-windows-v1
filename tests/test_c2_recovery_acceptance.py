from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "reports" / "C2_RECOVERY_ACCEPTANCE.json"
MATRIX_PATH = ROOT / "reports" / "C2_RECOVERY_TEST_MATRIX.md"
IMPLEMENTATION_COMMIT = "d772428844dc65ca47f5a147bfc75a2e51c2b706"


class C2RecoveryAcceptanceContractTests(unittest.TestCase):
    def report(self) -> dict[str, object]:
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    def test_report_and_matrix_exist(self):
        self.assertTrue(REPORT_PATH.is_file())
        self.assertTrue(MATRIX_PATH.is_file())

    def test_report_claims_only_exact_c2_gate(self):
        report = self.report()
        self.assertEqual(
            report["schema_version"],
            "BTC_LIVE_BACKEND_WINDOWS_V1_C2_RECOVERY_ACCEPTANCE_V1",
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["gate"], "C2_RECOVERY_HARDENING_PASS")
        self.assertEqual(report["source_commit"], IMPLEMENTATION_COMMIT)
        self.assertFalse(report["trading_approval"])

    def test_recovery_completeness_is_explicit(self):
        recovery = self.report()["recovery"]
        self.assertTrue(recovery["persistent_cursor_bounds"])
        self.assertTrue(recovery["stream_first_buffering"])
        self.assertEqual(recovery["binance_missing_closed_minutes"], 0)
        self.assertEqual(recovery["binance_duplicates_after_dedup"], 0)
        self.assertEqual(recovery["source_duplicates_after_dedup"], 0)
        self.assertEqual(recovery["polymarket_market_count"], 11)
        self.assertEqual(recovery["polymarket_asset_count"], 22)
        self.assertEqual(recovery["current_book_count"], 22)
        self.assertEqual(recovery["history_completed_asset_count"], 22)
        self.assertTrue(recovery["buffer_drained_after_authoritative_replay"])

    def test_restart_and_evaluations_are_proven(self):
        persistence = self.report()["persistence"]
        self.assertTrue(persistence["same_database_restart"])
        self.assertTrue(persistence["restart_history_requests_added"] == 0)
        self.assertTrue(persistence["outbox_ids_unique_and_increasing"])
        self.assertEqual(persistence["writer_consumer_count"], 1)
        evaluations = persistence["evaluations"]
        self.assertTrue(evaluations["recovered_committed"])
        self.assertTrue(evaluations["current_committed"])
        self.assertFalse(evaluations["recovered_execution_eligible"])
        self.assertFalse(evaluations["current_execution_eligible"])

    def test_verification_counts_match_fresh_gates(self):
        verification = self.report()["verification"]
        self.assertEqual(verification["focused_tests"], 129)
        self.assertEqual(verification["process_integration_tests"], 11)
        self.assertEqual(verification["full_suite_tests"], 591)
        self.assertEqual(verification["documented_skips"], 1)
        self.assertEqual(verification["offline_launcher_exit"], 0)
        self.assertTrue(verification["compileall_pass"])
        self.assertTrue(verification["pip_check_pass"])

    def test_sqlite_and_historical_c1_evidence_are_protected(self):
        sqlite = self.report()["sqlite"]
        self.assertEqual(sqlite["migration_version"], 2)
        self.assertEqual(sqlite["table_count"], 9)
        self.assertEqual(sqlite["quick_check"], "ok")
        self.assertEqual(sqlite["integrity_check"], "ok")
        self.assertEqual(sqlite["journal_mode"], "wal")
        self.assertEqual(sqlite["synchronous"], 2)
        self.assertEqual(sqlite["foreign_keys"], 1)
        protected = self.report()["protected_c1_artifacts"]
        self.assertTrue(protected["unchanged"])
        self.assertEqual(len(protected["sha256"]), 3)

    def test_security_scope_remains_non_trading(self):
        scope = self.report()["scope"]
        for field in (
            "external_provider_requests",
            "task14_runs",
            "registry_executions",
            "orders",
            "paper_fills",
            "wallet_or_signing",
            "authentication",
        ):
            self.assertIn(scope[field], (0, False))
        self.assertFalse(scope["c3_started"])
        self.assertFalse(scope["c4_started"])

    def test_matrix_records_each_load_bearing_boundary(self):
        matrix = MATRIX_PATH.read_text(encoding="utf-8")
        for marker in (
            "C2_RECOVERY_HARDENING_PASS",
            "Persistent cursor bounds",
            "Binance completeness",
            "Polymarket 11/22",
            "Same-database restart",
            "One writer",
            "Recovered/current evaluations",
            "Historical C1 artifacts",
        ):
            self.assertIn(marker, matrix)


if __name__ == "__main__":
    unittest.main()
