from __future__ import annotations

import dataclasses
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataCompletionAcceptanceTests(unittest.TestCase):
    def test_acceptance_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.data_completion_acceptance"))

    def test_fresh_gate_proves_required_data_layer_capabilities(self) -> None:
        from src.data_completion_acceptance import verify_data_completion_acceptance

        report = verify_data_completion_acceptance(PROJECT_ROOT)
        self.assertEqual(report.status, "DATA_COMPLETION_CAPABILITY_ACCEPTANCE_PASS")
        self.assertTrue(report.acceptance_pass)
        self.assertEqual(report.migration_version, 6)
        self.assertEqual(report.unknown_range_count, 4)
        self.assertEqual(report.unknown_inventory_class_count, 0)
        self.assertEqual(report.source_range_count, 9)
        self.assertGreaterEqual(report.import_run_count, 4)
        self.assertTrue(report.append_pass)
        self.assertTrue(report.bounded_backfill_pass)
        self.assertTrue(report.reconcile_pass)
        self.assertTrue(report.import_pass)
        self.assertTrue(report.import_replay_pass)
        self.assertTrue(report.expected_absent_pass)
        self.assertTrue(report.future_market_pass)
        self.assertTrue(report.backup_restore_pass)
        self.assertTrue(report.integrity_pass)
        self.assertFalse(report.production_historical_depth_ready)
        self.assertEqual(report.production_historical_depth_status, "FAIL_CLOSED_NOT_IMPORTED")
        self.assertFalse(report.production_data_complete)
        self.assertFalse(report.c7_entry_authorized)
        self.assertEqual(
            report.blocking_inventory_ids,
            (
                "POLYMARKET_PRICE_HISTORY",
                "POLYMARKET_HISTORICAL_DEPTH_C7",
                "PUBLIC_FEE_SCHEDULE_C7",
                "HISTORICAL_SOURCE_PACK_IMPORT",
            ),
        )
        self.assertEqual(report.historical_imported_row_count, 2341)
        self.assertEqual(report.external_provider_requests, 0)
        self.assertFalse(report.paper_execution_authorized)
        self.assertFalse(report.trading_approval)

    def test_inventory_has_exact_required_scenario_ids_and_terminal_states(self) -> None:
        from src.data_completion_acceptance import REQUIRED_INVENTORY_IDS, verify_data_completion_acceptance

        report = verify_data_completion_acceptance(PROJECT_ROOT)
        self.assertEqual(tuple(item.inventory_id for item in report.source_ranges), REQUIRED_INVENTORY_IDS)
        self.assertNotIn("UNKNOWN", {item.status for item in report.source_ranges})
        self.assertNotIn("SOURCE_CONFLICT_FATAL", {item.status for item in report.source_ranges})
        status_by_id = {item.inventory_id: item.status for item in report.source_ranges}
        self.assertEqual(status_by_id["POLYMARKET_HISTORICAL_DEPTH_C7"], "SOURCE_UNAVAILABLE_RETRYABLE")
        self.assertEqual(status_by_id["PUBLIC_FEE_SCHEDULE_C7"], "SOURCE_UNAVAILABLE_RETRYABLE")
        self.assertEqual(status_by_id["C4_SANITIZED_PARITY_FIXTURE_IMPORT"], "COMPLETE")
        self.assertEqual(
            status_by_id["HISTORICAL_SOURCE_PACK_IMPORT"],
            "SOURCE_UNAVAILABLE_RETRYABLE",
        )
        self.assertTrue(all(len(item.range_key) == 64 for item in report.source_ranges))
        self.assertTrue(all(len(item.evidence_sha256) == 64 for item in report.source_ranges))
        self.assertTrue(all(len(item.boundary_sha256) == 64 for item in report.source_ranges))
        self.assertTrue(all(item.evidence_class for item in report.source_ranges))
        for item in report.source_ranges:
            if item.status == "SOURCE_UNAVAILABLE_RETRYABLE":
                self.assertTrue(item.reason_code)

    def test_mutated_historical_acceptance_evidence_fails_closed(self) -> None:
        from src.data_completion_acceptance import verify_data_completion_acceptance

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            for name in (
                "C1_DOWNTIME_ACCEPTANCE.json",
                "C4_STRATEGY_47_ACCEPTANCE.json",
                "C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json",
            ):
                shutil.copyfile(PROJECT_ROOT / "reports" / name, root / "reports" / name)
            c1_path = root / "reports/C1_DOWNTIME_ACCEPTANCE.json"
            c1 = json.loads(c1_path.read_text(encoding="utf-8"))
            c1["binance_continuity"]["missing_binance_closed_minutes"] = 1
            c1_path.write_text(json.dumps(c1), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "C1_DATA_COMPLETENESS_EVIDENCE_INVALID"):
                verify_data_completion_acceptance(root)

    def test_report_is_immutable_sanitized_and_deterministic(self) -> None:
        from src.data_completion_acceptance import acceptance_report_payload, verify_data_completion_acceptance, write_acceptance_report

        report = verify_data_completion_acceptance(PROJECT_ROOT)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.status = "BLOCKED"
        payload = acceptance_report_payload(
            report,
            source_commit="a" * 40,
            verified_at_utc="2026-08-09T12:00:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data-completion.json"
            write_acceptance_report(
                report,
                output_path=output,
                source_commit="a" * 40,
                verified_at_utc="2026-08-09T12:00:00Z",
            )
            raw = output.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw), payload)
        self.assertNotIn(b"C:\\Users", raw)
        self.assertNotIn(b"/mnt/c/Users", raw)
        self.assertNotIn(b"asset-", raw)
        self.assertNotIn(b"NaN", raw)

    def test_current_pending_report_is_not_accepted_before_evidence_commit(self) -> None:
        payload = json.loads(
            (PROJECT_ROOT / "reports/DATA_COMPLETENESS_STATUS.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["status"], "PHASE_0_AUDIT_PENDING")
        self.assertEqual(payload["source_ranges"], [])
        self.assertEqual(payload["import_runs"], [])


if __name__ == "__main__":
    unittest.main()
