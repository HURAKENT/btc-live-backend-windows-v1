from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models import SignalRecord, StrategyEvaluation
from src.storage import SqliteStore
from src.strategy_dispatch import (
    StrategyDispatcher,
    V1ExecutableCheckpointInput,
    V1StrategyDispatchRequest,
)
from src.strategy_v1 import (
    BucketInput,
    Pf1SnapshotEvidence,
    StrictPriceHistoryEvidence,
    V1Evaluation,
    V1_IDENTITY_POLICIES,
)


_C5_ACCEPTANCE_PATH = Path("reports/C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json")
_C5_MATRIX_PATH = Path("reports/STRATEGY_47_STATUS_MATRIX.json")
_C5_ACCEPTANCE_SHA256 = (
    "ca05d61430047e6dad6774ae6243abed6dab1d2532641bc66977f0b5d349de9e"
)
_C5_MATRIX_SHA256 = (
    "1594ae25f3ce17d77eb200f088f7b6287a7745a95e6cc6d0e1babf03a1e40cd0"
)
_SHA64 = re.compile(r"[0-9a-f]{64}")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_ACTIVE_RUNTIME_STATE = "LIVE_READY"
_EXECUTABLE_INPUT = "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1"
_MISSING_DEPTH_INPUT = "C6_MISSING_HISTORICAL_DEPTH_V1"
_CLAIM_LEASE_MS = 30_000
_OPERATIONAL_EVALUATOR_STRATEGY_IDS = frozenset(
    {
        "YES_STRICT_A_OPERATIONAL",
        "YES_STRICT_A_T30",
        "YES_STRICT_A_T60",
    }
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
        raise ValueError("C6_INVALID_CANONICAL_JSON") from exc


def _decode_canonical_json(value: str, code: str) -> object:
    if type(value) is not str:
        raise ValueError(code)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(code) from None
    if _canonical_json(decoded) != value:
        raise ValueError(code)
    return decoded


def _load_json_bytes(path: Path, expected_sha256: str) -> tuple[bytes, dict[str, Any]]:
    if not path.is_file():
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    payload = path.read_bytes()
    if _sha256(payload) != expected_sha256:
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("C6_C5_PROVENANCE_MISMATCH") from None
    if type(decoded) is not dict:
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    return payload, decoded


@dataclass(frozen=True, slots=True)
class C5SchedulerBinding:
    strategy_id: str
    registry_index: int
    strategy_version: str
    rule_spec_sha256: str
    input_schema_version: str
    activation_status: str


@dataclass(frozen=True, slots=True)
class C5SchedulerContract:
    enabled: tuple[C5SchedulerBinding, ...]
    acceptance_sha256: str
    matrix_sha256: str
    source_commit: str


def load_c5_scheduler_contract(project_root: Path) -> C5SchedulerContract:
    if not isinstance(project_root, Path):
        raise ValueError("C6_INVALID_PROJECT_ROOT")
    _, acceptance = _load_json_bytes(
        project_root / _C5_ACCEPTANCE_PATH,
        _C5_ACCEPTANCE_SHA256,
    )
    _, matrix = _load_json_bytes(
        project_root / _C5_MATRIX_PATH,
        _C5_MATRIX_SHA256,
    )
    if (
        acceptance.get("status") != "C5_STRATEGY_47_ACTIVATION_PASS"
        or acceptance.get("acceptance_pass") is not True
        or acceptance.get("strategy_count") != 47
        or acceptance.get("classified_count") != 47
        or acceptance.get("unknown_count") != 0
        or acceptance.get("paper_execution_authorized") is not False
        or acceptance.get("trading_approval") is not False
        or matrix.get("schema_version") != "BTC_STRATEGY_47_STATUS_MATRIX_V3"
        or matrix.get("paper_execution_authorized") is not False
    ):
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    source_commit = matrix.get("source_commit")
    if (
        type(source_commit) is not str
        or _SHA40.fullmatch(source_commit) is None
        or acceptance.get("source_commit") != source_commit
    ):
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    rows = matrix.get("strategies")
    if type(rows) is not list or len(rows) != 47:
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    enabled: list[C5SchedulerBinding] = []
    for row in rows:
        if type(row) is not dict:
            raise ValueError("C6_C5_PROVENANCE_MISMATCH")
        if row.get("activation_status") != "PAPER_EVALUATION_ENABLED":
            continue
        values = (
            row.get("strategy_id"),
            row.get("version"),
            row.get("rule_spec_sha256"),
            row.get("input_schema_version"),
        )
        if (
            any(type(value) is not str or not value for value in values)
            or type(row.get("registry_index")) is not int
            or row.get("version") != "V1"
            or row.get("input_schema_version") != _EXECUTABLE_INPUT
            or _SHA64.fullmatch(row["rule_spec_sha256"]) is None
            or row.get("paper_eligible") is not True
            or row.get("parity_status") != "PARITY_PASS"
        ):
            raise ValueError("C6_C5_PROVENANCE_MISMATCH")
        enabled.append(
            C5SchedulerBinding(
                strategy_id=row["strategy_id"],
                registry_index=row["registry_index"],
                strategy_version=row["version"],
                rule_spec_sha256=row["rule_spec_sha256"],
                input_schema_version=row["input_schema_version"],
                activation_status=row["activation_status"],
            )
        )
    if (
        len(enabled) != 8
        or tuple(item.registry_index for item in enabled)
        != tuple(sorted(item.registry_index for item in enabled))
        or {item.strategy_id for item in enabled}
        != {
            "YES_PF1_OPERATIONAL",
            "YES_PF1_T30",
            "YES_PF1_T60",
            "YES_PF1_T6H",
            "YES_PF1_T8H",
            "YES_STRICT_A_OPERATIONAL",
            "YES_STRICT_A_T30",
            "YES_STRICT_A_T60",
        }
    ):
        raise ValueError("C6_C5_PROVENANCE_MISMATCH")
    return C5SchedulerContract(
        enabled=tuple(enabled),
        acceptance_sha256=_C5_ACCEPTANCE_SHA256,
        matrix_sha256=_C5_MATRIX_SHA256,
        source_commit=source_commit,
    )


@dataclass(frozen=True, slots=True)
class CheckpointSchedule:
    schedule_id: int
    schedule_key: str
    evaluation_key: str
    market_id: str
    market_identity_sha256: str
    resolution_ms: int
    registry_index: int
    strategy_id: str
    strategy_version: str
    rule_spec_sha256: str
    input_schema_version: str
    activation_status: str
    activation_source_commit: str
    c5_acceptance_sha256: str
    c5_status_matrix_sha256: str
    checkpoint_minutes: int
    due_at_ms: int
    state: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class DueCheckpoint:
    schedule: CheckpointSchedule
    origin: str
    poller_id: str


@dataclass(frozen=True, slots=True)
class CaptureResult:
    state: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class CapturedCheckpoint:
    checkpoint_minutes: int
    input_snapshot_hash: str
    input_payload_json: str
    origin: str


@dataclass(frozen=True, slots=True)
class DispatchComposition:
    market_id: str
    strategy_id: str
    input_schema_version: str
    checkpoints: tuple[CapturedCheckpoint, ...]


def encode_executable_checkpoint_input(
    value: V1ExecutableCheckpointInput,
) -> tuple[str, str]:
    if type(value) is not V1ExecutableCheckpointInput:
        raise ValueError("C6_INVALID_EXECUTABLE_CHECKPOINT_INPUT")
    payload = {
        "buckets": [asdict(bucket) for bucket in value.buckets],
        "checkpoint_minutes": value.checkpoint_minutes,
        "pf1_snapshot_evidence": (
            None
            if value.pf1_snapshot_evidence is None
            else asdict(value.pf1_snapshot_evidence)
        ),
        "prior_position": value.prior_position,
        "schema_version": _EXECUTABLE_INPUT,
        "strict_price_history_evidence": (
            None
            if value.strict_price_history_evidence is None
            else asdict(value.strict_price_history_evidence)
        ),
    }
    payload_json = _canonical_json(payload)
    return payload_json, _sha256(payload_json.encode("utf-8"))


def decode_executable_checkpoint_input(
    checkpoint: CapturedCheckpoint,
) -> V1ExecutableCheckpointInput:
    if type(checkpoint) is not CapturedCheckpoint:
        raise ValueError("C6_INVALID_CAPTURED_CHECKPOINT")
    payload = _decode_canonical_json(
        checkpoint.input_payload_json,
        "C6_INVALID_EXECUTABLE_CHECKPOINT_JSON",
    )
    if type(payload) is not dict or set(payload) != {
        "buckets",
        "checkpoint_minutes",
        "pf1_snapshot_evidence",
        "prior_position",
        "schema_version",
        "strict_price_history_evidence",
    }:
        raise ValueError("C6_INVALID_EXECUTABLE_CHECKPOINT_JSON")
    if (
        payload["schema_version"] != _EXECUTABLE_INPUT
        or payload["checkpoint_minutes"] != checkpoint.checkpoint_minutes
        or type(payload["buckets"]) is not list
    ):
        raise ValueError("C6_INVALID_EXECUTABLE_CHECKPOINT_JSON")
    try:
        buckets = tuple(BucketInput(**row) for row in payload["buckets"])
        strict = (
            None
            if payload["strict_price_history_evidence"] is None
            else StrictPriceHistoryEvidence(
                **payload["strict_price_history_evidence"]
            )
        )
        pf1 = (
            None
            if payload["pf1_snapshot_evidence"] is None
            else Pf1SnapshotEvidence(**payload["pf1_snapshot_evidence"])
        )
        return V1ExecutableCheckpointInput(
            checkpoint_minutes=payload["checkpoint_minutes"],
            buckets=buckets,
            prior_position=payload["prior_position"],
            strict_price_history_evidence=strict,
            pf1_snapshot_evidence=pf1,
        )
    except (TypeError, ValueError):
        raise ValueError("C6_INVALID_EXECUTABLE_CHECKPOINT_JSON") from None


@dataclass(frozen=True, slots=True)
class CompletionResult:
    inserted: bool
    evaluation_id: int
    signal_id: int | None
    outbox_id: int | None
    current_reevaluation_required: bool


@dataclass(frozen=True, slots=True)
class PendingCurrentReevaluation:
    evaluation_group_key: str
    market_id: str
    strategy_id: str
    strategy_version: str
    rule_spec_sha256: str
    poller_id: str | None = None


_SCHEDULE_COLUMNS = (
    "schedule_id,schedule_key,evaluation_key,market_id,"
    "market_identity_sha256,resolution_ms,registry_index,strategy_id,"
    "strategy_version,rule_spec_sha256,input_schema_version,"
    "activation_status,activation_source_commit,c5_acceptance_sha256,"
    "c5_status_matrix_sha256,checkpoint_minutes,due_at_ms,state,created_at_ms"
)


def _schedule_from_row(row: tuple[Any, ...]) -> CheckpointSchedule:
    if len(row) != 19:
        raise RuntimeError("C6_INVALID_SCHEDULE_ROW")
    return CheckpointSchedule(*row)


def _resolution_ms(payload_json: str, payload_sha256: str) -> int:
    if type(payload_sha256) is not str or _SHA64.fullmatch(payload_sha256) is None:
        raise ValueError("MARKET_IDENTITY_HASH_MISMATCH")
    if _sha256(payload_json.encode("utf-8")) != payload_sha256:
        raise ValueError("MARKET_IDENTITY_HASH_MISMATCH")
    decoded = _decode_canonical_json(payload_json, "INVALID_MARKET_IDENTITY_JSON")
    if type(decoded) is not dict or type(decoded.get("resolution_utc")) is not str:
        raise ValueError("INVALID_MARKET_IDENTITY_RESOLUTION")
    try:
        resolution = datetime.fromisoformat(
            decoded["resolution_utc"].replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError("INVALID_MARKET_IDENTITY_RESOLUTION") from None
    if resolution.tzinfo is None:
        raise ValueError("INVALID_MARKET_IDENTITY_RESOLUTION")
    resolution = resolution.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = resolution - epoch
    total_microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if total_microseconds < 0 or total_microseconds % 1_000:
        raise ValueError("INVALID_MARKET_IDENTITY_RESOLUTION")
    return total_microseconds // 1_000


def _identity_key(kind: str, payload: dict[str, object]) -> str:
    return _sha256(
        _canonical_json(
            {"kind": kind, "schema_version": 1, **payload}
        ).encode("utf-8")
    )


class CheckpointScheduler:
    def __init__(self, *, project_root: Path, store: SqliteStore) -> None:
        if not isinstance(project_root, Path) or type(store) is not SqliteStore:
            raise ValueError("C6_INVALID_SCHEDULER_DEPENDENCY")
        self._project_root = project_root.resolve()
        self._store = store
        self._connection: sqlite3.Connection = store._connection
        self._activation = load_c5_scheduler_contract(self._project_root)
        self._operational_bindings = tuple(
            binding
            for binding in self._activation.enabled
            if binding.strategy_id in _OPERATIONAL_EVALUATOR_STRATEGY_IDS
        )
        if {
            binding.strategy_id for binding in self._operational_bindings
        } != _OPERATIONAL_EVALUATOR_STRATEGY_IDS:
            raise ValueError("C6_OPERATIONAL_EVALUATOR_PROJECTION_MISMATCH")

    def register_market(
        self,
        *,
        market_id: str,
        created_at_ms: int,
    ) -> tuple[CheckpointSchedule, ...]:
        if type(market_id) is not str or not market_id:
            raise ValueError("C6_INVALID_MARKET_ID")
        if type(created_at_ms) is not int or created_at_ms < 0:
            raise ValueError("C6_INVALID_CREATED_AT")
        market = self._connection.execute(
            "SELECT payload_json,payload_sha256 FROM market_catalog WHERE market_id=?",
            (market_id,),
        ).fetchone()
        if market is None:
            raise ValueError("C6_MARKET_IDENTITY_MISSING")
        payload_json, market_hash = market
        expected_rows = self._expected_schedule_rows(
            market_id=market_id,
            payload_json=payload_json,
            market_hash=market_hash,
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_schedule_rows(expected_rows, created_at_ms)
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return self._read_market_schedules(market_id)

    def persist_market_and_register(
        self,
        *,
        market_id: str,
        market_identity_json: str,
        market_identity_sha256: str,
        created_at_ms: int,
    ) -> tuple[CheckpointSchedule, ...]:
        if type(market_id) is not str or not market_id:
            raise ValueError("C6_INVALID_MARKET_ID")
        if type(created_at_ms) is not int or created_at_ms < 0:
            raise ValueError("C6_INVALID_CREATED_AT")
        _resolution_ms(market_identity_json, market_identity_sha256)
        expected_rows = self._expected_schedule_rows(
            market_id=market_id,
            payload_json=market_identity_json,
            market_hash=market_identity_sha256,
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                "INSERT INTO market_catalog(market_id,payload_json,payload_sha256,updated_at_ms) "
                "VALUES (?,?,?,?) ON CONFLICT(market_id) DO NOTHING",
                (
                    market_id,
                    market_identity_json,
                    market_identity_sha256,
                    created_at_ms,
                ),
            )
            stored = self._connection.execute(
                "SELECT payload_json,payload_sha256 FROM market_catalog WHERE market_id=?",
                (market_id,),
            ).fetchone()
            if stored != (market_identity_json, market_identity_sha256):
                raise ValueError("MARKET_IDENTITY_CONFLICT")
            self._insert_schedule_rows(expected_rows, created_at_ms)
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return self._read_market_schedules(market_id)

    def _expected_schedule_rows(
        self,
        *,
        market_id: str,
        payload_json: str,
        market_hash: str,
    ) -> tuple[tuple[object, ...], ...]:
        resolution_ms = _resolution_ms(payload_json, market_hash)
        expected_rows: list[tuple[object, ...]] = []
        for binding in self._operational_bindings:
            policy = V1_IDENTITY_POLICIES.get(binding.strategy_id)
            if policy is None:
                raise ValueError("C6_C5_PROVENANCE_MISMATCH")
            evaluation_key = _identity_key(
                "C6_SCHEDULED_EVALUATION_GROUP",
                {
                    "input_schema_version": binding.input_schema_version,
                    "market_id": market_id,
                    "market_identity_sha256": market_hash,
                    "rule_spec_sha256": binding.rule_spec_sha256,
                    "strategy_id": binding.strategy_id,
                    "strategy_version": binding.strategy_version,
                },
            )
            for checkpoint_minutes in policy.checkpoints:
                due_at_ms = resolution_ms - checkpoint_minutes * 60_000
                identity = {
                    "checkpoint_minutes": checkpoint_minutes,
                    "due_at_ms": due_at_ms,
                    "input_schema_version": binding.input_schema_version,
                    "market_id": market_id,
                    "market_identity_sha256": market_hash,
                    "rule_spec_sha256": binding.rule_spec_sha256,
                    "strategy_id": binding.strategy_id,
                    "strategy_version": binding.strategy_version,
                }
                schedule_key = _identity_key("C6_CHECKPOINT_SCHEDULE", identity)
                expected_rows.append(
                    (
                        schedule_key,
                        evaluation_key,
                        market_id,
                        market_hash,
                        resolution_ms,
                        binding.registry_index,
                        binding.strategy_id,
                        binding.strategy_version,
                        binding.rule_spec_sha256,
                        binding.input_schema_version,
                        binding.activation_status,
                        self._activation.source_commit,
                        self._activation.acceptance_sha256,
                        self._activation.matrix_sha256,
                        checkpoint_minutes,
                        due_at_ms,
                    )
                )
        return tuple(expected_rows)

    def _insert_schedule_rows(
        self,
        expected_rows: tuple[tuple[object, ...], ...],
        created_at_ms: int,
    ) -> None:
        for row in expected_rows:
            self._connection.execute(
                    """
                    INSERT INTO strategy_checkpoint_schedules(
                        schedule_key,evaluation_key,market_id,
                        market_identity_sha256,resolution_ms,registry_index,
                        strategy_id,strategy_version,rule_spec_sha256,
                        input_schema_version,activation_status,
                        activation_source_commit,c5_acceptance_sha256,
                        c5_status_matrix_sha256,checkpoint_minutes,due_at_ms,
                        state,created_at_ms,updated_at_ms
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING',?,?)
                    ON CONFLICT DO NOTHING
                    """,
                    (*row, created_at_ms, created_at_ms),
                )
            stored = self._connection.execute(
                    """
                    SELECT schedule_key,evaluation_key,market_id,
                           market_identity_sha256,resolution_ms,registry_index,
                           strategy_id,strategy_version,rule_spec_sha256,
                           input_schema_version,activation_status,
                           activation_source_commit,c5_acceptance_sha256,
                           c5_status_matrix_sha256,checkpoint_minutes,due_at_ms
                    FROM strategy_checkpoint_schedules
                    WHERE market_id=? AND strategy_id=? AND checkpoint_minutes=?
                    """,
                    (row[2], row[6], row[14]),
                ).fetchone()
            if stored is None or tuple(stored) != row:
                raise ValueError("CHECKPOINT_SCHEDULE_CONFLICT")

    def _read_market_schedules(
        self, market_id: str
    ) -> tuple[CheckpointSchedule, ...]:
        evaluator_ids = tuple(sorted(_OPERATIONAL_EVALUATOR_STRATEGY_IDS))
        rows = self._connection.execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM strategy_checkpoint_schedules "
            "WHERE market_id=? "
            f"AND strategy_id IN ({','.join('?' for _ in evaluator_ids)}) "
            "ORDER BY due_at_ms,registry_index,checkpoint_minutes",
            (market_id, *evaluator_ids),
        ).fetchall()
        return tuple(_schedule_from_row(tuple(row)) for row in rows)

    def market_schedules(self, market_id: str) -> tuple[CheckpointSchedule, ...]:
        if type(market_id) is not str or not market_id:
            raise ValueError("C6_INVALID_MARKET_ID")
        return self._read_market_schedules(market_id)

    def claim_due(
        self,
        *,
        now_ms: int,
        recovery_cutoff_ms: int,
        active_market_id: str,
        runtime_state: str,
        poller_id: str,
        limit: int,
    ) -> tuple[DueCheckpoint, ...]:
        for value in (now_ms, recovery_cutoff_ms, limit):
            if type(value) is not int or value < 0:
                raise ValueError("C6_INVALID_DUE_BOUNDARY")
        if not 1 <= limit <= 1000:
            raise ValueError("C6_INVALID_DUE_LIMIT")
        if type(active_market_id) is not str or not active_market_id:
            raise ValueError("C6_INVALID_ACTIVE_MARKET")
        if type(poller_id) is not str or not poller_id:
            raise ValueError("C6_INVALID_POLLER_ID")
        if runtime_state == "ROLLOVER_BLOCKED":
            raise ValueError("ROLLOVER_BLOCKED_STRATEGY_GUARD")
        if runtime_state != _ACTIVE_RUNTIME_STATE:
            raise ValueError("C6_RUNTIME_NOT_STRATEGY_READY")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state='PENDING',claim_owner=NULL,claimed_at_ms=NULL,
                    updated_at_ms=?
                WHERE state='CLAIMED' AND claimed_at_ms<=?
                """,
                (now_ms, now_ms - _CLAIM_LEASE_MS),
            )
            self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET claim_owner=NULL,claimed_at_ms=NULL,updated_at_ms=?
                WHERE state='CAPTURED' AND claimed_at_ms<=?
                """,
                (now_ms, now_ms - _CLAIM_LEASE_MS),
            )
            evaluator_ids = tuple(sorted(_OPERATIONAL_EVALUATOR_STRATEGY_IDS))
            self._connection.execute(
                f"""
                UPDATE strategy_checkpoint_schedules
                SET state='BLOCKED',claim_owner=NULL,claimed_at_ms=NULL,
                    blocked_reason='UNSUPPORTED_OPERATIONAL_EVALUATOR',
                    updated_at_ms=?
                WHERE state IN ('PENDING','CAPTURED')
                  AND strategy_id NOT IN ({','.join('?' for _ in evaluator_ids)})
                """,
                (now_ms, *evaluator_ids),
            )
            self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state='BLOCKED',claim_owner=NULL,claimed_at_ms=NULL,
                    blocked_reason=CASE
                        WHEN blocked_reason IS NULL
                        THEN 'MISSED_CHECKPOINT_EXPIRED_MARKET'
                        ELSE 'MISSED_CHECKPOINT:' || blocked_reason
                    END,
                    updated_at_ms=?
                WHERE state='PENDING' AND market_id<>? AND due_at_ms<=?
                  AND (resolution_ms<=? OR blocked_reason IS NOT NULL)
                """,
                (now_ms, active_market_id, now_ms, now_ms),
            )
            stale = self._connection.execute(
                """
                SELECT 1 FROM strategy_checkpoint_schedules
                WHERE state IN ('PENDING','CAPTURED') AND due_at_ms<=?
                  AND market_id<>?
                LIMIT 1
                """,
                (now_ms, active_market_id),
            ).fetchone()
            if stale is not None:
                raise ValueError("STALE_MARKET_STRATEGY_GUARD")
            keys = tuple(
                row[0]
                for row in self._connection.execute(
                    """
                    SELECT schedule_key FROM strategy_checkpoint_schedules
                    WHERE state IN ('PENDING','CAPTURED') AND market_id=?
                      AND due_at_ms<=? AND claim_owner IS NULL
                    ORDER BY due_at_ms,registry_index,checkpoint_minutes
                    LIMIT ?
                    """,
                    (active_market_id, now_ms, limit),
                ).fetchall()
            )
            for key in keys:
                changed = self._connection.execute(
                    """
                    UPDATE strategy_checkpoint_schedules
                    SET state=CASE WHEN state='PENDING' THEN 'CLAIMED'
                                   ELSE 'CAPTURED' END,
                        claim_owner=?,claimed_at_ms=?,updated_at_ms=?
                    WHERE schedule_key=? AND state IN ('PENDING','CAPTURED')
                      AND claim_owner IS NULL
                    """,
                    (poller_id, now_ms, now_ms, key),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("C6_CHECKPOINT_CLAIM_RACE")
            rows = tuple(
                self._connection.execute(
                    f"SELECT {_SCHEDULE_COLUMNS},capture_origin,blocked_reason "
                    "FROM strategy_checkpoint_schedules "
                    f"WHERE schedule_key IN ({','.join('?' for _ in keys)}) "
                    "ORDER BY due_at_ms,registry_index,checkpoint_minutes",
                    keys,
                ).fetchall()
                if keys
                else ()
            )
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return tuple(
            DueCheckpoint(
                schedule=_schedule_from_row(tuple(row[:19])),
                origin=(
                    row[19]
                    if row[17] == "CAPTURED"
                    and row[19] in {"LIVE", "RECOVERED_AFTER_DOWNTIME"}
                    else (
                        "RECOVERED_AFTER_DOWNTIME"
                        if row[20] is not None or row[16] < recovery_cutoff_ms
                        else "LIVE"
                    )
                ),
                poller_id=poller_id,
            )
            for row in rows
        )

    def claim_schedule(
        self,
        schedule_key: str,
        *,
        origin: str,
        poller_id: str,
    ) -> DueCheckpoint:
        if origin not in {"LIVE", "RECOVERED_AFTER_DOWNTIME"}:
            raise ValueError("C6_INVALID_CAPTURE_ORIGIN")
        if type(poller_id) is not str or not poller_id:
            raise ValueError("C6_INVALID_POLLER_ID")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            changed = self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state='CLAIMED',claim_owner=?,claimed_at_ms=due_at_ms,
                    updated_at_ms=due_at_ms
                WHERE schedule_key=? AND state='PENDING'
                """,
                (poller_id, schedule_key),
            ).rowcount
            if changed != 1:
                raise ValueError("C6_CHECKPOINT_NOT_PENDING")
            row = self._connection.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM strategy_checkpoint_schedules "
                "WHERE schedule_key=?",
                (schedule_key,),
            ).fetchone()
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        if row is None:
            raise RuntimeError("C6_CHECKPOINT_CLAIM_MISSING")
        return DueCheckpoint(_schedule_from_row(tuple(row)), origin, poller_id)

    def capture(
        self,
        due: DueCheckpoint,
        *,
        input_payload_json: str,
        input_snapshot_hash: str,
        historical_depth_available: bool,
    ) -> CaptureResult:
        if type(due) is not DueCheckpoint:
            raise ValueError("C6_INVALID_DUE_CHECKPOINT")
        decoded = _decode_canonical_json(
            input_payload_json,
            "C6_INVALID_CHECKPOINT_INPUT_JSON",
        )
        if type(decoded) is not dict:
            raise ValueError("C6_INVALID_CHECKPOINT_INPUT_JSON")
        if (
            type(input_snapshot_hash) is not str
            or _SHA64.fullmatch(input_snapshot_hash) is None
            or _sha256(input_payload_json.encode("utf-8")) != input_snapshot_hash
        ):
            raise ValueError("C6_CHECKPOINT_INPUT_HASH_MISMATCH")
        if type(historical_depth_available) is not bool:
            raise ValueError("C6_INVALID_HISTORICAL_DEPTH_STATUS")
        if historical_depth_available:
            decode_executable_checkpoint_input(
                CapturedCheckpoint(
                    checkpoint_minutes=due.schedule.checkpoint_minutes,
                    input_snapshot_hash=input_snapshot_hash,
                    input_payload_json=input_payload_json,
                    origin=due.origin,
                )
            )
        else:
            expected_missing = {
                "checkpoint_minutes": due.schedule.checkpoint_minutes,
                "market_id": due.schedule.market_id,
                "schema_version": _MISSING_DEPTH_INPUT,
                "strategy_id": due.schedule.strategy_id,
            }
            if decoded != expected_missing:
                raise ValueError("C6_INVALID_MISSING_DEPTH_INPUT")
        blocked = (
            due.origin == "RECOVERED_AFTER_DOWNTIME"
            and not historical_depth_available
        )
        state = "BLOCKED" if blocked else "CAPTURED"
        reason = "RECOVERED_HISTORICAL_DEPTH_MISSING" if blocked else None
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            changed = self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state=?,capture_origin=?,input_snapshot_hash=?,
                    input_payload_json=?,historical_depth_available=?,
                    blocked_reason=?,updated_at_ms=due_at_ms
                WHERE schedule_key=? AND state='CLAIMED' AND claim_owner=?
                """,
                (
                    state,
                    due.origin,
                    input_snapshot_hash,
                    input_payload_json,
                    int(historical_depth_available),
                    reason,
                    due.schedule.schedule_key,
                    due.poller_id,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("C6_CHECKPOINT_CLAIM_MISMATCH")
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return CaptureResult(state, reason)

    def release_unavailable(
        self,
        due: DueCheckpoint,
        *,
        reason_code: str,
        updated_at_ms: int,
    ) -> None:
        if type(due) is not DueCheckpoint:
            raise ValueError("C6_INVALID_DUE_CHECKPOINT")
        if type(reason_code) is not str or not reason_code:
            raise ValueError("C6_INVALID_UNAVAILABLE_REASON")
        if type(updated_at_ms) is not int or updated_at_ms < 0:
            raise ValueError("C6_INVALID_DUE_BOUNDARY")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            changed = self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state='PENDING',claim_owner=NULL,claimed_at_ms=NULL,
                    blocked_reason=?,updated_at_ms=?
                WHERE schedule_key=? AND state='CLAIMED' AND claim_owner=?
                """,
                (
                    reason_code,
                    updated_at_ms,
                    due.schedule.schedule_key,
                    due.poller_id,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("C6_CHECKPOINT_CLAIM_MISMATCH")
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def compose_dispatch(
        self,
        *,
        market_id: str,
        strategy_id: str,
    ) -> DispatchComposition | None:
        policy = V1_IDENTITY_POLICIES.get(strategy_id)
        if policy is None:
            raise ValueError("C6_UNKNOWN_STRATEGY_ID")
        rows = self._connection.execute(
            """
            SELECT checkpoint_minutes,input_snapshot_hash,input_payload_json,
                   capture_origin,input_schema_version
            FROM strategy_checkpoint_schedules
            WHERE market_id=? AND strategy_id=? AND state IN ('CAPTURED','COMPLETED')
            ORDER BY checkpoint_minutes DESC
            """,
            (market_id, strategy_id),
        ).fetchall()
        by_checkpoint = {row[0]: row for row in rows}
        if any(checkpoint not in by_checkpoint for checkpoint in policy.checkpoints):
            return None
        schemas = {by_checkpoint[checkpoint][4] for checkpoint in policy.checkpoints}
        if schemas != {_EXECUTABLE_INPUT}:
            raise ValueError("C6_DISPATCH_INPUT_SCHEMA_MISMATCH")
        return DispatchComposition(
            market_id=market_id,
            strategy_id=strategy_id,
            input_schema_version=_EXECUTABLE_INPUT,
            checkpoints=tuple(
                CapturedCheckpoint(
                    checkpoint_minutes=checkpoint,
                    input_snapshot_hash=by_checkpoint[checkpoint][1],
                    input_payload_json=by_checkpoint[checkpoint][2],
                    origin=by_checkpoint[checkpoint][3],
                )
                for checkpoint in policy.checkpoints
            ),
        )

    def dispatch_composition(
        self,
        composition: DispatchComposition,
        *,
        dispatcher: StrategyDispatcher,
    ) -> tuple[V1Evaluation, ...]:
        if type(composition) is not DispatchComposition or type(
            dispatcher
        ) is not StrategyDispatcher:
            raise ValueError("C6_INVALID_DISPATCH_COMPOSITION")
        request = V1StrategyDispatchRequest(
            checkpoints=tuple(
                decode_executable_checkpoint_input(checkpoint)
                for checkpoint in composition.checkpoints
            )
        )
        result = dispatcher.dispatch(
            strategy_id=composition.strategy_id,
            request=request,
        )
        if type(result) is not tuple or any(
            type(item) is not V1Evaluation for item in result
        ):
            raise RuntimeError("C6_UNEXPECTED_DISPATCH_RESULT")
        return result

    def evaluation_input_snapshot_hash(self, evaluation_key: str) -> str:
        if type(evaluation_key) is not str or _SHA64.fullmatch(evaluation_key) is None:
            raise ValueError("C6_INVALID_EVALUATION_GROUP_KEY")
        rows = self._connection.execute(
            "SELECT checkpoint_minutes,input_snapshot_hash,state "
            "FROM strategy_checkpoint_schedules WHERE evaluation_key=? "
            "ORDER BY checkpoint_minutes DESC",
            (evaluation_key,),
        ).fetchall()
        if not rows or any(
            row[1] is None or row[2] not in {"CAPTURED", "COMPLETED"}
            for row in rows
        ):
            raise ValueError("C6_DISPATCH_CHECKPOINTS_INCOMPLETE")
        if len(rows) == 1:
            return rows[0][1]
        return _identity_key(
            "C6_DISPATCH_INPUT_GROUP",
            {
                "checkpoints": [
                    {
                        "checkpoint_minutes": row[0],
                        "input_snapshot_hash": row[1],
                    }
                    for row in rows
                ],
                "evaluation_key": evaluation_key,
            },
        )

    @staticmethod
    def scheduled_evaluation_key(
        evaluation_group_key: str,
        input_snapshot_hash: str,
        evaluation_revision: int,
    ) -> str:
        if (
            type(evaluation_group_key) is not str
            or _SHA64.fullmatch(evaluation_group_key) is None
            or type(input_snapshot_hash) is not str
            or _SHA64.fullmatch(input_snapshot_hash) is None
            or type(evaluation_revision) is not int
            or evaluation_revision < 1
        ):
            raise ValueError("C6_INVALID_SCHEDULED_EVALUATION_IDENTITY")
        return _identity_key(
            "C6_SCHEDULED_EVALUATION",
            {
                "evaluation_group_key": evaluation_group_key,
                "evaluation_revision": evaluation_revision,
                "input_snapshot_hash": input_snapshot_hash,
            },
        )

    def complete(
        self,
        due: DueCheckpoint,
        *,
        evaluation: StrategyEvaluation,
        signal: SignalRecord | None,
    ) -> CompletionResult:
        if type(due) is not DueCheckpoint or type(evaluation) is not StrategyEvaluation:
            raise ValueError("C6_INVALID_COMPLETION_TYPE")
        if signal is not None and type(signal) is not SignalRecord:
            raise ValueError("C6_INVALID_COMPLETION_TYPE")
        schedule = due.schedule
        if (
            evaluation.strategy_id != schedule.strategy_id
            or evaluation.strategy_version != schedule.strategy_version
        ):
            raise ValueError("CHECKPOINT_EVALUATION_IDENTITY_MISMATCH")
        if evaluation.origin != due.origin:
            raise ValueError("CHECKPOINT_EVALUATION_ORIGIN_MISMATCH")
        if due.origin == "RECOVERED_AFTER_DOWNTIME":
            if evaluation.execution_eligible:
                raise ValueError("RECOVERED_EVALUATION_EXECUTION_FORBIDDEN")
            if not evaluation.current_reevaluation_required:
                raise ValueError("RECOVERED_CURRENT_REEVALUATION_REQUIRED")
            if evaluation.historical_signal_is_current_live_signal:
                raise ValueError("RECOVERED_SIGNAL_CURRENT_LIVE_FORBIDDEN")
        elif evaluation.current_reevaluation_required:
            raise ValueError("LIVE_CURRENT_REEVALUATION_FLAG_FORBIDDEN")
        _decode_canonical_json(
            evaluation.payload_json,
            "NONCANONICAL_STRATEGY_EVALUATION_JSON",
        )
        if signal is not None:
            if signal.evaluation_key != evaluation.evaluation_key:
                raise ValueError("CHECKPOINT_EVALUATION_SIGNAL_MISMATCH")
            if signal.strategy_id != evaluation.strategy_id or signal.origin != due.origin:
                raise ValueError("CHECKPOINT_EVALUATION_SIGNAL_MISMATCH")
            if signal.execution_eligible:
                raise ValueError("C6_SIGNAL_EXECUTION_FORBIDDEN")
            _decode_canonical_json(signal.payload_json, "NONCANONICAL_SIGNAL_JSON")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            states = self._connection.execute(
                "SELECT schedule_key,state,claim_owner,capture_origin "
                "FROM strategy_checkpoint_schedules "
                "WHERE evaluation_key=? ORDER BY checkpoint_minutes DESC",
                (schedule.evaluation_key,),
            ).fetchall()
            if not states:
                raise ValueError("C6_CHECKPOINT_MISSING")
            policy = V1_IDENTITY_POLICIES.get(schedule.strategy_id)
            if policy is None or len(states) != len(policy.checkpoints):
                raise ValueError("C6_DISPATCH_CHECKPOINTS_INCOMPLETE")
            replay = all(row[1] == "COMPLETED" for row in states)
            if any(row[1] == "COMPLETED" for row in states) and not replay:
                raise RuntimeError("C6_PARTIAL_GROUP_COMPLETION")
            due_state = next(
                (row for row in states if row[0] == schedule.schedule_key),
                None,
            )
            if due_state is None or (
                not replay
                and (
                    due_state[1] not in {"CLAIMED", "CAPTURED"}
                    or due_state[2] != due.poller_id
                )
            ):
                raise ValueError("C6_CHECKPOINT_CLAIM_MISMATCH")
            if not replay and len(states) > 1:
                if any(row[1] != "CAPTURED" for row in states):
                    raise ValueError("C6_DISPATCH_CHECKPOINTS_INCOMPLETE")
                origins = {row[3] for row in states}
                if origins != {due.origin}:
                    raise ValueError("C6_MIXED_CHECKPOINT_ORIGIN")
            existing_signal = self._connection.execute(
                "SELECT signal_id FROM signals WHERE evaluation_key=?",
                (evaluation.evaluation_key,),
            ).fetchone()
            if replay and ((signal is None) != (existing_signal is None)):
                raise ValueError("C6_CHECKPOINT_SIGNAL_PRESENCE_CONFLICT")
            expected_input_hash = self.evaluation_input_snapshot_hash(
                schedule.evaluation_key
            )
            if evaluation.input_snapshot_hash != expected_input_hash:
                raise ValueError("C6_EVALUATION_INPUT_HASH_MISMATCH")
            expected_evaluation_key = self.scheduled_evaluation_key(
                schedule.evaluation_key,
                expected_input_hash,
                evaluation.evaluation_revision,
            )
            if evaluation.evaluation_key != expected_evaluation_key:
                raise ValueError("CHECKPOINT_EVALUATION_KEY_MISMATCH")
            evaluation_id, evaluation_inserted = self._insert_evaluation(
                evaluation,
                checkpoint_group_key=schedule.evaluation_key,
                rule_spec_sha256=schedule.rule_spec_sha256,
            )
            signal_id: int | None = None
            outbox_id: int | None = None
            signal_inserted = evaluation_inserted
            outbox_inserted = evaluation_inserted
            if signal is not None:
                signal_id, signal_inserted = self._insert_signal(signal)
                outbox_id, outbox_inserted = self._insert_outbox(signal_id, signal)
            if len({evaluation_inserted, signal_inserted, outbox_inserted}) != 1:
                raise RuntimeError("C6_CHECKPOINT_ATOMICITY_VIOLATION")
            if replay and evaluation_inserted:
                raise RuntimeError("C6_CHECKPOINT_REPLAY_INSERTED")
            changed = self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state='COMPLETED',evaluation_id=?,
                    current_reevaluation_required=?,updated_at_ms=?
                WHERE evaluation_key=? AND (
                    state='COMPLETED' OR
                    state IN ('CLAIMED','CAPTURED')
                )
                """,
                (
                    evaluation_id,
                    int(evaluation.current_reevaluation_required),
                    evaluation.evaluated_at_ms,
                    schedule.evaluation_key,
                ),
            ).rowcount
            if changed != len(states):
                raise ValueError("C6_CHECKPOINT_CLAIM_MISMATCH")
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            self._requeue_claim_after_failure(
                due,
                updated_at_ms=evaluation.evaluated_at_ms,
            )
            raise
        return CompletionResult(
            inserted=evaluation_inserted,
            evaluation_id=evaluation_id,
            signal_id=signal_id,
            outbox_id=outbox_id,
            current_reevaluation_required=evaluation.current_reevaluation_required,
        )

    def _requeue_claim_after_failure(
        self,
        due: DueCheckpoint,
        *,
        updated_at_ms: int,
    ) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET state='PENDING',claim_owner=NULL,claimed_at_ms=NULL,
                    updated_at_ms=?
                WHERE evaluation_key=? AND state='CLAIMED'
                """,
                (
                    updated_at_ms,
                    due.schedule.evaluation_key,
                ),
            )
            self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET claim_owner=NULL,claimed_at_ms=NULL,updated_at_ms=?
                WHERE evaluation_key=? AND state='CAPTURED'
                """,
                (updated_at_ms, due.schedule.evaluation_key),
            )
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()

    def _insert_evaluation(
        self,
        evaluation: StrategyEvaluation,
        *,
        checkpoint_group_key: str | None,
        rule_spec_sha256: str,
    ) -> tuple[int, bool]:
        cursor = self._connection.execute(
            """
            INSERT INTO strategy_evaluations(
                evaluation_key,strategy_id,strategy_version,status,
                input_snapshot_hash,evaluation_revision,execution_eligible,
                evaluated_at_ms,payload_json,checkpoint_group_key,
                rule_spec_sha256,origin,
                historical_signal_is_current_live_signal,
                current_reevaluation_required
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(evaluation_key) DO NOTHING
            """,
            (
                evaluation.evaluation_key,
                evaluation.strategy_id,
                evaluation.strategy_version,
                evaluation.status,
                evaluation.input_snapshot_hash,
                evaluation.evaluation_revision,
                int(evaluation.execution_eligible),
                evaluation.evaluated_at_ms,
                evaluation.payload_json,
                checkpoint_group_key,
                rule_spec_sha256,
                evaluation.origin,
                int(evaluation.historical_signal_is_current_live_signal),
                int(evaluation.current_reevaluation_required),
            ),
        )
        inserted = cursor.rowcount == 1
        stored = self._connection.execute(
            """
            SELECT evaluation_id,strategy_id,strategy_version,status,
                   input_snapshot_hash,evaluation_revision,execution_eligible,
                   evaluated_at_ms,payload_json,checkpoint_group_key,
                   rule_spec_sha256,origin,
                   historical_signal_is_current_live_signal,
                   current_reevaluation_required
            FROM strategy_evaluations WHERE evaluation_key=?
            """,
            (evaluation.evaluation_key,),
        ).fetchone()
        expected = (
            evaluation.strategy_id,
            evaluation.strategy_version,
            evaluation.status,
            evaluation.input_snapshot_hash,
            evaluation.evaluation_revision,
            int(evaluation.execution_eligible),
            evaluation.evaluated_at_ms,
            evaluation.payload_json,
            checkpoint_group_key,
            rule_spec_sha256,
            evaluation.origin,
            int(evaluation.historical_signal_is_current_live_signal),
            int(evaluation.current_reevaluation_required),
        )
        if stored is None or tuple(stored[1:]) != expected:
            raise ValueError("STRATEGY_EVALUATION_CONFLICT")
        return stored[0], inserted

    def _insert_signal(self, signal: SignalRecord) -> tuple[int, bool]:
        cursor = self._connection.execute(
            """
            INSERT INTO signals(
                identity_key,evaluation_key,strategy_id,signal_type,
                payload_json,created_at_ms,origin,execution_eligible,
                infrastructure_only
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(identity_key) DO NOTHING
            """,
            (
                signal.identity_key,
                signal.evaluation_key,
                signal.strategy_id,
                signal.signal_type,
                signal.payload_json,
                signal.created_at_ms,
                signal.origin,
                int(signal.execution_eligible),
                int(signal.infrastructure_only),
            ),
        )
        inserted = cursor.rowcount == 1
        stored = self._connection.execute(
            """
            SELECT signal_id,evaluation_key,strategy_id,signal_type,payload_json,
                   created_at_ms,origin,execution_eligible,infrastructure_only
            FROM signals WHERE identity_key=?
            """,
            (signal.identity_key,),
        ).fetchone()
        expected = (
            signal.evaluation_key,
            signal.strategy_id,
            signal.signal_type,
            signal.payload_json,
            signal.created_at_ms,
            signal.origin,
            int(signal.execution_eligible),
            int(signal.infrastructure_only),
        )
        if stored is None or tuple(stored[1:]) != expected:
            raise ValueError("SIGNAL_IDENTITY_CONFLICT")
        return stored[0], inserted

    def _insert_outbox(
        self, signal_id: int, signal: SignalRecord
    ) -> tuple[int, bool]:
        cursor = self._connection.execute(
            """
            INSERT INTO outbox_events(signal_id,topic,payload_json,created_at_ms)
            VALUES (?,?,?,?) ON CONFLICT(signal_id) DO NOTHING
            """,
            (
                signal_id,
                "strategy.evaluation",
                signal.payload_json,
                signal.created_at_ms,
            ),
        )
        inserted = cursor.rowcount == 1
        stored = self._connection.execute(
            "SELECT event_id,topic,payload_json,created_at_ms "
            "FROM outbox_events WHERE signal_id=?",
            (signal_id,),
        ).fetchone()
        expected = (
            "strategy.evaluation",
            signal.payload_json,
            signal.created_at_ms,
        )
        if stored is None or tuple(stored[1:]) != expected:
            raise ValueError("OUTBOX_SIGNAL_CONFLICT")
        return stored[0], inserted

    def pending_current_reevaluations(
        self, market_id: str
    ) -> tuple[PendingCurrentReevaluation, ...]:
        rows = self._connection.execute(
            """
            SELECT evaluation_key,market_id,strategy_id,strategy_version,
                   rule_spec_sha256
            FROM strategy_checkpoint_schedules
            WHERE market_id=? AND state='COMPLETED'
              AND current_reevaluation_required=1
              AND current_reevaluation_evaluation_id IS NULL
            GROUP BY evaluation_key,market_id,strategy_id,strategy_version,
                     rule_spec_sha256
            ORDER BY MIN(due_at_ms),registry_index
            """,
            (market_id,),
        ).fetchall()
        return tuple(PendingCurrentReevaluation(*row) for row in rows)

    def claim_current_reevaluations(
        self,
        *,
        market_id: str,
        poller_id: str,
        now_ms: int,
        limit: int,
    ) -> tuple[PendingCurrentReevaluation, ...]:
        if type(market_id) is not str or not market_id:
            raise ValueError("C6_INVALID_MARKET_ID")
        if type(poller_id) is not str or not poller_id:
            raise ValueError("C6_INVALID_POLLER_ID")
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("C6_INVALID_DUE_BOUNDARY")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("C6_INVALID_DUE_LIMIT")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET current_claim_owner=NULL,current_claimed_at_ms=NULL,
                    updated_at_ms=?
                WHERE current_reevaluation_evaluation_id IS NULL
                  AND current_claimed_at_ms<=?
                """,
                (now_ms, now_ms - _CLAIM_LEASE_MS),
            )
            rows = self._connection.execute(
                """
                SELECT evaluation_key,market_id,strategy_id,strategy_version,
                       rule_spec_sha256,COUNT(*)
                FROM strategy_checkpoint_schedules
                WHERE market_id=? AND state='COMPLETED'
                  AND current_reevaluation_required=1
                  AND current_reevaluation_evaluation_id IS NULL
                  AND current_claim_owner IS NULL
                GROUP BY evaluation_key,market_id,strategy_id,strategy_version,
                         rule_spec_sha256
                ORDER BY MIN(due_at_ms),MIN(registry_index)
                LIMIT ?
                """,
                (market_id, limit),
            ).fetchall()
            claimed: list[PendingCurrentReevaluation] = []
            for row in rows:
                changed = self._connection.execute(
                    """
                    UPDATE strategy_checkpoint_schedules
                    SET current_claim_owner=?,current_claimed_at_ms=?,updated_at_ms=?
                    WHERE evaluation_key=? AND state='COMPLETED'
                      AND current_reevaluation_required=1
                      AND current_reevaluation_evaluation_id IS NULL
                      AND current_claim_owner IS NULL
                    """,
                    (poller_id, now_ms, now_ms, row[0]),
                ).rowcount
                if changed != row[5]:
                    raise RuntimeError("C6_CURRENT_REEVALUATION_CLAIM_RACE")
                claimed.append(PendingCurrentReevaluation(*row[:5], poller_id))
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return tuple(claimed)

    def capture_current_reevaluation(
        self,
        pending: PendingCurrentReevaluation,
        *,
        input_payload_json: str,
        input_snapshot_hash: str,
    ) -> None:
        if (
            type(pending) is not PendingCurrentReevaluation
            or type(pending.poller_id) is not str
            or not pending.poller_id
        ):
            raise ValueError("C6_INVALID_CURRENT_REEVALUATION")
        rows = self._connection.execute(
            "SELECT checkpoint_minutes,current_input_snapshot_hash,"
            "current_input_payload_json,current_claim_owner "
            "FROM strategy_checkpoint_schedules "
            "WHERE evaluation_key=? AND state='COMPLETED' "
            "ORDER BY checkpoint_minutes DESC",
            (pending.evaluation_group_key,),
        ).fetchall()
        if not rows or {row[3] for row in rows} != {pending.poller_id}:
            raise ValueError("C6_CURRENT_REEVALUATION_NOT_PENDING")
        decoded = _decode_canonical_json(
            input_payload_json,
            "C6_INVALID_EXECUTABLE_CHECKPOINT_JSON",
        )
        if (
            type(input_snapshot_hash) is not str
            or _SHA64.fullmatch(input_snapshot_hash) is None
            or _sha256(input_payload_json.encode("utf-8")) != input_snapshot_hash
            or type(decoded) is not dict
        ):
            raise ValueError("C6_CURRENT_REEVALUATION_INPUT_MISMATCH")
        checkpoint_minutes = decoded.get("checkpoint_minutes")
        by_checkpoint = {row[0]: row for row in rows}
        if checkpoint_minutes not in by_checkpoint:
            raise ValueError("C6_CURRENT_REEVALUATION_INPUT_MISMATCH")
        decode_executable_checkpoint_input(
            CapturedCheckpoint(
                checkpoint_minutes=checkpoint_minutes,
                input_snapshot_hash=input_snapshot_hash,
                input_payload_json=input_payload_json,
                origin="LIVE",
            )
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            expected = (input_snapshot_hash, input_payload_json)
            stored = by_checkpoint[checkpoint_minutes][1:3]
            if stored == (None, None):
                changed = self._connection.execute(
                    "UPDATE strategy_checkpoint_schedules "
                    "SET current_input_snapshot_hash=?,current_input_payload_json=? "
                    "WHERE evaluation_key=? AND checkpoint_minutes=? "
                    "AND state='COMPLETED' AND current_reevaluation_required=1 "
                    "AND current_claim_owner=?",
                    (
                        *expected,
                        pending.evaluation_group_key,
                        checkpoint_minutes,
                        pending.poller_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("C6_CURRENT_REEVALUATION_NOT_PENDING")
            elif stored != expected:
                raise ValueError("C6_CURRENT_REEVALUATION_INPUT_CONFLICT")
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    @staticmethod
    def current_reevaluation_key(
        evaluation_group_key: str,
        input_snapshot_hash: str,
        evaluation_revision: int,
    ) -> str:
        if (
            type(evaluation_group_key) is not str
            or _SHA64.fullmatch(evaluation_group_key) is None
            or type(input_snapshot_hash) is not str
            or _SHA64.fullmatch(input_snapshot_hash) is None
            or type(evaluation_revision) is not int
            or evaluation_revision < 1
        ):
            raise ValueError("C6_INVALID_CURRENT_REEVALUATION_IDENTITY")
        return _identity_key(
            "C6_CURRENT_REEVALUATION",
            {
                "input_snapshot_hash": input_snapshot_hash,
                "evaluation_group_key": evaluation_group_key,
                "evaluation_revision": evaluation_revision,
            },
        )

    def current_input_snapshot_hash(self, evaluation_group_key: str) -> str:
        rows = self._connection.execute(
            "SELECT checkpoint_minutes,current_input_snapshot_hash "
            "FROM strategy_checkpoint_schedules WHERE evaluation_key=? "
            "ORDER BY checkpoint_minutes DESC",
            (evaluation_group_key,),
        ).fetchall()
        if not rows or any(row[1] is None for row in rows):
            raise ValueError("C6_CURRENT_REEVALUATION_INPUT_INCOMPLETE")
        if len(rows) == 1:
            return rows[0][1]
        return _identity_key(
            "C6_CURRENT_INPUT_GROUP",
            {
                "checkpoints": [
                    {
                        "checkpoint_minutes": row[0],
                        "input_snapshot_hash": row[1],
                    }
                    for row in rows
                ],
                "evaluation_group_key": evaluation_group_key,
            },
        )

    def current_reevaluation_schedules(
        self,
        pending: PendingCurrentReevaluation,
    ) -> tuple[CheckpointSchedule, ...]:
        if (
            type(pending) is not PendingCurrentReevaluation
            or type(pending.poller_id) is not str
            or not pending.poller_id
        ):
            raise ValueError("C6_INVALID_CURRENT_REEVALUATION")
        rows = self._connection.execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM strategy_checkpoint_schedules "
            "WHERE evaluation_key=? AND state='COMPLETED' "
            "AND current_reevaluation_required=1 "
            "ORDER BY checkpoint_minutes DESC",
            (pending.evaluation_group_key,),
        ).fetchall()
        if not rows:
            raise ValueError("C6_CURRENT_REEVALUATION_NOT_PENDING")
        return tuple(_schedule_from_row(tuple(row)) for row in rows)

    def compose_current_dispatch(
        self,
        pending: PendingCurrentReevaluation,
    ) -> DispatchComposition | None:
        rows = self._connection.execute(
            "SELECT checkpoint_minutes,current_input_snapshot_hash,"
            "current_input_payload_json FROM strategy_checkpoint_schedules "
            "WHERE evaluation_key=? AND state='COMPLETED' "
            "ORDER BY checkpoint_minutes DESC",
            (pending.evaluation_group_key,),
        ).fetchall()
        if not rows or any(row[1] is None or row[2] is None for row in rows):
            return None
        return DispatchComposition(
            market_id=pending.market_id,
            strategy_id=pending.strategy_id,
            input_schema_version=_EXECUTABLE_INPUT,
            checkpoints=tuple(
                CapturedCheckpoint(
                    checkpoint_minutes=row[0],
                    input_snapshot_hash=row[1],
                    input_payload_json=row[2],
                    origin="LIVE",
                )
                for row in rows
            ),
        )

    def complete_current_reevaluation(
        self,
        pending: PendingCurrentReevaluation,
        *,
        evaluation: StrategyEvaluation,
    ) -> int:
        if type(pending) is not PendingCurrentReevaluation or type(
            evaluation
        ) is not StrategyEvaluation:
            raise ValueError("C6_INVALID_CURRENT_REEVALUATION")
        expected_key = self.current_reevaluation_key(
            pending.evaluation_group_key,
            evaluation.input_snapshot_hash,
            evaluation.evaluation_revision,
        )
        try:
            current_input_hash = self.current_input_snapshot_hash(
                pending.evaluation_group_key
            )
        except ValueError:
            raise ValueError("C6_CURRENT_REEVALUATION_INPUT_MISMATCH") from None
        if current_input_hash != evaluation.input_snapshot_hash:
            raise ValueError("C6_CURRENT_REEVALUATION_INPUT_MISMATCH")
        if (
            evaluation.evaluation_key != expected_key
            or evaluation.strategy_id != pending.strategy_id
            or evaluation.strategy_version != pending.strategy_version
            or evaluation.origin != "LIVE"
            or evaluation.current_reevaluation_required
            or evaluation.historical_signal_is_current_live_signal
            or evaluation.execution_eligible
        ):
            raise ValueError("C6_INVALID_CURRENT_REEVALUATION")
        _decode_canonical_json(
            evaluation.payload_json,
            "NONCANONICAL_STRATEGY_EVALUATION_JSON",
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            evaluation_id, _ = self._insert_evaluation(
                evaluation,
                checkpoint_group_key=None,
                rule_spec_sha256=pending.rule_spec_sha256,
            )
            existing_ids = {
                row[0]
                for row in self._connection.execute(
                    "SELECT current_reevaluation_evaluation_id "
                    "FROM strategy_checkpoint_schedules "
                    "WHERE evaluation_key=? AND state='COMPLETED'",
                    (pending.evaluation_group_key,),
                ).fetchall()
            }
            if existing_ids == {evaluation_id}:
                self._connection.commit()
                return evaluation_id
            if existing_ids != {None}:
                raise ValueError("C6_CURRENT_REEVALUATION_CONFLICT")
            owners = {
                row[0]
                for row in self._connection.execute(
                    "SELECT current_claim_owner FROM strategy_checkpoint_schedules "
                    "WHERE evaluation_key=? AND state='COMPLETED'",
                    (pending.evaluation_group_key,),
                ).fetchall()
            }
            if owners != {pending.poller_id}:
                raise ValueError("C6_CURRENT_REEVALUATION_CLAIM_MISMATCH")
            changed = self._connection.execute(
                """
                UPDATE strategy_checkpoint_schedules
                SET current_reevaluation_evaluation_id=?,
                    current_claim_owner=NULL,current_claimed_at_ms=NULL,
                    updated_at_ms=?
                WHERE evaluation_key=? AND state='COMPLETED'
                  AND current_reevaluation_required=1
                  AND current_reevaluation_evaluation_id IS NULL
                  AND current_claim_owner=?
                """,
                (
                    evaluation_id,
                    evaluation.evaluated_at_ms,
                    pending.evaluation_group_key,
                    pending.poller_id,
                ),
            ).rowcount
            if changed < 1:
                raise ValueError("C6_CURRENT_REEVALUATION_CONFLICT")
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        return evaluation_id
