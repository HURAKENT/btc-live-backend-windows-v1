from __future__ import annotations

import json
import math
import hashlib
from decimal import Decimal, ROUND_HALF_EVEN
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from src.performance_models import (
    PerformanceObservation,
    StrategyReconstructionStatus,
    canonical_identity,
    canonical_sha256,
    v1_performance_price,
)
from src.performance_repository import PerformanceRepository
from src.strategy_dispatch import (
    V1HistoricalCheckpointInput,
    V1StrategyDispatchRequest,
    load_strategy_dispatcher,
)
from src.strategy_v1 import BucketInput, V1_IDENTITY_POLICIES
from src.strategy_v1_parity import historical_identity_bindings


_CONTRACT_RELATIVE_PATH = Path(
    "strategy_sources/frozen/contracts/TERMINAL_DISTRIBUTION_RECOVERY_V1.json"
)
_V2_GAP_REASON = "FROZEN_VOLATILITY_CONTRACT_HAS_NO_POST_BLOCK5_TRANSITION"


@dataclass(frozen=True, slots=True)
class RecoveredMarketStrategyInput:
    market_id: str
    market_date: str
    resolution_utc: str
    bucket_titles: tuple[str, ...]
    bucket_bounds: tuple[tuple[float | None, float | None], ...]
    buckets_by_checkpoint: Mapping[int, tuple[BucketInput, ...]]
    evidence_sha256: str

    @classmethod
    def create(cls, **values: Any) -> "RecoveredMarketStrategyInput":
        buckets = values.get("buckets_by_checkpoint")
        if type(buckets) is dict:
            values["buckets_by_checkpoint"] = MappingProxyType(dict(buckets))
        for name in ("bucket_titles", "bucket_bounds"):
            if type(values.get(name)) is list:
                values[name] = tuple(values[name])
        return cls(**values)

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value for value in (self.market_id, self.market_date, self.resolution_utc)):
            raise ValueError("INVALID_RECOVERED_MARKET_IDENTITY")
        try:
            datetime.fromisoformat(self.resolution_utc.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("INVALID_RECOVERED_MARKET_RESOLUTION_TIME") from None
        if len(self.bucket_titles) != 11 or len(self.bucket_bounds) != 11:
            raise ValueError("INVALID_RECOVERED_MARKET_BUCKETS")
        if set(self.buckets_by_checkpoint) != {30, 60, 120, 240, 360, 480, 720, 1080}:
            raise ValueError("INVALID_RECOVERED_CHECKPOINT_SET")
        if any(
            len(rows) != 11
            or tuple(row.bucket_index for row in rows) != tuple(range(11))
            for rows in self.buckets_by_checkpoint.values()
        ):
            raise ValueError("INVALID_RECOVERED_CHECKPOINT_BUCKETS")
        if len(self.evidence_sha256) != 64:
            raise ValueError("INVALID_RECOVERED_EVIDENCE_SHA256")


@dataclass(frozen=True, slots=True)
class ReconstructionReceipt:
    identity_count: int
    v1_identity_count: int
    v2_data_gap_count: int
    observation_count: int
    observation_inserted_count: int
    observation_replayed_count: int
    status_inserted_count: int
    status_replayed_count: int


class RecoveredStrategyReconstructor:
    """Point-in-time adapter feeding the existing frozen historical dispatcher."""

    def __init__(self, *, project_root: Path, repository: PerformanceRepository) -> None:
        if type(repository) is not PerformanceRepository:
            raise ValueError("INVALID_RECONSTRUCTION_REPOSITORY")
        self.project_root = Path(project_root).resolve()
        self.repository = repository
        self.dispatcher = load_strategy_dispatcher(self.project_root)
        status_payload = json.loads(
            (self.project_root / "reports/STRATEGY_47_STATUS_MATRIX.json").read_text(encoding="utf-8")
        )
        self.status_by_id = {
            str(row["strategy_id"]): row for row in status_payload.get("strategies", [])
        }
        if len(self.dispatcher.bindings) != 47 or set(self.status_by_id) != {
            binding.strategy_id for binding in self.dispatcher.bindings
        }:
            raise ValueError("RECONSTRUCTION_REGISTRY_47_MISMATCH")

    def reconstruct(self, recovered: RecoveredMarketStrategyInput) -> ReconstructionReceipt:
        if type(recovered) is not RecoveredMarketStrategyInput:
            raise ValueError("INVALID_RECOVERED_STRATEGY_INPUT")
        observations: list[PerformanceObservation] = []
        statuses: list[StrategyReconstructionStatus] = []
        u1, u2 = _post_baseline_universes(recovered.buckets_by_checkpoint)
        reconstructed_at_ms = int(
            datetime.fromisoformat(recovered.resolution_utc.replace("Z", "+00:00")).timestamp() * 1000
        )
        for binding in self.dispatcher.bindings:
            if binding.version == "V2":
                statuses.append(self._status(
                    recovered, binding.strategy_id, status="DATA_GAP",
                    reason_code=_V2_GAP_REASON,
                    missing_input=(
                        "exact post-2026-07-07 volatility model assignment, refit/selection rule, "
                        "regime thresholds, and authorized fitted state"
                    ),
                    checkpoints=tuple(int(value) for value in binding.schedule.get("checkpoints", (60, 30))),
                    observation_count=0, reconstructed_at_ms=reconstructed_at_ms,
                ))
                continue
            policy = V1_IDENTITY_POLICIES[binding.strategy_id]
            if not _strategy_is_applicable(binding.strategy_id, u1=u1, u2=u2):
                statuses.append(self._status(
                    recovered, binding.strategy_id, status="EXPECTED_ABSENT",
                    reason_code="STRATEGY_NOT_APPLICABLE_BY_FROZEN_POPULATION",
                    missing_input=None, checkpoints=policy.checkpoints,
                    observation_count=0, reconstructed_at_ms=reconstructed_at_ms,
                ))
                continue
            missing = [checkpoint for checkpoint in policy.checkpoints if checkpoint not in recovered.buckets_by_checkpoint]
            if missing:
                statuses.append(self._status(
                    recovered, binding.strategy_id, status="DATA_GAP",
                    reason_code="POINT_IN_TIME_CHECKPOINT_INPUT_MISSING",
                    missing_input="checkpoint_minutes=" + ",".join(map(str, missing)),
                    checkpoints=policy.checkpoints, observation_count=0,
                    reconstructed_at_ms=reconstructed_at_ms,
                ))
                continue
            request = V1StrategyDispatchRequest(checkpoints=tuple(
                V1HistoricalCheckpointInput(
                    checkpoint_minutes=checkpoint,
                    buckets=recovered.buckets_by_checkpoint[checkpoint],
                )
                for checkpoint in policy.checkpoints
            ))
            input_payload = {
                "market_date": recovered.market_date,
                "strategy_id": binding.strategy_id,
                "checkpoints": [
                    {
                        "checkpoint_minutes": item.checkpoint_minutes,
                        "buckets": [asdict(bucket) for bucket in item.buckets],
                    }
                    for item in request.checkpoints
                ],
            }
            input_sha256 = _float_payload_sha256(input_payload)
            evaluations = self.dispatcher.dispatch_historical(
                strategy_id=binding.strategy_id, request=request
            )
            if type(evaluations) is not tuple:
                raise ValueError("RECONSTRUCTION_V1_DISPATCH_RESULT_INVALID")
            built = [
                self._observation(
                    recovered=recovered, binding=binding, evaluation=evaluation,
                    input_sha256=input_sha256, reconstructed_at_ms=reconstructed_at_ms,
                )
                for evaluation in evaluations
            ]
            observations.extend(built)
            statuses.append(self._status(
                recovered, binding.strategy_id, status="RECOVERED",
                reason_code="RECOVERED_RETROSPECTIVE", missing_input=None,
                checkpoints=policy.checkpoints, observation_count=len(built),
                reconstructed_at_ms=reconstructed_at_ms,
                input_sha256=input_sha256,
            ))

        observation_results = self.repository.append_observation_batch(observations)
        status_results = [self.repository.append_reconstruction_status(row) for row in statuses]
        return ReconstructionReceipt(
            identity_count=len(statuses),
            v1_identity_count=sum(binding.version == "V1" for binding in self.dispatcher.bindings),
            v2_data_gap_count=sum(row.reason_code == _V2_GAP_REASON for row in statuses),
            observation_count=len(observations),
            observation_inserted_count=sum(row.inserted for row in observation_results),
            observation_replayed_count=sum(not row.inserted for row in observation_results),
            status_inserted_count=sum(row.inserted for row in status_results),
            status_replayed_count=sum(not row.inserted for row in status_results),
        )

    def _observation(
        self, *, recovered: RecoveredMarketStrategyInput, binding: Any,
        evaluation: Any, input_sha256: str, reconstructed_at_ms: int,
    ) -> PerformanceObservation:
        checkpoint = int(evaluation.checkpoint_minutes)
        selected_indices = tuple(int(value) for value in evaluation.selected_bucket_indices)
        selected = tuple(
            hashlib.sha256(recovered.bucket_titles[index].encode("utf-8")).hexdigest()
            for index in selected_indices
        )
        accepted = bool(evaluation.accepted)
        reference_price = None
        performance_price = None
        price_basis = "NOT_APPLICABLE"
        if accepted:
            reference_price = _optional_probability_micros(evaluation.market_probability)
            performance_price, price_basis = v1_performance_price(
                stressed_reference_cost_micros=_optional_probability_micros(
                    evaluation.stressed_reference_cost
                ),
                market_probability_micros=reference_price,
                selected_leg_count=len(selected),
            )
        result_payload = {
            "strategy_id": binding.strategy_id,
            "market_date": recovered.market_date,
            "checkpoint_minutes": checkpoint,
            "accepted": accepted,
            "reason": evaluation.reason,
            "side": evaluation.side,
            "selected_bucket_indices": list(selected_indices),
            "model_probability_micros": _optional_probability_micros(evaluation.model_probability),
            "market_probability_micros": _optional_probability_micros(evaluation.market_probability),
            "stressed_reference_cost_micros": _optional_probability_micros(
                evaluation.stressed_reference_cost
            ),
            "input_sha256": input_sha256,
        }
        result_sha256 = canonical_sha256(result_payload)
        logical = {
            "checkpoint_minutes": checkpoint,
            "horizon": f"T-{checkpoint}m",
            "market_date": recovered.market_date,
            "market_id": recovered.market_id,
            "parent_strategy_id": None,
            "side": str(evaluation.side),
            "source_decision_identity": None,
            "strategy_id": binding.strategy_id,
            "strategy_version": "V1",
        }
        status = self.status_by_id[binding.strategy_id]
        return PerformanceObservation.create(
            observation_key=canonical_identity(
                "performance-observation-recovered",
                {
                    "market_id": recovered.market_id,
                    "market_date": recovered.market_date,
                    "strategy_id": binding.strategy_id,
                    "checkpoint_minutes": checkpoint,
                    "input_sha256": input_sha256,
                    "result_sha256": result_sha256,
                },
            ),
            logical_decision_key=canonical_identity("performance-logical-decision", logical),
            source_layer="HISTORICAL", strategy_id=binding.strategy_id,
            strategy_version="V1", family=_family_label(binding),
            registry_index=binding.registry_index,
            activation_status=str(status["activation_status"]),
            activation_reason_code=str(status["activation_reason_code"]),
            market_id=recovered.market_id, market_date=recovered.market_date,
            evaluation_key=None, signal_identity_key=None, parent_strategy_id=None,
            source_decision_identity=None, checkpoint_minutes=checkpoint,
            horizon=f"T-{checkpoint}m", side=str(evaluation.side),
            selected_buckets=selected, accepted=accepted, emitted=False,
            reason_code=str(evaluation.reason), reference_price_micros=reference_price,
            performance_price_micros=performance_price,
            performance_price_basis=price_basis,
            scoring_status="RESOLUTION_PENDING" if accepted else "REJECTED",
            scoring_reason_code="SETTLEMENT_PENDING" if accepted else "EVALUATOR_REJECTED",
            observed_at_ms=reconstructed_at_ms, source_created_at_ms=None,
            provenance_run_id=f"RECOVERED_RETROSPECTIVE:{input_sha256}",
            source_result_sha256=result_sha256, input_sha256=input_sha256,
        )

    def _status(
        self, recovered: RecoveredMarketStrategyInput, strategy_id: str, *,
        status: str, reason_code: str, missing_input: str | None,
        checkpoints: tuple[int, ...], observation_count: int,
        reconstructed_at_ms: int, input_sha256: str | None = None,
    ) -> StrategyReconstructionStatus:
        input_hash = input_sha256 or canonical_sha256({
            "market_date": recovered.market_date,
            "strategy_id": strategy_id,
            "reason_code": reason_code,
            "missing_input": missing_input,
        })
        return StrategyReconstructionStatus.create(
            reconstruction_key=canonical_identity(
                "strategy-reconstruction", [recovered.market_date, strategy_id]
            ),
            market_date=recovered.market_date, strategy_id=strategy_id,
            status=status, reason_code=reason_code, missing_input=missing_input,
            checkpoint_minutes=tuple(checkpoints), observation_count=observation_count,
            evidence_sha256=recovered.evidence_sha256, input_sha256=input_hash,
            reconstructed_at_ms=reconstructed_at_ms,
            provenance={
                "market_id": recovered.market_id,
                "origin": "RECOVERED_RETROSPECTIVE",
                "provider_evidence_sha256": recovered.evidence_sha256,
            },
        )


def build_recovered_market_strategy_input(
    *,
    market_payload: Mapping[str, Any],
    history_by_asset: Mapping[str, Sequence[Mapping[str, Any]]],
    binance_candles: Sequence[Mapping[str, Any]],
    contract: TerminalDistributionRecoveryContract,
) -> RecoveredMarketStrategyInput:
    required = ("event_id", "market_date", "resolution_utc", "asset_ids", "outcomes", "bucket_bounds")
    if any(name not in market_payload for name in required):
        raise ValueError("RECOVERED_MARKET_METADATA_MISSING")
    asset_ids = tuple(str(value) for value in market_payload["asset_ids"])
    titles = tuple(str(value) for value in market_payload["outcomes"])
    bounds = tuple(
        (None if pair[0] is None else float(pair[0]), None if pair[1] is None else float(pair[1]))
        for pair in market_payload["bucket_bounds"]
    )
    if len(asset_ids) != 22 or len(titles) != 11 or len(bounds) != 11:
        raise ValueError("RECOVERED_MARKET_METADATA_INCOMPLETE")
    resolution = datetime.fromisoformat(str(market_payload["resolution_utc"]).replace("Z", "+00:00"))
    resolution_ms = int(resolution.timestamp() * 1000)
    checkpoints: dict[int, tuple[BucketInput, ...]] = {}
    causal_evidence: list[object] = []
    for checkpoint in (30, 60, 120, 240, 360, 480, 720, 1080):
        target_ms = resolution_ms - checkpoint * 60_000
        feature = build_terminal_feature(
            binance_candles, checkpoint_timestamp_ms=target_ms
        )
        model_probabilities = terminal_bucket_probabilities(
            contract, horizon_minutes=checkpoint, feature=feature,
            bucket_bounds=bounds,
        )
        rows: list[BucketInput] = []
        for index in range(11):
            yes_asset = asset_ids[index * 2]
            no_asset = asset_ids[index * 2 + 1]
            try:
                yes_point = sample_point_at_or_before(
                    history_by_asset[yes_asset], target_timestamp_ms=target_ms
                )
                no_point = sample_point_at_or_before(
                    history_by_asset[no_asset], target_timestamp_ms=target_ms
                )
            except KeyError:
                raise ValueError(f"CHECKPOINT_PRICE_HISTORY_MISSING:{checkpoint}:{index}") from None
            rows.append(BucketInput(
                bucket_index=index,
                model_p=float(model_probabilities[index]),
                market_q_yes=float(yes_point["price"]),
                market_q_no=float(no_point["price"]),
                vwap5=None,
                confirmed_fee=0.0,
                no_token_id=no_asset,
            ))
            causal_evidence.append({
                "checkpoint_minutes": checkpoint,
                "bucket_index": index,
                "target_timestamp_ms": target_ms,
                "yes_asset_id": yes_asset,
                "yes_observation_timestamp_ms": yes_point["timestamp_ms"],
                "yes_price": yes_point["price"],
                "no_asset_id": no_asset,
                "no_observation_timestamp_ms": no_point["timestamp_ms"],
                "no_price": no_point["price"],
                "max_binance_candle_time_ms": feature.max_candle_time_ms,
            })
        checkpoints[checkpoint] = tuple(rows)
    evidence_sha256 = _float_payload_sha256({
        "market_payload": dict(market_payload),
        "causal_checkpoint_evidence": causal_evidence,
        "terminal_contract_schema": contract.schema_version,
        "terminal_contract_baseline_parity": dict(contract.baseline_parity),
    })
    return RecoveredMarketStrategyInput.create(
        market_id=str(market_payload["event_id"]),
        market_date=str(market_payload["market_date"]),
        resolution_utc=resolution.isoformat(),
        bucket_titles=titles,
        bucket_bounds=bounds,
        buckets_by_checkpoint=checkpoints,
        evidence_sha256=evidence_sha256,
    )


def _post_baseline_universes(
    buckets_by_checkpoint: Mapping[int, tuple[BucketInput, ...]],
) -> tuple[bool, bool]:
    for checkpoint in (60, 30):
        buckets = buckets_by_checkpoint[checkpoint]
        selected = min(
            buckets,
            key=lambda row: (-(row.model_p - row.market_q_yes), row.bucket_index),
        )
        if selected.model_p - selected.market_q_yes < 0.02 - 1e-9 or selected.market_q_yes > 0.97 + 1e-9:
            continue
        maximum = max(row.market_q_yes for row in buckets)
        favorites = [row for row in buckets if abs(row.market_q_yes - maximum) <= 1e-9]
        return True, len(favorites) == 1 and favorites[0].bucket_index == selected.bucket_index
    return False, False


def _strategy_is_applicable(strategy_id: str, *, u1: bool, u2: bool) -> bool:
    binding = historical_identity_bindings().get(strategy_id)
    if binding is None or binding.universe == "ALL":
        return True
    if binding.universe == "U1":
        return u1
    if binding.universe == "U2":
        return u2
    if binding.universe == "U1|U2":
        return u1 or u2
    raise ValueError("RECONSTRUCTION_UNIVERSE_UNKNOWN")


def _optional_probability_micros(value: float | None) -> int | None:
    if value is None:
        return None
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )


def _family_label(binding: Any) -> str:
    if binding.evaluator_key == "STRICT_A_COMPOSED_V1":
        return "STRICT_A"
    if binding.evaluator_key == "PF1_COMPOSED_V1":
        return "PF1"
    return "HISTORICAL_V1"


def _float_payload_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TerminalFeature:
    spot: float
    sigma_30m: float
    sigma_180m: float
    vol_ratio_30_180: float
    position_inside_bucket: float
    max_candle_time_ms: int


@dataclass(frozen=True, slots=True)
class TerminalDistributionRecoveryContract:
    schema_version: str
    training_cutoff_exclusive: str
    quantiles: tuple[float, ...]
    horizons: Mapping[str, Mapping[str, Any]]
    baseline_parity: Mapping[str, Any]
    source_sha256: Mapping[str, str]

    @classmethod
    def load(cls, project_root: Path) -> "TerminalDistributionRecoveryContract":
        path = Path(project_root).resolve() / _CONTRACT_RELATIVE_PATH
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "TERMINAL_DISTRIBUTION_RECOVERY_V1":
            raise ValueError("INVALID_TERMINAL_RECOVERY_CONTRACT")
        quantiles = tuple(float(value) for value in payload.get("quantiles", ()))
        if quantiles != (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95):
            raise ValueError("INVALID_TERMINAL_RECOVERY_QUANTILES")
        horizons = payload.get("horizons")
        if type(horizons) is not dict or set(horizons) != {
            "30", "60", "120", "240", "360", "480", "720", "1080"
        }:
            raise ValueError("INVALID_TERMINAL_RECOVERY_HORIZONS")
        return cls(
            schema_version=payload["schema_version"],
            training_cutoff_exclusive=str(payload["training_cutoff_exclusive"]),
            quantiles=quantiles,
            horizons=MappingProxyType({key: MappingProxyType(dict(value)) for key, value in horizons.items()}),
            baseline_parity=MappingProxyType(dict(payload["baseline_parity"])),
            source_sha256=MappingProxyType(dict(payload["source_sha256"])),
        )


def sample_point_at_or_before(
    points: Sequence[Mapping[str, Any]], *, target_timestamp_ms: int
) -> dict[str, Any]:
    if type(target_timestamp_ms) is not int or target_timestamp_ms < 0:
        raise ValueError("INVALID_CHECKPOINT_TARGET_TIMESTAMP")
    eligible = [
        point for point in points
        if type(point.get("timestamp_ms")) is int
        and int(point["timestamp_ms"]) <= target_timestamp_ms
    ]
    if not eligible:
        raise ValueError("CHECKPOINT_PRICE_HISTORY_MISSING")
    selected = max(eligible, key=lambda point: int(point["timestamp_ms"]))
    price = float(selected["price"])
    if not 0.0 <= price <= 1.0:
        raise ValueError("INVALID_CHECKPOINT_PRICE")
    return {"timestamp_ms": int(selected["timestamp_ms"]), "price": price}


def build_terminal_feature(
    candles: Sequence[Mapping[str, Any]], *, checkpoint_timestamp_ms: int
) -> TerminalFeature:
    if type(checkpoint_timestamp_ms) is not int or checkpoint_timestamp_ms <= 0:
        raise ValueError("INVALID_TERMINAL_CHECKPOINT_TIMESTAMP")
    causal = sorted(
        (
            (int(row["close_time_ms"]), float(row["close"]))
            for row in candles
            if type(row.get("close_time_ms")) is int
            and int(row["close_time_ms"]) < checkpoint_timestamp_ms
        ),
        key=lambda value: value[0],
    )
    deduplicated: dict[int, float] = {}
    for timestamp, close in causal:
        if not math.isfinite(close) or close <= 0:
            raise ValueError("INVALID_TERMINAL_CANDLE_CLOSE")
        deduplicated[timestamp] = close
    ordered = sorted(deduplicated.items())
    if len(ordered) < 181:
        raise ValueError("TERMINAL_FEATURE_PREHISTORY_MISSING")
    window = ordered[-181:]
    closes = [value for _, value in window]
    log_returns = [
        math.log(closes[index] / closes[index - 1])
        for index in range(1, len(closes))
    ]
    sigma_30 = _sample_standard_deviation(log_returns[-30:])
    sigma_180 = _sample_standard_deviation(log_returns[-180:])
    if sigma_180 <= 0:
        raise ValueError("TERMINAL_FEATURE_ZERO_VOLATILITY")
    spot = closes[-1]
    return TerminalFeature(
        spot=spot,
        sigma_30m=sigma_30,
        sigma_180m=sigma_180,
        vol_ratio_30_180=sigma_30 / sigma_180,
        position_inside_bucket=(spot - math.floor(spot / 2_000.0) * 2_000.0) / 2_000.0,
        max_candle_time_ms=window[-1][0],
    )


def terminal_bucket_probabilities(
    contract: TerminalDistributionRecoveryContract,
    *,
    horizon_minutes: int,
    feature: TerminalFeature,
    bucket_bounds: Sequence[tuple[float | None, float | None]],
) -> tuple[float, ...]:
    if type(contract) is not TerminalDistributionRecoveryContract:
        raise ValueError("INVALID_TERMINAL_RECOVERY_CONTRACT_TYPE")
    try:
        spec = contract.horizons[str(horizon_minutes)]
    except KeyError:
        raise ValueError("TERMINAL_RECOVERY_HORIZON_UNSUPPORTED") from None
    log_quantiles = _terminal_log_quantiles(spec, horizon_minutes, feature)
    terminal_prices = [feature.spot * math.exp(value) for value in log_quantiles]
    unique_prices, unique_taus = _unique_coordinates(terminal_prices, contract.quantiles)
    raw: list[float] = []
    for lower, upper in bucket_bounds:
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("INVALID_TERMINAL_BUCKET_BOUNDS")
        low = 0.0 if lower is None else _linear_cdf(float(lower), unique_prices, unique_taus)
        high = 1.0 if upper is None else _linear_cdf(float(upper), unique_prices, unique_taus)
        raw.append(max(0.0, high - low))
    total = sum(raw)
    if total <= 0:
        raise ValueError("TERMINAL_RECOVERY_ZERO_PROBABILITY")
    return tuple(value / total for value in raw)


def _terminal_log_quantiles(
    spec: Mapping[str, Any], horizon_minutes: int, feature: TerminalFeature
) -> tuple[float, ...]:
    model = str(spec["selected_model"])
    if model == "unconditional_empirical":
        return tuple(float(value) for value in spec["log_return_quantiles"])
    scale = max(feature.sigma_180m * math.sqrt(horizon_minutes), 1e-8)
    if model in {"volatility_scaled_empirical", "student_t"}:
        standardized = spec["standardized_quantiles"]
    elif model == "historical_analog":
        edges = [float(value) for value in spec["vol_ratio_edges"]]
        vol_bin = max(0, min(_search_right(edges, feature.vol_ratio_30_180) - 1, len(edges) - 2))
        position_bin = max(
            0,
            min(_search_right([0.0, 0.25, 0.5, 0.75, 1.000001], feature.position_inside_bucket) - 1, 3),
        )
        standardized = spec["bin_quantiles"].get(
            f"{vol_bin}:{position_bin}", spec["fallback_quantiles"]
        )
    else:
        raise ValueError("TERMINAL_RECOVERY_MODEL_UNSUPPORTED")
    return tuple(scale * float(value) for value in standardized)


def _sample_standard_deviation(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("TERMINAL_FEATURE_PREHISTORY_MISSING")
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _search_right(values: Sequence[float], target: float) -> int:
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if target < values[middle]:
            high = middle
        else:
            low = middle + 1
    return low


def _unique_coordinates(
    prices: Sequence[float], probabilities: Sequence[float]
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    result_prices: list[float] = []
    result_probabilities: list[float] = []
    for price, probability in zip(prices, probabilities):
        if not result_prices or price != result_prices[-1]:
            result_prices.append(price)
            result_probabilities.append(probability)
    return tuple(result_prices), tuple(result_probabilities)


def _linear_cdf(
    target: float, prices: Sequence[float], probabilities: Sequence[float]
) -> float:
    if target < prices[0]:
        return 0.0
    if target > prices[-1]:
        return 1.0
    right = _search_right(prices, target)
    if right == 0:
        return probabilities[0]
    if right >= len(prices):
        return probabilities[-1]
    left = right - 1
    weight = (target - prices[left]) / (prices[right] - prices[left])
    return probabilities[left] * (1.0 - weight) + probabilities[right] * weight
