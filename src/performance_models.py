from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping, Self


FIVE_SHARES_MICROS = 5_000_000
PERFORMANCE_OBSERVATION_SCHEMA = "PERFORMANCE_OBSERVATION_V1"
PERFORMANCE_RESOLUTION_SCHEMA = "PERFORMANCE_RESOLUTION_V1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_LAYERS = frozenset({"HISTORICAL", "FORWARD"})
_SOURCE_VIEWS = frozenset({"HISTORICAL", "FORWARD", "COMBINED"})
_PRICE_BASES = frozenset(
    {
        "CONTRACT_STRESSED_REFERENCE_EXPLICIT",
        "CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED",
        "CONTRACT_V2_STRESSED_Q_3C",
        "FORWARD_CAPTURED_CONTRACT",
        "NOT_APPLICABLE",
        "UNAVAILABLE",
    }
)
_SCORING_STATUSES = frozenset({"REJECTED", "RESOLUTION_PENDING", "UNSCORABLE"})
_INGEST_MODES = frozenset({"HISTORICAL_BOOTSTRAP", "FORWARD_INCREMENTAL"})
_INGEST_STATUSES = frozenset({"STARTED", "COMPLETE", "FAILED"})
_CATCHUP_CLASSIFICATIONS = frozenset(
    {"RESOLVED", "PENDING", "EXPECTED_ABSENT", "DATA_GAP"}
)
_SERIES_KINDS = frozenset({"CUMULATIVE", "MONTHLY", "ROLLING_30D", "ROLLING_90D", "ROLLING_365D"})


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _normalize_json(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise ValueError("INVALID_PERFORMANCE_CANONICAL_JSON") from None


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_identity(namespace: str, coordinates: object) -> str:
    _require_nonempty(namespace, "INVALID_PERFORMANCE_IDENTITY_NAMESPACE")
    return f"{namespace}:{canonical_sha256(coordinates)}"


def v1_performance_price(
    *,
    stressed_reference_cost_micros: int | None,
    market_probability_micros: int | None,
    selected_leg_count: int,
) -> tuple[int, str]:
    _require_positive_int(selected_leg_count, "INVALID_SELECTED_LEG_COUNT")
    if stressed_reference_cost_micros is not None:
        _require_probability(
            stressed_reference_cost_micros,
            "INVALID_STRESSED_REFERENCE_COST_MICROS",
        )
        return (
            stressed_reference_cost_micros,
            "CONTRACT_STRESSED_REFERENCE_EXPLICIT",
        )
    if market_probability_micros is None:
        raise ValueError("MISSING_MARKET_PROBABILITY_MICROS")
    _require_probability(
        market_probability_micros,
        "INVALID_MARKET_PROBABILITY_MICROS",
    )
    return (
        min(1_000_000, market_probability_micros + 30_000 * selected_leg_count),
        "CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED",
    )


def v2_performance_price(
    *,
    stressed_q_3c_micros: int,
    actual_price_micros: int | None = None,
) -> tuple[int, str]:
    _require_probability(stressed_q_3c_micros, "INVALID_STRESSED_Q_3C_MICROS")
    if actual_price_micros is not None:
        _require_probability(actual_price_micros, "INVALID_ACTUAL_PRICE_MICROS")
    return stressed_q_3c_micros, "CONTRACT_V2_STRESSED_Q_3C"


def five_share_economics(performance_price_micros: int, *, won: bool) -> dict[str, int]:
    _require_probability(performance_price_micros, "INVALID_PERFORMANCE_PRICE_MICROS")
    if type(won) is not bool:
        raise ValueError("INVALID_PERFORMANCE_WON")
    cost = performance_price_micros * 5
    payout = FIVE_SHARES_MICROS if won else 0
    return {
        "shares_micros": FIVE_SHARES_MICROS,
        "cost_usd_micros": cost,
        "gross_payout_usd_micros": payout,
        "pnl_usd_micros": payout - cost,
        "turnover_usd_micros": cost,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class PerformanceObservation:
    observation_key: str
    logical_decision_key: str
    source_layer: str
    strategy_id: str
    strategy_version: str
    family: str
    registry_index: int
    activation_status: str
    activation_reason_code: str
    market_id: str
    market_date: str
    evaluation_key: str | None
    signal_identity_key: str | None
    parent_strategy_id: str | None
    source_decision_identity: str | None
    checkpoint_minutes: int
    horizon: str
    side: str
    selected_buckets: tuple[str, ...]
    accepted: bool
    emitted: bool
    reason_code: str
    reference_price_micros: int | None
    performance_price_micros: int | None
    performance_price_basis: str
    scoring_status: str
    scoring_reason_code: str
    observed_at_ms: int
    source_created_at_ms: int | None
    provenance_run_id: str
    source_result_sha256: str
    input_sha256: str
    schema_version: str = PERFORMANCE_OBSERVATION_SCHEMA
    shares_micros: int = 0
    decision_semantic_sha256: str = field(init=False, default="")
    selected_buckets_json: str = field(init=False, default="")
    selected_bucket_identity_sha256: str = field(init=False, default="")
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, **values: Any) -> Self:
        accepted = values.get("accepted")
        if type(accepted) is not bool:
            raise ValueError("INVALID_OBSERVATION_ACCEPTED")
        selected = values.get("selected_buckets")
        if type(selected) is list:
            values["selected_buckets"] = tuple(selected)
        values["shares_micros"] = FIVE_SHARES_MICROS if accepted else 0
        return cls(**values)

    def __post_init__(self) -> None:
        self._validate()
        selected_json = canonical_json(list(self.selected_buckets))
        semantic = {
            "accepted": self.accepted,
            "checkpoint_minutes": self.checkpoint_minutes,
            "horizon": self.horizon,
            "logical_decision_key": self.logical_decision_key,
            "market_date": self.market_date,
            "market_id": self.market_id,
            "parent_strategy_id": self.parent_strategy_id,
            "performance_price_basis": self.performance_price_basis,
            "performance_price_micros": self.performance_price_micros,
            "reason_code": self.reason_code,
            "reference_price_micros": self.reference_price_micros,
            "selected_buckets": list(self.selected_buckets),
            "side": self.side,
            "source_decision_identity": self.source_decision_identity,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
        }
        decision_hash = canonical_sha256(semantic)
        payload = {
            "accepted": self.accepted,
            "activation_reason_code": self.activation_reason_code,
            "activation_status": self.activation_status,
            "checkpoint_minutes": self.checkpoint_minutes,
            "decision_semantic_sha256": decision_hash,
            "emitted": self.emitted,
            "evaluation_key": self.evaluation_key,
            "family": self.family,
            "horizon": self.horizon,
            "input_sha256": self.input_sha256,
            "logical_decision_key": self.logical_decision_key,
            "market_date": self.market_date,
            "market_id": self.market_id,
            "observation_key": self.observation_key,
            "observed_at_ms": self.observed_at_ms,
            "parent_strategy_id": self.parent_strategy_id,
            "performance_price_basis": self.performance_price_basis,
            "performance_price_micros": self.performance_price_micros,
            "provenance_run_id": self.provenance_run_id,
            "reason_code": self.reason_code,
            "reference_price_micros": self.reference_price_micros,
            "registry_index": self.registry_index,
            "schema_version": self.schema_version,
            "scoring_reason_code": self.scoring_reason_code,
            "scoring_status": self.scoring_status,
            "selected_bucket_identity_sha256": canonical_sha256(list(self.selected_buckets)),
            "selected_buckets": list(self.selected_buckets),
            "shares_micros": self.shares_micros,
            "side": self.side,
            "signal_identity_key": self.signal_identity_key,
            "source_created_at_ms": self.source_created_at_ms,
            "source_decision_identity": self.source_decision_identity,
            "source_layer": self.source_layer,
            "source_result_sha256": self.source_result_sha256,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
        }
        object.__setattr__(self, "selected_buckets_json", selected_json)
        object.__setattr__(self, "selected_bucket_identity_sha256", canonical_sha256(list(self.selected_buckets)))
        object.__setattr__(self, "decision_semantic_sha256", decision_hash)
        payload_json = canonical_json(payload)
        object.__setattr__(self, "payload_json", payload_json)
        object.__setattr__(self, "payload_sha256", hashlib.sha256(payload_json.encode("utf-8")).hexdigest())

    def _validate(self) -> None:
        if self.schema_version != PERFORMANCE_OBSERVATION_SCHEMA:
            raise ValueError("INVALID_OBSERVATION_SCHEMA_VERSION")
        for name in (
            "observation_key", "logical_decision_key", "strategy_id",
            "strategy_version", "family", "activation_status",
            "activation_reason_code", "market_id", "horizon", "reason_code",
            "scoring_reason_code", "provenance_run_id",
        ):
            _require_nonempty(getattr(self, name), f"INVALID_OBSERVATION_{name.upper()}")
        if self.source_layer not in _SOURCE_LAYERS:
            raise ValueError("INVALID_OBSERVATION_SOURCE_LAYER")
        _require_nonnegative_int(self.registry_index, "INVALID_OBSERVATION_REGISTRY_INDEX")
        _require_date(self.market_date, "INVALID_OBSERVATION_MARKET_DATE")
        _require_optional_nonempty(self.evaluation_key, "INVALID_OBSERVATION_EVALUATION_KEY")
        _require_optional_nonempty(self.signal_identity_key, "INVALID_OBSERVATION_SIGNAL_IDENTITY_KEY")
        _require_optional_nonempty(self.parent_strategy_id, "INVALID_OBSERVATION_PARENT_STRATEGY_ID")
        _require_optional_nonempty(self.source_decision_identity, "INVALID_OBSERVATION_SOURCE_DECISION_IDENTITY")
        _require_positive_int(self.checkpoint_minutes, "INVALID_OBSERVATION_CHECKPOINT_MINUTES")
        if self.side not in {"YES", "NO"}:
            raise ValueError("INVALID_OBSERVATION_SIDE")
        if type(self.selected_buckets) is not tuple or any(
            type(bucket) is not str or not bucket for bucket in self.selected_buckets
        ) or len(set(self.selected_buckets)) != len(self.selected_buckets):
            raise ValueError("INVALID_OBSERVATION_SELECTED_BUCKETS")
        if type(self.accepted) is not bool or type(self.emitted) is not bool:
            raise ValueError("INVALID_OBSERVATION_BOOLEAN")
        if self.performance_price_basis not in _PRICE_BASES:
            raise ValueError("INVALID_OBSERVATION_PRICE_BASIS")
        if self.scoring_status not in _SCORING_STATUSES:
            raise ValueError("INVALID_OBSERVATION_SCORING_STATUS")
        if self.reference_price_micros is not None:
            _require_probability(self.reference_price_micros, "INVALID_OBSERVATION_REFERENCE_PRICE")
        if self.performance_price_micros is not None:
            _require_probability(self.performance_price_micros, "INVALID_OBSERVATION_PERFORMANCE_PRICE")
        _require_nonnegative_int(self.observed_at_ms, "INVALID_OBSERVATION_OBSERVED_AT")
        if self.source_created_at_ms is not None:
            _require_nonnegative_int(self.source_created_at_ms, "INVALID_OBSERVATION_SOURCE_CREATED_AT")
        _require_sha256(self.source_result_sha256, "INVALID_OBSERVATION_SOURCE_RESULT_SHA256")
        _require_sha256(self.input_sha256, "INVALID_OBSERVATION_INPUT_SHA256")
        if self.accepted:
            if not self.selected_buckets or self.shares_micros != FIVE_SHARES_MICROS:
                raise ValueError("INVALID_OBSERVATION_ECONOMICS")
            if self.scoring_status == "REJECTED":
                raise ValueError("INVALID_OBSERVATION_SCORING_STATUS")
            if self.scoring_status == "RESOLUTION_PENDING" and (
                self.performance_price_micros is None
                or self.performance_price_micros <= 0
                or self.performance_price_basis in {"NOT_APPLICABLE", "UNAVAILABLE"}
            ):
                raise ValueError("INVALID_OBSERVATION_ECONOMICS")
        elif (
            self.emitted
            or self.signal_identity_key is not None
            or self.shares_micros != 0
            or self.performance_price_micros is not None
            or self.performance_price_basis != "NOT_APPLICABLE"
            or self.scoring_status != "REJECTED"
        ):
            raise ValueError("INVALID_OBSERVATION_ECONOMICS")
        if self.emitted and (
            self.source_layer != "FORWARD"
            or not self.accepted
            or self.signal_identity_key is None
        ):
            raise ValueError("INVALID_OBSERVATION_EMISSION")

    def constructor_values(self) -> dict[str, Any]:
        computed = {
            "shares_micros", "decision_semantic_sha256", "selected_buckets_json",
            "selected_bucket_identity_sha256", "payload_json", "payload_sha256",
        }
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in computed
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PerformanceResolution:
    resolution_key: str
    observation_key: str
    revision: int
    supersedes_resolution_key: str | None
    settlement_identity: str
    settlement_source_event_id: int | None
    winning_bucket_identity: str
    won: bool
    shares_micros: int
    cost_usd_micros: int
    gross_payout_usd_micros: int
    pnl_usd_micros: int
    turnover_usd_micros: int
    resolution_date: str
    resolved_at_ms: int | None
    provenance_json: str
    schema_version: str = PERFORMANCE_RESOLUTION_SCHEMA
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create_for_observation(
        cls,
        observation: PerformanceObservation,
        *,
        resolution_key: str,
        revision: int,
        supersedes_resolution_key: str | None,
        settlement_identity: str,
        settlement_source_event_id: int | None,
        winning_bucket_identity: str,
        won: bool,
        resolution_date: str,
        resolved_at_ms: int | None,
        provenance_json: Mapping[str, object],
    ) -> Self:
        if type(observation) is not PerformanceObservation:
            raise ValueError("INVALID_PERFORMANCE_OBSERVATION_TYPE")
        if (
            not observation.accepted
            or observation.performance_price_micros is None
            or observation.performance_price_micros <= 0
        ):
            raise ValueError("UNSCORABLE_PERFORMANCE_OBSERVATION")
        economics = five_share_economics(observation.performance_price_micros, won=won)
        return cls(
            resolution_key=resolution_key,
            observation_key=observation.observation_key,
            revision=revision,
            supersedes_resolution_key=supersedes_resolution_key,
            settlement_identity=settlement_identity,
            settlement_source_event_id=settlement_source_event_id,
            winning_bucket_identity=winning_bucket_identity,
            won=won,
            resolution_date=resolution_date,
            resolved_at_ms=resolved_at_ms,
            provenance_json=canonical_json(dict(provenance_json)),
            **economics,
        )

    def __post_init__(self) -> None:
        for name in ("resolution_key", "observation_key", "settlement_identity", "winning_bucket_identity"):
            _require_nonempty(getattr(self, name), f"INVALID_RESOLUTION_{name.upper()}")
        if self.schema_version != PERFORMANCE_RESOLUTION_SCHEMA:
            raise ValueError("INVALID_RESOLUTION_SCHEMA_VERSION")
        _require_positive_int(self.revision, "INVALID_RESOLUTION_REVISION")
        _require_optional_nonempty(self.supersedes_resolution_key, "INVALID_RESOLUTION_SUPERSEDES_KEY")
        if (self.revision == 1) != (self.supersedes_resolution_key is None):
            raise ValueError("INVALID_RESOLUTION_SUPERSESSION")
        if self.settlement_source_event_id is not None:
            _require_positive_int(self.settlement_source_event_id, "INVALID_RESOLUTION_SOURCE_EVENT_ID")
        if type(self.won) is not bool or self.shares_micros != FIVE_SHARES_MICROS:
            raise ValueError("INVALID_RESOLUTION_ECONOMICS")
        for name in ("cost_usd_micros", "gross_payout_usd_micros", "turnover_usd_micros"):
            _require_nonnegative_int(getattr(self, name), f"INVALID_RESOLUTION_{name.upper()}")
        if self.gross_payout_usd_micros != (FIVE_SHARES_MICROS if self.won else 0):
            raise ValueError("INVALID_RESOLUTION_ECONOMICS")
        if self.turnover_usd_micros != self.cost_usd_micros or self.pnl_usd_micros != self.gross_payout_usd_micros - self.cost_usd_micros:
            raise ValueError("INVALID_RESOLUTION_ECONOMICS")
        _require_date(self.resolution_date, "INVALID_RESOLUTION_DATE")
        if self.resolved_at_ms is not None:
            _require_nonnegative_int(self.resolved_at_ms, "INVALID_RESOLVED_AT_MS")
        canonical_provenance = _canonical_json_string(self.provenance_json, "INVALID_RESOLUTION_PROVENANCE")
        object.__setattr__(self, "provenance_json", canonical_provenance)
        payload = {
            name: getattr(self, name)
            for name in (
                "schema_version", "resolution_key", "observation_key", "revision",
                "supersedes_resolution_key", "settlement_identity",
                "settlement_source_event_id", "winning_bucket_identity", "won",
                "shares_micros", "cost_usd_micros", "gross_payout_usd_micros",
                "pnl_usd_micros", "turnover_usd_micros", "resolution_date",
                "resolved_at_ms",
            )
        }
        payload["provenance"] = json.loads(canonical_provenance)
        _set_payload(self, payload)


@dataclass(frozen=True, slots=True, kw_only=True)
class PerformanceCursor:
    cursor_name: str
    cursor: Mapping[str, object]
    updated_at_ms: int
    schema_version: str = "PERFORMANCE_CURSOR_V1"
    cursor_json: str = field(init=False, default="")
    cursor_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, *, cursor_name: str, cursor: Mapping[str, object], updated_at_ms: int) -> Self:
        return cls(cursor_name=cursor_name, cursor=dict(cursor), updated_at_ms=updated_at_ms)

    def __post_init__(self) -> None:
        _require_nonempty(self.cursor_name, "INVALID_PERFORMANCE_CURSOR_NAME")
        if self.schema_version != "PERFORMANCE_CURSOR_V1":
            raise ValueError("INVALID_PERFORMANCE_CURSOR_SCHEMA")
        if not isinstance(self.cursor, Mapping):
            raise ValueError("INVALID_PERFORMANCE_CURSOR")
        _require_nonnegative_int(self.updated_at_ms, "INVALID_PERFORMANCE_CURSOR_TIMESTAMP")
        value = dict(self.cursor)
        payload = canonical_json(value)
        object.__setattr__(self, "cursor", _deep_freeze(value))
        object.__setattr__(self, "cursor_json", payload)
        object.__setattr__(self, "cursor_sha256", hashlib.sha256(payload.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True, kw_only=True)
class PerformanceIngestRun:
    ingest_run_key: str
    mode: str
    source_artifact_class: str
    source_sha256: str
    acceptance_sha256: str | None
    source_schema_version: str
    input_count: int
    inserted_count: int
    replayed_count: int
    rejected_count: int
    accepted_count: int
    unscorable_count: int
    conflict_count: int
    first_source_identity: str | None
    last_source_identity: str | None
    first_cursor_json: Mapping[str, object] | None
    last_cursor_json: Mapping[str, object] | None
    status: str
    started_at_ms: int
    completed_at_ms: int | None
    schema_version: str = "PERFORMANCE_INGEST_RUN_V1"
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, **values: Any) -> Self:
        return cls(**values)

    def __post_init__(self) -> None:
        for name in ("ingest_run_key", "source_artifact_class", "source_schema_version"):
            _require_nonempty(getattr(self, name), f"INVALID_INGEST_{name.upper()}")
        if self.schema_version != "PERFORMANCE_INGEST_RUN_V1" or self.mode not in _INGEST_MODES or self.status not in _INGEST_STATUSES:
            raise ValueError("INVALID_PERFORMANCE_INGEST_ENUM")
        _require_sha256(self.source_sha256, "INVALID_INGEST_SOURCE_SHA256")
        if self.acceptance_sha256 is not None:
            _require_sha256(self.acceptance_sha256, "INVALID_INGEST_ACCEPTANCE_SHA256")
        for name in ("input_count", "inserted_count", "replayed_count", "rejected_count", "accepted_count", "unscorable_count", "conflict_count"):
            _require_nonnegative_int(getattr(self, name), f"INVALID_INGEST_{name.upper()}")
        if self.accepted_count + self.rejected_count != self.input_count or self.unscorable_count > self.accepted_count:
            raise ValueError("INVALID_PERFORMANCE_INGEST_COUNTS")
        for name in ("first_source_identity", "last_source_identity"):
            _require_optional_nonempty(getattr(self, name), f"INVALID_INGEST_{name.upper()}")
        _require_nonnegative_int(self.started_at_ms, "INVALID_INGEST_STARTED_AT")
        if self.completed_at_ms is not None:
            _require_nonnegative_int(self.completed_at_ms, "INVALID_INGEST_COMPLETED_AT")
            if self.completed_at_ms < self.started_at_ms:
                raise ValueError("INVALID_INGEST_COMPLETED_AT")
        if (self.status == "COMPLETE") != (self.completed_at_ms is not None):
            raise ValueError("INVALID_PERFORMANCE_INGEST_STATUS")
        first_cursor = (
            None if self.first_cursor_json is None
            else _deep_freeze(dict(self.first_cursor_json))
        )
        last_cursor = (
            None if self.last_cursor_json is None
            else _deep_freeze(dict(self.last_cursor_json))
        )
        object.__setattr__(self, "first_cursor_json", first_cursor)
        object.__setattr__(self, "last_cursor_json", last_cursor)
        payload = {
            name: getattr(self, name)
            for name in (
                "schema_version", "ingest_run_key", "mode", "source_artifact_class",
                "source_sha256", "acceptance_sha256", "source_schema_version",
                "input_count", "inserted_count", "replayed_count", "rejected_count",
                "accepted_count", "unscorable_count", "conflict_count",
                "first_source_identity", "last_source_identity", "first_cursor_json",
                "last_cursor_json", "status", "started_at_ms", "completed_at_ms",
            )
        }
        _set_payload(self, payload)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatchupClassification:
    catchup_key: str
    market_date: str
    classification: str
    reason_code: str
    market_id: str | None
    source_event_identity: str | None
    classified_at_ms: int
    provenance: Mapping[str, object]
    revision: int = 1
    supersedes_catchup_key: str | None = None
    schema_version: str = "PERFORMANCE_CATCHUP_V1"
    provenance_json: str = field(init=False, default="")
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, **values: Any) -> Self:
        return cls(**values)

    def __post_init__(self) -> None:
        _require_nonempty(self.catchup_key, "INVALID_CATCHUP_KEY")
        _require_date(self.market_date, "INVALID_CATCHUP_MARKET_DATE")
        _require_positive_int(self.revision, "INVALID_CATCHUP_REVISION")
        _require_optional_nonempty(
            self.supersedes_catchup_key,
            "INVALID_CATCHUP_SUPERSEDES_KEY",
        )
        if (self.revision == 1) != (self.supersedes_catchup_key is None):
            raise ValueError("INVALID_CATCHUP_SUPERSESSION")
        if self.schema_version != "PERFORMANCE_CATCHUP_V1" or self.classification not in _CATCHUP_CLASSIFICATIONS:
            raise ValueError("INVALID_CATCHUP_CLASSIFICATION")
        _require_nonempty(self.reason_code, "INVALID_CATCHUP_REASON_CODE")
        _require_optional_nonempty(self.market_id, "INVALID_CATCHUP_MARKET_ID")
        _require_optional_nonempty(self.source_event_identity, "INVALID_CATCHUP_SOURCE_EVENT")
        _require_nonnegative_int(self.classified_at_ms, "INVALID_CATCHUP_TIMESTAMP")
        provenance = _deep_freeze(dict(self.provenance))
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "provenance_json", canonical_json(provenance))
        payload = {
            "schema_version": self.schema_version,
            "catchup_key": self.catchup_key,
            "revision": self.revision,
            "supersedes_catchup_key": self.supersedes_catchup_key,
            "market_date": self.market_date,
            "classification": self.classification,
            "reason_code": self.reason_code,
            "market_id": self.market_id,
            "source_event_identity": self.source_event_identity,
            "classified_at_ms": self.classified_at_ms,
            "provenance": provenance,
        }
        _set_payload(self, payload)


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregateRevision:
    revision_key: str
    strategy_id: str
    source_view: str
    calculation_version: str
    source_ledger_revision: int
    source_ledger_sha256: str
    generated_at_ms: int
    provenance: Mapping[str, object]
    schema_version: str = "PERFORMANCE_MATERIALIZATION_REVISION_V1"
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, **values: Any) -> Self:
        return cls(**values)

    def __post_init__(self) -> None:
        _validate_materialization_identity(self.revision_key, self.strategy_id, self.source_view, self.calculation_version)
        if self.schema_version != "PERFORMANCE_MATERIALIZATION_REVISION_V1":
            raise ValueError("INVALID_MATERIALIZATION_REVISION_SCHEMA")
        _require_nonnegative_int(self.source_ledger_revision, "INVALID_SOURCE_LEDGER_REVISION")
        _require_sha256(self.source_ledger_sha256, "INVALID_SOURCE_LEDGER_SHA256")
        _require_nonnegative_int(self.generated_at_ms, "INVALID_MATERIALIZATION_TIMESTAMP")
        provenance = _deep_freeze(dict(self.provenance))
        object.__setattr__(self, "provenance", provenance)
        _set_payload(self, {
            "schema_version": self.schema_version,
            "revision_key": self.revision_key,
            "strategy_id": self.strategy_id,
            "source_view": self.source_view,
            "calculation_version": self.calculation_version,
            "source_ledger_revision": self.source_ledger_revision,
            "source_ledger_sha256": self.source_ledger_sha256,
            "generated_at_ms": self.generated_at_ms,
            "provenance": provenance,
        })


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregateRow:
    aggregate_key: str
    revision_key: str
    strategy_id: str
    source_view: str
    window_kind: str
    window_key: str
    calculation_version: str
    payload: Mapping[str, object]
    schema_version: str = "PERFORMANCE_AGGREGATE_V1"
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, **values: Any) -> Self:
        return cls(**values)

    def __post_init__(self) -> None:
        _validate_materialization_identity(self.aggregate_key, self.strategy_id, self.source_view, self.calculation_version)
        _require_nonempty(self.revision_key, "INVALID_AGGREGATE_REVISION_KEY")
        _require_nonempty(self.window_kind, "INVALID_AGGREGATE_WINDOW_KIND")
        _require_nonempty(self.window_key, "INVALID_AGGREGATE_WINDOW_KEY")
        if self.schema_version != "PERFORMANCE_AGGREGATE_V1":
            raise ValueError("INVALID_AGGREGATE_SCHEMA")
        payload = _deep_freeze(dict(self.payload))
        object.__setattr__(self, "payload", payload)
        _set_payload(self, payload)


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeseriesRow:
    timeseries_key: str
    revision_key: str
    strategy_id: str
    source_view: str
    series_kind: str
    period_key: str
    calculation_version: str
    observation_count: int
    observation_sha256: str
    payload: Mapping[str, object]
    schema_version: str = "PERFORMANCE_TIMESERIES_V1"
    payload_json: str = field(init=False, default="")
    payload_sha256: str = field(init=False, default="")

    @classmethod
    def create(cls, **values: Any) -> Self:
        return cls(**values)

    def __post_init__(self) -> None:
        _validate_materialization_identity(self.timeseries_key, self.strategy_id, self.source_view, self.calculation_version)
        _require_nonempty(self.revision_key, "INVALID_TIMESERIES_REVISION_KEY")
        if self.series_kind not in _SERIES_KINDS:
            raise ValueError("INVALID_TIMESERIES_KIND")
        _require_nonempty(self.period_key, "INVALID_TIMESERIES_PERIOD_KEY")
        if self.schema_version != "PERFORMANCE_TIMESERIES_V1":
            raise ValueError("INVALID_TIMESERIES_SCHEMA")
        _require_nonnegative_int(self.observation_count, "INVALID_TIMESERIES_OBSERVATION_COUNT")
        _require_sha256(self.observation_sha256, "INVALID_TIMESERIES_OBSERVATION_SHA256")
        payload = _deep_freeze(dict(self.payload))
        object.__setattr__(self, "payload", payload)
        _set_payload(self, payload)


def _set_payload(value: object, payload: object) -> None:
    payload_json = canonical_json(payload)
    object.__setattr__(value, "payload_json", payload_json)
    object.__setattr__(value, "payload_sha256", hashlib.sha256(payload_json.encode("utf-8")).hexdigest())


def _validate_materialization_identity(identity: str, strategy_id: str, source_view: str, calculation_version: str) -> None:
    _require_nonempty(identity, "INVALID_MATERIALIZATION_IDENTITY")
    _require_nonempty(strategy_id, "INVALID_MATERIALIZATION_STRATEGY_ID")
    if source_view not in _SOURCE_VIEWS:
        raise ValueError("INVALID_MATERIALIZATION_SOURCE_VIEW")
    _require_nonempty(calculation_version, "INVALID_MATERIALIZATION_CALCULATION_VERSION")


def _canonical_json_string(value: str, error_code: str) -> str:
    if type(value) is not str:
        raise ValueError(error_code)
    try:
        decoded = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(error_code) from None
    canonical = canonical_json(decoded)
    if value != canonical:
        raise ValueError(error_code)
    return canonical


def _normalize_json(value: object) -> object:
    if type(value) is float:
        raise ValueError("PERFORMANCE_BINARY_FLOAT_FORBIDDEN")
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("INVALID_PERFORMANCE_CANONICAL_JSON_KEY")
            normalized[key] = _normalize_json(item)
        return normalized
    elif isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    elif value is None or type(value) in (bool, int, str):
        return value
    raise ValueError("INVALID_PERFORMANCE_CANONICAL_JSON")


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _require_nonempty(value: object, error_code: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(error_code)


def _require_optional_nonempty(value: object, error_code: str) -> None:
    if value is not None:
        _require_nonempty(value, error_code)


def _require_nonnegative_int(value: object, error_code: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(error_code)


def _require_positive_int(value: object, error_code: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(error_code)


def _require_probability(value: object, error_code: str) -> None:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError(error_code)


def _require_sha256(value: object, error_code: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(error_code)


def _require_date(value: object, error_code: str) -> None:
    if type(value) is not str:
        raise ValueError(error_code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(error_code) from None
    if parsed.isoformat() != value:
        raise ValueError(error_code)
