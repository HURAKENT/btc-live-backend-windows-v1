from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterable, Sequence

from src.performance_models import PerformanceObservation, PerformanceResolution


_RATIO_QUANTUM = Decimal("0.000000000001")
_SOURCE_VIEWS = frozenset({"HISTORICAL", "FORWARD", "COMBINED"})
_ROLLING_WINDOWS = (("30D", 30), ("90D", 90), ("365D", 365))


def build_metrics(
    observations: Sequence[PerformanceObservation],
    effective_resolutions: Sequence[PerformanceResolution] | dict[str, PerformanceResolution],
    *,
    source_view: str,
    as_of_date: date | str,
) -> dict[str, object]:
    """Pure deterministic fold of immutable performance facts."""
    if source_view not in _SOURCE_VIEWS:
        raise ValueError("INVALID_PERFORMANCE_SOURCE_VIEW")
    as_of = _parse_date(as_of_date, "INVALID_PERFORMANCE_AS_OF_DATE")
    raw = _deduplicate_observations(observations)
    resolutions = _deduplicate_resolutions(effective_resolutions)
    strategy_ids = {observation.strategy_id for observation in raw}
    if len(strategy_ids) > 1:
        raise ValueError("MULTIPLE_PERFORMANCE_STRATEGIES")

    historical_count = sum(row.source_layer == "HISTORICAL" for row in raw)
    forward_count = sum(row.source_layer == "FORWARD" for row in raw)
    identical_overlap_count = 0
    if source_view == "COMBINED":
        selected, identical_overlap_count = _combined_observations(raw)
    else:
        selected = [row for row in raw if row.source_layer == source_view]
    selected.sort(key=_financial_order)

    selected_keys = {row.observation_key for row in selected}
    selected_resolutions: dict[str, PerformanceResolution] = {}
    for observation_key, resolution in resolutions.items():
        if observation_key not in selected_keys:
            continue
        observation = next(row for row in selected if row.observation_key == observation_key)
        _validate_resolution(observation, resolution)
        selected_resolutions[observation_key] = resolution

    accepted = [row for row in selected if row.accepted]
    priced = [row for row in accepted if row.performance_price_micros is not None]
    resolved_pairs = [
        (row, selected_resolutions[row.observation_key])
        for row in selected
        if row.observation_key in selected_resolutions
    ]
    wins = sum(resolution.won for _, resolution in resolved_pairs)
    losses = len(resolved_pairs) - wins
    if wins + losses != len(resolved_pairs):
        raise ValueError("PERFORMANCE_RESOLUTION_PARTITION_BROKEN")

    turnover = sum(resolution.turnover_usd_micros for _, resolution in resolved_pairs)
    payout = sum(resolution.gross_payout_usd_micros for _, resolution in resolved_pairs)
    pnl = sum(resolution.pnl_usd_micros for _, resolution in resolved_pairs)
    cumulative_series, max_drawdown, current_drawdown, longest_win, longest_loss = (
        _sequence_metrics(resolved_pairs)
    )
    monthly_series = _monthly_series(resolved_pairs)
    rolling_windows = {
        label: _resolved_summary(
            [
                pair
                for pair in resolved_pairs
                if as_of - timedelta(days=days - 1)
                <= _parse_date(pair[0].market_date, "INVALID_OBSERVATION_MARKET_DATE")
                <= as_of
            ]
        )
        for label, days in _ROLLING_WINDOWS
    }
    rolling_series = {
        label: _rolling_series(resolved_pairs, days)
        for label, days in _ROLLING_WINDOWS
    }
    resolved_dates = [resolution.resolution_date for _, resolution in resolved_pairs]
    accepted_dates = [row.market_date for row in accepted]
    positive_cost_winners = [
        resolution
        for _, resolution in resolved_pairs
        if resolution.won and resolution.cost_usd_micros > 0
    ]
    positive_cost_losers = [
        resolution
        for _, resolution in resolved_pairs
        if not resolution.won and resolution.cost_usd_micros > 0
    ]
    annualized_reason = _annualization_reason(resolved_pairs)

    return {
        "source_view": source_view,
        "shares_micros": 5_000_000,
        "as_of_date": as_of.isoformat(),
        "opportunity_count": len(selected),
        "accepted_signal_count": len(accepted),
        "emitted_signal_count": sum(row.emitted for row in accepted),
        "priced_signal_count": len(priced),
        "resolved_signal_count": len(resolved_pairs),
        "unresolved_signal_count": len(accepted) - len(resolved_pairs),
        "unscorable_signal_count": sum(row.scoring_status == "UNSCORABLE" for row in accepted),
        "wins": wins,
        "losses": losses,
        "win_rate_numerator": wins,
        "win_rate_denominator": len(resolved_pairs),
        "win_rate_ratio": _ratio(wins, len(resolved_pairs)),
        "turnover_usd_micros": turnover,
        "gross_payout_usd_micros": payout,
        "pnl_usd_micros": pnl,
        "roi_numerator_usd_micros": pnl,
        "roi_denominator_usd_micros": turnover,
        "roi_ratio": _ratio(pnl, turnover),
        "average_price_numerator_micros": sum(
            row.performance_price_micros or 0 for row in priced
        ),
        "average_price_denominator": len(priced),
        "average_price_micros": _ratio(
            sum(row.performance_price_micros or 0 for row in priced), len(priced)
        ),
        "average_winner_return_numerator_count": len(positive_cost_winners),
        "average_winner_return_ratio": _average_returns(positive_cost_winners),
        "average_loser_return_numerator_count": len(positive_cost_losers),
        "average_loser_return_ratio": _average_returns(positive_cost_losers),
        "max_drawdown_usd_micros": max_drawdown,
        "current_drawdown_usd_micros": current_drawdown,
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
        "first_signal_date": min(accepted_dates) if accepted_dates else None,
        "last_signal_date": max(accepted_dates) if accepted_dates else None,
        "last_resolved_date": max(resolved_dates) if resolved_dates else None,
        "decision_coverage_numerator": len(accepted),
        "decision_coverage_denominator": len(selected),
        "decision_coverage_ratio": _ratio(len(accepted), len(selected)),
        "resolution_coverage_numerator": len(resolved_pairs),
        "resolution_coverage_denominator": len(accepted),
        "resolution_coverage_ratio": _ratio(len(resolved_pairs), len(accepted)),
        "price_coverage_numerator": len(priced),
        "price_coverage_denominator": len(accepted),
        "price_coverage_ratio": _ratio(len(priced), len(accepted)),
        "annualized_return_ratio": None,
        "annualized_return_reason_code": annualized_reason,
        "cumulative_series": cumulative_series,
        "monthly_series": monthly_series,
        "rolling_windows": rolling_windows,
        "rolling_series": rolling_series,
        "historical_raw_observation_count": historical_count,
        "forward_raw_observation_count": forward_count,
        "identical_overlap_count": identical_overlap_count,
        "effective_observation_count": len(selected),
        "combined_preferred_source": "FORWARD" if source_view == "COMBINED" else None,
    }


def _deduplicate_observations(
    observations: Sequence[PerformanceObservation],
) -> list[PerformanceObservation]:
    if type(observations) not in (list, tuple):
        raise ValueError("INVALID_PERFORMANCE_OBSERVATIONS")
    unique: dict[str, PerformanceObservation] = {}
    for observation in observations:
        if type(observation) is not PerformanceObservation:
            raise ValueError("INVALID_PERFORMANCE_OBSERVATION_TYPE")
        stored = unique.get(observation.observation_key)
        if stored is not None and stored.payload_sha256 != observation.payload_sha256:
            raise ValueError("PERFORMANCE_OBSERVATION_CONFLICT")
        unique[observation.observation_key] = observation
    return list(unique.values())


def _deduplicate_resolutions(
    resolutions: Sequence[PerformanceResolution] | dict[str, PerformanceResolution],
) -> dict[str, PerformanceResolution]:
    values: Iterable[PerformanceResolution]
    if type(resolutions) is dict:
        values = resolutions.values()
    elif type(resolutions) in (list, tuple):
        values = resolutions
    else:
        raise ValueError("INVALID_PERFORMANCE_RESOLUTIONS")
    unique: dict[str, PerformanceResolution] = {}
    for resolution in values:
        if type(resolution) is not PerformanceResolution:
            raise ValueError("INVALID_PERFORMANCE_RESOLUTION_TYPE")
        stored = unique.get(resolution.observation_key)
        if stored is not None and stored.payload_sha256 != resolution.payload_sha256:
            raise ValueError("MULTIPLE_EFFECTIVE_PERFORMANCE_RESOLUTIONS")
        unique[resolution.observation_key] = resolution
    return unique


def _combined_observations(
    observations: Sequence[PerformanceObservation],
) -> tuple[list[PerformanceObservation], int]:
    by_logical: dict[str, dict[str, PerformanceObservation]] = defaultdict(dict)
    for observation in observations:
        layer = by_logical[observation.logical_decision_key]
        existing = layer.get(observation.source_layer)
        if existing is not None and existing.payload_sha256 != observation.payload_sha256:
            raise ValueError("PERFORMANCE_OBSERVATION_CONFLICT")
        layer[observation.source_layer] = observation
    selected: list[PerformanceObservation] = []
    identical_overlap_count = 0
    for logical_key in sorted(by_logical):
        layers = by_logical[logical_key]
        historical = layers.get("HISTORICAL")
        forward = layers.get("FORWARD")
        if historical is not None and forward is not None:
            if historical.decision_semantic_sha256 != forward.decision_semantic_sha256:
                raise ValueError("COMBINED_CROSS_LAYER_CONFLICT")
            identical_overlap_count += 1
            selected.append(forward)
        else:
            selected.append(forward or historical)  # type: ignore[arg-type]
    return selected, identical_overlap_count


def _validate_resolution(
    observation: PerformanceObservation,
    resolution: PerformanceResolution,
) -> None:
    if (
        resolution.observation_key != observation.observation_key
        or not observation.accepted
        or observation.performance_price_micros is None
        or observation.performance_price_micros <= 0
    ):
        raise ValueError("INVALID_EFFECTIVE_PERFORMANCE_RESOLUTION")
    expected_cost = observation.performance_price_micros * 5
    expected_payout = 5_000_000 if resolution.won else 0
    if (
        resolution.shares_micros != 5_000_000
        or resolution.cost_usd_micros != expected_cost
        or resolution.turnover_usd_micros != expected_cost
        or resolution.gross_payout_usd_micros != expected_payout
        or resolution.pnl_usd_micros != expected_payout - expected_cost
    ):
        raise ValueError("PERFORMANCE_RESOLUTION_ECONOMICS_CONFLICT")


def _financial_order(observation: PerformanceObservation) -> tuple[str, int, str]:
    return (
        observation.market_date,
        -observation.checkpoint_minutes,
        observation.observation_key,
    )


def _sequence_metrics(
    pairs: Sequence[tuple[PerformanceObservation, PerformanceResolution]],
) -> tuple[list[dict[str, object]], int, int, int, int]:
    cumulative = 0
    high_water = 0
    max_drawdown = 0
    current_win = 0
    current_loss = 0
    longest_win = 0
    longest_loss = 0
    points: list[dict[str, object]] = []
    for observation, resolution in pairs:
        cumulative += resolution.pnl_usd_micros
        high_water = max(high_water, cumulative)
        max_drawdown = max(max_drawdown, high_water - cumulative)
        if resolution.won:
            current_win += 1
            current_loss = 0
        else:
            current_loss += 1
            current_win = 0
        longest_win = max(longest_win, current_win)
        longest_loss = max(longest_loss, current_loss)
        points.append(
            {
                "market_date": observation.market_date,
                "checkpoint_minutes": observation.checkpoint_minutes,
                "observation_key": observation.observation_key,
                "won": resolution.won,
                "pnl_usd_micros": resolution.pnl_usd_micros,
                "cumulative_pnl_usd_micros": cumulative,
            }
        )
    return points, max_drawdown, high_water - cumulative, longest_win, longest_loss


def _monthly_series(
    pairs: Sequence[tuple[PerformanceObservation, PerformanceResolution]],
) -> list[dict[str, object]]:
    grouped: dict[str, list[tuple[PerformanceObservation, PerformanceResolution]]] = defaultdict(list)
    for pair in pairs:
        grouped[pair[0].market_date[:7]].append(pair)
    return [
        {"month": month, **_resolved_summary(grouped[month])}
        for month in sorted(grouped)
    ]


def _rolling_series(
    pairs: Sequence[tuple[PerformanceObservation, PerformanceResolution]],
    days: int,
) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for index, pair in enumerate(pairs):
        endpoint = _parse_date(pair[0].market_date, "INVALID_OBSERVATION_MARKET_DATE")
        start = endpoint - timedelta(days=days - 1)
        window = [
            candidate
            for candidate in pairs[: index + 1]
            if start
            <= _parse_date(candidate[0].market_date, "INVALID_OBSERVATION_MARKET_DATE")
            <= endpoint
        ]
        points.append(
            {
                "as_of_date": endpoint.isoformat(),
                "observation_key": pair[0].observation_key,
                **_resolved_summary(window),
            }
        )
    return points


def _resolved_summary(
    pairs: Sequence[tuple[PerformanceObservation, PerformanceResolution]],
) -> dict[str, object]:
    wins = sum(resolution.won for _, resolution in pairs)
    count = len(pairs)
    turnover = sum(resolution.turnover_usd_micros for _, resolution in pairs)
    payout = sum(resolution.gross_payout_usd_micros for _, resolution in pairs)
    pnl = sum(resolution.pnl_usd_micros for _, resolution in pairs)
    return {
        "resolved_signal_count": count,
        "wins": wins,
        "losses": count - wins,
        "win_rate_numerator": wins,
        "win_rate_denominator": count,
        "win_rate_ratio": _ratio(wins, count),
        "turnover_usd_micros": turnover,
        "gross_payout_usd_micros": payout,
        "pnl_usd_micros": pnl,
        "roi_numerator_usd_micros": pnl,
        "roi_denominator_usd_micros": turnover,
        "roi_ratio": _ratio(pnl, turnover),
    }


def _average_returns(resolutions: Sequence[PerformanceResolution]) -> str | None:
    if not resolutions:
        return None
    with localcontext() as context:
        context.prec = 50
        total = sum(
            Decimal(resolution.pnl_usd_micros) / Decimal(resolution.cost_usd_micros)
            for resolution in resolutions
        )
        value = total / Decimal(len(resolutions))
        return format(value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        value = Decimal(numerator) / Decimal(denominator)
        return format(value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def _annualization_reason(
    pairs: Sequence[tuple[PerformanceObservation, PerformanceResolution]],
) -> str:
    if not pairs:
        return "NO_RESOLVED_SIGNALS"
    first = _parse_date(pairs[0][0].market_date, "INVALID_OBSERVATION_MARKET_DATE")
    last = _parse_date(pairs[-1][0].market_date, "INVALID_OBSERVATION_MARKET_DATE")
    if (last - first).days < 364:
        return "INSUFFICIENT_365_DAY_RESOLVED_HISTORY"
    return "ANNUALIZATION_NOT_DEFINED_WITHOUT_CAPITAL_ALLOCATION_POLICY"


def _parse_date(value: date | str, error_code: str) -> date:
    if type(value) is date:
        return value
    if type(value) is not str:
        raise ValueError(error_code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(error_code) from None
    if parsed.isoformat() != value:
        raise ValueError(error_code)
    return parsed
