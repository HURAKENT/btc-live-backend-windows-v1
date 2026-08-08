from __future__ import annotations

import dataclasses
import hashlib
import importlib
import importlib.util
import unittest
from pathlib import Path


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _module():
    return importlib.import_module("src.strategy_v2")


class StrategyV2ModuleContractTests(unittest.TestCase):
    def test_strategy_v2_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("src.strategy_v2"))

    def test_overlay_module_does_not_embed_forecasting_dependencies(self):
        module = _module()
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("scipy", source.lower())
        self.assertNotIn("pandas", source.lower())
        self.assertNotIn("forecast_variance", source)

    def test_exact_thirteen_overlay_bindings_are_immutable(self):
        module = _module()
        observed = {
            child: (binding.parent_strategy_id, binding.side, binding.regime)
            for child, binding in module.V2_OVERLAY_BINDINGS.items()
        }
        self.assertEqual(
            observed,
            {
                "NO_A0_V2_VOL": ("NO_A0", "NO", "VOL_VETO_ONLY"),
                "NO_A2_V2_VOL": ("NO_A2", "NO", "VOL_CONFIRMATION"),
                "NO_FADE_P1_U1_T18_V2_VOL": (
                    "NO_FADE_P1_U1_T18", "NO", "VOL_VETO_ONLY"
                ),
                "NO_FADE_P2_U1_OPERATIONAL_V2_VOL": (
                    "NO_FADE_P2_U1_OPERATIONAL", "NO", "VOL_VETO_ONLY"
                ),
                "NO_FADE_P2_U1_T60_V2_VOL": (
                    "NO_FADE_P2_U1_T60", "NO", "VOL_VETO_ONLY"
                ),
                "NO_FADE_P2_U2_OPERATIONAL_V2_VOL": (
                    "NO_FADE_P2_U2_OPERATIONAL", "NO", "VOL_VETO_ONLY"
                ),
                "NO_FADE_P2_U2_T60_V2_VOL": (
                    "NO_FADE_P2_U2_T60", "NO", "VOL_VETO_ONLY"
                ),
                "YES_FAVORITE_ONLY_V2_VOL": (
                    "YES_FAVORITE_ONLY", "YES", "VOL_CONFIRMATION"
                ),
                "YES_PF1_OPERATIONAL_V2_VOL": (
                    "YES_PF1_OPERATIONAL", "YES", "VOL_CONFIRMATION"
                ),
                "YES_PF1_T30_V2_VOL": (
                    "YES_PF1_T30", "YES", "VOL_CONFIRMATION"
                ),
                "YES_PF1_T60_V2_VOL": (
                    "YES_PF1_T60", "YES", "VOL_CONFIRMATION"
                ),
                "YES_STRICT_A_OPERATIONAL_V2_VOL": (
                    "YES_STRICT_A_OPERATIONAL", "YES", "VOL_CONFIRMATION"
                ),
                "YES_STRICT_A_T60_V2_VOL": (
                    "YES_STRICT_A_T60", "YES", "VOL_VETO_ONLY"
                ),
            },
        )
        with self.assertRaises(TypeError):
            module.V2_OVERLAY_BINDINGS["EXTRA"] = None


class StrategyV2EvaluationTests(unittest.TestCase):
    def _parent(self, **overrides):
        module = _module()
        values = {
            "decision_identity": "decision:2026-08-08:NO_A2:T60",
            "strategy_id": "NO_A2",
            "side": "NO",
            "selected_buckets": ("$115,000-$120,000",),
            "horizon": "T-60m",
            "fallback_policy": "T60_ONLY",
            "shares": 5,
            "baseline_accept": True,
            "actual_price_micros": 400_000,
            "source_decision_sha256": _sha("parent-decision"),
        }
        values.update(overrides)
        return module.ParentOverlayDecision(**values)

    def _forecast(self, **overrides):
        module = _module()
        values = {
            "p_vol_side_micros": 450_000,
            "source_decision_sha256": _sha("parent-decision"),
            "forecast_row_sha256": _sha("forecast-row"),
        }
        values.update(overrides)
        return module.VolatilityOverlayInput(**values)

    def _evaluate(
        self,
        *,
        overlay_strategy_id="NO_A2_V2_VOL",
        regime="VOL_CONFIRMATION",
        parent=None,
        forecast=None,
    ):
        module = _module()
        return module.evaluate_volatility_overlay(
            overlay_strategy_id=overlay_strategy_id,
            regime=regime,
            parent=self._parent() if parent is None else parent,
            volatility_input=self._forecast() if forecast is None else forecast,
        )

    def test_confirmation_accepts_exact_twenty_thousand_micros_edge(self):
        result = self._evaluate(
            forecast=self._forecast(p_vol_side_micros=450_000)
        )
        self.assertEqual(result.stressed_q_3c_micros, 430_000)
        self.assertEqual(result.volatility_edge_micros, 20_000)
        self.assertEqual(result.threshold_micros, 20_000)
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason_code, "VOLATILITY_OVERLAY_ACCEPTED")

    def test_confirmation_rejects_one_micro_below_threshold(self):
        result = self._evaluate(
            forecast=self._forecast(p_vol_side_micros=449_999)
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.volatility_edge_micros, 19_999)
        self.assertEqual(
            result.reason_code,
            "VOLATILITY_EDGE_BELOW_THRESHOLD",
        )

    def test_veto_only_accepts_zero_edge_and_rejects_negative_edge(self):
        accepted = self._evaluate(
            overlay_strategy_id="NO_A0_V2_VOL",
            regime="VOL_VETO_ONLY",
            parent=self._parent(strategy_id="NO_A0"),
            forecast=self._forecast(p_vol_side_micros=430_000),
        )
        rejected = self._evaluate(
            overlay_strategy_id="NO_A0_V2_VOL",
            regime="VOL_VETO_ONLY",
            parent=self._parent(strategy_id="NO_A0"),
            forecast=self._forecast(p_vol_side_micros=429_999),
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.threshold_micros, 0)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.volatility_edge_micros, -1)

    def test_parent_baseline_rejection_is_never_overridden(self):
        result = self._evaluate(
            parent=self._parent(baseline_accept=False),
            forecast=self._forecast(p_vol_side_micros=1_000_000),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_code, "PARENT_BASELINE_REJECTED")

    def test_parent_decision_fields_are_preserved_exactly(self):
        parent = self._parent(
            decision_identity="immutable-parent-identity",
            strategy_id="YES_FAVORITE_ONLY",
            side="YES",
            selected_buckets=("favorite", "neighbor"),
            horizon="T-60-primary-T-30-fallback",
            fallback_policy="T60_THEN_T30",
            shares=5,
            actual_price_micros=510_000,
        )
        forecast = self._forecast(
            p_vol_side_micros=600_000,
            source_decision_sha256=parent.source_decision_sha256,
        )
        result = self._evaluate(
            overlay_strategy_id="YES_FAVORITE_ONLY_V2_VOL",
            parent=parent,
            forecast=forecast,
        )
        self.assertEqual(result.parent_decision_identity, parent.decision_identity)
        self.assertEqual(result.parent_strategy_id, parent.strategy_id)
        self.assertEqual(result.side, parent.side)
        self.assertEqual(result.selected_buckets, parent.selected_buckets)
        self.assertEqual(result.horizon, parent.horizon)
        self.assertEqual(result.fallback_policy, parent.fallback_policy)
        self.assertEqual(result.shares, parent.shares)
        self.assertEqual(result.source_decision_sha256, parent.source_decision_sha256)
        self.assertEqual(result.forecast_row_sha256, forecast.forecast_row_sha256)

    def test_stress_adds_thirty_thousand_per_leg_and_caps_at_one_million(self):
        two_leg = self._evaluate(
            parent=self._parent(
                actual_price_micros=510_000,
                selected_buckets=("favorite", "neighbor"),
            ),
            forecast=self._forecast(p_vol_side_micros=600_000),
        )
        capped = self._evaluate(
            parent=self._parent(
                actual_price_micros=980_000,
                selected_buckets=("favorite", "neighbor"),
            ),
            forecast=self._forecast(p_vol_side_micros=1_000_000),
        )
        self.assertEqual(two_leg.stressed_q_3c_micros, 570_000)
        self.assertEqual(capped.stressed_q_3c_micros, 1_000_000)

    def test_forecast_must_be_bound_to_exact_parent_decision(self):
        with self.assertRaisesRegex(
            ValueError,
            "VOLATILITY_SOURCE_DECISION_MISMATCH",
        ):
            self._evaluate(
                forecast=self._forecast(
                    source_decision_sha256=_sha("different-parent")
                )
            )

    def test_provenance_hashes_are_required_lowercase_sha256(self):
        for field in ("source_decision_sha256", "forecast_row_sha256"):
            with self.subTest(field=field):
                overrides = {field: "A" * 64}
                constructor = self._parent if field == "source_decision_sha256" else self._forecast
                with self.assertRaisesRegex(
                    ValueError,
                    f"INVALID_{field.upper()}",
                ):
                    constructor(**overrides)

    def test_exact_integer_types_and_probability_ranges_are_fail_closed(self):
        for field, value in (
            ("actual_price_micros", True),
            ("actual_price_micros", -1),
            ("actual_price_micros", 1_000_001),
            ("shares", True),
            ("shares", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self._parent(**{field: value})
        for value in (True, -1, 1_000_001):
            with self.subTest(p_vol_side_micros=value):
                with self.assertRaises(ValueError):
                    self._forecast(p_vol_side_micros=value)

    def test_input_objects_and_result_are_immutable(self):
        parent = self._parent()
        forecast = self._forecast()
        result = self._evaluate(parent=parent, forecast=forecast)
        self.assertTrue(dataclasses.is_dataclass(parent))
        self.assertTrue(dataclasses.is_dataclass(forecast))
        self.assertTrue(dataclasses.is_dataclass(result))
        for obj, field in (
            (parent, "shares"),
            (forecast, "p_vol_side_micros"),
            (result, "accepted"),
        ):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, field, None)

    def test_invalid_regime_and_nonexact_input_objects_are_rejected(self):
        module = _module()
        with self.assertRaisesRegex(ValueError, "V2_OVERLAY_REGIME_MISMATCH"):
            self._evaluate(regime="BASELINE")
        with self.assertRaisesRegex(ValueError, "INVALID_PARENT_OVERLAY_DECISION_TYPE"):
            module.evaluate_volatility_overlay(
                overlay_strategy_id="NO_A2_V2_VOL",
                regime="VOL_CONFIRMATION",
                parent={},
                volatility_input=self._forecast(),
            )
        with self.assertRaisesRegex(ValueError, "INVALID_VOLATILITY_OVERLAY_INPUT_TYPE"):
            module.evaluate_volatility_overlay(
                overlay_strategy_id="NO_A2_V2_VOL",
                regime="VOL_CONFIRMATION",
                parent=self._parent(),
                volatility_input={},
            )

    def test_evaluation_is_deterministic(self):
        first = self._evaluate()
        second = self._evaluate()
        self.assertEqual(first, second)

    def test_unregistered_overlay_child_is_rejected(self):
        module = _module()
        with self.assertRaisesRegex(
            ValueError,
            "UNREGISTERED_V2_OVERLAY_STRATEGY",
        ):
            module.evaluate_volatility_overlay(
                overlay_strategy_id="UNREGISTERED_V2_VOL",
                regime="VOL_CONFIRMATION",
                parent=self._parent(),
                volatility_input=self._forecast(),
            )

    def test_registered_child_rejects_parent_side_and_mode_mismatch(self):
        module = _module()
        cases = (
            (
                "V2_OVERLAY_PARENT_MISMATCH",
                self._parent(strategy_id="NO_A0"),
                "VOL_CONFIRMATION",
            ),
            (
                "V2_OVERLAY_SIDE_MISMATCH",
                self._parent(side="YES"),
                "VOL_CONFIRMATION",
            ),
            (
                "V2_OVERLAY_REGIME_MISMATCH",
                self._parent(),
                "VOL_VETO_ONLY",
            ),
        )
        for reason, parent, regime in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    module.evaluate_volatility_overlay(
                        overlay_strategy_id="NO_A2_V2_VOL",
                        regime=regime,
                        parent=parent,
                        volatility_input=self._forecast(),
                    )

    def test_parent_requires_exactly_five_shares(self):
        with self.assertRaisesRegex(ValueError, "INVALID_SHARES"):
            self._parent(shares=6)

    def test_structured_selected_buckets_derive_leg_count(self):
        module = _module()
        parent = module.ParentOverlayDecision(
            decision_identity="decision:basket",
            strategy_id="YES_FAVORITE_ONLY",
            side="YES",
            selected_buckets=("favorite", "neighbor"),
            horizon="T-60m",
            fallback_policy="T60_THEN_T30",
            shares=5,
            baseline_accept=True,
            actual_price_micros=510_000,
            source_decision_sha256=_sha("parent-decision"),
        )
        result = module.evaluate_volatility_overlay(
            overlay_strategy_id="YES_FAVORITE_ONLY_V2_VOL",
            regime="VOL_CONFIRMATION",
            parent=parent,
            volatility_input=self._forecast(p_vol_side_micros=600_000),
        )
        self.assertEqual(result.selected_buckets, ("favorite", "neighbor"))
        self.assertEqual(result.leg_count, 2)
        self.assertEqual(result.stressed_q_3c_micros, 570_000)


if __name__ == "__main__":
    unittest.main()
