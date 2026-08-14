from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from src.current_input import _student_t_cdf
from src.historical_opportunity import HistoricalOpportunityMap
from src.strategy_dispatch import (
    StrategyDispatchBinding,
    V1HistoricalCheckpointInput,
    V1StrategyDispatchRequest,
    V2StrategyDispatchRequest,
    load_strategy_dispatcher,
)
from src.strategy_v1 import BucketInput, V1_IDENTITY_POLICIES
from src.strategy_v2 import ParentOverlayDecision, VolatilityOverlayInput


EXPECTED_SOURCE_SHA256 = {
    "source_pack_70": "911f4108cd8f6fe8d14c3bcbc435062caa0fa850c9f51a9712687507ac8691ce",
    "source_pack_100": "cfcf8b47acb87c3efec9a0d0f86f86dd83c7eeed919975c80dac9094ed7d1dc2",
    "checkpoint_matrix_170": "026dc0be49119c6b4a36c77cd343f606c39464de256008a1c70b041bfa42d0c7",
    "atlas_170": "66e81c1864c353be6b52318891c23439d4090baaedec22fbca5adb547a6b25fa",
    "markets_170": "8c197dc99a1acf48a0c85166ffeba8af0dff9536d19d3beab1d0991faa1fa6ec",
    "settlements_170": "ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1",
    "no_checkpoint_coverage": "4d4a1f9c634990b2941ac3dc90e88660331030da8255dfdbf1b2bb26d48d4967",
    "confirmation_dates": "2f87e5bcb0078a10e3a58453c3c4f1c6eb5e7d0057bfb359f5fb873eb15aeea8",
    "vol_forecast_ledger": "e5b48eb6761254781fc7db2c1a7c51ab9fba4a8a00aa92785678bb00f9b8bcf4",
}


@dataclass(frozen=True, slots=True)
class HistoricalArtifactPaths:
    source_pack_70: Path
    source_pack_100: Path
    checkpoint_matrix_170: Path
    atlas_170: Path
    markets_170: Path
    settlements_170: Path
    no_checkpoint_coverage: Path
    confirmation_dates: Path
    vol_forecast_ledger: Path


@dataclass(frozen=True, slots=True)
class HistoricalEvaluationUnit:
    market_date: str
    strategy_id: str
    request: V1StrategyDispatchRequest | V2StrategyDispatchRequest
    input_sha256: str


@dataclass(frozen=True, slots=True)
class NewHistoricalParentResult:
    market_date: str
    strategy_id: str
    decision_identity: str
    side: str
    checkpoint_minutes: int
    selected_bucket_indices: tuple[int, ...]
    selected_buckets: tuple[str, ...]
    horizon: str
    accepted: bool
    actual_price_micros: int
    source_decision_sha256: str

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (
                self.market_date,
                self.strategy_id,
                self.decision_identity,
                self.horizon,
            )
        ):
            raise ValueError("AHR_INVALID_NEW_PARENT_RESULT")
        if self.side not in {"YES", "NO"}:
            raise ValueError("AHR_INVALID_NEW_PARENT_RESULT")
        if type(self.checkpoint_minutes) is not int or self.checkpoint_minutes <= 0:
            raise ValueError("AHR_INVALID_NEW_PARENT_RESULT")
        if (
            type(self.selected_bucket_indices) is not tuple
            or not self.selected_bucket_indices
            or any(type(index) is not int for index in self.selected_bucket_indices)
            or type(self.selected_buckets) is not tuple
            or len(self.selected_buckets) != len(self.selected_bucket_indices)
        ):
            raise ValueError("AHR_INVALID_NEW_PARENT_RESULT")
        if type(self.accepted) is not bool:
            raise ValueError("AHR_INVALID_NEW_PARENT_RESULT")
        if (
            type(self.actual_price_micros) is not int
            or not 0 <= self.actual_price_micros <= 1_000_000
            or not _is_sha256(self.source_decision_sha256)
        ):
            raise ValueError("AHR_INVALID_NEW_PARENT_RESULT")


class HistoricalInputBuilder:
    def __init__(
        self,
        *,
        project_root: Path,
        artifacts: HistoricalArtifactPaths,
        pyarrow_path: Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifacts = artifacts
        self.pyarrow_path = pyarrow_path
        self.dispatcher = load_strategy_dispatcher(self.project_root)
        self._verified: dict[str, str] | None = None
        self._matrix: dict[tuple[str, int], tuple[dict[str, Any], ...]] | None = None
        self._atlas: dict[tuple[str, int], tuple[dict[str, Any], ...]] | None = None
        self._market_tokens: dict[tuple[str, str], str] | None = None
        self._coverage: dict[tuple[str, str, int], float] | None = None
        self._confirmation: frozenset[str] | None = None
        self._forecasts: dict[tuple[str, int], dict[str, Any]] | None = None
        self._opportunities: HistoricalOpportunityMap | None = None

    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(binding.strategy_id for binding in self.dispatcher.bindings)

    def verify_sources(self) -> dict[str, str]:
        if self._verified is not None:
            return dict(self._verified)
        observed: dict[str, str] = {}
        for name in HistoricalArtifactPaths.__dataclass_fields__:
            path = Path(getattr(self.artifacts, name))
            if not path.is_file():
                raise ValueError(f"AHR_SOURCE_MISSING:{name}:{path}")
            actual = _file_sha256(path)
            expected = EXPECTED_SOURCE_SHA256.get(name)
            if expected is None or actual != expected:
                raise ValueError(
                    f"AHR_SOURCE_HASH_MISMATCH:{name}:{actual}:{expected}"
                )
            observed[name] = actual
        self._verified = dict(observed)
        return observed

    def market_dates(self) -> tuple[str, ...]:
        self._load_matrix()
        assert self._matrix is not None
        return tuple(sorted({market_date for market_date, _ in self._matrix}))

    def opportunity_map(self) -> HistoricalOpportunityMap:
        if self._opportunities is None:
            self._load_matrix()
            self._load_confirmation_dates()
            assert self._matrix is not None
            assert self._confirmation is not None
            self._opportunities = HistoricalOpportunityMap.reproduce(
                dispatcher=self.dispatcher,
                matrix=self._matrix,
                confirmation_dates=self._confirmation,
            )
        return self._opportunities

    def is_contract_opportunity(
        self,
        *,
        strategy_id: str,
        market_date: str,
        checkpoint_minutes: int | None = None,
    ) -> bool:
        policy = V1_IDENTITY_POLICIES.get(strategy_id)
        if policy is None:
            return False
        checkpoints = (
            policy.checkpoints
            if checkpoint_minutes is None
            else (checkpoint_minutes,)
        )
        opportunities = self.opportunity_map()
        return any(
            opportunities.is_applicable(
                strategy_id=strategy_id,
                market_date=market_date,
                checkpoint_minutes=checkpoint,
            )
            for checkpoint in checkpoints
        )

    def build_v1_unit(
        self, *, strategy_id: str, market_date: str
    ) -> HistoricalEvaluationUnit:
        binding = self.dispatcher.binding(strategy_id)
        if binding.version != "V1":
            raise ValueError("AHR_V1_STRATEGY_REQUIRED")
        policy = V1_IDENTITY_POLICIES.get(strategy_id)
        if policy is None:
            raise ValueError("AHR_V1_POLICY_MISSING")
        if not self.is_contract_opportunity(
            strategy_id=strategy_id, market_date=market_date
        ):
            raise ValueError("AHR_NOT_CONTRACT_OPPORTUNITY")

        if binding.evaluator_key in {
            "NO_CONFIRMATION_V1",
            "FAVORITE_ONLY_V1",
            "FAVORITE_NEIGHBOR_BASKET_V1",
        }:
            buckets_by_checkpoint = self._other_v1_buckets(market_date)
        else:
            buckets_by_checkpoint = self._matrix_buckets(
                market_date, policy.checkpoints
            )
        request = V1StrategyDispatchRequest(
            checkpoints=tuple(
                V1HistoricalCheckpointInput(
                    checkpoint_minutes=checkpoint,
                    buckets=buckets_by_checkpoint[checkpoint],
                )
                for checkpoint in policy.checkpoints
            )
        )
        return HistoricalEvaluationUnit(
            market_date=market_date,
            strategy_id=strategy_id,
            request=request,
            input_sha256=_payload_sha256(_request_payload(request)),
        )

    def build_v2_unit(
        self,
        *,
        strategy_id: str,
        market_date: str,
        parent_result: object,
    ) -> HistoricalEvaluationUnit:
        binding, forecast_key = self._v2_context(
            strategy_id=strategy_id,
            market_date=market_date,
            parent_result=parent_result,
        )
        self._load_forecasts()
        assert self._forecasts is not None
        forecast = self._forecasts.get(forecast_key)
        if forecast is None:
            raise ValueError("AHR_VOL_FORECAST_MISSING")
        source_rows = self._bucket_source_rows(
            parent_result.strategy_id,
            market_date,
            parent_result.checkpoint_minutes,
        )
        selected_probabilities: list[float] = []
        selected_hashes: list[str] = []
        for index in parent_result.selected_bucket_indices:
            try:
                row = source_rows[index]
            except (IndexError, KeyError):
                raise ValueError("AHR_PARENT_BUCKET_MISSING") from None
            title = str(row["bucket_title"])
            selected_hashes.append(hashlib.sha256(title.encode("utf-8")).hexdigest())
            lower = _optional_float(row.get("lower_bound"))
            upper = _optional_float(row.get("upper_bound"))
            if lower is None and upper is None:
                lower, upper = _parse_bucket_title(title)
            selected_probabilities.append(
                _student_t_bucket_probability(
                    lower=lower,
                    upper=upper,
                    spot=float(forecast["spot"]),
                    variance=float(forecast["forecast_variance"]),
                    degrees_of_freedom=float(forecast["student_t_df"]),
                )
            )
        if tuple(selected_hashes) != parent_result.selected_buckets:
            raise ValueError("AHR_PARENT_BUCKET_IDENTITY_MISMATCH")
        p_yes = min(1.0, max(0.0, sum(selected_probabilities)))
        p_side = p_yes if parent_result.side == "YES" else 1.0 - p_yes
        p_side_micros = _micros(p_side)
        request = V2StrategyDispatchRequest(
            parent=ParentOverlayDecision(
                decision_identity=parent_result.decision_identity,
                strategy_id=binding.parent_strategy_id or "",
                side=binding.schedule["side"],
                selected_buckets=parent_result.selected_buckets,
                horizon=parent_result.horizon,
                fallback_policy="FROZEN_IDENTITY_SCHEDULE",
                shares=5,
                baseline_accept=parent_result.accepted,
                actual_price_micros=parent_result.actual_price_micros,
                source_decision_sha256=parent_result.source_decision_sha256,
            ),
            volatility_input=VolatilityOverlayInput(
                p_vol_side_micros=p_side_micros,
                source_decision_sha256=parent_result.source_decision_sha256,
                forecast_row_sha256=str(forecast["forecast_row_sha256"]),
            ),
        )
        return HistoricalEvaluationUnit(
            market_date=market_date,
            strategy_id=strategy_id,
            request=request,
            input_sha256=_payload_sha256(_request_payload(request)),
        )

    def is_v2_contract_opportunity(
        self,
        *,
        strategy_id: str,
        market_date: str,
        parent_result: object,
    ) -> bool:
        _, forecast_key = self._v2_context(
            strategy_id=strategy_id,
            market_date=market_date,
            parent_result=parent_result,
        )
        self._load_forecasts()
        assert self._forecasts is not None
        return forecast_key in self._forecasts

    def _v2_context(
        self,
        *,
        strategy_id: str,
        market_date: str,
        parent_result: object,
    ) -> tuple[StrategyDispatchBinding, tuple[str, int]]:
        if type(parent_result) is not NewHistoricalParentResult:
            raise ValueError("AHR_NEW_PARENT_RESULT_REQUIRED")
        binding = self.dispatcher.binding(strategy_id)
        if binding.version != "V2":
            raise ValueError("AHR_V2_STRATEGY_REQUIRED")
        if (
            parent_result.market_date != market_date
            or parent_result.strategy_id != binding.parent_strategy_id
            or parent_result.side != binding.schedule["side"]
        ):
            raise ValueError("AHR_NEW_PARENT_RESULT_MISMATCH")
        return binding, (market_date, parent_result.checkpoint_minutes)

    def _load_matrix(self) -> None:
        if self._matrix is not None:
            return
        parquet = self._parquet()
        columns = (
            "market_date",
            "horizon_minutes",
            "bucket_index",
            "bucket_title",
            "q_yes",
            "q_no",
            "model_p",
            "no_source_history_sha256",
        )
        rows = parquet.read_table(
            self.artifacts.checkpoint_matrix_170, columns=list(columns)
        ).to_pylist()
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for row in rows:
            key = (str(row["market_date"]), int(row["horizon_minutes"]))
            grouped.setdefault(key, []).append(row)
        self._matrix = {
            key: _ordered_bucket_rows(value) for key, value in grouped.items()
        }

    def _load_other_sources(self) -> None:
        if self._atlas is not None:
            return
        parquet = self._parquet()
        atlas_rows = parquet.read_table(
            self.artifacts.atlas_170,
            columns=[
                "market_date",
                "horizon_minutes",
                "bucket_index",
                "market_id",
                "bucket_title",
                "lower_bound",
                "upper_bound",
                "market_q_raw",
                "model_p",
            ],
        ).to_pylist()
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for row in atlas_rows:
            key = (str(row["market_date"]), int(row["horizon_minutes"]))
            if key[1] in {30, 60}:
                grouped.setdefault(key, []).append(row)
        self._atlas = {
            key: _ordered_bucket_rows(value) for key, value in grouped.items()
        }
        market_rows = parquet.read_table(
            self.artifacts.markets_170,
            columns=["market_date", "market_id", "no_token_id"],
        ).to_pylist()
        self._market_tokens = {
            (str(row["market_date"]), str(row["market_id"])): str(row["no_token_id"])
            for row in market_rows
        }
        coverage: dict[tuple[str, str, int], float] = {}
        with Path(self.artifacts.no_checkpoint_coverage).open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            for row in csv.DictReader(stream):
                if row.get("exists") != "True" or row.get("no_lookahead") != "True":
                    raise ValueError("AHR_NO_COVERAGE_INVALID")
                checkpoint = {"T-60": 60, "T-30": 30}.get(row.get("checkpoint"))
                if checkpoint is not None:
                    coverage[
                        (row["market_date"], row["no_token_id"], checkpoint)
                    ] = float(row["price"])
        self._coverage = coverage
        self._load_confirmation_dates()

    def _load_confirmation_dates(self) -> None:
        if self._confirmation is not None:
            return
        self.verify_sources()
        with Path(self.artifacts.confirmation_dates).open(
            "r", encoding="utf-8-sig", newline=""
        ) as stream:
            self._confirmation = frozenset(
                str(row["market_date"]) for row in csv.DictReader(stream)
            )

    def _load_forecasts(self) -> None:
        if self._forecasts is not None:
            return
        parquet = self._parquet()
        rows = parquet.read_table(self.artifacts.vol_forecast_ledger).to_pylist()
        forecasts: dict[tuple[str, int], dict[str, Any]] = {}
        for raw in rows:
            if not raw.get("selected_model"):
                continue
            row = dict(raw)
            checkpoint = int(row["checkpoint_minutes"])
            variance_rate = float(row["forecast_variance"]) / max(checkpoint, 1)
            low = float(row["regime_low"])
            high = float(row["regime_high"])
            row["variance_rate"] = variance_rate
            row["regime"] = (
                "CALM"
                if variance_rate <= low
                else "NORMAL" if variance_rate <= high else "STORM"
            )
            row["forecast_row_sha256"] = hashlib.sha256(
                str(sorted(row.items())).encode()
            ).hexdigest()
            key = (str(row["market_date"]), checkpoint)
            if key in forecasts:
                raise ValueError("AHR_VOL_FORECAST_DUPLICATE")
            forecasts[key] = row
        self._forecasts = forecasts

    def _matrix_buckets(
        self, market_date: str, checkpoints: tuple[int, ...]
    ) -> dict[int, tuple[BucketInput, ...]]:
        self._load_matrix()
        assert self._matrix is not None
        result: dict[int, tuple[BucketInput, ...]] = {}
        for checkpoint in checkpoints:
            rows = self._matrix.get((market_date, checkpoint))
            if rows is None:
                raise ValueError("AHR_CHECKPOINT_INPUT_MISSING")
            result[checkpoint] = tuple(
                BucketInput(
                    bucket_index=int(row["bucket_index"]),
                    model_p=float(row["model_p"]),
                    market_q_yes=float(row["q_yes"]),
                    market_q_no=float(row["q_no"]),
                    vwap5=None,
                    confirmed_fee=0.0,
                    no_token_id=str(row["no_source_history_sha256"]),
                )
                for row in rows
            )
        return result

    def _other_v1_buckets(
        self, market_date: str
    ) -> dict[int, tuple[BucketInput, ...]]:
        self._load_other_sources()
        assert self._atlas is not None
        assert self._market_tokens is not None
        assert self._coverage is not None
        assert self._confirmation is not None
        result: dict[int, tuple[BucketInput, ...]] = {}
        for checkpoint in (60, 30):
            rows = self._atlas.get((market_date, checkpoint))
            if rows is None:
                raise ValueError("AHR_ATLAS_INPUT_MISSING")
            buckets: list[BucketInput] = []
            for row in rows:
                token = self._market_tokens.get(
                    (market_date, str(row["market_id"]))
                )
                if token is None:
                    raise ValueError("AHR_NO_TOKEN_MISSING")
                q_yes = float(row["market_q_raw"])
                q_no = (
                    self._coverage.get((market_date, token, checkpoint))
                    if market_date in self._confirmation
                    else 1.0 - q_yes
                )
                if q_no is None:
                    raise ValueError("AHR_ACTUAL_NO_MISSING")
                buckets.append(
                    BucketInput(
                        bucket_index=int(row["bucket_index"]),
                        model_p=float(row["model_p"]),
                        market_q_yes=q_yes,
                        market_q_no=float(q_no),
                        vwap5=None,
                        confirmed_fee=0.0,
                        no_token_id=token,
                    )
                )
            result[checkpoint] = tuple(buckets)
        return result

    def _bucket_source_rows(
        self,
        parent_strategy_id: str,
        market_date: str,
        checkpoint: int,
    ) -> tuple[dict[str, Any], ...]:
        binding = self.dispatcher.binding(parent_strategy_id)
        if binding.evaluator_key in {
            "NO_CONFIRMATION_V1",
            "FAVORITE_ONLY_V1",
            "FAVORITE_NEIGHBOR_BASKET_V1",
        }:
            self._load_other_sources()
            assert self._atlas is not None
            rows = self._atlas.get((market_date, checkpoint))
        else:
            self._load_matrix()
            assert self._matrix is not None
            rows = self._matrix.get((market_date, checkpoint))
        if rows is None:
            raise ValueError("AHR_PARENT_BUCKET_SOURCE_MISSING")
        return rows

    def _parquet(self):
        self.verify_sources()
        if self.pyarrow_path is not None:
            path = str(Path(self.pyarrow_path).resolve())
            if path not in sys.path:
                sys.path.insert(0, path)
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:
            raise RuntimeError("AHR_PYARROW_REQUIRED") from exc
        return parquet


def _ordered_bucket_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    ordered = tuple(sorted(rows, key=lambda row: int(row["bucket_index"])))
    if len(ordered) != 11 or tuple(int(row["bucket_index"]) for row in ordered) != tuple(
        range(11)
    ):
        raise ValueError("AHR_EXPECTED_11_BUCKETS")
    return ordered


def _request_payload(request: object) -> object:
    if type(request) is V1StrategyDispatchRequest:
        return {
            "checkpoints": [
                {
                    "checkpoint_minutes": item.checkpoint_minutes,
                    "buckets": [asdict(bucket) for bucket in item.buckets],
                }
                for item in request.checkpoints
            ]
        }
    if type(request) is V2StrategyDispatchRequest:
        return {"parent": asdict(request.parent), "volatility_input": asdict(request.volatility_input)}
    raise ValueError("AHR_REQUEST_TYPE_INVALID")


def _payload_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _micros(value: float) -> int:
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )


def _optional_float(value: object) -> float | None:
    return None if value in (None, "") else float(value)


def _parse_bucket_title(title: str) -> tuple[float | None, float | None]:
    normalized = (
        title.strip()
        .replace("$", "")
        .replace(",", "")
        .replace(" ", "")
        .replace("–", "-")
        .replace("—", "-")
        .lower()
        .replace("lessthan", "<")
        .replace("orless", "<")
        .replace("ormore", "+")
        .replace("orhigher", "+")
    )
    if normalized.startswith("<"):
        return None, float(normalized.lstrip("<="))
    if normalized.endswith("+"):
        return float(normalized[:-1]), None
    if normalized.startswith(">"):
        return float(normalized[1:]), None
    parts = normalized.split("-")
    if len(parts) != 2:
        raise ValueError("AHR_BUCKET_TITLE_UNPARSEABLE")
    return float(parts[0]), float(parts[1])


def _student_t_bucket_probability(
    *,
    lower: float | None,
    upper: float | None,
    spot: float,
    variance: float,
    degrees_of_freedom: float,
) -> float:
    if spot <= 0 or variance <= 0:
        raise ValueError("AHR_VOL_INPUT_INVALID")
    df = max(float(degrees_of_freedom), 4.1)
    scale = math.sqrt(float(variance) * (df - 2.0) / df)
    lower_log = -math.inf if lower is None else math.log(lower / spot)
    upper_log = math.inf if upper is None else math.log(upper / spot)
    lower_cdf = 0.0 if lower_log == -math.inf else _student_t_cdf(
        lower_log, df=df, loc=0.0, scale=scale
    )
    upper_cdf = 1.0 if upper_log == math.inf else _student_t_cdf(
        upper_log, df=df, loc=0.0, scale=scale
    )
    return min(1.0, max(0.0, upper_cdf - lower_cdf))
