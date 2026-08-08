from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_MAX_PROBABILITY_MICROS = 1_000_000
_STRESS_PER_LEG_MICROS = 30_000
_CONFIRMATION_EDGE_MICROS = 20_000
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _require_nonempty_string(name: str, value: Any) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_sha256(name: str, value: Any) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_positive_integer(name: str, value: Any) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_probability_micros(name: str, value: Any) -> None:
    if (
        type(value) is not int
        or value < 0
        or value > _MAX_PROBABILITY_MICROS
    ):
        raise ValueError(f"INVALID_{name.upper()}")


@dataclass(frozen=True, slots=True)
class ParentOverlayDecision:
    decision_identity: str
    strategy_id: str
    side: str
    selected_bucket: str
    horizon: str
    fallback_policy: str
    shares: int
    baseline_accept: bool
    actual_price_micros: int
    leg_count: int
    source_decision_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "decision_identity",
            "strategy_id",
            "selected_bucket",
            "horizon",
            "fallback_policy",
        ):
            _require_nonempty_string(name, getattr(self, name))
        if type(self.side) is not str or self.side not in ("YES", "NO"):
            raise ValueError("INVALID_SIDE")
        _require_positive_integer("shares", self.shares)
        if type(self.baseline_accept) is not bool:
            raise ValueError("INVALID_BASELINE_ACCEPT")
        _require_probability_micros(
            "actual_price_micros",
            self.actual_price_micros,
        )
        _require_positive_integer("leg_count", self.leg_count)
        _require_sha256(
            "source_decision_sha256",
            self.source_decision_sha256,
        )


@dataclass(frozen=True, slots=True)
class VolatilityOverlayInput:
    p_vol_side_micros: int
    source_decision_sha256: str
    forecast_row_sha256: str

    def __post_init__(self) -> None:
        _require_probability_micros(
            "p_vol_side_micros",
            self.p_vol_side_micros,
        )
        _require_sha256(
            "source_decision_sha256",
            self.source_decision_sha256,
        )
        _require_sha256("forecast_row_sha256", self.forecast_row_sha256)


@dataclass(frozen=True, slots=True)
class V2OverlayEvaluation:
    overlay_strategy_id: str
    regime: str
    parent_decision_identity: str
    parent_strategy_id: str
    side: str
    selected_bucket: str
    horizon: str
    fallback_policy: str
    shares: int
    baseline_accept: bool
    actual_price_micros: int
    leg_count: int
    p_vol_side_micros: int
    stressed_q_3c_micros: int
    volatility_edge_micros: int
    threshold_micros: int
    accepted: bool
    reason_code: str
    source_decision_sha256: str
    forecast_row_sha256: str


def evaluate_volatility_overlay(
    *,
    overlay_strategy_id: str,
    regime: str,
    parent: ParentOverlayDecision,
    volatility_input: VolatilityOverlayInput,
) -> V2OverlayEvaluation:
    _require_nonempty_string("overlay_strategy_id", overlay_strategy_id)
    if type(regime) is not str or regime not in (
        "VOL_CONFIRMATION",
        "VOL_VETO_ONLY",
    ):
        raise ValueError("INVALID_OVERLAY_REGIME")
    if type(parent) is not ParentOverlayDecision:
        raise ValueError("INVALID_PARENT_OVERLAY_DECISION_TYPE")
    if type(volatility_input) is not VolatilityOverlayInput:
        raise ValueError("INVALID_VOLATILITY_OVERLAY_INPUT_TYPE")
    if (
        volatility_input.source_decision_sha256
        != parent.source_decision_sha256
    ):
        raise ValueError("VOLATILITY_SOURCE_DECISION_MISMATCH")

    stressed_q_3c_micros = min(
        _MAX_PROBABILITY_MICROS,
        parent.actual_price_micros
        + (_STRESS_PER_LEG_MICROS * parent.leg_count),
    )
    threshold_micros = (
        _CONFIRMATION_EDGE_MICROS if regime == "VOL_CONFIRMATION" else 0
    )
    volatility_edge_micros = (
        volatility_input.p_vol_side_micros - stressed_q_3c_micros
    )
    if not parent.baseline_accept:
        accepted = False
        reason_code = "PARENT_BASELINE_REJECTED"
    elif volatility_edge_micros >= threshold_micros:
        accepted = True
        reason_code = "VOLATILITY_OVERLAY_ACCEPTED"
    else:
        accepted = False
        reason_code = "VOLATILITY_EDGE_BELOW_THRESHOLD"

    return V2OverlayEvaluation(
        overlay_strategy_id=overlay_strategy_id,
        regime=regime,
        parent_decision_identity=parent.decision_identity,
        parent_strategy_id=parent.strategy_id,
        side=parent.side,
        selected_bucket=parent.selected_bucket,
        horizon=parent.horizon,
        fallback_policy=parent.fallback_policy,
        shares=parent.shares,
        baseline_accept=parent.baseline_accept,
        actual_price_micros=parent.actual_price_micros,
        leg_count=parent.leg_count,
        p_vol_side_micros=volatility_input.p_vol_side_micros,
        stressed_q_3c_micros=stressed_q_3c_micros,
        volatility_edge_micros=volatility_edge_micros,
        threshold_micros=threshold_micros,
        accepted=accepted,
        reason_code=reason_code,
        source_decision_sha256=parent.source_decision_sha256,
        forecast_row_sha256=volatility_input.forecast_row_sha256,
    )
