from __future__ import annotations

import dataclasses
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StrategyC4AcceptanceTests(unittest.TestCase):
    def test_acceptance_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.strategy_c4_acceptance"))

    def test_real_frozen_pack_proves_one_exact_forty_seven_gate(self) -> None:
        from src.strategy_c4_acceptance import verify_c4_strategy_acceptance

        report = verify_c4_strategy_acceptance(PROJECT_ROOT)

        self.assertTrue(report.acceptance_pass)
        self.assertEqual(report.status, "C4_STRATEGY_47_ACCEPTANCE_PASS")
        self.assertEqual(report.strategy_count, 47)
        self.assertEqual(report.v1_strategy_count, 34)
        self.assertEqual(report.v2_strategy_count, 13)
        self.assertEqual(len(report.strategy_ids), 47)
        self.assertEqual(len(set(report.strategy_ids)), 47)
        self.assertEqual(report.v1_fixture_record_count, 1647)
        # 2,894 Early Horizon + 1,357 Early Confidence + 884
        # Confirmation/Basket identity decisions.
        self.assertEqual(report.v1_decision_count, 5135)
        self.assertEqual(report.v2_fixture_record_count, 694)
        self.assertFalse(report.trading_approval)
        self.assertEqual(report.external_provider_requests, 0)

    def test_report_is_immutable_and_deterministic(self) -> None:
        from src.strategy_c4_acceptance import (
            acceptance_report_payload,
            verify_c4_strategy_acceptance,
        )

        report = verify_c4_strategy_acceptance(PROJECT_ROOT)
        self.assertEqual(
            acceptance_report_payload(report, source_commit="a" * 40),
            acceptance_report_payload(report, source_commit="a" * 40),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.status = "BLOCKED"

    def test_status_matrix_is_exact_ordered_and_keeps_activation_for_c5(self) -> None:
        from src.strategy_c4_acceptance import (
            build_strategy_status_matrix,
            verify_c4_strategy_acceptance,
        )

        report = verify_c4_strategy_acceptance(PROJECT_ROOT)
        matrix = build_strategy_status_matrix(
            report,
            source_commit="b" * 40,
        )
        self.assertEqual(matrix["strategy_count"], 47)
        self.assertEqual(
            matrix["status_counts"],
            {
                "SOURCE_VERIFIED": 47,
                "SPEC_FROZEN": 47,
                "EVALUATOR_IMPLEMENTED": 47,
                "PARITY_PASS": 47,
                "PENDING_C5_CLASSIFICATION": 47,
            },
        )
        self.assertEqual(
            [row["strategy_id"] for row in matrix["strategies"]],
            list(report.strategy_ids),
        )
        self.assertTrue(
            all(
                row["activation_status"] == "PENDING_C5_CLASSIFICATION"
                and row["paper_eligible"] is False
                for row in matrix["strategies"]
            )
        )
        self.assertFalse(matrix["trading_approval"])

    def test_writer_uses_canonical_json_and_rejects_invalid_commit(self) -> None:
        from src.strategy_c4_acceptance import (
            write_c4_acceptance_outputs,
            verify_c4_strategy_acceptance,
        )

        report = verify_c4_strategy_acceptance(PROJECT_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "C4_INVALID_SOURCE_COMMIT"):
                write_c4_acceptance_outputs(
                    report,
                    report_path=root / "report.json",
                    matrix_path=root / "matrix.json",
                    source_commit="not-a-commit",
                )
            write_c4_acceptance_outputs(
                report,
                report_path=root / "report.json",
                matrix_path=root / "matrix.json",
                source_commit="c" * 40,
            )
            for path in (root / "report.json", root / "matrix.json"):
                raw = path.read_bytes()
                self.assertTrue(raw.endswith(b"\n"))
                self.assertNotIn(b"NaN", raw)
                self.assertEqual(
                    json.loads(raw),
                    json.loads(raw.decode("utf-8")),
                )

    def test_committed_report_and_c5_matrix_preserve_fresh_c4_gate_result(self) -> None:
        from src.strategy_c4_acceptance import (
            acceptance_report_payload,
            build_strategy_status_matrix,
            verify_c4_strategy_acceptance,
        )

        source_commit = "dde6707e719897d253e233815be2ddd5d46789c9"
        report = verify_c4_strategy_acceptance(PROJECT_ROOT)
        self.assertEqual(
            json.loads((PROJECT_ROOT / "reports/C4_STRATEGY_47_ACCEPTANCE.json").read_text("utf-8")),
            json.loads(json.dumps(acceptance_report_payload(report, source_commit=source_commit))),
        )
        matrix = json.loads(
            (PROJECT_ROOT / "reports/STRATEGY_47_STATUS_MATRIX.json").read_text(
                "utf-8"
            )
        )
        self.assertEqual(matrix["schema_version"], "BTC_STRATEGY_47_STATUS_MATRIX_V3")
        self.assertEqual(
            [row["strategy_id"] for row in matrix["strategies"]],
            list(report.strategy_ids),
        )
        self.assertTrue(
            all(
                row["rule_source_status"] == "SOURCE_VERIFIED"
                and row["rule_spec_status"] == "SPEC_FROZEN"
                and row["evaluator_status"] == "EVALUATOR_IMPLEMENTED"
                and row["parity_status"] == "PARITY_PASS"
                for row in matrix["strategies"]
            )
        )
        self.assertFalse(matrix["trading_approval"])


if __name__ == "__main__":
    unittest.main()
