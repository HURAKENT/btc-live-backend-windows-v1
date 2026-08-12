from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from src.strategy_registry import StrategyRulePack, load_strategy_rule_pack
from src.strategy_v1 import (
    BucketInput,
    Pf1SnapshotEvidence,
    StrictPriceHistoryEvidence,
    V1Evaluation,
    V1_IDENTITY_POLICIES,
    apply_identity_checkpoint_policy,
    evaluate_confirmation,
    evaluate_favorite_neighbor,
    evaluate_favorite_only,
    evaluate_no_fade_historical,
    evaluate_pf1,
    evaluate_pf1_historical,
    evaluate_strict_a,
    evaluate_strict_historical,
)
from src.strategy_v2 import (
    ParentOverlayDecision,
    V2_OVERLAY_BINDINGS,
    V2OverlayEvaluation,
    VolatilityOverlayInput,
    evaluate_volatility_overlay,
)


_V2_EVALUATOR_KEY = "VOL_OVERLAY_A_V1"
_V2_INPUT_SCHEMA = "BTC_STRATEGY_VOL_OVERLAY_INPUT_V1"
_V2_THRESHOLD_BY_MODE = MappingProxyType(
    {"VOL_CONFIRMATION": 20_000, "VOL_VETO_ONLY": 0}
)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("C4_INVALID_DISPATCH_SCHEDULE") from exc


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("C4_INVALID_DISPATCH_SCHEDULE")


@dataclass(frozen=True, slots=True)
class StrategyDispatchBinding:
    strategy_id: str
    registry_index: int
    version: str
    evaluator_key: str
    input_schema_version: str
    parent_strategy_id: str | None
    _schedule_json: str = field(repr=False)

    @property
    def schedule(self) -> Mapping[str, Any]:
        value = _freeze_json(json.loads(self._schedule_json))
        if type(value) is not MappingProxyType:
            raise AssertionError("validated schedule is not an object")
        return value

    @property
    def schedule_json(self) -> str:
        return self._schedule_json


@dataclass(frozen=True, slots=True)
class V2StrategyDispatchRequest:
    parent: ParentOverlayDecision
    volatility_input: VolatilityOverlayInput

    def __post_init__(self) -> None:
        if type(self.parent) is not ParentOverlayDecision:
            raise ValueError("C4_INVALID_PARENT_OVERLAY_DECISION_TYPE")
        if type(self.volatility_input) is not VolatilityOverlayInput:
            raise ValueError("C4_INVALID_VOLATILITY_OVERLAY_INPUT_TYPE")


@dataclass(frozen=True, slots=True)
class V1HistoricalCheckpointInput:
    checkpoint_minutes: int
    buckets: tuple[BucketInput, ...]

    def __post_init__(self) -> None:
        if type(self.checkpoint_minutes) is not int:
            raise ValueError("C4_INVALID_V1_CHECKPOINT")
        if type(self.buckets) is not tuple or any(
            type(row) is not BucketInput for row in self.buckets
        ):
            raise ValueError("C4_INVALID_V1_BUCKET_INPUT")


@dataclass(frozen=True, slots=True)
class V1ExecutableCheckpointInput:
    checkpoint_minutes: int
    buckets: tuple[BucketInput, ...]
    prior_position: bool
    strict_price_history_evidence: StrictPriceHistoryEvidence | None = None
    pf1_snapshot_evidence: Pf1SnapshotEvidence | None = None

    def __post_init__(self) -> None:
        if type(self.checkpoint_minutes) is not int:
            raise ValueError("C4_INVALID_V1_CHECKPOINT")
        if type(self.buckets) is not tuple or any(
            type(row) is not BucketInput for row in self.buckets
        ):
            raise ValueError("C4_INVALID_V1_BUCKET_INPUT")
        if type(self.prior_position) is not bool:
            raise ValueError("C4_INVALID_V1_PRIOR_POSITION")
        if self.strict_price_history_evidence is not None and type(
            self.strict_price_history_evidence
        ) is not StrictPriceHistoryEvidence:
            raise ValueError("C4_INVALID_STRICT_PRICE_HISTORY_EVIDENCE")
        if self.pf1_snapshot_evidence is not None and type(
            self.pf1_snapshot_evidence
        ) is not Pf1SnapshotEvidence:
            raise ValueError("C4_INVALID_PF1_SNAPSHOT_EVIDENCE")


V1CheckpointInput = V1HistoricalCheckpointInput | V1ExecutableCheckpointInput


@dataclass(frozen=True, slots=True)
class V1StrategyDispatchRequest:
    checkpoints: tuple[V1CheckpointInput, ...]

    def __post_init__(self) -> None:
        if type(self.checkpoints) is not tuple or not self.checkpoints:
            raise ValueError("C4_INVALID_V1_CHECKPOINT_INPUTS")
        if any(
            type(item)
            not in (V1HistoricalCheckpointInput, V1ExecutableCheckpointInput)
            for item in self.checkpoints
        ):
            raise ValueError("C4_INVALID_V1_CHECKPOINT_INPUT_TYPE")
        minutes = tuple(item.checkpoint_minutes for item in self.checkpoints)
        if len(set(minutes)) != len(minutes):
            raise ValueError("C4_V1_DUPLICATE_CHECKPOINT")


@dataclass(frozen=True, slots=True)
class StrategyDispatcher:
    bindings: tuple[StrategyDispatchBinding, ...]
    _bindings_by_id: Mapping[str, StrategyDispatchBinding] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.bindings) is not tuple or len(self.bindings) != 47:
            raise ValueError("C4_DISPATCH_BINDING_DRIFT: expected 47 bindings")
        observed_indices = tuple(
            binding.registry_index for binding in self.bindings
        )
        if observed_indices != tuple(range(1, 48)):
            raise ValueError("C4_DISPATCH_BINDING_DRIFT: registry order")
        observed_ids = tuple(binding.strategy_id for binding in self.bindings)
        if len(set(observed_ids)) != 47:
            raise ValueError("C4_DISPATCH_BINDING_DRIFT: duplicate identity")
        if sum(binding.version == "V1" for binding in self.bindings) != 34:
            raise ValueError("C4_DISPATCH_BINDING_DRIFT: V1 count")
        if sum(binding.version == "V2" for binding in self.bindings) != 13:
            raise ValueError("C4_DISPATCH_BINDING_DRIFT: V2 count")
        _validate_v1_bindings(self.bindings)
        _validate_v2_bindings(self.bindings)
        object.__setattr__(
            self,
            "_bindings_by_id",
            MappingProxyType(
                {binding.strategy_id: binding for binding in self.bindings}
            ),
        )

    def binding(self, strategy_id: str) -> StrategyDispatchBinding:
        if type(strategy_id) is not str or strategy_id not in self._bindings_by_id:
            raise ValueError("C4_UNKNOWN_STRATEGY_ID")
        return self._bindings_by_id[strategy_id]

    def dispatch(
        self,
        *,
        strategy_id: str,
        request: object,
    ) -> tuple[V1Evaluation, ...] | V2OverlayEvaluation:
        binding = self.binding(strategy_id)
        if binding.version == "V1":
            if type(request) is not V1StrategyDispatchRequest:
                raise ValueError("C4_INVALID_V1_DISPATCH_REQUEST_TYPE")
            return _dispatch_v1(binding=binding, request=request)
        if type(request) is not V2StrategyDispatchRequest:
            raise ValueError("C4_INVALID_V2_DISPATCH_REQUEST_TYPE")
        return evaluate_volatility_overlay(
            overlay_strategy_id=binding.strategy_id,
            regime=binding.schedule["overlay_mode"],
            parent=request.parent,
            volatility_input=request.volatility_input,
        )

    def dispatch_historical(
        self,
        *,
        strategy_id: str,
        request: object,
    ) -> tuple[V1Evaluation, ...] | V2OverlayEvaluation:
        binding = self.binding(strategy_id)
        if binding.version == "V1":
            if type(request) is not V1StrategyDispatchRequest:
                raise ValueError("AHR_INVALID_V1_HISTORICAL_REQUEST_TYPE")
            return _dispatch_v1_historical(binding=binding, request=request)
        if type(request) is not V2StrategyDispatchRequest:
            raise ValueError("AHR_INVALID_V2_HISTORICAL_REQUEST_TYPE")
        return evaluate_volatility_overlay(
            overlay_strategy_id=binding.strategy_id,
            regime=binding.schedule["overlay_mode"],
            parent=request.parent,
            volatility_input=request.volatility_input,
        )


def _dispatch_v1_historical(
    *,
    binding: StrategyDispatchBinding,
    request: V1StrategyDispatchRequest,
) -> tuple[V1Evaluation, ...]:
    policy = V1_IDENTITY_POLICIES.get(binding.strategy_id)
    if policy is None:
        raise ValueError("C4_DISPATCH_BINDING_DRIFT: V1 identity set")
    observed_checkpoints = tuple(
        item.checkpoint_minutes for item in request.checkpoints
    )
    if set(observed_checkpoints) != set(policy.checkpoints) or len(
        observed_checkpoints
    ) != len(policy.checkpoints):
        raise ValueError("C4_V1_CHECKPOINT_SET_MISMATCH")
    if any(
        type(item) is not V1HistoricalCheckpointInput
        for item in request.checkpoints
    ):
        raise ValueError("AHR_V1_HISTORICAL_INPUT_SCHEMA_MISMATCH")
    by_checkpoint = {
        item.checkpoint_minutes: item for item in request.checkpoints
    }

    if binding.evaluator_key == "NO_CONFIRMATION_V1":
        t60 = by_checkpoint[60]
        t30 = by_checkpoint.get(30)
        result = evaluate_confirmation(
            binding.strategy_id,
            t60.buckets,
            t30=None if t30 is None else t30.buckets,
        )
        return (result,)

    evaluations: list[V1Evaluation] = []
    for checkpoint in policy.checkpoints:
        item = by_checkpoint[checkpoint]
        if binding.evaluator_key == "FAVORITE_ONLY_V1":
            result = evaluate_favorite_only(checkpoint, item.buckets)
        elif binding.evaluator_key == "FAVORITE_NEIGHBOR_BASKET_V1":
            result = evaluate_favorite_neighbor(checkpoint, item.buckets)
        elif binding.evaluator_key in {"NO_FADE_P1_V1", "NO_FADE_P2_V1"}:
            result = evaluate_no_fade_historical(
                binding.strategy_id, checkpoint, item.buckets
            )
        elif binding.evaluator_key == "STRICT_A_COMPOSED_V1":
            result = evaluate_strict_historical(
                binding.strategy_id, checkpoint, item.buckets
            )
        elif binding.evaluator_key == "PF1_COMPOSED_V1":
            result = evaluate_pf1_historical(
                binding.strategy_id, checkpoint, item.buckets
            )
        else:
            raise ValueError("C4_UNKNOWN_V1_EVALUATOR_KEY")
        evaluations.append(result)
    return apply_identity_checkpoint_policy(
        binding.strategy_id, tuple(evaluations)
    )


def _dispatch_v1(
    *,
    binding: StrategyDispatchBinding,
    request: V1StrategyDispatchRequest,
) -> tuple[V1Evaluation, ...]:
    policy = V1_IDENTITY_POLICIES.get(binding.strategy_id)
    if policy is None:
        raise ValueError("C4_DISPATCH_BINDING_DRIFT: V1 identity set")
    observed_checkpoints = tuple(
        item.checkpoint_minutes for item in request.checkpoints
    )
    if set(observed_checkpoints) != set(policy.checkpoints) or len(
        observed_checkpoints
    ) != len(policy.checkpoints):
        raise ValueError("C4_V1_CHECKPOINT_SET_MISMATCH")
    by_checkpoint = {
        item.checkpoint_minutes: item for item in request.checkpoints
    }
    expected_type = (
        V1ExecutableCheckpointInput
        if binding.input_schema_version
        == "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1"
        else V1HistoricalCheckpointInput
    )
    if any(type(item) is not expected_type for item in request.checkpoints):
        raise ValueError("C4_V1_INPUT_SCHEMA_MISMATCH")

    if binding.evaluator_key == "NO_CONFIRMATION_V1":
        t60 = by_checkpoint[60]
        t30 = by_checkpoint.get(30)
        result = evaluate_confirmation(
            binding.strategy_id,
            t60.buckets,
            t30=None if t30 is None else t30.buckets,
        )
        return (result,)

    evaluations: list[V1Evaluation] = []
    for checkpoint in policy.checkpoints:
        item = by_checkpoint[checkpoint]
        if binding.evaluator_key == "FAVORITE_ONLY_V1":
            result = evaluate_favorite_only(checkpoint, item.buckets)
        elif binding.evaluator_key == "FAVORITE_NEIGHBOR_BASKET_V1":
            result = evaluate_favorite_neighbor(checkpoint, item.buckets)
        elif binding.evaluator_key in {"NO_FADE_P1_V1", "NO_FADE_P2_V1"}:
            result = evaluate_no_fade_historical(
                binding.strategy_id, checkpoint, item.buckets
            )
        elif binding.evaluator_key == "STRICT_A_COMPOSED_V1":
            if type(item) is not V1ExecutableCheckpointInput:
                raise ValueError("C4_V1_INPUT_SCHEMA_MISMATCH")
            if item.pf1_snapshot_evidence is not None:
                raise ValueError("C4_V1_IRRELEVANT_EVIDENCE")
            result = evaluate_strict_a(
                binding.strategy_id,
                checkpoint,
                item.buckets,
                prior_position=item.prior_position,
                price_history_evidence=item.strict_price_history_evidence,
            )
        elif binding.evaluator_key == "PF1_COMPOSED_V1":
            if type(item) is not V1ExecutableCheckpointInput:
                raise ValueError("C4_V1_INPUT_SCHEMA_MISMATCH")
            if item.strict_price_history_evidence is not None:
                raise ValueError("C4_V1_IRRELEVANT_EVIDENCE")
            result = evaluate_pf1(
                binding.strategy_id,
                checkpoint,
                item.buckets,
                prior_position=item.prior_position,
                snapshot_evidence=item.pf1_snapshot_evidence,
            )
        else:
            raise ValueError("C4_UNKNOWN_V1_EVALUATOR_KEY")
        evaluations.append(result)
    return apply_identity_checkpoint_policy(
        binding.strategy_id, tuple(evaluations)
    )


def _validate_v2_bindings(
    bindings: tuple[StrategyDispatchBinding, ...],
) -> None:
    v2_bindings = {
        binding.strategy_id: binding
        for binding in bindings
        if binding.version == "V2"
    }
    if frozenset(v2_bindings) != frozenset(V2_OVERLAY_BINDINGS):
        raise ValueError("C4_DISPATCH_BINDING_DRIFT: V2 identity set")
    for strategy_id, evaluator_binding in V2_OVERLAY_BINDINGS.items():
        rule_binding = v2_bindings[strategy_id]
        schedule = rule_binding.schedule
        expected_schedule = {
            "overlay_mode": evaluator_binding.regime,
            "side": evaluator_binding.side,
            "threshold_micros": _V2_THRESHOLD_BY_MODE[evaluator_binding.regime],
        }
        if (
            rule_binding.parent_strategy_id
            != evaluator_binding.parent_strategy_id
            or rule_binding.evaluator_key != _V2_EVALUATOR_KEY
            or rule_binding.input_schema_version != _V2_INPUT_SCHEMA
            or dict(schedule) != expected_schedule
        ):
            raise ValueError(
                f"C4_DISPATCH_BINDING_DRIFT: {strategy_id}"
            )


def _validate_v1_bindings(
    bindings: tuple[StrategyDispatchBinding, ...],
) -> None:
    v1_bindings = {
        binding.strategy_id: binding
        for binding in bindings
        if binding.version == "V1"
    }
    if frozenset(v1_bindings) != frozenset(V1_IDENTITY_POLICIES):
        raise ValueError("C4_DISPATCH_BINDING_DRIFT: V1 identity set")
    confirmation = {"NO_A0", "NO_A2", "NO_B2", "NO_C1"}
    strict = {
        "YES_STRICT_A_T60",
        "YES_STRICT_A_OPERATIONAL",
        "YES_STRICT_A_T30",
    }
    pf1 = {
        "YES_PF1_OPERATIONAL",
        "YES_PF1_T60",
        "YES_PF1_T30",
        "YES_PF1_T6H",
        "YES_PF1_T8H",
    }
    for strategy_id, binding in v1_bindings.items():
        if strategy_id in confirmation:
            expected_key = "NO_CONFIRMATION_V1"
        elif strategy_id == "YES_FAVORITE_ONLY":
            expected_key = "FAVORITE_ONLY_V1"
        elif strategy_id == "YES_FAVORITE_NEIGHBOR_BASKET":
            expected_key = "FAVORITE_NEIGHBOR_BASKET_V1"
        elif strategy_id in strict:
            expected_key = "STRICT_A_COMPOSED_V1"
        elif strategy_id in pf1:
            expected_key = "PF1_COMPOSED_V1"
        elif strategy_id.startswith("NO_FADE_P1_"):
            expected_key = "NO_FADE_P1_V1"
        elif strategy_id.startswith("NO_FADE_P2_"):
            expected_key = "NO_FADE_P2_V1"
        else:
            raise ValueError("C4_DISPATCH_BINDING_DRIFT: V1 family")
        expected_schema = (
            "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1"
            if strategy_id in strict | pf1
            else "BTC_STRATEGY_HISTORICAL_INPUT_V1"
        )
        if (
            binding.evaluator_key != expected_key
            or binding.input_schema_version != expected_schema
            or binding.parent_strategy_id is not None
        ):
            raise ValueError(
                f"C4_DISPATCH_BINDING_DRIFT: {strategy_id}"
            )


def _build_strategy_dispatch_bindings(
    rule_pack: StrategyRulePack,
) -> tuple[StrategyDispatchBinding, ...]:
    if type(rule_pack) is not StrategyRulePack:
        raise ValueError("C4_INVALID_RULE_PACK_TYPE")
    bindings = tuple(
        StrategyDispatchBinding(
            strategy_id=rule.strategy_id,
            registry_index=rule.registry_index,
            version=rule.version,
            evaluator_key=rule.evaluator_key,
            input_schema_version=rule.input_schema_version,
            parent_strategy_id=rule.parent_strategy_id,
            _schedule_json=_canonical_json(rule.schedule),
        )
        for rule in rule_pack.rules
    )
    StrategyDispatcher(bindings)
    return bindings


def load_strategy_dispatcher(project_root: Path) -> StrategyDispatcher:
    if not isinstance(project_root, Path):
        raise ValueError("C4_INVALID_PROJECT_ROOT_TYPE")
    rule_pack = load_strategy_rule_pack(
        project_root / "strategy_sources" / "frozen" / "STRATEGY_RULE_MAP_47.json",
        lock_path=(
            project_root
            / "contract"
            / "STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json"
        ),
        registry_json_path=(
            project_root / "registry" / "STRATEGY_REGISTRY_47.json"
        ),
        registry_csv_path=(
            project_root / "registry" / "STRATEGY_REGISTRY_47.csv"
        ),
    )
    return StrategyDispatcher(_build_strategy_dispatch_bindings(rule_pack))
