from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


_STRICT_TOL: Final = 1e-9
_CONFIRMATION_TOL: Final = 1e-12
_STRESS: Final = 0.03
_EDGE_MINIMUM: Final = 0.02
_NO_EDGE_MINIMUM: Final = 0.05

V1_SOURCE_SHA256: Mapping[str, str] = MappingProxyType(
    {
        "STRICT_A_SELECTOR": "cb34c5b4df8cdc13629576bf9d326d4ff7debab55a43de8d822756352b973e1c",
        "STRICT_A_DECISION": "3ebad4a415e79e845e88ba775e395f5699c197bfcd7c3b472718390f4908e078",
        "PF1_CORE": "71fd5851c9dbefae3c2bdc5c3cbc2558728479c13518ad58a9e35ab878983ea4",
        "PF1_MODEL": "087e219574833767bfd470b2f3fafe873fdc5c72c11b6f4770de3255c50e6bd4",
        "PF1_CONFIG": "a86aec5cbd499bc6459a30eacb89e82fe966e1e6687b639a1d5b59f1a8438ce8",
        "PF1_EVALUATION_CONTRACT": "c6535fe8b2d4327582c1bcbbbf2c7fe67f52e009033721362b8a8e4fb4cc011f",
        "EARLY_HORIZON_STRATEGIES": "cad37155b4d9ba9b4dd779c49d0572623e74f8be26ca18e31c50018689d3aed0",
        "EARLY_CONFIDENCE_SELECTORS": "353173414762fc758c46086b01164edb58e187a6f04c6c6e1bdbbaff8ec24430",
        "NO_CONFIRMATION_CONCEPTS": "45be74ea8825e504fda85d3d3b365275c92ceec37831080f422f370704b661c2",
        "NO_FADE_RULES": "4cdc8b80c033c8b95092589c605cbe3564b9929375e2a54bdad45e8a943d4822",
        "NO_FADE_ENGINE": "936c72d8015cf2db3cac96bcfa2b1f04346f7e0f9826a02a7bac4fa7b5421c4c",
        "FAVORITE_BASKET_RULES": "d942e94f11fe4318de048f22e4864006f7bd1d64e06a00520c971160c8c800ef",
        "FAVORITE_BASKET_ENGINE": "5993a6a37ea7513c69a32da40d951f4128e07ed959781151558df693713cf839",
    }
)


def _probability(value: float | None, field: str, *, optional: bool) -> float | None:
    if value is None and optional:
        return None
    if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"INVALID_{field}")
    return value


@dataclass(frozen=True, slots=True)
class BucketInput:
    bucket_index: int
    model_p: float
    market_q_yes: float
    market_q_no: float | None
    vwap5: float | None
    confirmed_fee: float
    no_token_id: str | None

    def __post_init__(self) -> None:
        if type(self.bucket_index) is not int or not 0 <= self.bucket_index <= 10:
            raise ValueError("INVALID_BUCKET_INDEX")
        _probability(self.model_p, "MODEL_P", optional=False)
        _probability(self.market_q_yes, "MARKET_Q_YES", optional=False)
        _probability(self.market_q_no, "MARKET_Q_NO", optional=True)
        _probability(self.vwap5, "VWAP5", optional=True)
        _probability(self.confirmed_fee, "CONFIRMED_FEE", optional=False)
        if self.no_token_id is not None and (
            type(self.no_token_id) is not str or not self.no_token_id
        ):
            raise ValueError("INVALID_NO_TOKEN_ID")


@dataclass(frozen=True, slots=True)
class V1Evaluation:
    accepted: bool
    reason: str
    side: str
    checkpoint_minutes: int
    selected_bucket_indices: tuple[int, ...]
    favorite_bucket_index: int | None = None
    model_probability: float | None = None
    market_probability: float | None = None
    stressed_reference_cost: float | None = None
    historical_edge: float | None = None
    execution_cost: float | None = None
    executable_edge: float | None = None
    model_drop: float | None = None


def _ordered(buckets: tuple[BucketInput, ...]) -> tuple[BucketInput, ...]:
    if type(buckets) is not tuple:
        raise ValueError("INVALID_BUCKET_SEQUENCE")
    if any(type(row) is not BucketInput for row in buckets):
        raise ValueError("INVALID_BUCKET_INPUT")
    if len(buckets) != 11 or {row.bucket_index for row in buckets} != set(range(11)):
        raise ValueError("EXPECTED_EXACTLY_11_BUCKETS")
    return tuple(sorted(buckets, key=lambda row: row.bucket_index))


def _checkpoint(value: int, allowed: frozenset[int]) -> int:
    if type(value) is not int or value not in allowed:
        raise ValueError("INVALID_CHECKPOINT")
    return value


def _favorite(
    buckets: tuple[BucketInput, ...], *, tolerance: float
) -> BucketInput | None:
    maximum = max(row.market_q_yes for row in buckets)
    tied = [row for row in buckets if abs(row.market_q_yes - maximum) <= tolerance]
    return tied[0] if len(tied) == 1 else None


def _rejected(
    reason: str,
    *,
    side: str,
    checkpoint_minutes: int,
    selected: tuple[int, ...] = (),
    favorite: int | None = None,
    row: BucketInput | None = None,
    stressed_cost: float | None = None,
    historical_edge: float | None = None,
    execution_cost: float | None = None,
    executable_edge: float | None = None,
    model_drop: float | None = None,
) -> V1Evaluation:
    return V1Evaluation(
        accepted=False,
        reason=reason,
        side=side,
        checkpoint_minutes=checkpoint_minutes,
        selected_bucket_indices=selected,
        favorite_bucket_index=favorite,
        model_probability=None if row is None else row.model_p,
        market_probability=None if row is None else row.market_q_yes,
        stressed_reference_cost=stressed_cost,
        historical_edge=historical_edge,
        execution_cost=execution_cost,
        executable_edge=executable_edge,
        model_drop=model_drop,
    )


def evaluate_strict_a(
    checkpoint_minutes: int,
    buckets: tuple[BucketInput, ...],
    *,
    prior_position: bool = False,
) -> V1Evaluation:
    checkpoint = _checkpoint(checkpoint_minutes, frozenset({30, 60}))
    if type(prior_position) is not bool:
        raise ValueError("INVALID_PRIOR_POSITION")
    ordered = _ordered(buckets)
    if checkpoint == 30 and prior_position:
        return _rejected(
            "T30_BLOCKED_EXISTING_T60_POSITION", side="YES", checkpoint_minutes=30
        )
    favorite = _favorite(ordered, tolerance=_STRICT_TOL)
    if favorite is None:
        return _rejected(
            "NO_UNIQUE_REFERENCE_FAVORITE",
            side="YES",
            checkpoint_minutes=checkpoint,
        )
    reference_cost = favorite.market_q_yes + _STRESS
    historical_edge = favorite.model_p - reference_cost
    selected = (favorite.bucket_index,)
    if historical_edge < _EDGE_MINIMUM or reference_cost >= 1.0:
        return _rejected(
            "HISTORICAL_REFERENCE_GATE_REJECTED",
            side="YES",
            checkpoint_minutes=checkpoint,
            selected=selected,
            favorite=favorite.bucket_index,
            row=favorite,
            stressed_cost=reference_cost,
            historical_edge=historical_edge,
        )
    if favorite.vwap5 is None:
        return _rejected(
            "EXECUTION_REJECTED_MISSING_DEPTH",
            side="YES",
            checkpoint_minutes=checkpoint,
            selected=selected,
            favorite=favorite.bucket_index,
            row=favorite,
            stressed_cost=reference_cost,
            historical_edge=historical_edge,
        )
    execution_cost = favorite.vwap5 + favorite.confirmed_fee
    executable_edge = favorite.model_p - execution_cost
    if executable_edge < _EDGE_MINIMUM:
        return _rejected(
            "EXECUTABLE_EDGE_GATE_REJECTED",
            side="YES",
            checkpoint_minutes=checkpoint,
            selected=selected,
            favorite=favorite.bucket_index,
            row=favorite,
            stressed_cost=reference_cost,
            historical_edge=historical_edge,
            execution_cost=execution_cost,
            executable_edge=executable_edge,
        )
    return V1Evaluation(
        True,
        "SIGNAL_ACCEPTED",
        "YES",
        checkpoint,
        selected,
        favorite.bucket_index,
        favorite.model_p,
        favorite.market_q_yes,
        reference_cost,
        historical_edge,
        execution_cost,
        executable_edge,
    )


def evaluate_pf1(
    checkpoint_minutes: int,
    buckets: tuple[BucketInput, ...],
    *,
    prior_position: bool = False,
) -> V1Evaluation:
    checkpoint = _checkpoint(checkpoint_minutes, frozenset({30, 60}))
    if type(prior_position) is not bool:
        raise ValueError("INVALID_PRIOR_POSITION")
    ordered = _ordered(buckets)
    if checkpoint == 30 and prior_position:
        return _rejected("SKIPPED_ALREADY_POSITIONED", side="YES", checkpoint_minutes=30)
    if any(row.vwap5 is None for row in ordered):
        return _rejected(
            "EXECUTION_REJECTED_MISSING_DEPTH",
            side="YES",
            checkpoint_minutes=checkpoint,
        )
    selected = max(
        ordered,
        key=lambda row: (row.model_p - float(row.vwap5), -row.bucket_index),
    )
    selected_q = float(selected.vwap5)
    edge = selected.model_p - selected_q
    if edge + _CONFIRMATION_TOL < _EDGE_MINIMUM:
        reason = "NO_SIGNAL_EDGE_BELOW_THRESHOLD"
    elif selected_q > 0.95 + _CONFIRMATION_TOL:
        reason = "NO_SIGNAL_Q_ABOVE_CAP"
    else:
        reason = "PAPER_POSITION_OPENED"
    if reason != "PAPER_POSITION_OPENED":
        return _rejected(
            reason,
            side="YES",
            checkpoint_minutes=checkpoint,
            selected=(selected.bucket_index,),
            row=selected,
            historical_edge=edge,
            execution_cost=selected_q,
            executable_edge=edge,
        )
    return V1Evaluation(
        True,
        reason,
        "YES",
        checkpoint,
        (selected.bucket_index,),
        model_probability=selected.model_p,
        market_probability=selected_q,
        historical_edge=edge,
        execution_cost=selected_q,
        executable_edge=edge,
    )


def evaluate_pf1_historical(
    checkpoint_minutes: int, buckets: tuple[BucketInput, ...]
) -> V1Evaluation:
    checkpoint = _checkpoint(
        checkpoint_minutes, frozenset({30, 60, 360, 480})
    )
    ordered = _ordered(buckets)
    selected = max(
        ordered,
        key=lambda row: (row.model_p - row.market_q_yes, -row.bucket_index),
    )
    edge = selected.model_p - selected.market_q_yes
    accepted = edge >= _EDGE_MINIMUM - _STRICT_TOL and (
        selected.market_q_yes <= 0.97 + _STRICT_TOL
    )
    return V1Evaluation(
        accepted,
        "SIGNAL_ACCEPTED" if accepted else "PF1_GATE",
        "YES",
        checkpoint,
        (selected.bucket_index,),
        model_probability=selected.model_p,
        market_probability=selected.market_q_yes,
        stressed_reference_cost=min(1.0, selected.market_q_yes + _STRESS),
        historical_edge=edge,
    )


def evaluate_no_fade(
    partition: str,
    checkpoint_minutes: int,
    buckets: tuple[BucketInput, ...],
) -> V1Evaluation:
    if type(partition) is not str or partition not in {"P1", "P2"}:
        raise ValueError("UNKNOWN_NO_FADE_PARTITION")
    checkpoint = _checkpoint(
        checkpoint_minutes, frozenset({30, 60, 120, 240, 720, 1080})
    )
    ordered = _ordered(buckets)
    favorite = _favorite(ordered, tolerance=_STRICT_TOL)
    if favorite is None:
        return _rejected(
            "NO_UNIQUE_MARKET_FAVORITE", side="NO", checkpoint_minutes=checkpoint
        )
    candidates = tuple(row for row in ordered if row is not favorite)
    minimum = min(row.model_p - row.market_q_yes for row in candidates)
    tied = [
        row
        for row in candidates
        if abs((row.model_p - row.market_q_yes) - minimum) <= _STRICT_TOL
    ]
    if len(tied) != 1:
        return _rejected(
            "TIED_NO_FADE_SCORE",
            side="NO",
            checkpoint_minutes=checkpoint,
            favorite=favorite.bucket_index,
        )
    selected = tied[0]
    p_no = 1.0 - selected.model_p
    q_no_proxy = 1.0 - selected.market_q_yes
    raw_edge = p_no - q_no_proxy
    stressed_cost = q_no_proxy + _STRESS
    gate_edge = p_no - stressed_cost if partition == "P1" else raw_edge
    if stressed_cost >= 1.0 - _STRICT_TOL:
        reason = "STRESSED_COST_NOT_BELOW_ONE"
    elif gate_edge < _EDGE_MINIMUM - _STRICT_TOL:
        reason = "P1_STRESSED_PROXY_GATE" if partition == "P1" else "P2_RAW_PROXY_GATE"
    else:
        reason = "SIGNAL_ACCEPTED"
    if reason != "SIGNAL_ACCEPTED":
        return _rejected(
            reason,
            side="NO",
            checkpoint_minutes=checkpoint,
            selected=(selected.bucket_index,),
            favorite=favorite.bucket_index,
            row=selected,
            stressed_cost=stressed_cost,
            historical_edge=gate_edge,
        )
    return V1Evaluation(
        True,
        reason,
        "NO",
        checkpoint,
        (selected.bucket_index,),
        favorite.bucket_index,
        p_no,
        q_no_proxy,
        stressed_cost,
        gate_edge,
    )


def evaluate_favorite_only(
    checkpoint_minutes: int, buckets: tuple[BucketInput, ...]
) -> V1Evaluation:
    checkpoint = _checkpoint(checkpoint_minutes, frozenset({30, 60}))
    ordered = _ordered(buckets)
    favorite = _favorite(ordered, tolerance=_STRICT_TOL)
    if favorite is None:
        return _rejected(
            "NO_UNIQUE_MARKET_FAVORITE", side="YES", checkpoint_minutes=checkpoint
        )
    cost = favorite.market_q_yes + _STRESS
    edge = favorite.model_p - cost
    accepted = edge >= _EDGE_MINIMUM - _STRICT_TOL and cost < 1.0 - _STRICT_TOL
    return V1Evaluation(
        accepted,
        "SIGNAL_ACCEPTED" if accepted else "FAVORITE_ONLY_GATE",
        "YES",
        checkpoint,
        (favorite.bucket_index,),
        favorite.bucket_index,
        favorite.model_p,
        favorite.market_q_yes,
        cost,
        edge,
    )


def evaluate_favorite_neighbor(
    checkpoint_minutes: int, buckets: tuple[BucketInput, ...]
) -> V1Evaluation:
    checkpoint = _checkpoint(checkpoint_minutes, frozenset({30, 60}))
    ordered = _ordered(buckets)
    favorite = _favorite(ordered, tolerance=_STRICT_TOL)
    if favorite is None:
        return _rejected(
            "NO_UNIQUE_MARKET_FAVORITE", side="YES", checkpoint_minutes=checkpoint
        )
    neighbor_indices = tuple(
        index
        for index in (favorite.bucket_index - 1, favorite.bucket_index + 1)
        if 0 <= index <= 10
    )
    neighbors = tuple(ordered[index] for index in neighbor_indices)
    scores = tuple(
        favorite.model_p
        + row.model_p
        - favorite.market_q_yes
        - row.market_q_yes
        - (2.0 * _STRESS)
        for row in neighbors
    )
    if len(scores) == 2 and abs(scores[0] - scores[1]) <= _STRICT_TOL:
        return _rejected(
            "TIED_NEIGHBOR_EDGE",
            side="YES",
            checkpoint_minutes=checkpoint,
            favorite=favorite.bucket_index,
        )
    neighbor = neighbors[max(range(len(neighbors)), key=lambda index: scores[index])]
    model_p = favorite.model_p + neighbor.model_p
    market_q = favorite.market_q_yes + neighbor.market_q_yes
    cost = market_q + (2.0 * _STRESS)
    edge = model_p - cost
    if cost >= 1.0 - _STRICT_TOL:
        reason = "STRESSED_COST_NOT_BELOW_ONE"
    elif edge < _EDGE_MINIMUM - _STRICT_TOL:
        reason = "STRESSED_EDGE_BELOW_002"
    else:
        reason = "SIGNAL_ACCEPTED"
    return V1Evaluation(
        reason == "SIGNAL_ACCEPTED",
        reason,
        "YES",
        checkpoint,
        (favorite.bucket_index, neighbor.bucket_index),
        favorite.bucket_index,
        model_p,
        market_q,
        cost,
        edge,
    )


def _actual_no(row: BucketInput) -> float:
    if row.market_q_no is None:
        raise ValueError("MISSING_ACTUAL_NO_Q")
    return row.market_q_no


def _confirmation_single(
    concept: str,
    buckets: tuple[BucketInput, ...],
    checkpoint: int,
) -> V1Evaluation:
    ordered = _ordered(buckets)
    if concept == "A0":
        favorite = _favorite(ordered, tolerance=_CONFIRMATION_TOL)
        if favorite is None:
            return _rejected(
                "NO_UNIQUE_MARKET_FAVORITE", side="NO", checkpoint_minutes=checkpoint
            )
        pool = tuple(row for row in ordered if row is not favorite)
        selected = max(
            pool,
            key=lambda row: (
                row.market_q_yes - row.model_p,
                -row.bucket_index,
            ),
        )
        edge = selected.market_q_yes - selected.model_p
        accepted = edge + _CONFIRMATION_TOL >= _NO_EDGE_MINIMUM
        actual_q = _actual_no(selected)
        return V1Evaluation(
            accepted,
            "SIGNAL_ACCEPTED" if accepted else "A0_LEGACY_PROXY_EDGE_GATE",
            "NO",
            checkpoint,
            (selected.bucket_index,),
            favorite.bucket_index,
            1.0 - selected.model_p,
            actual_q,
            historical_edge=edge,
        )
    pool = ordered if concept == "A2" else tuple(
        row for row in ordered if row.bucket_index in {0, 1, 9, 10}
    )
    ranked = tuple(
        (1.0 - row.model_p - _actual_no(row), row) for row in pool
    )
    edge, selected = max(ranked, key=lambda pair: (pair[0], -pair[1].bucket_index))
    accepted = edge + _CONFIRMATION_TOL >= _NO_EDGE_MINIMUM
    return V1Evaluation(
        accepted,
        "SIGNAL_ACCEPTED" if accepted else f"{concept}_DIRECT_NO_EDGE_GATE",
        "NO",
        checkpoint,
        (selected.bucket_index,),
        model_probability=1.0 - selected.model_p,
        market_probability=_actual_no(selected),
        historical_edge=edge,
    )


def evaluate_confirmation(
    concept: str,
    buckets: tuple[BucketInput, ...],
    *,
    t30: tuple[BucketInput, ...] | None = None,
) -> V1Evaluation:
    if type(concept) is not str or concept not in {"A0", "A2", "B2", "C1"}:
        raise ValueError("UNAPPROVED_CONFIRMATION_CONCEPT")
    if concept != "C1":
        first = _confirmation_single(concept, buckets, 60)
        if first.accepted or t30 is None:
            return first
        return _confirmation_single(concept, t30, 30)
    t60_rows = _ordered(buckets)
    if t30 is None:
        raise ValueError("C1_REQUIRES_T60_AND_T30")
    t30_rows = _ordered(t30)
    favorite60 = _favorite(t60_rows, tolerance=_CONFIRMATION_TOL)
    favorite30 = _favorite(t30_rows, tolerance=_CONFIRMATION_TOL)
    if favorite60 is None or favorite30 is None:
        return _rejected(
            "C1_REQUIRES_UNIQUE_FAVORITES", side="NO", checkpoint_minutes=30
        )
    if favorite60.bucket_index == favorite30.bucket_index:
        return _rejected(
            "C1_FAVORITE_DID_NOT_SWITCH",
            side="NO",
            checkpoint_minutes=30,
            favorite=favorite30.bucket_index,
        )
    if favorite60.no_token_id is None:
        raise ValueError("C1_MISSING_OLD_FAVORITE_TOKEN")
    matches = tuple(
        row for row in t30_rows if row.no_token_id == favorite60.no_token_id
    )
    if len(matches) != 1:
        raise ValueError("C1_OLD_FAVORITE_TOKEN_NOT_UNIQUE")
    selected = matches[0]
    actual_q = _actual_no(selected)
    model_drop = favorite60.model_p - selected.model_p
    direct_edge = (1.0 - selected.model_p) - actual_q
    accepted = (
        model_drop + _CONFIRMATION_TOL >= _EDGE_MINIMUM
        and direct_edge + _CONFIRMATION_TOL >= _NO_EDGE_MINIMUM
    )
    reason = "SIGNAL_ACCEPTED" if accepted else "C1_DROP_OR_DIRECT_EDGE_GATE"
    return V1Evaluation(
        accepted,
        reason,
        "NO",
        30,
        (selected.bucket_index,),
        favorite60.bucket_index,
        1.0 - selected.model_p,
        actual_q,
        historical_edge=direct_edge,
        model_drop=model_drop,
    )
