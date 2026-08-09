from __future__ import annotations

import dataclasses
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CheckpointC6AcceptanceTests(unittest.TestCase):
    def test_acceptance_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("src.checkpoint_c6_acceptance")
        )

    def test_fresh_gate_proves_persistent_exactly_once_scheduler(self) -> None:
        from src.checkpoint_c6_acceptance import verify_c6_acceptance

        report = verify_c6_acceptance(PROJECT_ROOT)

        self.assertTrue(report.acceptance_pass)
        self.assertEqual(report.status, "C6_CHECKPOINT_SCHEDULER_PASS")
        self.assertEqual(report.migration_version, 3)
        self.assertEqual(report.enabled_strategy_count, 8)
        self.assertEqual(report.market_count, 3)
        self.assertEqual(report.schedule_count, 30)
        self.assertEqual(report.schedules_per_market, (10, 10, 10))
        self.assertTrue(report.exact_replay_pass)
        self.assertTrue(report.recovered_replay_pass)
        self.assertTrue(report.missing_depth_block_pass)
        self.assertTrue(report.current_reevaluation_pass)
        self.assertTrue(report.atomic_commit_pass)
        self.assertTrue(report.stale_market_guard_pass)
        self.assertTrue(report.runtime_writer_path_pass)
        self.assertTrue(report.real_dispatcher_pass)
        self.assertFalse(report.production_input_ready)
        self.assertEqual(report.data_completion_gate, "OPEN_BLOCKS_C7")
        self.assertFalse(report.paper_execution_authorized)
        self.assertFalse(report.trading_approval)
        self.assertEqual(report.external_provider_requests, 0)

    def test_report_is_immutable_and_writer_is_canonical(self) -> None:
        from src.checkpoint_c6_acceptance import (
            acceptance_report_payload,
            verify_c6_acceptance,
            write_c6_acceptance_report,
        )

        report = verify_c6_acceptance(PROJECT_ROOT)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.status = "BLOCKED"
        payload = acceptance_report_payload(
            report,
            source_commit="a" * 40,
            verified_at_utc="2026-08-09T12:00:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "c6.json"
            write_c6_acceptance_report(
                report,
                output_path=output,
                source_commit="a" * 40,
                verified_at_utc="2026-08-09T12:00:00Z",
            )
            raw = output.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b"NaN", raw)
            self.assertEqual(json.loads(raw), payload)

    def test_data_completion_gate_remains_explicitly_open(self) -> None:
        payload = json.loads(
            (PROJECT_ROOT / "reports/DATA_COMPLETENESS_STATUS.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(payload["status"], "PHASE_0_AUDIT_PENDING")
        self.assertEqual(payload["source_ranges"], [])
        self.assertEqual(payload["import_runs"], [])
        self.assertEqual(payload["unknown_ranges"], "NOT_YET_INVENTORIED")
        decision = (PROJECT_ROOT / "docs/FINAL_PROJECT_DECISION_LOG.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("D-012", decision)
        self.assertIn("C7 and C11 are blocked", decision)

    def test_committed_c6_report_equals_fresh_gate(self) -> None:
        from src.checkpoint_c6_acceptance import (
            acceptance_report_payload,
            verify_c6_acceptance,
        )

        report_path = PROJECT_ROOT / "reports/C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json"
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        fresh = verify_c6_acceptance(PROJECT_ROOT)
        self.assertEqual(
            persisted,
            acceptance_report_payload(
                fresh,
                source_commit=persisted["source_commit"],
                verified_at_utc=persisted["verified_at_utc"],
            ),
        )
        progress = (PROJECT_ROOT / "docs/FINAL_PROJECT_PROGRESS.md").read_text(
            encoding="utf-8"
        )
        matrix = (
            PROJECT_ROOT / "reports/FINAL_PROJECT_ACCEPTANCE_MATRIX.md"
        ).read_text(encoding="utf-8")
        self.assertIn("- [x] C6 persistent scheduler/replay.", progress)
        self.assertIn("| C6 Scheduler/replay | PASS |", matrix)


if __name__ == "__main__":
    unittest.main()
