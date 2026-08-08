from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from src.strategy_registry import StrategyRulePack, load_strategy_rule_pack
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
    ) -> V2OverlayEvaluation:
        binding = self.binding(strategy_id)
        if binding.version == "V1":
            raise ValueError("C4_V1_DISPATCH_NOT_IMPLEMENTED")
        if type(request) is not V2StrategyDispatchRequest:
            raise ValueError("C4_INVALID_V2_DISPATCH_REQUEST_TYPE")
        return evaluate_volatility_overlay(
            overlay_strategy_id=binding.strategy_id,
            regime=binding.schedule["overlay_mode"],
            parent=request.parent,
            volatility_input=request.volatility_input,
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
