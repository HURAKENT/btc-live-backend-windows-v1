from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from pathlib import Path

from src.strategy_registry import load_strategy_rule_pack
from src.strategy_v2 import (
    ParentOverlayDecision,
    V2_OVERLAY_BINDINGS,
    VolatilityOverlayInput,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_ROOT = PROJECT_ROOT / "strategy_sources" / "frozen"
RULE_PACK_PATH = FROZEN_ROOT / "STRATEGY_RULE_MAP_47.json"
LOCK_PATH = PROJECT_ROOT / "contract" / "STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json"
REGISTRY_JSON_PATH = PROJECT_ROOT / "registry" / "STRATEGY_REGISTRY_47.json"
REGISTRY_CSV_PATH = PROJECT_ROOT / "registry" / "STRATEGY_REGISTRY_47.csv"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _pack():
    return load_strategy_rule_pack(
        RULE_PACK_PATH,
        lock_path=LOCK_PATH,
        registry_json_path=REGISTRY_JSON_PATH,
        registry_csv_path=REGISTRY_CSV_PATH,
    )


def _request(*, strategy_id: str = "NO_A2", side: str = "NO"):
    from src.strategy_dispatch import V2StrategyDispatchRequest

    source_sha = _sha("parent-decision")
    return V2StrategyDispatchRequest(
        parent=ParentOverlayDecision(
            decision_identity="decision:2026-08-08:NO_A2:T60",
            strategy_id=strategy_id,
            side=side,
            selected_buckets=("synthetic-bucket",),
            horizon="T-60m",
            fallback_policy="T60_ONLY",
            shares=5,
            baseline_accept=True,
            actual_price_micros=400_000,
            source_decision_sha256=source_sha,
        ),
        volatility_input=VolatilityOverlayInput(
            p_vol_side_micros=450_000,
            source_decision_sha256=source_sha,
            forecast_row_sha256=_sha("forecast-row"),
        ),
    )


class StrategyDispatchBindingTests(unittest.TestCase):
    def test_trusted_pack_builds_exact_ordered_forty_seven_bindings(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        pack = _pack()
        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        expected = tuple(
            (
                rule.strategy_id,
                rule.registry_index,
                rule.version,
                rule.evaluator_key,
                rule.input_schema_version,
                rule.parent_strategy_id,
                json.dumps(rule.schedule, allow_nan=False, sort_keys=True, separators=(",", ":")),
            )
            for rule in pack.rules
        )
        observed = tuple(
            (
                binding.strategy_id,
                binding.registry_index,
                binding.version,
                binding.evaluator_key,
                binding.input_schema_version,
                binding.parent_strategy_id,
                binding.schedule_json,
            )
            for binding in dispatcher.bindings
        )

        self.assertEqual(len(observed), 47)
        self.assertEqual(observed, expected)
        self.assertEqual(
            tuple(binding.strategy_id for binding in dispatcher.bindings),
            tuple(json.loads(LOCK_PATH.read_text(encoding="utf-8"))["strategy_ids"]),
        )
        self.assertEqual(
            sum(binding.version == "V1" for binding in dispatcher.bindings), 34
        )
        self.assertEqual(
            sum(binding.version == "V2" for binding in dispatcher.bindings), 13
        )

    def test_bindings_and_nested_schedule_are_immutable(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        binding = dispatcher.binding("NO_FADE_P1_T60_U1_EQUALS_U2")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            binding.version = "V2"
        with self.assertRaises(TypeError):
            binding.schedule["accepted"] = False
        self.assertIsInstance(binding.schedule["identity_fields"], tuple)
        with self.assertRaises(TypeError):
            dispatcher.bindings[0] = binding

    def test_every_frozen_schedule_is_accessible_including_finite_decimals(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        schedules = {
            binding.strategy_id: binding.schedule
            for binding in dispatcher.bindings
        }

        self.assertEqual(len(schedules), 47)
        self.assertEqual(schedules["NO_A0"]["execution_stress"], 0.03)

    def test_unknown_identity_fails_closed(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "C4_UNKNOWN_STRATEGY_ID"):
            dispatcher.binding("UNKNOWN")
        with self.assertRaisesRegex(ValueError, "C4_UNKNOWN_STRATEGY_ID"):
            dispatcher.dispatch(strategy_id="UNKNOWN", request=object())

    def test_v2_rule_pack_binding_drift_fails_closed(self):
        from src.strategy_dispatch import _build_strategy_dispatch_bindings

        pack = _pack()
        rules = list(pack.rules)
        index = next(
            index
            for index, rule in enumerate(rules)
            if rule.strategy_id == "NO_A2_V2_VOL"
        )
        rules[index] = dataclasses.replace(
            rules[index],
            parent_strategy_id="NO_A0",
            _schedule_json=(
                '{"overlay_mode":"VOL_VETO_ONLY","side":"NO",'
                '"threshold_micros":0}'
            ),
        )
        drifted = dataclasses.replace(pack, rules=tuple(rules))

        with self.assertRaisesRegex(ValueError, "C4_DISPATCH_BINDING_DRIFT"):
            _build_strategy_dispatch_bindings(drifted)

    def test_all_thirteen_v2_bindings_match_immutable_evaluator_contract(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        observed = {
            binding.strategy_id: (
                binding.parent_strategy_id,
                binding.schedule["side"],
                binding.schedule["overlay_mode"],
            )
            for binding in dispatcher.bindings
            if binding.version == "V2"
        }
        expected = {
            child: (binding.parent_strategy_id, binding.side, binding.regime)
            for child, binding in V2_OVERLAY_BINDINGS.items()
        }
        self.assertEqual(observed, expected)


class StrategyDispatcherEvaluationTests(unittest.TestCase):
    def test_v2_dispatch_derives_regime_from_trusted_binding(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        request = _request()
        self.assertNotIn("regime", {field.name for field in dataclasses.fields(request)})

        result = dispatcher.dispatch(
            strategy_id="NO_A2_V2_VOL",
            request=request,
        )

        self.assertEqual(result.overlay_strategy_id, "NO_A2_V2_VOL")
        self.assertEqual(result.regime, "VOL_CONFIRMATION")
        self.assertTrue(result.accepted)
        self.assertEqual(result.threshold_micros, 20_000)

    def test_dispatch_requires_exact_request_type(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        request = _request()

        class Lookalike:
            parent = request.parent
            volatility_input = request.volatility_input

        with self.assertRaisesRegex(ValueError, "C4_INVALID_V2_DISPATCH_REQUEST_TYPE"):
            dispatcher.dispatch(
                strategy_id="NO_A2_V2_VOL",
                request=Lookalike(),
            )

    def test_v2_request_components_require_exact_types(self):
        from src.strategy_dispatch import V2StrategyDispatchRequest

        request = _request()
        with self.assertRaisesRegex(
            ValueError, "C4_INVALID_PARENT_OVERLAY_DECISION_TYPE"
        ):
            V2StrategyDispatchRequest(
                parent=object(),
                volatility_input=request.volatility_input,
            )
        with self.assertRaisesRegex(
            ValueError, "C4_INVALID_VOLATILITY_OVERLAY_INPUT_TYPE"
        ):
            V2StrategyDispatchRequest(
                parent=request.parent,
                volatility_input=object(),
            )

    def test_caller_cannot_redirect_child_to_a_different_parent(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "V2_OVERLAY_PARENT_MISMATCH"):
            dispatcher.dispatch(
                strategy_id="NO_A2_V2_VOL",
                request=_request(strategy_id="NO_A0"),
            )

    def test_v1_dispatch_is_explicitly_not_implemented(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "C4_V1_DISPATCH_NOT_IMPLEMENTED"):
            dispatcher.dispatch(strategy_id="NO_A2", request=object())


if __name__ == "__main__":
    unittest.main()
