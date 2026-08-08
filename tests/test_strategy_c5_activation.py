from __future__ import annotations

import dataclasses
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StrategyC5ActivationTests(unittest.TestCase):
    def test_activation_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.strategy_c5_activation"))

    def test_real_frozen_policy_classifies_all_forty_seven(self) -> None:
        from src.strategy_c5_activation import verify_c5_activation

        report = verify_c5_activation(PROJECT_ROOT)

        self.assertTrue(report.acceptance_pass)
        self.assertEqual(report.status, "C5_STRATEGY_47_ACTIVATION_PASS")
        self.assertEqual(report.strategy_count, 47)
        self.assertEqual(report.classified_count, 47)
        self.assertEqual(
            dict(report.activation_counts),
            {
                "DISABLED_MISSING_EXECUTION_DATA": 26,
                "DISABLED_RESEARCH_ONLY": 13,
                "PAPER_EVALUATION_ENABLED": 8,
            },
        )
        self.assertEqual(report.unknown_count, 0)
        self.assertEqual(report.external_provider_requests, 0)
        self.assertFalse(report.paper_execution_authorized)
        self.assertFalse(report.trading_approval)
        enabled = {
            item.strategy_id
            for item in report.identities
            if item.activation_status == "PAPER_EVALUATION_ENABLED"
        }
        self.assertEqual(
            enabled,
            {
                "YES_PF1_OPERATIONAL",
                "YES_PF1_T30",
                "YES_PF1_T60",
                "YES_PF1_T6H",
                "YES_PF1_T8H",
                "YES_STRICT_A_OPERATIONAL",
                "YES_STRICT_A_T30",
                "YES_STRICT_A_T60",
            },
        )

    def test_exact_policy_is_derived_from_version_schema_and_frozen_live_status(self) -> None:
        from src.strategy_c5_activation import classify_activation

        enabled = classify_activation(
            version="V1",
            input_schema_version="BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1",
            registry_live_status="PRESERVE_EXISTING_STATUS",
            parity_status="PARITY_PASS",
        )
        historical = classify_activation(
            version="V1",
            input_schema_version="BTC_STRATEGY_HISTORICAL_INPUT_V1",
            registry_live_status="PRESERVE_EXISTING_STATUS",
            parity_status="PARITY_PASS",
        )
        overlay = classify_activation(
            version="V2",
            input_schema_version="BTC_STRATEGY_VOL_OVERLAY_INPUT_V1",
            registry_live_status="DISABLED_RESEARCH_ONLY",
            parity_status="PARITY_PASS",
        )

        self.assertEqual(enabled.activation_status, "PAPER_EVALUATION_ENABLED")
        self.assertTrue(enabled.paper_eligible)
        self.assertEqual(
            historical.activation_status, "DISABLED_MISSING_EXECUTION_DATA"
        )
        self.assertFalse(historical.paper_eligible)
        self.assertEqual(overlay.activation_status, "DISABLED_RESEARCH_ONLY")
        self.assertFalse(overlay.paper_eligible)

    def test_unreviewed_policy_combinations_fail_closed(self) -> None:
        from src.strategy_c5_activation import classify_activation

        cases = (
            ("V1", "BTC_STRATEGY_VOL_OVERLAY_INPUT_V1", "PRESERVE_EXISTING_STATUS"),
            ("V2", "BTC_STRATEGY_HISTORICAL_INPUT_V1", "DISABLED_RESEARCH_ONLY"),
            ("V2", "BTC_STRATEGY_VOL_OVERLAY_INPUT_V1", "PRESERVE_EXISTING_STATUS"),
            ("V3", "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1", "UNKNOWN"),
        )
        for version, schema, live_status in cases:
            with self.subTest(version=version, schema=schema, live_status=live_status):
                with self.assertRaisesRegex(ValueError, "C5_UNREVIEWED_POLICY_COMBINATION"):
                    classify_activation(
                        version=version,
                        input_schema_version=schema,
                        registry_live_status=live_status,
                        parity_status="PARITY_PASS",
                    )
                with self.assertRaisesRegex(ValueError, "C5_UNREVIEWED_POLICY_COMBINATION"):
                    classify_activation(
                        version=version,
                        input_schema_version=schema,
                        registry_live_status=live_status,
                        parity_status="PARITY_FAIL",
                    )

    def test_parity_failure_is_explicitly_disabled(self) -> None:
        from src.strategy_c5_activation import classify_activation

        decision = classify_activation(
            version="V1",
            input_schema_version="BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1",
            registry_live_status="PRESERVE_EXISTING_STATUS",
            parity_status="PARITY_FAIL",
        )
        self.assertEqual(decision.activation_status, "DISABLED_PARITY_FAIL")
        self.assertEqual(decision.reason_code, "C4_PARITY_FAILED")
        self.assertFalse(decision.paper_eligible)

    def test_status_matrix_contains_exact_policy_and_parity_provenance(self) -> None:
        from src.strategy_c5_activation import (
            build_c5_status_matrix,
            verify_c5_activation,
        )

        report = verify_c5_activation(PROJECT_ROOT)
        matrix = build_c5_status_matrix(
            report,
            source_commit="a" * 40,
            verified_at_utc="2026-08-08T12:34:56Z",
        )
        rows = matrix["strategies"]

        self.assertEqual(len(rows), 47)
        self.assertEqual([row["registry_index"] for row in rows], list(range(1, 48)))
        self.assertTrue(all(row["parity_status"] == "PARITY_PASS" for row in rows))
        self.assertTrue(all(row["expected_trade_identity_sha256"] == row["actual_trade_identity_sha256"] for row in rows))
        self.assertTrue(all(row["expected_trade_count"] == row["actual_trade_count"] for row in rows))
        self.assertTrue(all(row["expected_pnl_micros"] == row["actual_pnl_micros"] for row in rows))
        self.assertTrue(all(row["rule_source_fingerprint_sha256"] for row in rows))
        self.assertTrue(all(row["rule_spec_sha256"] for row in rows))
        self.assertTrue(all(row["activation_reason_code"] for row in rows))
        self.assertTrue(
            all(row["last_verified_at_utc"] == "2026-08-08T12:34:56Z" for row in rows)
        )
        self.assertFalse(matrix["paper_execution_authorized"])
        self.assertFalse(matrix["trading_approval"])
        expected_evidence = {
            "V1_EARLY_HORIZON": (
                "PARITY_V1_EARLY_HORIZON_FULL",
                "PARITY_V1_EARLY_HORIZON_RECEIPT",
                17,
            ),
            "V1_EARLY_CONFIDENCE": (
                "PARITY_V1_EARLY_CONFIDENCE_FULL",
                "PARITY_V1_EARLY_CONFIDENCE_RECEIPT",
                11,
            ),
            "V1_CONFIRMATION_BASKET": (
                "PARITY_V1_CONFIRMATION_BASKET_FULL",
                "PARITY_V1_CONFIRMATION_BASKET_RECEIPT",
                6,
            ),
            "V2_VOL_OVERLAY": (
                "PARITY_VOL_OVERLAY_DECISIONS",
                "PARITY_VOL_OVERLAY_RECEIPT",
                13,
            ),
        }
        for population, (fixture_id, receipt_id, count) in expected_evidence.items():
            selected = [row for row in rows if row["parity_population"] == population]
            self.assertEqual(len(selected), count)
            self.assertTrue(all(row["parity_fixture_artifact"]["artifact_id"] == fixture_id for row in selected))
            self.assertTrue(all(row["parity_receipt_artifact"]["artifact_id"] == receipt_id for row in selected))
            self.assertTrue(all(row["parity_fixture_artifact"]["sha256"] for row in selected))
            self.assertTrue(all(row["parity_receipt_artifact"]["sha256"] for row in selected))

    def test_persisted_c4_report_must_match_fresh_c4_gate(self) -> None:
        from src.strategy_c4_acceptance import verify_c4_strategy_acceptance
        from src.strategy_c5_activation import validate_c4_acceptance_payload

        live = verify_c4_strategy_acceptance(PROJECT_ROOT)
        payload = json.loads(
            (PROJECT_ROOT / "reports/C4_STRATEGY_47_ACCEPTANCE.json").read_text(
                encoding="utf-8"
            )
        )
        validate_c4_acceptance_payload(payload, live)
        for field, value in (
            ("rule_pack_sha256", "0" * 64),
            ("parity_fixture_sha256", {}),
            ("strategy_ids", list(reversed(payload["strategy_ids"]))),
            ("source_commit", "not-a-commit"),
        ):
            with self.subTest(field=field):
                mutated = dict(payload)
                mutated[field] = value
                with self.assertRaisesRegex(ValueError, "C5_C4_REPORT_NOT_ACCEPTED"):
                    validate_c4_acceptance_payload(mutated, live)

    def test_report_is_immutable_and_writer_is_deterministic(self) -> None:
        from src.strategy_c5_activation import (
            verify_c5_activation,
            write_c5_acceptance_outputs,
        )

        report = verify_c5_activation(PROJECT_ROOT)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.status = "BLOCKED"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "C5_INVALID_SOURCE_COMMIT"):
                write_c5_acceptance_outputs(
                    report,
                    report_path=root / "report.json",
                    matrix_path=root / "matrix.json",
                    source_commit="invalid",
                    verified_at_utc="2026-08-08T12:34:56Z",
                )
            with self.assertRaisesRegex(ValueError, "C5_INVALID_VERIFIED_TIMESTAMP"):
                write_c5_acceptance_outputs(
                    report,
                    report_path=root / "report.json",
                    matrix_path=root / "matrix.json",
                    source_commit="b" * 40,
                    verified_at_utc="not-utc",
                )
            write_c5_acceptance_outputs(
                report,
                report_path=root / "report.json",
                matrix_path=root / "matrix.json",
                source_commit="b" * 40,
                verified_at_utc="2026-08-08T12:34:56Z",
            )
            for path in (root / "report.json", root / "matrix.json"):
                raw = path.read_bytes()
                self.assertTrue(raw.endswith(b"\n"))
                self.assertNotIn(b"NaN", raw)
                json.loads(raw)


if __name__ == "__main__":
    unittest.main()
