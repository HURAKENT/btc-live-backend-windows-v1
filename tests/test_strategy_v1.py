from __future__ import annotations

import dataclasses
import importlib
import math
import unittest


try:
    strategy_v1 = importlib.import_module("src.strategy_v1")
except ModuleNotFoundError:
    strategy_v1 = None


class StrategyV1Tests(unittest.TestCase):
    def _api(self, name: str):
        if strategy_v1 is None or not hasattr(strategy_v1, name):
            self.fail(f"MISSING_C4_V1_API:{name}")
        return getattr(strategy_v1, name)

    def _buckets(self):
        bucket_type = self._api("BucketInput")
        rows = []
        for index in range(11):
            rows.append(
                bucket_type(
                    bucket_index=index,
                    model_p=0.05,
                    market_q_yes=0.05,
                    market_q_no=0.99,
                    vwap5=0.10,
                    confirmed_fee=0.00,
                    no_token_id=f"NO-{index}",
                )
            )
        rows[5] = dataclasses.replace(rows[5], market_q_yes=0.20)
        return tuple(rows)

    def _replace(self, rows, index: int, **changes):
        mutable = list(rows)
        mutable[index] = dataclasses.replace(mutable[index], **changes)
        return tuple(mutable)

    def test_models_are_frozen_and_slotted(self):
        row = self._buckets()[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.model_p = 0.20
        self.assertFalse(hasattr(row, "__dict__"))

    def test_primary_source_hashes_are_bound_to_audited_files(self):
        hashes = self._api("V1_SOURCE_SHA256")
        self.assertEqual(
            "cb34c5b4df8cdc13629576bf9d326d4ff7debab55a43de8d822756352b973e1c",
            hashes["STRICT_A_SELECTOR"],
        )
        self.assertEqual(
            "71fd5851c9dbefae3c2bdc5c3cbc2558728479c13518ad58a9e35ab878983ea4",
            hashes["PF1_CORE"],
        )
        self.assertEqual(
            "45be74ea8825e504fda85d3d3b365275c92ceec37831080f422f370704b661c2",
            hashes["NO_CONFIRMATION_CONCEPTS"],
        )
        self.assertEqual(
            "d942e94f11fe4318de048f22e4864006f7bd1d64e06a00520c971160c8c800ef",
            hashes["FAVORITE_BASKET_RULES"],
        )

    def test_bucket_input_rejects_bool_index_and_nonfinite_probability(self):
        bucket_type = self._api("BucketInput")
        with self.assertRaisesRegex(ValueError, "INVALID_BUCKET_INDEX"):
            bucket_type(True, 0.1, 0.1, 0.9, 0.1, 0.0, "NO-X")
        with self.assertRaisesRegex(ValueError, "INVALID_MODEL_P"):
            bucket_type(0, math.nan, 0.1, 0.9, 0.1, 0.0, "NO-X")

    def test_snapshot_rejects_duplicate_bucket_and_list_input(self):
        evaluate = self._api("evaluate_strict_a")
        rows = self._buckets()
        with self.assertRaisesRegex(ValueError, "INVALID_BUCKET_SEQUENCE"):
            evaluate(60, list(rows))
        duplicate = rows[:-1] + (dataclasses.replace(rows[-1], bucket_index=9),)
        with self.assertRaisesRegex(ValueError, "EXPECTED_EXACTLY_11_BUCKETS"):
            evaluate(60, duplicate)

    def test_strict_a_accepts_exact_audited_gates(self):
        rows = self._replace(
            self._buckets(), 5, model_p=0.26, vwap5=0.21, confirmed_fee=0.01
        )
        result = self._api("evaluate_strict_a")(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.23, result.stressed_reference_cost)
        self.assertAlmostEqual(0.04, result.executable_edge)

    def test_strict_a_preserves_source_float_boundary_behavior(self):
        rows = self._replace(
            self._buckets(), 5, model_p=0.25, vwap5=0.20, confirmed_fee=0.00
        )
        result = self._api("evaluate_strict_a")(60, rows)
        self.assertFalse(result.accepted)
        self.assertEqual("HISTORICAL_REFERENCE_GATE_REJECTED", result.reason)
        self.assertLess(result.historical_edge, 0.02)

    def test_strict_a_rejects_reference_favorite_tie(self):
        rows = self._replace(self._buckets(), 4, market_q_yes=0.20)
        result = self._api("evaluate_strict_a")(60, rows)
        self.assertFalse(result.accepted)
        self.assertEqual("NO_UNIQUE_REFERENCE_FAVORITE", result.reason)

    def test_strict_a_rejects_missing_depth(self):
        rows = self._replace(self._buckets(), 5, model_p=0.30, vwap5=None)
        result = self._api("evaluate_strict_a")(60, rows)
        self.assertEqual("EXECUTION_REJECTED_MISSING_DEPTH", result.reason)

    def test_strict_a_rejects_executable_edge_below_threshold(self):
        rows = self._replace(
            self._buckets(), 5, model_p=0.26, vwap5=0.24, confirmed_fee=0.01
        )
        result = self._api("evaluate_strict_a")(60, rows)
        self.assertEqual("EXECUTABLE_EDGE_GATE_REJECTED", result.reason)

    def test_strict_a_blocks_t30_after_t60_position(self):
        result = self._api("evaluate_strict_a")(30, self._buckets(), prior_position=True)
        self.assertEqual("T30_BLOCKED_EXISTING_T60_POSITION", result.reason)

    def test_pf1_selects_max_model_minus_vwap_and_lower_exact_tie(self):
        rows = self._replace(self._buckets(), 2, model_p=0.40, vwap5=0.35)
        rows = self._replace(rows, 7, model_p=0.30, vwap5=0.25)
        result = self._api("evaluate_pf1")(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)

    def test_pf1_q_cap_boundary_is_inclusive(self):
        rows = self._replace(self._buckets(), 3, model_p=0.98, vwap5=0.95)
        result = self._api("evaluate_pf1")(60, rows)
        self.assertTrue(result.accepted)

    def test_pf1_rejects_q_above_cap(self):
        rows = self._replace(self._buckets(), 3, model_p=0.99, vwap5=0.951)
        result = self._api("evaluate_pf1")(60, rows)
        self.assertEqual("NO_SIGNAL_Q_ABOVE_CAP", result.reason)

    def test_pf1_requires_complete_depth_snapshot(self):
        rows = self._replace(self._buckets(), 10, vwap5=None)
        result = self._api("evaluate_pf1")(60, rows)
        self.assertEqual("EXECUTION_REJECTED_MISSING_DEPTH", result.reason)

    def test_pf1_blocks_t30_after_t60_position(self):
        result = self._api("evaluate_pf1")(30, self._buckets(), prior_position=True)
        self.assertEqual("SKIPPED_ALREADY_POSITIONED", result.reason)

    def test_historical_pf1_preserves_raw_selector_and_q_cap(self):
        rows = self._replace(
            self._buckets(), 2, model_p=0.30, market_q_yes=0.27
        )
        result = self._api("evaluate_pf1_historical")(360, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.03, result.historical_edge)

    def test_historical_pf1_q_cap_is_point_nine_seven(self):
        rows = self._replace(
            self._buckets(), 2, model_p=1.00, market_q_yes=0.971
        )
        result = self._api("evaluate_pf1_historical")(360, rows)
        self.assertEqual("PF1_GATE", result.reason)

    def test_no_fade_p1_selects_unique_nonfavorite_minimum_and_boundary(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        result = self._api("evaluate_no_fade")("P1", 60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)
        self.assertEqual(5, result.favorite_bucket_index)
        self.assertAlmostEqual(0.02, result.historical_edge)

    def test_no_fade_p2_uses_raw_edge_while_p1_uses_stressed_edge(self):
        rows = self._replace(self._buckets(), 2, model_p=0.08, market_q_yes=0.10)
        p1 = self._api("evaluate_no_fade")("P1", 60, rows)
        p2 = self._api("evaluate_no_fade")("P2", 60, rows)
        self.assertFalse(p1.accepted)
        self.assertTrue(p2.accepted)

    def test_no_fade_rejects_tied_selector_score(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        rows = self._replace(rows, 3, model_p=0.05, market_q_yes=0.10)
        result = self._api("evaluate_no_fade")("P1", 60, rows)
        self.assertEqual("TIED_NO_FADE_SCORE", result.reason)

    def test_no_fade_rejects_nonunique_favorite(self):
        rows = self._replace(self._buckets(), 4, market_q_yes=0.20)
        result = self._api("evaluate_no_fade")("P1", 60, rows)
        self.assertEqual("NO_UNIQUE_MARKET_FAVORITE", result.reason)

    def test_no_fade_stressed_cost_is_strictly_below_one(self):
        rows = self._replace(self._buckets(), 2, model_p=0.00, market_q_yes=0.02)
        result = self._api("evaluate_no_fade")("P2", 60, rows)
        self.assertEqual("STRESSED_COST_NOT_BELOW_ONE", result.reason)

    def test_no_fade_rejects_unknown_partition(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_NO_FADE_PARTITION"):
            self._api("evaluate_no_fade")("P3", 60, self._buckets())

    def test_favorite_only_accepts_inclusive_stressed_edge(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        result = self._api("evaluate_favorite_only")(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.02, result.historical_edge)

    def test_favorite_only_rejects_nonunique_favorite(self):
        rows = self._replace(self._buckets(), 4, market_q_yes=0.20)
        result = self._api("evaluate_favorite_only")(60, rows)
        self.assertEqual("NO_UNIQUE_MARKET_FAVORITE", result.reason)

    def test_favorite_neighbor_selects_best_immediate_neighbor(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        rows = self._replace(rows, 4, model_p=0.18, market_q_yes=0.10)
        rows = self._replace(rows, 6, model_p=0.15, market_q_yes=0.10)
        result = self._api("evaluate_favorite_neighbor")(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((5, 4), result.selected_bucket_indices)

    def test_favorite_neighbor_rejects_tied_neighbor_score(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        rows = self._replace(rows, 4, model_p=0.18, market_q_yes=0.10)
        rows = self._replace(rows, 6, model_p=0.18, market_q_yes=0.10)
        result = self._api("evaluate_favorite_neighbor")(60, rows)
        self.assertEqual("TIED_NEIGHBOR_EDGE", result.reason)

    def test_favorite_neighbor_preserves_favorite_then_neighbor_order(self):
        rows = tuple(reversed(self._buckets()))
        rows = self._replace(tuple(sorted(rows, key=lambda row: row.bucket_index)), 5, model_p=0.25)
        rows = self._replace(rows, 4, model_p=0.18, market_q_yes=0.10)
        rows = self._replace(rows, 6, model_p=0.15, market_q_yes=0.10)
        result = self._api("evaluate_favorite_neighbor")(60, tuple(reversed(rows)))
        self.assertEqual((5, 4), result.selected_bucket_indices)

    def test_confirmation_a2_uses_direct_actual_no_edge(self):
        rows = self._replace(self._buckets(), 3, model_p=0.20, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("A2", rows)
        self.assertTrue(result.accepted)
        self.assertEqual((3,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.10, result.historical_edge)

    def test_confirmation_a0_excludes_unique_favorite_and_uses_proxy_selector(self):
        rows = self._replace(self._buckets(), 5, model_p=0.00, market_q_no=0.10)
        rows = self._replace(rows, 2, model_p=0.04, market_q_yes=0.10, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("A0", rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)

    def test_confirmation_b2_limits_selection_to_tail_indices(self):
        rows = self._replace(self._buckets(), 4, model_p=0.00, market_q_no=0.10)
        rows = self._replace(rows, 9, model_p=0.20, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("B2", rows)
        self.assertEqual((9,), result.selected_bucket_indices)

    def test_confirmation_uses_t30_only_after_t60_rejects(self):
        t60 = self._buckets()
        t30 = self._replace(self._buckets(), 3, model_p=0.20, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("A2", t60, t30=t30)
        self.assertTrue(result.accepted)
        self.assertEqual(30, result.checkpoint_minutes)

    def test_confirmation_c1_requires_switch_and_trades_old_favorite_token(self):
        t60 = self._replace(self._buckets(), 5, model_p=0.40, market_q_yes=0.30)
        t30 = self._replace(self._buckets(), 5, model_p=0.35, market_q_yes=0.20, market_q_no=0.50)
        t30 = self._replace(t30, 6, market_q_yes=0.35)
        result = self._api("evaluate_confirmation")("C1", t60, t30=t30)
        self.assertTrue(result.accepted)
        self.assertEqual(30, result.checkpoint_minutes)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertEqual(5, result.favorite_bucket_index)
        self.assertAlmostEqual(0.05, result.model_drop)

    def test_confirmation_c1_rejects_without_favorite_switch(self):
        result = self._api("evaluate_confirmation")(
            "C1", self._buckets(), t30=self._buckets()
        )
        self.assertEqual("C1_FAVORITE_DID_NOT_SWITCH", result.reason)

    def test_confirmation_requires_actual_no_price(self):
        rows = self._replace(self._buckets(), 3, model_p=0.20, market_q_no=None)
        with self.assertRaisesRegex(ValueError, "MISSING_ACTUAL_NO_Q"):
            self._api("evaluate_confirmation")("A2", rows)

    def test_confirmation_rejects_unknown_concept(self):
        with self.assertRaisesRegex(ValueError, "UNAPPROVED_CONFIRMATION_CONCEPT"):
            self._api("evaluate_confirmation")("Z9", self._buckets())


if __name__ == "__main__":
    unittest.main()
