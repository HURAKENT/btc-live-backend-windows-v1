from __future__ import annotations

import hashlib
import importlib
import importlib.util
import unittest


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class C4V1FixtureBuilderTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("tools.build_c4_v1_parity_fixture")

    @staticmethod
    def _matrix_rows(date: str = "2099-01-01", horizon: int = 60):
        rows = []
        for index in range(11):
            q_yes = 0.27 if index == 2 else 0.20 if index == 5 else 0.10
            rows.append(
                {
                    "actual_winner": index == 2,
                    "bucket_index": index,
                    "bucket_title": f"secret bucket {index}",
                    "frozen_q_yes": q_yes,
                    "horizon_minutes": horizon,
                    "market_date": date,
                    "model_p": 0.30 if index == 2 else 0.10,
                    "no_observation_timestamp": 1_999,
                    "no_source_history_sha256": _sha(f"no:{index}"),
                    "q_no": 1.0 - q_yes,
                    "q_yes": q_yes,
                    "target_timestamp": 2_000,
                    "yes_observation_timestamp": 1_999,
                    "yes_source_history_sha256": _sha(f"yes:{index}"),
                }
            )
        return rows

    @staticmethod
    def _decision(date: str = "2099-01-01", horizon: int = 60):
        return {
            "accepted": True,
            "bucket_index": 2,
            "bucket_title": "secret bucket 2",
            "favorite_bucket_index": 2,
            "horizon_minutes": horizon,
            "market_date": date,
            "partition": "NA",
            "q": 0.27,
            "reason": None,
            "shares": 5,
            "side": "YES",
            "strategy_id": "PF1",
            "stressed_q": 0.30,
            "universe": "ALL",
            "won": True,
        }

    def test_builder_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.build_c4_v1_parity_fixture")
        )

    def test_conversion_uses_actual_q_and_sanitizes_date_and_bucket_title(self) -> None:
        records, audit = self._module().convert_early_horizon_rows(
            self._matrix_rows(),
            [self._decision()],
            covered_strategy_ids=("YES_PF1_T60",),
            require_canonical_counts=False,
        )

        serialized = self._module().canonical_jsonl(records).decode("utf-8")
        self.assertNotIn("2099-01-01", serialized)
        self.assertNotIn("secret bucket", serialized)
        decision = records[1]
        self.assertEqual(decision["buckets"][2]["q_yes_micros"], 270_000)
        self.assertEqual(audit["decision_expectation_count"], 1)
        self.assertEqual(audit["quantization_decision_mismatches"], 0)

    def test_frozen_q_is_not_substituted_for_full_decision_q(self) -> None:
        matrix = self._matrix_rows()
        matrix[2]["frozen_q_yes"] = 0.99

        records, _ = self._module().convert_early_horizon_rows(
            matrix,
            [self._decision()],
            covered_strategy_ids=("YES_PF1_T60",),
            require_canonical_counts=False,
        )

        self.assertEqual(records[1]["buckets"][2]["q_yes_micros"], 270_000)

    def test_all_eleven_buckets_are_required_before_any_record_is_emitted(self) -> None:
        with self.assertRaisesRegex(ValueError, "C4_V1_FIXTURE_EXPECTED_11_BUCKETS"):
            self._module().convert_early_horizon_rows(
                self._matrix_rows()[:-1],
                [self._decision()],
                covered_strategy_ids=("YES_PF1_T60",),
                require_canonical_counts=False,
            )

    def test_source_decision_and_economics_are_recomputed(self) -> None:
        decision = self._decision()
        decision["accepted"] = False
        decision["reason"] = "PF1_GATE"
        with self.assertRaisesRegex(ValueError, "C4_V1_PARITY_DECISION_MISMATCH"):
            self._module().convert_early_horizon_rows(
                self._matrix_rows(),
                [decision],
                covered_strategy_ids=("YES_PF1_T60",),
                require_canonical_counts=False,
            )

        decision = self._decision()
        decision["stressed_q"] = 0.31
        with self.assertRaisesRegex(ValueError, "C4_V1_FIXTURE_STRESSED_Q_MISMATCH"):
            self._module().convert_early_horizon_rows(
                self._matrix_rows(),
                [decision],
                covered_strategy_ids=("YES_PF1_T60",),
                require_canonical_counts=False,
            )

    def test_conversion_is_deterministic_and_import_has_no_pyarrow_dependency(self) -> None:
        module = self._module()
        first = module.convert_early_horizon_rows(
            self._matrix_rows(),
            [self._decision()],
            covered_strategy_ids=("YES_PF1_T60",),
            require_canonical_counts=False,
        )
        second = module.convert_early_horizon_rows(
            self._matrix_rows(),
            [self._decision()],
            covered_strategy_ids=("YES_PF1_T60",),
            require_canonical_counts=False,
        )
        self.assertEqual(first, second)
        self.assertNotIn("pyarrow", module.__dict__)

    def test_early_confidence_uses_only_baseline_and_binds_population(self) -> None:
        baseline = {
            "accepted": True,
            "actual_q": 0.27,
            "horizon_minutes": 60,
            "lambda_value": 1.0,
            "market_date": "2099-01-01",
            "reason": None,
            "regime": "BASELINE",
            "selected_bucket": 2,
            "side": "YES",
            "strategy": "PF1",
            "stressed_q": 0.30,
            "universe": "ALL",
            "won": True,
        }
        calibrated = {**baseline, "regime": "CALIBRATED_MONOTONE"}

        records, audit = self._module().convert_early_confidence_rows(
            self._matrix_rows(),
            [baseline, calibrated],
            covered_strategy_ids=("YES_PF1_T60",),
            require_canonical_counts=False,
        )

        self.assertEqual(records[0]["source_population"], "EARLY_CONFIDENCE")
        self.assertEqual(audit["baseline_decision_row_count"], 1)
        self.assertEqual(audit["source_decision_row_count"], 2)


if __name__ == "__main__":
    unittest.main()
