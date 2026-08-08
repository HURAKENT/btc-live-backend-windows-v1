from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILDER = PROJECT_ROOT / "tools" / "build_c4_v2_parity_fixture.py"
RECEIPT = (
    PROJECT_ROOT
    / "strategy_sources"
    / "frozen"
    / "parity"
    / "VOL_OVERLAY_DECISION_PARITY_RECEIPT.json"
)


class C4V2FixtureBuilderTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("tools.build_c4_v2_parity_fixture")

    @staticmethod
    def _row(**overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "strategy_id": "NO_A2",
            "checkpoint_minutes": 60,
            "selected_bucket": "synthetic bucket",
            "side": "NO",
            "baseline_accept": True,
            "actual_q": 0.4,
            "stressed_q_3c": 0.43,
            "shares": 5,
            "won": True,
            "number_of_legs": 1,
            "source_decision_sha256": "a" * 64,
            "forecast_row_sha256": "b" * 64,
            "p_vol_side": 0.45,
            "regime": "VOL_CONFIRMATION",
            "accepted": True,
            "turnover_5_shares": 2.15,
            "pnl_5_shares": 2.85,
        }
        row.update(overrides)
        return row

    def test_builder_module_exists(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("tools.build_c4_v2_parity_fixture")
        )

    def test_converter_receipt_is_checkout_independent(self) -> None:
        receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(BUILDER.read_bytes()).hexdigest(),
            receipt["converter_sha256"],
        )
        attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn(
            "tools/build_c4_v2_parity_fixture.py text eol=lf",
            attributes.splitlines(),
        )

    def test_exact_row_is_quantized_and_sanitized(self) -> None:
        rows, audit = self._module().convert_overlay_rows([self._row()])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["overlay_strategy_id"], "NO_A2_V2_VOL")
        self.assertEqual(row["actual_price_micros"], 400_000)
        self.assertEqual(row["p_vol_side_micros"], 450_000)
        self.assertEqual(row["expected_stressed_q_micros"], 430_000)
        self.assertEqual(row["expected_turnover_micros"], 2_150_000)
        self.assertEqual(row["expected_pnl_micros"], 2_850_000)
        self.assertNotIn("synthetic bucket", str(row))
        self.assertEqual(len(row["selected_buckets"]), 1)
        self.assertEqual(audit["decision_mismatches"], 0)
        self.assertEqual(audit["quantization_ambiguities"], 0)

    def test_only_exact_frozen_parent_mode_pair_is_selected(self) -> None:
        baseline = self._row(regime="BASELINE")
        wrong_mode = self._row(regime="VOL_VETO_ONLY")
        rows, audit = self._module().convert_overlay_rows(
            [baseline, wrong_mode, self._row()]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(audit["source_rows"], 3)
        self.assertEqual(audit["selected_rows"], 1)

    def test_leg_count_must_match_structured_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "C4_V2_FIXTURE_LEG_COUNT_MISMATCH"):
            self._module().convert_overlay_rows(
                [
                    self._row(
                        strategy_id="YES_FAVORITE_ONLY",
                        side="YES",
                        selected_bucket="one | two",
                        number_of_legs=1,
                    )
                ]
            )

    def test_source_decision_and_quantized_stress_are_recomputed(self) -> None:
        with self.assertRaisesRegex(ValueError, "C4_V2_FIXTURE_SOURCE_DECISION_MISMATCH"):
            self._module().convert_overlay_rows(
                [self._row(accepted=False, turnover_5_shares=0.0, pnl_5_shares=0.0)]
            )
        with self.assertRaisesRegex(ValueError, "C4_V2_FIXTURE_STRESS_MISMATCH"):
            self._module().convert_overlay_rows(
                [self._row(stressed_q_3c=0.430001)]
            )

    def test_source_trade_economics_are_recomputed_from_five_shares(self) -> None:
        for row, reason in (
            (
                self._row(turnover_5_shares=2.150001),
                "C4_V2_FIXTURE_TURNOVER_MISMATCH",
            ),
            (
                self._row(pnl_5_shares=2.849999),
                "C4_V2_FIXTURE_PNL_MISMATCH",
            ),
            (
                self._row(won=False),
                "C4_V2_FIXTURE_PNL_MISMATCH",
            ),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    self._module().convert_overlay_rows([row])

    def test_threshold_ambiguity_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "C4_V2_FIXTURE_QUANTIZATION_AMBIGUITY"):
            self._module().convert_overlay_rows(
                [self._row(p_vol_side=0.4499996, accepted=True)]
            )

    def test_exact_types_hashes_and_finite_values_are_required(self) -> None:
        for mutation in (
            {"shares": True},
            {"won": 1},
            {"actual_q": float("nan")},
            {"source_decision_sha256": "A" * 64},
            {"selected_bucket": ""},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    self._module().convert_overlay_rows([self._row(**mutation)])

    def test_conversion_is_deterministic(self) -> None:
        first = self._module().convert_overlay_rows([self._row()])
        second = self._module().convert_overlay_rows([self._row()])
        self.assertEqual(first, second)

    def test_git_commit_and_sha256_contracts_are_distinct(self) -> None:
        module = self._module()
        self.assertEqual(module._git_sha40("a" * 40), "a" * 40)
        with self.assertRaises(ValueError):
            module._git_sha40("a" * 64)


if __name__ == "__main__":
    unittest.main()
