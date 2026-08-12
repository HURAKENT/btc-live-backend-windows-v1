from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path

from src.historical_input_builder import HistoricalArtifactPaths
from src.historical_revalidation import (
    EXIT_PARITY,
    EXIT_SOURCE_INTEGRITY,
    AhrParityError,
    AhrRunConfig,
    AhrSafetyError,
    assert_files_unchanged,
    compare_semantic,
    evaluate_persist_compare,
    historical_bindings,
    offline_network_guard,
    run_baseline,
    snapshot_files,
    validate_resume_manifest,
)
from src.strategy_dispatch import load_strategy_dispatcher


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HistoricalRevalidationTests(unittest.TestCase):
    def test_expected_parity_is_loaded_only_after_result_is_persisted(self) -> None:
        events: list[str] = []

        result = evaluate_persist_compare(
            evaluate=lambda: events.append("evaluate") or {"accepted": True},
            persist=lambda value: events.append("persist"),
            load_expected=lambda: events.append("expected") or {"accepted": True},
            compare=lambda actual, expected: events.append("compare"),
        )

        self.assertEqual(result, {"accepted": True})
        self.assertEqual(events, ["evaluate", "persist", "expected", "compare"])

    def test_current_dispatcher_exposes_all_47_historical_bindings(self) -> None:
        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        bindings = historical_bindings(dispatcher)

        self.assertEqual(len(bindings), 47)
        self.assertEqual(sum(row.version == "V1" for row in bindings), 34)
        self.assertEqual(sum(row.version == "V2" for row in bindings), 13)

    def test_research_only_v2_identity_is_not_filtered(self) -> None:
        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)

        self.assertIn(
            "NO_A2_V2_VOL",
            tuple(row.strategy_id for row in historical_bindings(dispatcher)),
        )

    def test_network_attempt_is_a_safety_failure(self) -> None:
        with self.assertRaisesRegex(AhrSafetyError, "AHR_NETWORK_REQUEST_BLOCKED"):
            with offline_network_guard():
                socket.create_connection(("127.0.0.1", 9), timeout=0.01)

    def test_production_file_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "production.sqlite3")
            path.write_bytes(b"before")
            snapshot = snapshot_files((path,))
            path.write_bytes(b"after")

            with self.assertRaisesRegex(AhrSafetyError, "AHR_PRODUCTION_FILE_MODIFIED"):
                assert_files_unchanged(snapshot)

    def test_changed_decision_causes_semantic_parity_failure(self) -> None:
        expected = {
            "strategy_id": "YES_STRICT_A_T60",
            "checkpoint_minutes": 60,
            "accepted": True,
            "reason": "SIGNAL_ACCEPTED",
            "selected_bucket_indices": [2],
            "model_probability_micros": 300_000,
        }
        actual = {**expected, "accepted": False}

        with self.assertRaises(AhrParityError) as caught:
            compare_semantic(actual=actual, expected=expected)
        self.assertEqual(caught.exception.exit_code, EXIT_PARITY)

    def test_nonsemantic_metadata_is_ignored(self) -> None:
        expected = {"accepted": True, "run_id": "old", "generated_at": "old"}
        actual = {"accepted": True, "run_id": "new", "generated_at": "new"}

        compare_semantic(actual=actual, expected=expected)

    def test_resume_rejects_changed_input_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "AHR_RESUME_MANIFEST_MISMATCH"):
            validate_resume_manifest(
                state={"input_manifest_sha256": "a" * 64},
                current_manifest_sha256="b" * 64,
            )

    def test_blocked_source_run_still_writes_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            artifacts = HistoricalArtifactPaths(
                source_pack_70=missing,
                source_pack_100=missing,
                checkpoint_matrix_170=missing,
                atlas_170=missing,
                markets_170=missing,
                settlements_170=missing,
                no_checkpoint_coverage=missing,
                confirmation_dates=missing,
                vol_forecast_ledger=missing,
            )
            code = run_baseline(
                AhrRunConfig(
                    project_root=PROJECT_ROOT,
                    run_root=root / "runs",
                    artifacts=artifacts,
                    smoke_dates=("2026-01-01",),
                )
            )

            self.assertEqual(code, EXIT_SOURCE_INTEGRITY)
            receipts = tuple((root / "runs").glob("*/ACCEPTANCE.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "BLOCKED")
            self.assertEqual(receipt["earliest_failed_gate"], "SOURCE_INTEGRITY")
            self.assertEqual(receipt["error_class"], "ValueError")
            self.assertIn("AHR_SOURCE_MISSING", receipt["error_message"])
            self.assertIn("failed_strategy_id", receipt)
            self.assertIn("failed_market_date", receipt)


if __name__ == "__main__":
    unittest.main()
