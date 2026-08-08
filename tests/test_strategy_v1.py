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

    def _strict_evidence(self):
        return self._api("StrictPriceHistoryEvidence")(
            provenance="CLOB_PRICE_HISTORY",
            source_sha256="a" * 64,
            checkpoint_timestamp_ms=2_000,
            observation_timestamp_ms=1_999,
        )

    def _pf1_evidence(self, **changes):
        values = {
            "bucket_count": 11,
            "snapshot_complete": True,
            "synchronized": True,
            "fresh": True,
            "crossed_book_count": 0,
            "fee_provenance": "GAMMA_FEE_SCHEDULE_FILL_WEIGHTED",
            "snapshot_sha256": "b" * 64,
            "fee_schedule_sha256": "c" * 64,
        }
        values.update(changes)
        return self._api("Pf1SnapshotEvidence")(**values)

    def _no_execution(self, index=2, **changes):
        values = {
            "bucket_index": index,
            "actual_no_vwap5": 0.85,
            "available_depth_shares": 5.0,
            "confirmed_fee": 0.01,
            "depth_provenance_sha256": "d" * 64,
            "fee_provenance_sha256": "e" * 64,
        }
        values.update(changes)
        return (self._api("NoExecutionEvidence")(**values),)

    def _strict(self, checkpoint, rows, **kwargs):
        kwargs.setdefault("price_history_evidence", self._strict_evidence())
        identity = (
            "YES_STRICT_A_OPERATIONAL"
            if kwargs.get("prior_position")
            else "YES_STRICT_A_T60" if checkpoint == 60 else "YES_STRICT_A_T30"
        )
        return self._api("evaluate_strict_a")(identity, checkpoint, rows, **kwargs)

    def _pf1(self, checkpoint, rows, **kwargs):
        kwargs.setdefault("snapshot_evidence", self._pf1_evidence())
        identity = (
            "YES_PF1_OPERATIONAL"
            if kwargs.get("prior_position")
            else "YES_PF1_T60" if checkpoint == 60 else "YES_PF1_T30"
        )
        return self._api("evaluate_pf1")(identity, checkpoint, rows, **kwargs)

    def _no_fade(self, partition, checkpoint, rows, **kwargs):
        kwargs.setdefault("no_execution", self._no_execution())
        identities = {
            ("P1", 30): "NO_FADE_P1_U1_OPERATIONAL",
            ("P1", 60): "NO_FADE_P1_U1_OPERATIONAL",
            ("P1", 720): "NO_FADE_P1_U1_T12",
            ("P1", 1080): "NO_FADE_P1_U1_T18",
            ("P2", 30): "NO_FADE_P2_U1_T30",
            ("P2", 60): "NO_FADE_P2_U1_T60",
            ("P2", 120): "NO_FADE_P2_U1_T2H",
            ("P2", 240): "NO_FADE_P2_U2_T4H",
            ("P2", 720): "NO_FADE_P2_U1_T12",
        }
        return self._api("evaluate_no_fade")(
            identities[(partition, checkpoint)], checkpoint, rows, **kwargs
        )

    def test_models_are_frozen_and_slotted(self):
        row = self._buckets()[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.model_p = 0.20
        self.assertFalse(hasattr(row, "__dict__"))
        for evidence in (
            self._strict_evidence(),
            self._pf1_evidence(),
            self._no_execution()[0],
        ):
            with self.subTest(evidence=type(evidence).__name__):
                self.assertFalse(hasattr(evidence, "__dict__"))
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(
                        evidence,
                        dataclasses.fields(evidence)[0].name,
                        getattr(evidence, dataclasses.fields(evidence)[0].name),
                    )

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
            "a510e45a5dfa4399bf05d8a418d28c055383cc0dfb8062e725c86ef105215e70",
            hashes["NO_A0_FROZEN_RULES"],
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
        rows = self._buckets()
        with self.assertRaisesRegex(ValueError, "INVALID_BUCKET_SEQUENCE"):
            self._strict(60, list(rows))
        duplicate = rows[:-1] + (dataclasses.replace(rows[-1], bucket_index=9),)
        with self.assertRaisesRegex(ValueError, "EXPECTED_EXACTLY_11_BUCKETS"):
            self._strict(60, duplicate)

    def test_strict_a_accepts_exact_audited_gates(self):
        rows = self._replace(
            self._buckets(), 5, model_p=0.26, vwap5=0.21, confirmed_fee=0.01
        )
        result = self._strict(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.23, result.stressed_reference_cost)
        self.assertAlmostEqual(0.04, result.executable_edge)

    def test_strict_a_preserves_source_float_boundary_behavior(self):
        rows = self._replace(
            self._buckets(), 5, model_p=0.25, vwap5=0.20, confirmed_fee=0.00
        )
        result = self._strict(60, rows)
        self.assertFalse(result.accepted)
        self.assertEqual("HISTORICAL_REFERENCE_GATE_REJECTED", result.reason)
        self.assertLess(result.historical_edge, 0.02)

    def test_strict_a_rejects_reference_favorite_tie(self):
        rows = self._replace(self._buckets(), 4, market_q_yes=0.20)
        result = self._strict(60, rows)
        self.assertFalse(result.accepted)
        self.assertEqual("NO_UNIQUE_REFERENCE_FAVORITE", result.reason)

    def test_strict_a_rejects_missing_depth(self):
        rows = self._replace(self._buckets(), 5, model_p=0.30, vwap5=None)
        result = self._strict(60, rows)
        self.assertEqual("EXECUTION_REJECTED_MISSING_DEPTH", result.reason)

    def test_strict_a_rejects_executable_edge_below_threshold(self):
        rows = self._replace(
            self._buckets(), 5, model_p=0.26, vwap5=0.24, confirmed_fee=0.01
        )
        result = self._strict(60, rows)
        self.assertEqual("EXECUTABLE_EDGE_GATE_REJECTED", result.reason)

    def test_strict_a_blocks_t30_after_t60_position(self):
        result = self._strict(30, self._buckets(), prior_position=True)
        self.assertEqual("T30_BLOCKED_EXISTING_T60_POSITION", result.reason)

    def test_pf1_selects_max_model_minus_vwap_and_lower_exact_tie(self):
        rows = self._replace(self._buckets(), 2, model_p=0.40, vwap5=0.35)
        rows = self._replace(rows, 7, model_p=0.30, vwap5=0.25)
        result = self._pf1(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)

    def test_pf1_q_cap_boundary_is_inclusive(self):
        rows = self._replace(self._buckets(), 3, model_p=0.98, vwap5=0.95)
        result = self._pf1(60, rows)
        self.assertTrue(result.accepted)

    def test_pf1_rejects_q_above_cap(self):
        rows = self._replace(self._buckets(), 3, model_p=0.99, vwap5=0.951)
        result = self._pf1(60, rows)
        self.assertEqual("NO_SIGNAL_Q_ABOVE_CAP", result.reason)

    def test_pf1_requires_complete_depth_snapshot(self):
        rows = self._replace(self._buckets(), 10, vwap5=None)
        result = self._pf1(60, rows)
        self.assertEqual("EXECUTION_REJECTED_MISSING_DEPTH", result.reason)

    def test_pf1_blocks_t30_after_t60_position(self):
        result = self._pf1(30, self._buckets(), prior_position=True)
        self.assertEqual("SKIPPED_ALREADY_POSITIONED", result.reason)

    def test_historical_pf1_preserves_raw_selector_and_q_cap(self):
        rows = self._replace(
            self._buckets(), 2, model_p=0.30, market_q_yes=0.27
        )
        result = self._api("evaluate_pf1_historical")("YES_PF1_T6H", 360, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.03, result.historical_edge)
        self.assertTrue(result.historical_only)
        self.assertFalse(result.execution_eligible)

    def test_historical_pf1_q_cap_is_point_nine_seven(self):
        rows = self._replace(
            self._buckets(), 2, model_p=1.00, market_q_yes=0.971
        )
        result = self._api("evaluate_pf1_historical")("YES_PF1_T6H", 360, rows)
        self.assertEqual("PF1_GATE", result.reason)

    def test_no_fade_p1_selects_unique_nonfavorite_minimum_and_boundary(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        result = self._no_fade("P1", 60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)
        self.assertEqual(5, result.favorite_bucket_index)
        self.assertAlmostEqual(0.02, result.historical_edge)

    def test_no_fade_p2_uses_raw_edge_while_p1_uses_stressed_edge(self):
        rows = self._replace(self._buckets(), 2, model_p=0.08, market_q_yes=0.10)
        p1 = self._no_fade("P1", 60, rows)
        p2 = self._no_fade("P2", 60, rows)
        self.assertFalse(p1.accepted)
        self.assertTrue(p2.accepted)

    def test_no_fade_rejects_tied_selector_score(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        rows = self._replace(rows, 3, model_p=0.05, market_q_yes=0.10)
        result = self._no_fade("P1", 60, rows)
        self.assertEqual("TIED_NO_FADE_SCORE", result.reason)

    def test_no_fade_rejects_nonunique_favorite(self):
        rows = self._replace(self._buckets(), 4, market_q_yes=0.20)
        result = self._no_fade("P1", 60, rows)
        self.assertEqual("NO_UNIQUE_MARKET_FAVORITE", result.reason)

    def test_no_fade_stressed_cost_is_strictly_below_one(self):
        rows = self._replace(self._buckets(), 2, model_p=0.00, market_q_yes=0.02)
        result = self._no_fade("P2", 60, rows)
        self.assertEqual("STRESSED_COST_NOT_BELOW_ONE", result.reason)

    def test_no_fade_rejects_unknown_partition(self):
        with self.assertRaisesRegex(ValueError, "IDENTITY_EVALUATOR_MISMATCH"):
            self._api("evaluate_no_fade")("NO_FADE_UNKNOWN", 60, self._buckets())

    def test_no_fade_t12_and_t18_identities_use_minute_horizons(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)

        t12 = self._no_fade("P1", 720, rows)
        t18 = self._no_fade("P1", 1080, rows)

        self.assertTrue(t12.accepted)
        self.assertEqual(720, t12.checkpoint_minutes)
        self.assertTrue(t18.accepted)
        self.assertEqual(1080, t18.checkpoint_minutes)

    def test_no_fade_rejects_hour_labels_as_minute_values(self):
        for invalid_checkpoint in (12, 18):
            with self.subTest(checkpoint_minutes=invalid_checkpoint):
                with self.assertRaisesRegex(
                    ValueError, "IDENTITY_CHECKPOINT_MISMATCH"
                ):
                    self._api("evaluate_no_fade")(
                        "NO_FADE_P1_U1_T12", invalid_checkpoint, self._buckets()
                    )

    def test_favorite_only_accepts_candidate_b_raw_edge(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        result = self._api("evaluate_favorite_only")(60, rows)
        self.assertTrue(result.accepted)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.05, result.historical_edge)

    def test_favorite_only_rejects_nonunique_favorite(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        rows = self._replace(rows, 4, market_q_yes=0.20)
        result = self._api("evaluate_favorite_only")(60, rows)
        self.assertEqual("CANDIDATE_B_SELECTED_NOT_UNIQUE_FAVORITE", result.reason)

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
        result = self._api("evaluate_confirmation")("NO_A2", rows)
        self.assertTrue(result.accepted)
        self.assertEqual((3,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.10, result.historical_edge)

    def test_confirmation_a0_excludes_unique_favorite_and_uses_proxy_selector(self):
        rows = self._replace(self._buckets(), 5, model_p=0.00, market_q_no=0.10)
        rows = self._replace(rows, 2, model_p=0.04, market_q_yes=0.10, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("NO_A0", rows)
        self.assertTrue(result.accepted)
        self.assertEqual((2,), result.selected_bucket_indices)

    def test_confirmation_b2_limits_selection_to_tail_indices(self):
        rows = self._replace(self._buckets(), 4, model_p=0.00, market_q_no=0.10)
        rows = self._replace(rows, 9, model_p=0.20, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("NO_B2", rows)
        self.assertEqual((9,), result.selected_bucket_indices)

    def test_confirmation_uses_t30_only_after_t60_rejects(self):
        t60 = self._buckets()
        t30 = self._replace(self._buckets(), 3, model_p=0.20, market_q_no=0.70)
        result = self._api("evaluate_confirmation")("NO_A2", t60, t30=t30)
        self.assertTrue(result.accepted)
        self.assertEqual(30, result.checkpoint_minutes)

    def test_confirmation_c1_requires_switch_and_trades_old_favorite_token(self):
        t60 = self._replace(self._buckets(), 5, model_p=0.40, market_q_yes=0.30)
        t30 = self._replace(self._buckets(), 5, model_p=0.35, market_q_yes=0.20, market_q_no=0.50)
        t30 = self._replace(t30, 6, market_q_yes=0.35)
        result = self._api("evaluate_confirmation")("NO_C1", t60, t30=t30)
        self.assertTrue(result.accepted)
        self.assertEqual(30, result.checkpoint_minutes)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertEqual(5, result.favorite_bucket_index)
        self.assertAlmostEqual(0.05, result.model_drop)

    def test_confirmation_c1_rejects_without_favorite_switch(self):
        result = self._api("evaluate_confirmation")(
            "NO_C1", self._buckets(), t30=self._buckets()
        )
        self.assertEqual("C1_FAVORITE_DID_NOT_SWITCH", result.reason)

    def test_confirmation_requires_actual_no_price(self):
        rows = self._replace(self._buckets(), 3, model_p=0.20, market_q_no=None)
        with self.assertRaisesRegex(ValueError, "MISSING_ACTUAL_NO_Q"):
            self._api("evaluate_confirmation")("NO_A2", rows)

    def test_confirmation_rejects_unknown_concept(self):
        with self.assertRaisesRegex(ValueError, "UNAPPROVED_CONFIRMATION_CONCEPT"):
            self._api("evaluate_confirmation")("Z9", self._buckets())

    def test_no_fade_requires_actual_no_execution_after_proxy_selection(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        result = self._api("evaluate_no_fade")(
            "NO_FADE_P1_U1_OPERATIONAL", 60, rows, no_execution=()
        )
        self.assertEqual("NO_EXECUTION_NOT_CALCULABLE", result.reason)

    def test_strict_historical_reproduces_selector_without_execution_evidence(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        result = self._api("evaluate_strict_historical")(
            "YES_STRICT_A_T60", 60, rows
        )
        self.assertTrue(result.accepted)
        self.assertEqual((5,), result.selected_bucket_indices)
        self.assertAlmostEqual(0.23, result.stressed_reference_cost)
        self.assertAlmostEqual(0.02, result.historical_edge)
        self.assertTrue(result.historical_only)
        self.assertFalse(result.execution_eligible)

        execution = self._api("evaluate_strict_a")(
            "YES_STRICT_A_T60", 60, rows
        )
        self.assertEqual("STRICT_PRICE_HISTORY_EVIDENCE_REQUIRED", execution.reason)
        self.assertFalse(execution.historical_only)
        self.assertFalse(execution.execution_eligible)
        operational_t30 = self._api("evaluate_strict_historical")(
            "YES_STRICT_A_OPERATIONAL", 30, rows
        )
        self.assertTrue(operational_t30.accepted)
        self.assertEqual(30, operational_t30.checkpoint_minutes)

    def test_strict_historical_rejects_gate_and_wrong_schedule(self):
        rejected = self._api("evaluate_strict_historical")(
            "YES_STRICT_A_T60", 60, self._buckets()
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual("STRICT_EDGE_GATE", rejected.reason)
        self.assertTrue(rejected.historical_only)
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            self._api("evaluate_strict_historical")(
                "YES_STRICT_A_T60", 30, self._buckets()
            )
        with self.assertRaisesRegex(ValueError, "IDENTITY_EVALUATOR_MISMATCH"):
            self._api("evaluate_strict_historical")(
                "YES_PF1_T60", 60, self._buckets()
            )

    def test_no_fade_historical_reproduces_proxy_only_p1_and_p2(self):
        rows = self._replace(
            self._buckets(), 2, model_p=0.05, market_q_yes=0.10
        )
        p1 = self._api("evaluate_no_fade_historical")(
            "NO_FADE_P1_U1_OPERATIONAL", 60, rows
        )
        p2 = self._api("evaluate_no_fade_historical")(
            "NO_FADE_P2_U1_OPERATIONAL", 60, rows
        )
        for result in (p1, p2):
            self.assertTrue(result.accepted)
            self.assertEqual((2,), result.selected_bucket_indices)
            self.assertTrue(result.historical_only)
            self.assertFalse(result.execution_eligible)
            self.assertIsNone(result.actual_no_vwap5)

        execution = self._api("evaluate_no_fade")(
            "NO_FADE_P1_U1_OPERATIONAL", 60, rows, no_execution=()
        )
        self.assertEqual("NO_EXECUTION_NOT_CALCULABLE", execution.reason)
        self.assertFalse(execution.historical_only)

    def test_no_fade_historical_rejects_proxy_gate_and_wrong_schedule(self):
        rows = self._replace(
            self._buckets(), 2, model_p=0.08, market_q_yes=0.10
        )
        rejected = self._api("evaluate_no_fade_historical")(
            "NO_FADE_P1_U1_OPERATIONAL", 60, rows
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual("STRESSED_EDGE_BELOW_002", rejected.reason)
        self.assertTrue(rejected.historical_only)
        p2_control = self._api("evaluate_no_fade_historical")(
            "NO_FADE_P2_U1_OPERATIONAL", 60, rows
        )
        self.assertTrue(p2_control.accepted)
        self.assertAlmostEqual(0.02, p2_control.historical_edge)
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            self._api("evaluate_no_fade_historical")(
                "NO_FADE_P2_U2_T4H", 60, rows
            )
        with self.assertRaisesRegex(ValueError, "IDENTITY_EVALUATOR_MISMATCH"):
            self._api("evaluate_no_fade_historical")("NO_A0", 60, rows)

    def test_no_fade_uses_distinct_actual_no_vwap_depth_and_fee(self):
        rows = self._replace(
            self._buckets(), 2, model_p=0.05, market_q_yes=0.10, vwap5=0.01
        )
        result = self._api("evaluate_no_fade")(
            "NO_FADE_P1_U1_OPERATIONAL", 60, rows, no_execution=self._no_execution()
        )
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(0.85, result.actual_no_vwap5)
        self.assertAlmostEqual(0.01, result.actual_no_fee)
        self.assertAlmostEqual(0.86, result.actual_no_execution_cost)
        self.assertAlmostEqual(0.10, result.actual_no_executable_edge)

    def test_no_fade_execution_edge_excludes_fee_but_fee_remains_required(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        result = self._api("evaluate_no_fade")(
            "NO_FADE_P1_U1_OPERATIONAL",
            60,
            rows,
            no_execution=self._no_execution(
                actual_no_vwap5=0.92,
                confirmed_fee=0.02,
            ),
        )
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(0.94, result.actual_no_execution_cost)
        self.assertAlmostEqual(0.03, result.actual_no_executable_edge)

        not_calculable = self._api("evaluate_no_fade")(
            "NO_FADE_P1_U1_OPERATIONAL",
            60,
            rows,
            no_execution=self._no_execution(
                actual_no_vwap5=0.92,
                confirmed_fee=None,
            ),
        )
        self.assertEqual("NO_EXECUTION_NOT_CALCULABLE", not_calculable.reason)

    def test_no_fade_actual_no_gate_rejects_depth_fee_and_edge(self):
        rows = self._replace(self._buckets(), 2, model_p=0.05, market_q_yes=0.10)
        cases = (
            (self._no_execution(available_depth_shares=4.99), "NO_EXECUTION_INSUFFICIENT_DEPTH"),
            (self._no_execution(confirmed_fee=None), "NO_EXECUTION_NOT_CALCULABLE"),
            (self._no_execution(actual_no_vwap5=0.94, confirmed_fee=0.01), "NO_EXECUTABLE_EDGE_GATE_REJECTED"),
        )
        for evidence, reason in cases:
            with self.subTest(reason=reason):
                result = self._api("evaluate_no_fade")(
                    "NO_FADE_P1_U1_OPERATIONAL", 60, rows, no_execution=evidence
                )
                self.assertEqual(reason, result.reason)

    def test_favorite_only_is_candidate_b_unique_favorite_control(self):
        rows = self._replace(self._buckets(), 5, model_p=0.25)
        rows = self._replace(rows, 2, model_p=0.40, market_q_yes=0.10)
        result = self._api("evaluate_favorite_only")(60, rows)
        self.assertFalse(result.accepted)
        self.assertEqual("CANDIDATE_B_SELECTED_NOT_UNIQUE_FAVORITE", result.reason)
        self.assertEqual((2,), result.selected_bucket_indices)

    def test_confirmation_a2_b2_near_tie_chooses_lower_index(self):
        rows = self._replace(self._buckets(), 2, model_p=0.20, market_q_no=0.70)
        rows = self._replace(rows, 3, model_p=0.20, market_q_no=0.6999999999995)
        a2 = self._api("evaluate_confirmation")("NO_A2", rows)
        self.assertEqual((2,), a2.selected_bucket_indices)
        tails = self._replace(self._buckets(), 0, model_p=0.20, market_q_no=0.70)
        tails = self._replace(tails, 1, model_p=0.20, market_q_no=0.6999999999995)
        b2 = self._api("evaluate_confirmation")("NO_B2", tails)
        self.assertEqual((0,), b2.selected_bucket_indices)

    def test_confirmation_a0_exact_selector_score_tie_is_no_signal(self):
        rows = self._replace(
            self._buckets(), 2, model_p=0.05, market_q_yes=0.10
        )
        rows = self._replace(rows, 3, model_p=0.05, market_q_yes=0.10)
        result = self._api("evaluate_confirmation")("NO_A0", rows)
        self.assertFalse(result.accepted)
        self.assertEqual("A0_TIED_SELECTOR_SCORE", result.reason)
        self.assertEqual((), result.selected_bucket_indices)

    def test_strict_requires_exact_clob_price_history_provenance(self):
        rows = self._replace(self._buckets(), 5, model_p=0.26, vwap5=0.21)
        missing = self._api("evaluate_strict_a")("YES_STRICT_A_T60", 60, rows)
        self.assertEqual("STRICT_PRICE_HISTORY_EVIDENCE_REQUIRED", missing.reason)
        accepted = self._api("evaluate_strict_a")(
            "YES_STRICT_A_T60",
            60,
            rows,
            price_history_evidence=self._strict_evidence(),
        )
        self.assertTrue(accepted.accepted)
        with self.assertRaisesRegex(ValueError, "INVALID_STRICT_PROVENANCE"):
            self._api("StrictPriceHistoryEvidence")(
                provenance="CLOB_CURRENT_BOOK",
                source_sha256="a" * 64,
                checkpoint_timestamp_ms=2_000,
                observation_timestamp_ms=1_999,
            )

    def test_pf1_fails_closed_on_snapshot_evidence_before_numeric_selection(self):
        rows = self._replace(self._buckets(), 2, model_p=0.40, vwap5=0.35)
        missing = self._api("evaluate_pf1")("YES_PF1_T60", 60, rows)
        self.assertEqual("PF1_SNAPSHOT_EVIDENCE_REQUIRED", missing.reason)
        cases = (
            ({"snapshot_complete": False}, "PF1_SNAPSHOT_INCOMPLETE"),
            ({"synchronized": False}, "PF1_SNAPSHOT_UNSYNCHRONIZED"),
            ({"fresh": False}, "PF1_SNAPSHOT_STALE"),
            ({"crossed_book_count": 1}, "PF1_CROSSED_BOOK"),
            ({"fee_provenance": "UNKNOWN"}, "PF1_FEE_PROVENANCE_INVALID"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                result = self._api("evaluate_pf1")(
                    "YES_PF1_T60",
                    60,
                    rows,
                    snapshot_evidence=self._pf1_evidence(**changes),
                )
                self.assertEqual(reason, result.reason)

    def test_pf1_accepts_only_after_complete_provenance_gate(self):
        rows = self._replace(self._buckets(), 2, model_p=0.40, vwap5=0.35)
        result = self._api("evaluate_pf1")(
            "YES_PF1_T60", 60, rows, snapshot_evidence=self._pf1_evidence()
        )
        self.assertTrue(result.accepted)

    def test_identity_checkpoint_policy_rejects_unmapped_horizons(self):
        validate = self._api("validate_identity_checkpoint")
        self.assertEqual(34, len(self._api("V1_IDENTITY_POLICIES")))
        self.assertEqual(720, validate("NO_FADE_P1_U1_T12", 720))
        for invalid in (30, 60, 1080):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
                    validate("NO_FADE_P1_U1_T12", invalid)
        with self.assertRaisesRegex(ValueError, "UNKNOWN_V1_IDENTITY"):
            validate("NO_FADE_UNKNOWN", 60)
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            validate("NO_FADE_P1_U1_EARLY_LATEST_ONLY", 1080)
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            validate("NO_FADE_P1_U1_EARLY_EARLIEST_ONLY", 720)

    def test_evaluator_entry_points_enforce_identity_horizons(self):
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            self._api("evaluate_strict_a")(
                "YES_STRICT_A_T60",
                30,
                self._buckets(),
                price_history_evidence=self._strict_evidence(),
            )
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            self._api("evaluate_pf1")(
                "YES_PF1_T60",
                30,
                self._buckets(),
                snapshot_evidence=self._pf1_evidence(),
            )
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHECKPOINT_MISMATCH"):
            self._api("evaluate_no_fade")(
                "NO_FADE_P2_U2_T4H",
                60,
                self._buckets(),
                no_execution=self._no_execution(),
            )

    def test_identity_operational_and_early_policy(self):
        apply_policy = self._api("apply_identity_checkpoint_policy")
        result_type = self._api("V1Evaluation")
        rejected60 = result_type(False, "REJECT", "NO", 60, ())
        accepted30 = result_type(True, "SIGNAL_ACCEPTED", "NO", 30, (2,))
        selected = apply_policy(
            "NO_FADE_P1_U1_OPERATIONAL", (rejected60, accepted30)
        )
        self.assertEqual((accepted30,), selected)
        accepted60 = dataclasses.replace(accepted30, checkpoint_minutes=60)
        self.assertEqual(
            (accepted60,),
            apply_policy(
                "NO_FADE_P1_U1_OPERATIONAL", (accepted60, accepted30)
            ),
        )
        early18 = dataclasses.replace(accepted30, checkpoint_minutes=1080)
        early12 = dataclasses.replace(accepted30, checkpoint_minutes=720)
        self.assertEqual(
            (early18, early12),
            apply_policy(
                "NO_FADE_P1_U1_EARLY_COMBINED", (early18, early12)
            ),
        )
        self.assertEqual(
            (early12,),
            apply_policy(
                "NO_FADE_P1_U1_EARLY_LATEST_ONLY", (early12,)
            ),
        )
        self.assertEqual(
            (early18,),
            apply_policy(
                "NO_FADE_P1_U1_EARLY_EARLIEST_ONLY", (early18,)
            ),
        )

    def test_fallback_policy_requires_primary_before_fallback(self):
        apply_policy = self._api("apply_identity_checkpoint_policy")
        result_type = self._api("V1Evaluation")
        accepted30 = result_type(True, "SIGNAL_ACCEPTED", "NO", 30, (2,))
        with self.assertRaisesRegex(
            ValueError, "MISSING_PRIMARY_IDENTITY_CHECKPOINT"
        ):
            apply_policy("NO_FADE_P1_U1_OPERATIONAL", (accepted30,))
        accepted60 = dataclasses.replace(accepted30, checkpoint_minutes=60)
        self.assertEqual(
            (accepted60,),
            apply_policy("NO_FADE_P1_U1_OPERATIONAL", (accepted60,)),
        )

    def test_fallback_policy_requires_fallback_when_primary_rejects(self):
        apply_policy = self._api("apply_identity_checkpoint_policy")
        result_type = self._api("V1Evaluation")
        rejected60 = result_type(False, "REJECT", "NO", 60, ())
        with self.assertRaisesRegex(
            ValueError, "MISSING_FALLBACK_IDENTITY_CHECKPOINT"
        ):
            apply_policy("NO_FADE_P1_U1_OPERATIONAL", (rejected60,))

    def test_pair_and_independent_policies_require_complete_evidence(self):
        apply_policy = self._api("apply_identity_checkpoint_policy")
        result_type = self._api("V1Evaluation")
        accepted60 = result_type(True, "SIGNAL_ACCEPTED", "NO", 60, (2,))
        early18 = dataclasses.replace(accepted60, checkpoint_minutes=1080)
        cases = (
            ("NO_C1", (accepted60,)),
            ("NO_FADE_P1_U1_EARLY_COMBINED", (early18,)),
        )
        for identity, evaluations in cases:
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(
                    ValueError, "MISSING_REQUIRED_IDENTITY_CHECKPOINTS"
                ):
                    apply_policy(identity, evaluations)


if __name__ == "__main__":
    unittest.main()
