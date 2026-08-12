from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from pathlib import Path

from src.strategy_registry import load_strategy_rule_pack
from src.strategy_v1 import (
    BucketInput,
    Pf1SnapshotEvidence,
    StrictPriceHistoryEvidence,
    V1_IDENTITY_POLICIES,
)
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


def _buckets(*, favorite: int = 5) -> tuple[BucketInput, ...]:
    return tuple(
        BucketInput(
            bucket_index=index,
            model_p=0.70 if index == favorite else 0.05,
            market_q_yes=0.50 if index == favorite else 0.05,
            market_q_no=0.50 if index == favorite else 0.95,
            vwap5=0.50 if index == favorite else 0.05,
            confirmed_fee=0.0,
            no_token_id=f"synthetic-no-{index}",
        )
        for index in range(11)
    )


def _v1_request(strategy_id: str):
    from src.strategy_dispatch import (
        V1ExecutableCheckpointInput,
        V1HistoricalCheckpointInput,
        V1StrategyDispatchRequest,
    )

    policy = V1_IDENTITY_POLICIES[strategy_id]
    executable = strategy_id.startswith("YES_STRICT_A_") or strategy_id.startswith(
        "YES_PF1_"
    )
    checkpoints = []
    for checkpoint in policy.checkpoints:
        rows = _buckets(favorite=4 if strategy_id == "NO_C1" and checkpoint == 30 else 5)
        if executable:
            checkpoints.append(
                V1ExecutableCheckpointInput(
                    checkpoint_minutes=checkpoint,
                    buckets=rows,
                    prior_position=False,
                    strict_price_history_evidence=(
                        StrictPriceHistoryEvidence(
                            provenance="CLOB_PRICE_HISTORY",
                            source_sha256=_sha(f"strict-{strategy_id}-{checkpoint}"),
                            checkpoint_timestamp_ms=2,
                            observation_timestamp_ms=1,
                        )
                        if strategy_id.startswith("YES_STRICT_A_")
                        else None
                    ),
                    pf1_snapshot_evidence=(
                        Pf1SnapshotEvidence(
                            bucket_count=11,
                            snapshot_complete=True,
                            synchronized=True,
                            fresh=True,
                            crossed_book_count=0,
                            fee_provenance="GAMMA_FEE_SCHEDULE_FILL_WEIGHTED",
                            snapshot_sha256=_sha(
                                f"snapshot-{strategy_id}-{checkpoint}"
                            ),
                            fee_schedule_sha256=_sha(
                                f"fee-{strategy_id}-{checkpoint}"
                            ),
                        )
                        if strategy_id.startswith("YES_PF1_")
                        else None
                    ),
                )
            )
        else:
            checkpoints.append(
                V1HistoricalCheckpointInput(
                    checkpoint_minutes=checkpoint,
                    buckets=rows,
                )
            )
    return V1StrategyDispatchRequest(checkpoints=tuple(checkpoints))


def _historical_v1_request(strategy_id: str):
    from src.strategy_dispatch import (
        V1HistoricalCheckpointInput,
        V1StrategyDispatchRequest,
    )

    return V1StrategyDispatchRequest(
        checkpoints=tuple(
            V1HistoricalCheckpointInput(
                checkpoint_minutes=checkpoint,
                buckets=_buckets(),
            )
            for checkpoint in V1_IDENTITY_POLICIES[strategy_id].checkpoints
        )
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

    def test_v1_in_memory_binding_drift_fails_closed(self):
        from src.strategy_dispatch import StrategyDispatcher, load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        bindings = list(dispatcher.bindings)
        index = next(
            index
            for index, binding in enumerate(bindings)
            if binding.strategy_id == "NO_A2"
        )
        bindings[index] = dataclasses.replace(
            bindings[index],
            evaluator_key="NO_FADE_P1_V1",
        )
        with self.assertRaisesRegex(ValueError, "C4_DISPATCH_BINDING_DRIFT"):
            StrategyDispatcher(tuple(bindings))

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
    def test_historical_strict_a_uses_historical_evaluator(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        result = dispatcher.dispatch_historical(
            strategy_id="YES_STRICT_A_T60",
            request=_historical_v1_request("YES_STRICT_A_T60"),
        )

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].historical_only)
        self.assertNotEqual(result[0].reason, "STRICT_PRICE_HISTORY_EVIDENCE_REQUIRED")

    def test_historical_pf1_uses_historical_evaluator(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        result = dispatcher.dispatch_historical(
            strategy_id="YES_PF1_T6H",
            request=_historical_v1_request("YES_PF1_T6H"),
        )

        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].historical_only)
        self.assertFalse(result[0].execution_eligible)

    def test_historical_no_identity_uses_existing_historical_evaluator(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        strategy_id = "NO_FADE_P1_U1_T18"
        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        result = dispatcher.dispatch_historical(
            strategy_id=strategy_id,
            request=_historical_v1_request(strategy_id),
        )

        self.assertTrue(result)
        self.assertTrue(all(item.historical_only for item in result))

    def test_historical_dispatch_fails_closed_for_unknown_identity(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "C4_UNKNOWN_STRATEGY_ID"):
            dispatcher.dispatch_historical(strategy_id="UNKNOWN", request=object())

    def test_historical_v2_uses_current_overlay_evaluator(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        result = dispatcher.dispatch_historical(
            strategy_id="NO_A2_V2_VOL",
            request=_request(),
        )

        self.assertEqual(result.overlay_strategy_id, "NO_A2_V2_VOL")
        self.assertEqual(result.regime, "VOL_CONFIRMATION")

    def test_normal_dispatch_still_rejects_historical_executable_input(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        with self.assertRaisesRegex(ValueError, "C4_V1_INPUT_SCHEMA_MISMATCH"):
            dispatcher.dispatch(
                strategy_id="YES_STRICT_A_T60",
                request=_historical_v1_request("YES_STRICT_A_T60"),
            )

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

    def test_all_thirty_four_v1_identities_dispatch_through_bound_evaluator(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        observed = {}
        for binding in dispatcher.bindings:
            if binding.version != "V1":
                continue
            result = dispatcher.dispatch(
                strategy_id=binding.strategy_id,
                request=_v1_request(binding.strategy_id),
            )
            observed[binding.strategy_id] = result

        self.assertEqual(set(observed), set(V1_IDENTITY_POLICIES))
        self.assertEqual(len(observed), 34)
        self.assertTrue(all(type(value) is tuple for value in observed.values()))
        self.assertTrue(
            all(value and value[0].checkpoint_minutes in V1_IDENTITY_POLICIES[key].checkpoints for key, value in observed.items())
        )

    def test_v1_dispatch_requires_binding_selected_input_schema(self):
        from src.strategy_dispatch import (
            V1ExecutableCheckpointInput,
            V1StrategyDispatchRequest,
            load_strategy_dispatcher,
        )

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        rows = _buckets()
        executable = V1StrategyDispatchRequest(
            checkpoints=(
                V1ExecutableCheckpointInput(
                    checkpoint_minutes=60,
                    buckets=rows,
                    prior_position=False,
                ),
                V1ExecutableCheckpointInput(
                    checkpoint_minutes=30,
                    buckets=rows,
                    prior_position=False,
                ),
            )
        )
        with self.assertRaisesRegex(ValueError, "C4_V1_INPUT_SCHEMA_MISMATCH"):
            dispatcher.dispatch(strategy_id="NO_A2", request=executable)

    def test_pf1_t6h_and_t8h_are_executable_bound_identities(self):
        from src.strategy_dispatch import load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        for strategy_id in ("YES_PF1_T6H", "YES_PF1_T8H"):
            result = dispatcher.dispatch(
                strategy_id=strategy_id,
                request=_v1_request(strategy_id),
            )
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].execution_eligible)

    def test_v1_request_rejects_missing_duplicate_or_extra_checkpoints(self):
        from src.strategy_dispatch import V1StrategyDispatchRequest, load_strategy_dispatcher

        dispatcher = load_strategy_dispatcher(PROJECT_ROOT)
        base = _v1_request("NO_A2")
        with self.assertRaisesRegex(ValueError, "C4_V1_CHECKPOINT_SET_MISMATCH"):
            dispatcher.dispatch(
                strategy_id="NO_A2",
                request=V1StrategyDispatchRequest(checkpoints=base.checkpoints[:1]),
            )
        with self.assertRaisesRegex(ValueError, "C4_V1_DUPLICATE_CHECKPOINT"):
            V1StrategyDispatchRequest(
                checkpoints=(base.checkpoints[0], base.checkpoints[0])
            )


if __name__ == "__main__":
    unittest.main()
