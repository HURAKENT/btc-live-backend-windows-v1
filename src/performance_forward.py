from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.performance_models import (
    PerformanceCursor,
    PerformanceIngestRun,
    PerformanceObservation,
    PerformanceResolution,
    canonical_identity,
    canonical_sha256,
)
from src.performance_repository import PerformanceRepository, RepositoryWriteResult
from src.market_calendar import MarketCatchupClassifier, POST_BASELINE_START_DATE
from src.storage import SqliteReadStore, SqliteStore


FORWARD_CURSOR_NAME = "forward_performance:evaluations"
FORWARD_OBSERVATION_SCHEMA = "FORWARD_PERFORMANCE_OBSERVATION_SOURCE_V1"
FORWARD_SETTLEMENT_SCHEMA = "FORWARD_PERFORMANCE_SETTLEMENT_SOURCE_V1"
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ForwardObservationBatch:
    observations: list[PerformanceObservation]
    cursor: PerformanceCursor
    first_evaluation_id: int | None
    last_evaluation_id: int
    scanned_evaluation_count: int


@dataclass(frozen=True, slots=True)
class ForwardCycleReceipt:
    scanned_evaluation_count: int
    observation_inserted_count: int
    observation_replayed_count: int
    resolution_inserted_count: int
    resolution_replayed_count: int
    catchup_inserted_count: int
    catchup_replayed_count: int
    materialization_inserted_count: int
    materialization_replayed_count: int
    ingest_receipt_outcome: str | None
    first_evaluation_id: int | None
    last_evaluation_id: int


class ForwardPerformanceObservationBuilder:
    """Build forward performance observations from committed LIVE evaluations."""

    def __init__(
        self,
        *,
        store: SqliteStore | SqliteReadStore,
        observed_at_ms: int,
        project_root: Path | None = None,
    ) -> None:
        if type(store) not in (SqliteStore, SqliteReadStore):
            raise ValueError("INVALID_FORWARD_PERFORMANCE_STORE")
        if type(observed_at_ms) is not int or observed_at_ms < 0:
            raise ValueError("INVALID_FORWARD_OBSERVED_AT")
        self.store = store
        self.observed_at_ms = observed_at_ms
        self.project_root = Path(project_root or DEFAULT_PROJECT_ROOT)
        self._registry = _load_registry(self.project_root)

    def build_since(self, last_evaluation_id: int) -> ForwardObservationBatch:
        if type(last_evaluation_id) is not int or last_evaluation_id < 0:
            raise ValueError("INVALID_FORWARD_CURSOR_POSITION")
        rows = self.store.rows(
            """
            SELECT evaluation_id, evaluation_key, strategy_id, strategy_version,
                   status, input_snapshot_hash, evaluation_revision,
                   execution_eligible, evaluated_at_ms, payload_json,
                   origin, historical_signal_is_current_live_signal,
                   current_reevaluation_required
            FROM strategy_evaluations
            WHERE evaluation_id > ?
            ORDER BY evaluation_id ASC
            """,
            (last_evaluation_id,),
        )
        observations: list[PerformanceObservation] = []
        max_seen = last_evaluation_id
        first_seen: int | None = None
        for row in rows:
            evaluation_id = int(row[0])
            max_seen = max(max_seen, evaluation_id)
            first_seen = evaluation_id if first_seen is None else first_seen
            payload = _loads(row[9])
            if (
                row[10] != "LIVE"
                or int(row[11]) == 1
                or _is_infrastructure_evaluation(row[2], payload)
            ):
                continue
            observations.extend(self._build_observations(row, payload))

        cursor = PerformanceCursor.create(
            cursor_name=FORWARD_CURSOR_NAME,
            cursor={
                "last_evaluation_id": max_seen,
                "source": "strategy_evaluations",
            },
            updated_at_ms=self.observed_at_ms,
        )
        return ForwardObservationBatch(
            observations=observations,
            cursor=cursor,
            first_evaluation_id=first_seen,
            last_evaluation_id=max_seen,
            scanned_evaluation_count=len(rows),
        )

    def _build_observations(
        self,
        row: Sequence[Any],
        payload: Mapping[str, Any],
    ) -> list[PerformanceObservation]:
        evaluation = {
            "evaluation_id": int(row[0]),
            "evaluation_key": row[1],
            "strategy_id": row[2],
            "strategy_version": row[3],
            "status": row[4],
            "input_snapshot_hash": row[5],
            "evaluation_revision": int(row[6]),
            "execution_eligible": bool(row[7]),
            "evaluated_at_ms": int(row[8]),
            "payload": payload,
            "reason_code": "",
            "origin": row[10],
        }
        results = _result_payloads(evaluation["payload"])
        return [
            self._build_observation(row, evaluation, result, result_index=index)
            for index, result in enumerate(results)
        ]

    def _build_observation(
        self,
        row: Sequence[Any],
        evaluation: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        result_index: int,
    ) -> PerformanceObservation:
        schedule = self._schedule_for_evaluation(
            evaluation_id=evaluation["evaluation_id"],
            evaluation_key=evaluation["evaluation_key"],
            checkpoint_minutes=_optional_int(result.get("checkpoint_minutes")),
            strategy_id=evaluation["strategy_id"],
            strategy_version=evaluation["strategy_version"],
        )
        input_payload = _loads(schedule["input_payload_json"])
        input_sha256 = _sha256_or_compute(
            schedule["input_snapshot_hash"],
            schedule["input_payload_json"],
        )
        market_payload = self._market_payload(schedule["market_id"])
        market_date = (
            _string(result.get("market_date"))
            or _payload_market_date(input_payload)
            or _payload_market_date(market_payload)
        )
        if market_date is None:
            raise ValueError("FORWARD_MARKET_DATE_MISSING")
        side = _string(result.get("side"))
        if side not in {"YES", "NO"}:
            raise ValueError("FORWARD_EVALUATION_SIDE_MISSING")
        accepted = _accepted(result)
        selected_buckets = _selected_buckets(
            input_payload=input_payload,
            market_payload=market_payload,
            selected_indices=result.get("selected_bucket_indices", ()),
            accepted=accepted,
        )
        signal_identity = self._emitted_signal_identity(
            evaluation_key=evaluation["evaluation_key"],
            strategy_id=evaluation["strategy_id"],
            accepted=accepted,
        )
        price = _performance_price_micros(result) if accepted else None
        reference = _reference_price_micros(result) if accepted else None
        if not accepted:
            scoring_status = "REJECTED"
            scoring_reason = "EVALUATOR_REJECTED"
            price_basis = "NOT_APPLICABLE"
        elif price is None:
            scoring_status = "UNSCORABLE"
            scoring_reason = "UNSCORABLE_MISSING_PRICE"
            price_basis = "UNAVAILABLE"
        else:
            scoring_status = "RESOLUTION_PENDING"
            scoring_reason = "SETTLEMENT_PENDING"
            price_basis = "FORWARD_CAPTURED_CONTRACT"

        metadata = self._strategy_metadata(
            strategy_id=evaluation["strategy_id"],
            strategy_version=evaluation["strategy_version"],
            schedule=schedule,
        )
        parent_strategy_id = _string(result.get("parent_strategy_id")) or metadata.get("parent_strategy_id")
        source_decision_identity = (
            _string(result.get("source_decision_identity"))
            or _string(result.get("parent_decision_identity"))
            or _string(result.get("source_decision_sha256"))
        )
        logical_decision_key = canonical_identity(
            "performance-logical-decision",
            {
                "checkpoint_minutes": schedule["checkpoint_minutes"],
                "horizon": _horizon(schedule["checkpoint_minutes"]),
                "market_date": market_date,
                "market_id": schedule["market_id"],
                "parent_strategy_id": parent_strategy_id,
                "side": side,
                "source_decision_identity": source_decision_identity,
                "strategy_id": evaluation["strategy_id"],
                "strategy_version": evaluation["strategy_version"],
            },
        )
        source_result_sha256 = hashlib.sha256(
            (
                str(row[9])
                + "\n"
                + json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ).encode("utf-8")
        ).hexdigest()
        observation_key = canonical_identity(
            "forward-observation",
            {
                "evaluation_id": evaluation["evaluation_id"],
                "evaluation_key": evaluation["evaluation_key"],
                "input_sha256": input_sha256,
                "result_index": result_index,
            },
        )
        return PerformanceObservation.create(
            observation_key=observation_key,
            logical_decision_key=logical_decision_key,
            source_layer="FORWARD",
            strategy_id=evaluation["strategy_id"],
            strategy_version=evaluation["strategy_version"],
            family=metadata["family"],
            registry_index=metadata["registry_index"],
            activation_status=metadata["activation_status"],
            activation_reason_code=metadata["activation_reason_code"],
            market_id=schedule["market_id"],
            market_date=market_date,
            evaluation_key=evaluation["evaluation_key"],
            signal_identity_key=signal_identity,
            parent_strategy_id=parent_strategy_id,
            source_decision_identity=source_decision_identity,
            checkpoint_minutes=schedule["checkpoint_minutes"],
            horizon=_horizon(schedule["checkpoint_minutes"]),
            side=side,
            selected_buckets=selected_buckets,
            accepted=accepted,
            emitted=signal_identity is not None,
            reason_code=_reason_code(result, fallback=evaluation["reason_code"]),
            reference_price_micros=reference,
            performance_price_micros=price,
            performance_price_basis=price_basis,
            scoring_status=scoring_status,
            scoring_reason_code=scoring_reason,
            observed_at_ms=self.observed_at_ms,
            source_created_at_ms=evaluation["evaluated_at_ms"],
            provenance_run_id=f"forward:{self.observed_at_ms}",
            source_result_sha256=source_result_sha256,
            input_sha256=input_sha256,
        )

    def _schedule_for_evaluation(
        self,
        *,
        evaluation_id: int,
        evaluation_key: str,
        checkpoint_minutes: int | None,
        strategy_id: str,
        strategy_version: str,
    ) -> dict[str, Any]:
        rows = self.store.rows(
            """
            SELECT schedule_key, market_id, registry_index, strategy_id,
                   strategy_version, rule_spec_sha256, input_schema_version,
                   activation_status, checkpoint_minutes, capture_origin,
                   input_snapshot_hash, input_payload_json,
                   current_input_snapshot_hash, current_input_payload_json,
                   evaluation_id, current_reevaluation_evaluation_id
            FROM strategy_checkpoint_schedules
            WHERE (
                evaluation_id = ?
                OR current_reevaluation_evaluation_id = ?
                OR evaluation_key = ?
            )
            AND (? IS NULL OR checkpoint_minutes = ?)
            ORDER BY
                CASE
                    WHEN evaluation_id = ? THEN 0
                    WHEN current_reevaluation_evaluation_id = ? THEN 1
                    ELSE 2
                END,
                schedule_id ASC
            """,
            (
                evaluation_id,
                evaluation_id,
                evaluation_key,
                checkpoint_minutes,
                checkpoint_minutes,
                evaluation_id,
                evaluation_id,
            ),
        )
        if len(rows) != 1:
            raise ValueError("FORWARD_EVALUATION_CHECKPOINT_EVIDENCE_MISSING")
        item = rows[0]
        if item[3] != strategy_id or item[4] != strategy_version:
            raise ValueError("FORWARD_EVALUATION_CHECKPOINT_IDENTITY_MISMATCH")
        current_match = item[15] == evaluation_id
        input_hash = item[12] if current_match and item[12] else item[10]
        input_payload = item[13] if current_match and item[13] else item[11]
        if not input_payload:
            raise ValueError("FORWARD_EVALUATION_INPUT_EVIDENCE_MISSING")
        actual_input_hash = hashlib.sha256(input_payload.encode("utf-8")).hexdigest()
        if input_hash != actual_input_hash:
            raise ValueError("FORWARD_EVALUATION_INPUT_HASH_MISMATCH")
        if not current_match and item[9] not in (None, "LIVE"):
            raise ValueError("FORWARD_EVALUATION_RECOVERED_INPUT_FORBIDDEN")
        return {
            "schedule_key": item[0],
            "market_id": item[1],
            "registry_index": int(item[2]),
            "strategy_id": item[3],
            "strategy_version": item[4],
            "rule_spec_sha256": item[5],
            "input_schema_version": item[6],
            "activation_status": item[7],
            "checkpoint_minutes": int(item[8]),
            "input_snapshot_hash": input_hash,
            "input_payload_json": input_payload,
        }

    def _market_payload(self, market_id: str) -> Mapping[str, Any]:
        row = self.store.rows(
            "SELECT payload_json FROM market_catalog WHERE market_id = ?",
            (market_id,),
        )
        if not row:
            raise ValueError("FORWARD_MARKET_EVIDENCE_MISSING")
        return _loads(row[0][0])

    def _emitted_signal_identity(
        self,
        *,
        evaluation_key: str,
        strategy_id: str,
        accepted: bool,
    ) -> str | None:
        if not accepted:
            return None
        rows = self.store.rows(
            """
            SELECT identity_key
            FROM signals
            WHERE evaluation_key = ?
              AND strategy_id = ?
              AND origin = 'LIVE'
              AND infrastructure_only = 0
            ORDER BY signal_id ASC
            """,
            (evaluation_key, strategy_id),
        )
        if len(rows) > 1:
            raise ValueError("FORWARD_EVALUATION_MULTIPLE_EMITTED_SIGNALS")
        return rows[0][0] if rows else None

    def _strategy_metadata(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        schedule: Mapping[str, Any],
    ) -> dict[str, Any]:
        registry = self._registry.get(strategy_id)
        if registry is None or registry.get("version") != strategy_version:
            raise ValueError("FORWARD_STRATEGY_REGISTRY_MISMATCH")
        if int(registry.get("registry_index", -1)) != int(schedule["registry_index"]):
            raise ValueError("FORWARD_STRATEGY_REGISTRY_MISMATCH")
        if registry.get("activation_status") != schedule["activation_status"]:
            raise ValueError("FORWARD_STRATEGY_STATUS_MISMATCH")
        return {
            "activation_reason_code": registry["activation_reason_code"],
            "activation_status": registry["activation_status"],
            "family": registry["family"],
            "parent_strategy_id": registry.get("parent_strategy_id"),
            "registry_index": int(registry["registry_index"]),
        }


class ForwardSettlementProjector:
    """Project forward resolutions from already persisted public evidence."""

    def __init__(
        self,
        *,
        store: SqliteStore | SqliteReadStore,
        repository: PerformanceRepository,
        reconciled_at_ms: int,
    ) -> None:
        if type(store) not in (SqliteStore, SqliteReadStore):
            raise ValueError("INVALID_FORWARD_SETTLEMENT_STORE")
        if type(repository) is not PerformanceRepository:
            raise ValueError("INVALID_FORWARD_SETTLEMENT_REPOSITORY")
        if type(reconciled_at_ms) is not int or reconciled_at_ms < 0:
            raise ValueError("INVALID_FORWARD_SETTLEMENT_TIMESTAMP")
        self.store = store
        self.repository = repository
        self.reconciled_at_ms = reconciled_at_ms

    def project(self) -> list[PerformanceResolution]:
        resolved = self.repository.read_effective_resolutions()
        observations = [
            row
            for row in self.repository.read_observations(source_layer="FORWARD")
            if row.accepted
            and row.scoring_status == "RESOLUTION_PENDING"
        ]
        resolutions: list[PerformanceResolution] = []
        for observation in observations:
            previous = resolved.get(observation.observation_key)
            evidence = self._resolution_evidence(
                market_id=observation.market_id,
                market_date=observation.market_date,
            )
            if evidence is None:
                continue
            universe = self._bucket_universe(observation.market_id)
            winning = _canonical_winner_identity(
                evidence["winning_bucket_identity"], universe
            )
            won = (
                winning in observation.selected_buckets
                if observation.side == "YES"
                else winning not in observation.selected_buckets
            )
            settlement_identity = canonical_identity(
                "forward-settlement",
                {
                    "market_date": observation.market_date,
                    "market_id": observation.market_id,
                    "winning_bucket_identity": winning,
                },
            )
            revision = 1 if previous is None else previous.revision + 1
            resolution_key = canonical_identity(
                "forward-resolution",
                {
                    "observation_key": observation.observation_key,
                    "revision": revision,
                    "settlement_identity": settlement_identity,
                },
            )
            if previous is not None and (
                previous.settlement_identity == settlement_identity
                and previous.winning_bucket_identity == winning
            ):
                previous_provenance = _loads(previous.provenance_json)
                previous_source_key = _string(
                    previous_provenance.get("source_event_natural_key")
                )
                if (
                    evidence["supersedes_source_event_natural_key"]
                    != previous_source_key
                ):
                    continue
            resolutions.append(
                PerformanceResolution.create_for_observation(
                    observation,
                    resolution_key=resolution_key,
                    revision=revision,
                    supersedes_resolution_key=(
                        None if previous is None else previous.resolution_key
                    ),
                    settlement_identity=settlement_identity,
                    settlement_source_event_id=evidence["event_id"],
                    winning_bucket_identity=winning,
                    won=won,
                    resolution_date=observation.market_date,
                    resolved_at_ms=evidence["resolved_at_ms"],
                    provenance_json={
                        "reconciled_at_ms": self.reconciled_at_ms,
                        "schema_version": FORWARD_SETTLEMENT_SCHEMA,
                        "source_event_natural_key": evidence["natural_key"],
                        "source_event_payload_sha256": evidence["payload_sha256"],
                    },
                )
            )
        return resolutions

    def _resolution_evidence(
        self,
        *,
        market_id: str,
        market_date: str,
    ) -> dict[str, Any] | None:
        rows = self.store.rows(
            """
            SELECT event_id, natural_key, event_type, payload_json,
                   payload_sha256, source_timestamp_ms
            FROM source_events
            WHERE event_type IN (
                'POLYMARKET_MARKET_RESOLVED',
                'POLYMARKET_DAILY_RANGE_MARKET_RESOLVED',
                'MARKET_RESOLVED'
            )
            ORDER BY event_id ASC
            """
        )
        matches: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row[3])
            payload_market = _string(payload.get("market_id")) or _string(payload.get("event_id"))
            payload_date = _payload_market_date(payload)
            if payload_market != market_id or payload_date != market_date:
                continue
            if payload.get("resolved") is not True:
                raise ValueError("FORWARD_RESOLUTION_INVALID")
            count = payload.get("winning_bucket_count", 1)
            winning = _string(payload.get("winning_bucket_identity")) or _string(
                payload.get("winning_bucket_id")
            )
            if count != 1 or winning is None:
                raise ValueError("FORWARD_RESOLUTION_AMBIGUOUS")
            matches.append(
                {
                    "event_id": int(row[0]),
                    "natural_key": row[1],
                    "payload_sha256": row[4],
                    "resolved_at_ms": _optional_int(payload.get("resolved_at_ms")) or int(row[5]),
                    "supersedes_source_event_natural_key": _string(
                        payload.get("supersedes_source_event_natural_key")
                    ),
                    "winning_bucket_identity": winning,
                }
            )
        if not matches:
            return None
        current = matches[0]
        for candidate in matches[1:]:
            if candidate["winning_bucket_identity"] == current["winning_bucket_identity"]:
                supersedes = candidate["supersedes_source_event_natural_key"]
                if supersedes == current["natural_key"]:
                    current = candidate
                elif supersedes is not None:
                    raise ValueError("FORWARD_RESOLUTION_AMBIGUOUS")
                continue
            if candidate["supersedes_source_event_natural_key"] != current["natural_key"]:
                raise ValueError("FORWARD_RESOLUTION_AMBIGUOUS")
            current = candidate
        return current

    def _bucket_universe(self, market_id: str) -> tuple[tuple[str, str], ...]:
        rows = self.store.rows(
            "SELECT payload_json FROM market_catalog WHERE market_id = ?",
            (market_id,),
        )
        if len(rows) != 1:
            raise ValueError("FORWARD_MARKET_EVIDENCE_MISSING")
        payload = _loads(rows[0][0])
        outcomes = payload.get("outcomes")
        if (
            not isinstance(outcomes, Sequence)
            or isinstance(outcomes, (str, bytes))
            or len(outcomes) != 11
            or any(not isinstance(item, str) or not item for item in outcomes)
        ):
            raise ValueError("FORWARD_MARKET_BUCKET_UNIVERSE_MISSING")
        return tuple(
            (title, hashlib.sha256(title.encode("utf-8")).hexdigest())
            for title in outcomes
        )


class ForwardPerformanceCycle:
    """One bounded forward performance refresh over local persisted evidence."""

    def __init__(
        self,
        *,
        store: SqliteStore,
        repository: PerformanceRepository,
        observed_at_ms: int,
        project_root: Path | None = None,
        catchup_end_date: str | None = None,
    ) -> None:
        if type(store) is not SqliteStore:
            raise ValueError("INVALID_FORWARD_CYCLE_STORE")
        if type(repository) is not PerformanceRepository:
            raise ValueError("INVALID_FORWARD_CYCLE_REPOSITORY")
        self.store = store
        self.repository = repository
        self.observed_at_ms = observed_at_ms
        self.project_root = Path(project_root or DEFAULT_PROJECT_ROOT)
        if catchup_end_date is not None:
            _require_date(catchup_end_date)
        self.catchup_end_date = catchup_end_date

    def run_once(self) -> ForwardCycleReceipt:
        current = self.repository.read_cursor(FORWARD_CURSOR_NAME)
        last_evaluation_id = (
            0
            if current is None
            else int(current.cursor.get("last_evaluation_id", 0))
        )
        cursor_updated_at_ms = (
            self.observed_at_ms
            if current is None
            else max(self.observed_at_ms, current.updated_at_ms + 1)
        )
        batch = ForwardPerformanceObservationBuilder(
            store=self.store,
            observed_at_ms=cursor_updated_at_ms,
            project_root=self.project_root,
        ).build_since(last_evaluation_id)
        ingest_result: RepositoryWriteResult | None = None
        if batch.scanned_evaluation_count > 0:
            ingest = _forward_ingest_run(
                batch=batch,
                observed_at_ms=cursor_updated_at_ms,
            )
            observation_results, ingest_result = (
                self.repository.append_observations_cursor_and_ingest_run(
                    batch.observations,
                    batch.cursor,
                    ingest,
                )
            )
        else:
            observation_results = []
        resolutions = ForwardSettlementProjector(
            store=self.store,
            repository=self.repository,
            reconciled_at_ms=self.observed_at_ms,
        ).project()
        resolution_results: list[RepositoryWriteResult] = []
        if resolutions:
            resolution_results, _ = self.repository.append_resolutions_and_ingest_run(
                resolutions,
                _forward_resolution_ingest_run(
                    resolutions=resolutions,
                    observed_at_ms=self.observed_at_ms,
                ),
            )
        catchup_results: list[RepositoryWriteResult] = []
        if self.catchup_end_date is not None:
            for classification in MarketCatchupClassifier(
                store=self.store,
                classified_at_ms=self.observed_at_ms,
            ).classify_range(
                start_date=POST_BASELINE_START_DATE,
                end_date=self.catchup_end_date,
            ):
                catchup_results.append(
                    self.repository.append_catchup_classification(classification)
                )
        materialization_results = {"inserted": 0, "replayed": 0}
        if self.store.count("strategy_performance_observations") > 0:
            from src.performance_engine import StrategyPerformanceEngine

            materialization_results = StrategyPerformanceEngine(
                project_root=self.project_root,
                repository=self.repository,
            ).refresh_all(as_of_date=_as_of_date(self.observed_at_ms))
        return ForwardCycleReceipt(
            scanned_evaluation_count=batch.scanned_evaluation_count,
            observation_inserted_count=sum(row.inserted for row in observation_results),
            observation_replayed_count=sum(row.outcome == "REPLAYED" for row in observation_results),
            resolution_inserted_count=sum(row.inserted for row in resolution_results),
            resolution_replayed_count=sum(row.outcome == "REPLAYED" for row in resolution_results),
            catchup_inserted_count=sum(row.inserted for row in catchup_results),
            catchup_replayed_count=sum(row.outcome == "REPLAYED" for row in catchup_results),
            materialization_inserted_count=materialization_results["inserted"],
            materialization_replayed_count=materialization_results["replayed"],
            ingest_receipt_outcome=None if ingest_result is None else ingest_result.outcome,
            first_evaluation_id=batch.first_evaluation_id,
            last_evaluation_id=batch.last_evaluation_id,
        )


def _loads(payload_json: str) -> Mapping[str, Any]:
    payload = json.loads(payload_json)
    if not isinstance(payload, Mapping):
        raise ValueError("INVALID_FORWARD_PAYLOAD")
    return payload


def _result_payloads(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    value = payload.get("results", payload.get("result"))
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if any(not isinstance(item, Mapping) for item in value):
            raise ValueError("FORWARD_EVALUATION_RESULT_MISSING")
        return tuple(value)
    raise ValueError("FORWARD_EVALUATION_RESULT_MISSING")


def _is_infrastructure_evaluation(
    strategy_id: object,
    payload: Mapping[str, Any],
) -> bool:
    if strategy_id == "CANARY_SYNC_READY_V1":
        return True
    if payload.get("canary_infrastructure_only") is True:
        return True
    return payload.get("infrastructure_only") is True


def _accepted(result: Mapping[str, Any]) -> bool:
    value = result.get("accepted")
    if type(value) is not bool:
        raise ValueError("FORWARD_EVALUATION_ACCEPTED_MISSING")
    return value


def _reason_code(result: Mapping[str, Any], *, fallback: str) -> str:
    return _string(result.get("reason_code")) or _string(result.get("reason")) or fallback or "UNKNOWN"


def _horizon(checkpoint_minutes: int) -> str:
    if type(checkpoint_minutes) is not int or checkpoint_minutes <= 0:
        raise ValueError("INVALID_FORWARD_CHECKPOINT_MINUTES")
    return f"T-{checkpoint_minutes}m"


def _canonical_winner_identity(
    value: str,
    universe: Sequence[tuple[str, str]],
) -> str:
    matches = [identity for title, identity in universe if value in {title, identity}]
    if len(matches) != 1:
        raise ValueError("FORWARD_RESOLUTION_BUCKET_MAPPING_INVALID")
    return matches[0]


def _selected_buckets(
    *,
    input_payload: Mapping[str, Any],
    market_payload: Mapping[str, Any],
    selected_indices: object,
    accepted: bool,
) -> tuple[str, ...]:
    if selected_indices is None:
        selected_indices = ()
    if not isinstance(selected_indices, Sequence) or isinstance(selected_indices, (str, bytes)):
        raise ValueError("FORWARD_SELECTED_BUCKETS_INVALID")
    by_index = _bucket_identity_map(input_payload, market_payload)
    selected: list[str] = []
    for raw_index in selected_indices:
        if type(raw_index) is not int:
            raise ValueError("FORWARD_SELECTED_BUCKET_INDEX_INVALID")
        identity = by_index.get(raw_index)
        if identity is None:
            raise ValueError("FORWARD_SELECTED_BUCKET_SOURCE_MISSING")
        selected.append(identity)
    if accepted and not selected:
        raise ValueError("FORWARD_ACCEPTED_BUCKETS_MISSING")
    return tuple(selected)


def _bucket_identity_map(
    input_payload: Mapping[str, Any],
    market_payload: Mapping[str, Any],
) -> dict[int, str]:
    buckets = input_payload.get("buckets")
    if not isinstance(buckets, Sequence) or isinstance(buckets, (str, bytes)):
        raise ValueError("FORWARD_INPUT_BUCKETS_MISSING")
    outcomes = market_payload.get("outcomes")
    if (
        not isinstance(outcomes, Sequence)
        or isinstance(outcomes, (str, bytes))
        or len(outcomes) != len(buckets)
        or any(not isinstance(item, str) or not item for item in outcomes)
    ):
        raise ValueError("FORWARD_MARKET_BUCKET_UNIVERSE_MISSING")
    result: dict[int, str] = {}
    for fallback_index, bucket in enumerate(buckets):
        if not isinstance(bucket, Mapping):
            raise ValueError("FORWARD_INPUT_BUCKET_INVALID")
        index = bucket.get("bucket_index", fallback_index)
        if type(index) is not int:
            raise ValueError("FORWARD_INPUT_BUCKET_INDEX_INVALID")
        if index != fallback_index:
            raise ValueError("FORWARD_INPUT_BUCKET_INDEX_INVALID")
        result[index] = hashlib.sha256(outcomes[index].encode("utf-8")).hexdigest()
    return result


def _performance_price_micros(result: Mapping[str, Any]) -> int | None:
    for key in (
        "performance_price_micros",
        "captured_contract_price_micros",
        "stressed_reference_cost_micros",
    ):
        value = result.get(key)
        if value is not None:
            return _probability_micros(value)
    for key in (
        "performance_price",
        "captured_contract_price",
        "stressed_reference_cost",
    ):
        value = result.get(key)
        if value is not None:
            return _probability_micros(value)
    return None


def _reference_price_micros(result: Mapping[str, Any]) -> int | None:
    for key in ("reference_price_micros", "market_probability_micros"):
        value = result.get(key)
        if value is not None:
            return _probability_micros(value)
    for key in ("reference_price", "market_probability"):
        value = result.get(key)
        if value is not None:
            return _probability_micros(value)
    return None


def _probability_micros(value: object) -> int:
    if type(value) is int:
        micros = value
    else:
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("INVALID_FORWARD_PRICE") from None
        micros = int((decimal * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not 0 <= micros <= 1_000_000:
        raise ValueError("INVALID_FORWARD_PRICE")
    return micros


def _sha256_or_compute(value: object, payload_json: str) -> str:
    if isinstance(value, str) and len(value) == 64:
        return value
    return canonical_sha256(json.loads(payload_json))


def _payload_market_date(payload: Mapping[str, Any]) -> str | None:
    for key in ("market_date", "date"):
        value = _string(payload.get(key))
        if value:
            return value[:10]
    resolution = _string(payload.get("resolution_utc"))
    if resolution:
        return resolution[:10]
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _require_date(value: str) -> None:
    if type(value) is not str:
        raise ValueError("INVALID_FORWARD_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("INVALID_FORWARD_DATE") from None
    if parsed.isoformat() != value:
        raise ValueError("INVALID_FORWARD_DATE")


def _as_of_date(observed_at_ms: int) -> str:
    if type(observed_at_ms) is not int or observed_at_ms < 0:
        raise ValueError("INVALID_FORWARD_OBSERVED_AT")
    return datetime.fromtimestamp(
        observed_at_ms / 1000,
        tz=timezone.utc,
    ).date().isoformat()


def _forward_ingest_run(
    *,
    batch: ForwardObservationBatch,
    observed_at_ms: int,
) -> PerformanceIngestRun:
    observations = batch.observations
    source_sha256 = canonical_sha256(
        {
            "cursor": dict(batch.cursor.cursor),
            "observations": [row.payload_sha256 for row in observations],
            "scanned_evaluation_count": batch.scanned_evaluation_count,
        }
    )
    return PerformanceIngestRun.create(
        ingest_run_key=canonical_identity(
            "performance-ingest-forward",
            {
                "cursor_sha256": batch.cursor.cursor_sha256,
                "observed_at_ms": observed_at_ms,
                "source_sha256": source_sha256,
            },
        ),
        mode="FORWARD_INCREMENTAL",
        source_artifact_class="SQLITE_LIVE_EVALUATION_CURSOR",
        source_sha256=source_sha256,
        acceptance_sha256=None,
        source_schema_version=FORWARD_OBSERVATION_SCHEMA,
        input_count=len(observations),
        inserted_count=len(observations),
        replayed_count=0,
        rejected_count=sum(not row.accepted for row in observations),
        accepted_count=sum(row.accepted for row in observations),
        unscorable_count=sum(row.scoring_status == "UNSCORABLE" for row in observations),
        conflict_count=0,
        first_source_identity=observations[0].observation_key if observations else None,
        last_source_identity=observations[-1].observation_key if observations else None,
        first_cursor_json=(
            None
            if batch.first_evaluation_id is None
            else {"evaluation_id": batch.first_evaluation_id}
        ),
        last_cursor_json=dict(batch.cursor.cursor),
        status="COMPLETE",
        started_at_ms=observed_at_ms,
        completed_at_ms=observed_at_ms,
    )


def _forward_resolution_ingest_run(
    *,
    resolutions: Sequence[PerformanceResolution],
    observed_at_ms: int,
) -> PerformanceIngestRun:
    source_sha256 = canonical_sha256(
        [row.payload_sha256 for row in resolutions]
    )
    return PerformanceIngestRun.create(
        ingest_run_key=canonical_identity(
            "performance-ingest-forward-resolution",
            {"source_sha256": source_sha256},
        ),
        mode="FORWARD_INCREMENTAL",
        source_artifact_class="SQLITE_PUBLIC_SETTLEMENT_EVENTS",
        source_sha256=source_sha256,
        acceptance_sha256=None,
        source_schema_version=FORWARD_SETTLEMENT_SCHEMA,
        input_count=len(resolutions),
        inserted_count=len(resolutions),
        replayed_count=0,
        rejected_count=0,
        accepted_count=len(resolutions),
        unscorable_count=0,
        conflict_count=0,
        first_source_identity=resolutions[0].resolution_key,
        last_source_identity=resolutions[-1].resolution_key,
        first_cursor_json=None,
        last_cursor_json=None,
        status="COMPLETE",
        started_at_ms=observed_at_ms,
        completed_at_ms=observed_at_ms,
    )


def _load_registry(project_root: Path) -> dict[str, dict[str, Any]]:
    path = project_root / "reports" / "STRATEGY_47_STATUS_MATRIX.json"
    if not path.exists():
        raise ValueError("FORWARD_STRATEGY_REGISTRY_MISSING")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("strategies")
    if not isinstance(rows, list):
        raise ValueError("FORWARD_STRATEGY_REGISTRY_INVALID")
    registry: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("strategy_id"), str):
            raise ValueError("FORWARD_STRATEGY_REGISTRY_INVALID")
        copied = dict(row)
        copied["family"] = _family_from_registry(copied)
        if copied["strategy_id"] in registry:
            raise ValueError("FORWARD_STRATEGY_REGISTRY_INVALID")
        registry[copied["strategy_id"]] = copied
    if len(registry) != 47:
        raise ValueError("FORWARD_STRATEGY_REGISTRY_INVALID")
    return registry


def _family_from_registry(row: Mapping[str, Any]) -> str:
    if row.get("version") == "V2":
        return "VOLATILITY_V2"
    evaluator = row.get("evaluator_key")
    if evaluator == "STRICT_A_COMPOSED_V1":
        return "STRICT_A"
    if evaluator == "PF1_COMPOSED_V1":
        return "PF1"
    return "HISTORICAL_V1"


def _family(strategy_id: str, strategy_version: str) -> str:
    if strategy_version == "V2" or strategy_id.endswith("_V2_VOL"):
        return "VOLATILITY_V2"
    if strategy_id.startswith("YES_STRICT_A"):
        return "STRICT_A"
    if "PF1" in strategy_id:
        return "PF1"
    return "HISTORICAL_V1"


def _activation_reason(activation_status: str) -> str:
    if activation_status == "ENABLED_OPERATIONAL":
        return "CURRENT_PRODUCTION_EVALUATOR"
    if activation_status == "DISABLED_RESEARCH_ONLY":
        return "RESEARCH_ONLY"
    return activation_status
