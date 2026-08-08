from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from src.strategy_v2 import (
    ParentOverlayDecision,
    V2_OVERLAY_BINDINGS,
    VolatilityOverlayInput,
    evaluate_volatility_overlay,
)


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_ROW_KEYS = frozenset(
    {
        "actual_price_micros",
        "baseline_accept",
        "decision_identity",
        "expected_accept",
        "expected_pnl_micros",
        "expected_stressed_q_micros",
        "expected_turnover_micros",
        "forecast_row_sha256",
        "horizon",
        "overlay_strategy_id",
        "p_vol_side_micros",
        "parent_strategy_id",
        "selected_buckets",
        "side",
        "source_decision_sha256",
        "won",
    }
)


@dataclass(frozen=True, slots=True)
class StrategyIdentityParity:
    strategy_id: str
    fixture_row_count: int
    expected_trade_count: int
    actual_trade_count: int
    expected_wins: int
    actual_wins: int
    expected_losses: int
    actual_losses: int
    expected_turnover_micros: int
    actual_turnover_micros: int
    expected_pnl_micros: int
    actual_pnl_micros: int
    expected_roi: str
    actual_roi: str
    expected_trade_identity_sha256: str
    actual_trade_identity_sha256: str
    parity_pass: bool


@dataclass(frozen=True, slots=True)
class StrategyParityReport:
    fixture_sha256: str
    fixture_row_count: int
    strategy_count: int
    identities: tuple[StrategyIdentityParity, ...]
    parity_pass: bool


@dataclass(frozen=True, slots=True)
class _ValidatedRow:
    actual_price_micros: int
    baseline_accept: bool
    decision_identity: str
    expected_accept: bool
    expected_pnl_micros: int
    expected_stressed_q_micros: int
    expected_turnover_micros: int
    forecast_row_sha256: str
    horizon: str
    overlay_strategy_id: str
    p_vol_side_micros: int
    parent_strategy_id: str
    selected_buckets: tuple[str, ...]
    side: str
    source_decision_sha256: str
    won: bool


@dataclass(slots=True)
class _Accumulator:
    fixture_row_count: int = 0
    expected_trade_count: int = 0
    actual_trade_count: int = 0
    expected_wins: int = 0
    actual_wins: int = 0
    expected_turnover_micros: int = 0
    actual_turnover_micros: int = 0
    expected_pnl_micros: int = 0
    actual_pnl_micros: int = 0
    expected_trade_identities: list[str] | None = None
    actual_trade_identities: list[str] | None = None

    def __post_init__(self) -> None:
        self.expected_trade_identities = []
        self.actual_trade_identities = []


def verify_v2_overlay_parity(fixture_path: Path) -> StrategyParityReport:
    if not isinstance(fixture_path, Path):
        raise ValueError("C4_V2_PARITY_FIXTURE_PATH_TYPE")
    fixture_bytes = fixture_path.read_bytes()
    if not fixture_bytes:
        raise ValueError("C4_V2_PARITY_FIXTURE_EMPTY")
    try:
        fixture_text = fixture_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("C4_V2_PARITY_FIXTURE_UTF8") from exc
    if not fixture_text.endswith("\n"):
        raise ValueError("C4_V2_PARITY_FIXTURE_NONCANONICAL")

    rows: list[_ValidatedRow] = []
    for line_number, line in enumerate(fixture_text.splitlines(), start=1):
        if not line:
            raise ValueError(f"C4_V2_PARITY_FIXTURE_EMPTY_LINE:{line_number}")
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"C4_V2_PARITY_FIXTURE_JSON:{line_number}") from exc
        canonical = _canonical_json(payload)
        if canonical != line:
            raise ValueError(f"C4_V2_PARITY_FIXTURE_NONCANONICAL:{line_number}")
        rows.append(_validate_row(payload, line_number))
    if not rows:
        raise ValueError("C4_V2_PARITY_FIXTURE_EMPTY")

    seen_decisions: set[str] = set()
    accumulators: dict[str, _Accumulator] = {}
    for row in rows:
        identity_key = _trade_identity(row)
        if identity_key in seen_decisions:
            raise ValueError("C4_V2_PARITY_DUPLICATE_IDENTITY")
        seen_decisions.add(identity_key)

        binding = V2_OVERLAY_BINDINGS.get(row.overlay_strategy_id)
        if binding is None:
            raise ValueError("C4_V2_PARITY_UNKNOWN_STRATEGY")
        parent = ParentOverlayDecision(
            decision_identity=row.decision_identity,
            strategy_id=row.parent_strategy_id,
            side=row.side,
            selected_buckets=row.selected_buckets,
            horizon=row.horizon,
            fallback_policy="FROZEN_IDENTITY_SCHEDULE",
            shares=5,
            baseline_accept=row.baseline_accept,
            actual_price_micros=row.actual_price_micros,
            source_decision_sha256=row.source_decision_sha256,
        )
        result = evaluate_volatility_overlay(
            overlay_strategy_id=row.overlay_strategy_id,
            regime=binding.regime,
            parent=parent,
            volatility_input=VolatilityOverlayInput(
                p_vol_side_micros=row.p_vol_side_micros,
                source_decision_sha256=row.source_decision_sha256,
                forecast_row_sha256=row.forecast_row_sha256,
            ),
        )
        if result.stressed_q_3c_micros != row.expected_stressed_q_micros:
            raise ValueError("C4_V2_PARITY_STRESSED_COST_MISMATCH")
        if result.accepted != row.expected_accept:
            raise ValueError("C4_V2_PARITY_DECISION_MISMATCH")

        actual_turnover_micros = 0
        actual_pnl_micros = 0
        if result.accepted:
            actual_turnover_micros = 5 * result.stressed_q_3c_micros
            actual_pnl_micros = 5 * (
                (1_000_000 if row.won else 0) - result.stressed_q_3c_micros
            )
        if actual_turnover_micros != row.expected_turnover_micros:
            raise ValueError("C4_V2_PARITY_TURNOVER_MISMATCH")
        if actual_pnl_micros != row.expected_pnl_micros:
            raise ValueError("C4_V2_PARITY_PNL_MISMATCH")

        accumulator = accumulators.setdefault(row.overlay_strategy_id, _Accumulator())
        accumulator.fixture_row_count += 1
        if row.expected_accept:
            accumulator.expected_trade_count += 1
            accumulator.expected_wins += int(row.won)
            accumulator.expected_turnover_micros += row.expected_turnover_micros
            accumulator.expected_pnl_micros += row.expected_pnl_micros
            assert accumulator.expected_trade_identities is not None
            accumulator.expected_trade_identities.append(identity_key)
        if result.accepted:
            accumulator.actual_trade_count += 1
            accumulator.actual_wins += int(row.won)
            accumulator.actual_turnover_micros += actual_turnover_micros
            accumulator.actual_pnl_micros += actual_pnl_micros
            assert accumulator.actual_trade_identities is not None
            accumulator.actual_trade_identities.append(identity_key)

    identities = tuple(
        _finalize_identity(strategy_id, accumulators[strategy_id])
        for strategy_id in sorted(accumulators)
    )
    return StrategyParityReport(
        fixture_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
        fixture_row_count=len(rows),
        strategy_count=len(identities),
        identities=identities,
        parity_pass=all(identity.parity_pass for identity in identities),
    )


def _validate_row(payload: Any, line_number: int) -> _ValidatedRow:
    source = f"line={line_number}"
    if type(payload) is not dict or frozenset(payload) != _ROW_KEYS:
        raise ValueError(f"C4_V2_PARITY_FIXTURE_SCHEMA:{source}")
    strings = {
        name: _exact_string(payload[name], name, source)
        for name in (
            "decision_identity",
            "forecast_row_sha256",
            "horizon",
            "overlay_strategy_id",
            "parent_strategy_id",
            "side",
            "source_decision_sha256",
        )
    }
    for hash_name in ("forecast_row_sha256", "source_decision_sha256"):
        if _SHA256_PATTERN.fullmatch(strings[hash_name]) is None:
            raise ValueError(f"C4_V2_PARITY_FIXTURE_SHA256:{hash_name}:{source}")
    selected_value = payload["selected_buckets"]
    if (
        type(selected_value) is not list
        or not selected_value
        or any(type(item) is not str or not item for item in selected_value)
        or len(set(selected_value)) != len(selected_value)
    ):
        raise ValueError(f"C4_V2_PARITY_FIXTURE_SELECTED_BUCKETS:{source}")
    integers = {
        name: _exact_integer(payload[name], name, source)
        for name in (
            "actual_price_micros",
            "expected_pnl_micros",
            "expected_stressed_q_micros",
            "expected_turnover_micros",
            "p_vol_side_micros",
        )
    }
    for name in (
        "actual_price_micros",
        "expected_stressed_q_micros",
        "p_vol_side_micros",
    ):
        if not 0 <= integers[name] <= 1_000_000:
            raise ValueError(f"C4_V2_PARITY_FIXTURE_RANGE:{name}:{source}")
    if integers["expected_turnover_micros"] < 0:
        raise ValueError(f"C4_V2_PARITY_FIXTURE_RANGE:turnover:{source}")
    booleans = {
        name: _exact_boolean(payload[name], name, source)
        for name in ("baseline_accept", "expected_accept", "won")
    }
    if not booleans["expected_accept"] and (
        integers["expected_turnover_micros"] != 0
        or integers["expected_pnl_micros"] != 0
    ):
        raise ValueError(f"C4_V2_PARITY_FIXTURE_REJECTED_ECONOMICS:{source}")
    return _ValidatedRow(
        actual_price_micros=integers["actual_price_micros"],
        baseline_accept=booleans["baseline_accept"],
        decision_identity=strings["decision_identity"],
        expected_accept=booleans["expected_accept"],
        expected_pnl_micros=integers["expected_pnl_micros"],
        expected_stressed_q_micros=integers["expected_stressed_q_micros"],
        expected_turnover_micros=integers["expected_turnover_micros"],
        forecast_row_sha256=strings["forecast_row_sha256"],
        horizon=strings["horizon"],
        overlay_strategy_id=strings["overlay_strategy_id"],
        p_vol_side_micros=integers["p_vol_side_micros"],
        parent_strategy_id=strings["parent_strategy_id"],
        selected_buckets=tuple(selected_value),
        side=strings["side"],
        source_decision_sha256=strings["source_decision_sha256"],
        won=booleans["won"],
    )


def _finalize_identity(
    strategy_id: str, accumulator: _Accumulator
) -> StrategyIdentityParity:
    expected_hash = _identity_set_sha256(accumulator.expected_trade_identities or [])
    actual_hash = _identity_set_sha256(accumulator.actual_trade_identities or [])
    expected_losses = accumulator.expected_trade_count - accumulator.expected_wins
    actual_losses = accumulator.actual_trade_count - accumulator.actual_wins
    expected_roi = _roi(
        accumulator.expected_pnl_micros,
        accumulator.expected_turnover_micros,
    )
    actual_roi = _roi(
        accumulator.actual_pnl_micros,
        accumulator.actual_turnover_micros,
    )
    parity = (
        accumulator.expected_trade_count == accumulator.actual_trade_count
        and accumulator.expected_wins == accumulator.actual_wins
        and expected_losses == actual_losses
        and accumulator.expected_turnover_micros == accumulator.actual_turnover_micros
        and accumulator.expected_pnl_micros == accumulator.actual_pnl_micros
        and expected_roi == actual_roi
        and expected_hash == actual_hash
    )
    return StrategyIdentityParity(
        strategy_id=strategy_id,
        fixture_row_count=accumulator.fixture_row_count,
        expected_trade_count=accumulator.expected_trade_count,
        actual_trade_count=accumulator.actual_trade_count,
        expected_wins=accumulator.expected_wins,
        actual_wins=accumulator.actual_wins,
        expected_losses=expected_losses,
        actual_losses=actual_losses,
        expected_turnover_micros=accumulator.expected_turnover_micros,
        actual_turnover_micros=accumulator.actual_turnover_micros,
        expected_pnl_micros=accumulator.expected_pnl_micros,
        actual_pnl_micros=accumulator.actual_pnl_micros,
        expected_roi=expected_roi,
        actual_roi=actual_roi,
        expected_trade_identity_sha256=expected_hash,
        actual_trade_identity_sha256=actual_hash,
        parity_pass=parity,
    )


def _trade_identity(row: _ValidatedRow) -> str:
    return hashlib.sha256(
        _canonical_json(
            [
                row.overlay_strategy_id,
                row.source_decision_sha256,
                row.forecast_row_sha256,
            ]
        ).encode("utf-8")
    ).hexdigest()


def _identity_set_sha256(identities: list[str]) -> str:
    return hashlib.sha256(_canonical_json(sorted(identities)).encode("utf-8")).hexdigest()


def _roi(pnl_micros: int, turnover_micros: int) -> str:
    if turnover_micros == 0:
        return "0"
    with localcontext() as context:
        context.prec = 50
        value = Decimal(pnl_micros) / Decimal(turnover_micros)
        return format(value.quantize(Decimal("0.000000000001")), "f")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("C4_V2_PARITY_FIXTURE_JSON_VALUE") from exc


def _exact_string(value: object, name: str, source: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"C4_V2_PARITY_FIXTURE_STRING:{name}:{source}")
    return value


def _exact_integer(value: object, name: str, source: str) -> int:
    if type(value) is not int:
        raise ValueError(f"C4_V2_PARITY_FIXTURE_INTEGER:{name}:{source}")
    return value


def _exact_boolean(value: object, name: str, source: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"C4_V2_PARITY_FIXTURE_BOOLEAN:{name}:{source}")
    return value
