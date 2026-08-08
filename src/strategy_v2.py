from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Mapping


_MAX_PROBABILITY_MICROS = 1_000_000
_STRESS_PER_LEG_MICROS = 30_000
_CONFIRMATION_EDGE_MICROS = 20_000
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class V2OverlayBinding:
    parent_strategy_id: str
    side: str
    regime: str


V2_OVERLAY_BINDINGS: Final[Mapping[str, V2OverlayBinding]] = MappingProxyType(
    {
        "NO_A0_V2_VOL": V2OverlayBinding("NO_A0", "NO", "VOL_VETO_ONLY"),
        "NO_A2_V2_VOL": V2OverlayBinding(
            "NO_A2", "NO", "VOL_CONFIRMATION"
        ),
        "NO_FADE_P1_U1_T18_V2_VOL": V2OverlayBinding(
            "NO_FADE_P1_U1_T18", "NO", "VOL_VETO_ONLY"
        ),
        "NO_FADE_P2_U1_OPERATIONAL_V2_VOL": V2OverlayBinding(
            "NO_FADE_P2_U1_OPERATIONAL", "NO", "VOL_VETO_ONLY"
        ),
        "NO_FADE_P2_U1_T60_V2_VOL": V2OverlayBinding(
            "NO_FADE_P2_U1_T60", "NO", "VOL_VETO_ONLY"
        ),
        "NO_FADE_P2_U2_OPERATIONAL_V2_VOL": V2OverlayBinding(
            "NO_FADE_P2_U2_OPERATIONAL", "NO", "VOL_VETO_ONLY"
        ),
        "NO_FADE_P2_U2_T60_V2_VOL": V2OverlayBinding(
            "NO_FADE_P2_U2_T60", "NO", "VOL_VETO_ONLY"
        ),
        "YES_FAVORITE_ONLY_V2_VOL": V2OverlayBinding(
            "YES_FAVORITE_ONLY", "YES", "VOL_CONFIRMATION"
        ),
        "YES_PF1_OPERATIONAL_V2_VOL": V2OverlayBinding(
            "YES_PF1_OPERATIONAL", "YES", "VOL_CONFIRMATION"
        ),
        "YES_PF1_T30_V2_VOL": V2OverlayBinding(
            "YES_PF1_T30", "YES", "VOL_CONFIRMATION"
        ),
        "YES_PF1_T60_V2_VOL": V2OverlayBinding(
            "YES_PF1_T60", "YES", "VOL_CONFIRMATION"
        ),
        "YES_STRICT_A_OPERATIONAL_V2_VOL": V2OverlayBinding(
            "YES_STRICT_A_OPERATIONAL", "YES", "VOL_CONFIRMATION"
        ),
        "YES_STRICT_A_T60_V2_VOL": V2OverlayBinding(
            "YES_STRICT_A_T60", "YES", "VOL_VETO_ONLY"
        ),
    }
)


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
    selected_buckets: tuple[str, ...]
    horizon: str
    fallback_policy: str
    shares: int
    baseline_accept: bool
    actual_price_micros: int
    source_decision_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "decision_identity",
            "strategy_id",
            "horizon",
            "fallback_policy",
        ):
            _require_nonempty_string(name, getattr(self, name))
        if (
            type(self.selected_buckets) is not tuple
            or not self.selected_buckets
            or any(
                type(bucket) is not str or not bucket
                for bucket in self.selected_buckets
            )
            or len(set(self.selected_buckets)) != len(self.selected_buckets)
        ):
            raise ValueError("INVALID_SELECTED_BUCKETS")
        if type(self.side) is not str or self.side not in ("YES", "NO"):
            raise ValueError("INVALID_SIDE")
        if type(self.shares) is not int or self.shares != 5:
            raise ValueError("INVALID_SHARES")
        if type(self.baseline_accept) is not bool:
            raise ValueError("INVALID_BASELINE_ACCEPT")
        _require_probability_micros(
            "actual_price_micros",
            self.actual_price_micros,
        )
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
    selected_buckets: tuple[str, ...]
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
    if type(parent) is not ParentOverlayDecision:
        raise ValueError("INVALID_PARENT_OVERLAY_DECISION_TYPE")
    if type(volatility_input) is not VolatilityOverlayInput:
        raise ValueError("INVALID_VOLATILITY_OVERLAY_INPUT_TYPE")
    binding = V2_OVERLAY_BINDINGS.get(overlay_strategy_id)
    if binding is None:
        raise ValueError("UNREGISTERED_V2_OVERLAY_STRATEGY")
    if parent.strategy_id != binding.parent_strategy_id:
        raise ValueError("V2_OVERLAY_PARENT_MISMATCH")
    if parent.side != binding.side:
        raise ValueError("V2_OVERLAY_SIDE_MISMATCH")
    if type(regime) is not str or regime != binding.regime:
        raise ValueError("V2_OVERLAY_REGIME_MISMATCH")
    if (
        volatility_input.source_decision_sha256
        != parent.source_decision_sha256
    ):
        raise ValueError("VOLATILITY_SOURCE_DECISION_MISMATCH")

    stressed_q_3c_micros = min(
        _MAX_PROBABILITY_MICROS,
        parent.actual_price_micros
        + (_STRESS_PER_LEG_MICROS * len(parent.selected_buckets)),
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
        selected_buckets=parent.selected_buckets,
        horizon=parent.horizon,
        fallback_policy=parent.fallback_policy,
        shares=parent.shares,
        baseline_accept=parent.baseline_accept,
        actual_price_micros=parent.actual_price_micros,
        leg_count=len(parent.selected_buckets),
        p_vol_side_micros=volatility_input.p_vol_side_micros,
        stressed_q_3c_micros=stressed_q_3c_micros,
        volatility_edge_micros=volatility_edge_micros,
        threshold_micros=threshold_micros,
        accepted=accepted,
        reason_code=reason_code,
        source_decision_sha256=parent.source_decision_sha256,
        forecast_row_sha256=volatility_input.forecast_row_sha256,
    )
