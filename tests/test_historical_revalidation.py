from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.historical_input_builder import HistoricalArtifactPaths, HistoricalInputBuilder
from src.historical_revalidation import (
    EXIT_PARITY,
    EXIT_SOURCE_INTEGRITY,
    AhrParityError,
    AhrRunProgress,
    AhrRunConfig,
    AhrSafetyError,
    _compare_v1_expected_components,
    _apply_progress_to_acceptance,
    _select_smoke_dates,
    _load_expected_v1,
    _v1_result_record,
    assert_files_unchanged,
    compare_semantic,
    default_artifacts,
    evaluate_persist_compare,
    historical_bindings,
    offline_network_guard,
    run_baseline,
    snapshot_files,
    validate_resume_manifest,
)
from src.strategy_dispatch import load_strategy_dispatcher
from src.strategy_v1 import V1Evaluation


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HistoricalRevalidationTests(unittest.TestCase):
    def test_smoke_dates_are_selected_from_current_contract_opportunities(self) -> None:
        builder = HistoricalInputBuilder(
            project_root=PROJECT_ROOT,
            artifacts=default_artifacts(),
        )
        dates = _select_smoke_dates(builder)

        self.assertEqual(
            dates,
            ("2026-02-13", "2026-03-04", "2026-03-08"),
        )
        for market_date in dates:
            for strategy_id in (
                "NO_FADE_P1_U1_T18",
                "YES_STRICT_A_T60",
                "YES_PF1_T60",
            ):
                self.assertTrue(
                    builder.is_contract_opportunity(
                        strategy_id=strategy_id,
                        market_date=market_date,
                    )
                )

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

    def test_pf1_expected_row_does_not_fallback_to_wrong_population(self) -> None:
        builder = HistoricalInputBuilder(
            project_root=PROJECT_ROOT,
            artifacts=default_artifacts(),
        )
        with self.assertRaisesRegex(AhrParityError, "AHR_EXPECTED_V1_EVIDENCE_MISSING"):
            _load_expected_v1(
                PROJECT_ROOT,
                {
                    "strategy_id": "YES_PF1_T60",
                    "market_date": "2026-01-01",
                    "checkpoint_minutes": 60,
                },
                builder.opportunity_map(),
            )

    def test_equality_expected_loader_returns_both_components(self) -> None:
        builder = HistoricalInputBuilder(
            project_root=PROJECT_ROOT,
            artifacts=default_artifacts(),
        )
        opportunities = builder.opportunity_map()
        market_date = next(
            date
            for date in opportunities.historical_dates
            if date in opportunities.early_confidence_dates
            and date in opportunities.u2_dates
        )
        components = _load_expected_v1(
            PROJECT_ROOT,
            {
                "strategy_id": "NO_FADE_P1_T60_U1_EQUALS_U2",
                "market_date": market_date,
                "checkpoint_minutes": 60,
            },
            opportunities,
        )

        self.assertEqual(tuple(row.universe for row in components), ("U1", "U2"))
        actual = dict(components[0].semantic)
        actual.pop("strategy_id")
        actual["strategy_id"] = "NO_FADE_P1_T60_U1_EQUALS_U2"
        self.assertEqual(_compare_v1_expected_components(actual, components), 2)

    def test_v1_record_derives_reference_favorite_from_current_request(self) -> None:
        buckets = tuple(
            SimpleNamespace(bucket_index=index, market_q_yes=0.9 if index == 5 else 0.01)
            for index in range(11)
        )
        request = SimpleNamespace(
            checkpoints=(SimpleNamespace(checkpoint_minutes=60, buckets=buckets),)
        )
        result = V1Evaluation(
            accepted=True,
            reason="SIGNAL_ACCEPTED",
            side="YES",
            checkpoint_minutes=60,
            selected_bucket_indices=(4,),
        )

        record = _v1_result_record(
            "YES_PF1_T60", "2026-01-01", "a" * 64, result, "unit", request
        )

        self.assertEqual(record["favorite_bucket_index"], 5)

    def test_resume_rejects_changed_input_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "AHR_RESUME_MANIFEST_MISMATCH"):
            validate_resume_manifest(
                state={"input_manifest_sha256": "a" * 64},
                current_manifest_sha256="b" * 64,
            )

    def test_failure_receipt_preserves_progress_after_completed_units(self) -> None:
        progress = AhrRunProgress(identities_discovered=4)
        progress.identity_ids_dispatched.update(
            {"YES_STRICT_A_T60", "YES_PF1_T60"}
        )
        progress.identity_ids_completed.add("YES_STRICT_A_T60")
        progress.dates_processed.update({"2026-02-13", "2026-03-04"})
        progress.families_touched.update({"STRICT_A", "PF1"})
        progress.units_dispatched = 4
        progress.units_completed = 3
        acceptance: dict[str, object] = {}

        _apply_progress_to_acceptance(acceptance, progress)

        self.assertEqual(acceptance["strategies_discovered"], 4)
        self.assertEqual(acceptance["strategies_dispatched"], 2)
        self.assertEqual(acceptance["strategies_completed"], 1)
        self.assertEqual(acceptance["units_dispatched"], 4)
        self.assertEqual(acceptance["units_completed"], 3)
        self.assertEqual(acceptance["dates_processed"], 2)
        self.assertEqual(acceptance["strategy_families"], 2)

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
