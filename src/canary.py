from __future__ import annotations

import hashlib
import json
from typing import Any

from src.models import (
    CanonicalSnapshot,
    SignalRecord,
    StrategyEvaluation,
)
from src.outbox import OutboxBroker, commit_signal_and_outbox
from src.storage import SqliteStore


CANARY_ID = "CANARY_SYNC_READY_V1"
CANARY_VERSION = "1"
PART_OF_REGISTRY_47 = False
PAPER_OR_LIVE_ELIGIBLE = False


def _require_payload_field(
    payload: dict[str, Any],
    field: str,
    expected_type: type[Any],
) -> Any:
    value = payload.get(field)
    if type(value) is not expected_type:
        raise ValueError(f"INVALID_CANARY_SNAPSHOT_TYPE: {field}")
    if expected_type is str and not value:
        raise ValueError(f"INVALID_CANARY_SNAPSHOT_VALUE: {field}")
    return value


def _snapshot_payload(snapshot: CanonicalSnapshot) -> dict[str, Any]:
    if type(snapshot) is not CanonicalSnapshot:
        raise ValueError("INVALID_CANARY_SNAPSHOT_TYPE")
    try:
        payload = json.loads(snapshot.payload_json)
    except json.JSONDecodeError:
        raise ValueError("INVALID_CANARY_SNAPSHOT_JSON") from None
    if type(payload) is not dict:
        raise ValueError("INVALID_CANARY_SNAPSHOT_SHAPE")
    for field in (
        "backend_session_id",
        "market_identity",
        "triggering_event_natural_key",
    ):
        _require_payload_field(payload, field, str)
    for field in (
        "binance_ready",
        "polymarket_ready",
        "canonical",
        "trigger_committed_after_live_ready",
    ):
        _require_payload_field(payload, field, bool)
    return payload


def _reason_code(payload: dict[str, Any]) -> str:
    if not payload["binance_ready"]:
        return "BINANCE_NOT_LIVE_READY"
    if not payload["polymarket_ready"]:
        return "POLYMARKET_NOT_RECONCILED"
    if not payload["canonical"]:
        return "SNAPSHOT_NOT_CANONICAL"
    if not payload["trigger_committed_after_live_ready"]:
        return "NO_NEW_CLOSED_KLINE_AFTER_LIVE_READY"
    return "CANARY_READY"


def evaluate_canary(snapshot: CanonicalSnapshot) -> StrategyEvaluation:
    payload = _snapshot_payload(snapshot)
    reason_code = _reason_code(payload)
    recovered = snapshot.recovery_origin == "RECOVERED_AFTER_DOWNTIME"
    identity = {
        "backend_session_id": payload["backend_session_id"],
        "canary_id": CANARY_ID,
        "canary_version": CANARY_VERSION,
        "market_identity": payload["market_identity"],
        "origin": snapshot.recovery_origin,
        "snapshot_hash": snapshot.payload_sha256,
        "triggering_event_natural_key": payload[
            "triggering_event_natural_key"
        ],
    }
    identity_json = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity_hash = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
    if reason_code != "CANARY_READY":
        status = "BLOCKED"
    elif recovered:
        status = "RECOVERED_SIGNAL"
    else:
        status = "SIGNAL"

    evaluation_payload = {
        **identity,
        "canary_infrastructure_only": True,
        "current_reevaluation_required": recovered,
        "execution_eligible": False,
        "historical_signal_is_current_live_signal": False,
        "paper_or_live_eligible": False,
        "reason_code": reason_code,
        "status": status,
    }
    return StrategyEvaluation(
        evaluation_key=f"canary-evaluation:{identity_hash}",
        strategy_id=CANARY_ID,
        strategy_version=CANARY_VERSION,
        status=status,
        input_snapshot_hash=snapshot.payload_sha256,
        evaluation_revision=1,
        execution_eligible=False,
        evaluated_at_ms=snapshot.created_at_ms,
        payload_json=json.dumps(
            evaluation_payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        reason_code=reason_code,
        origin=snapshot.recovery_origin,
        historical_signal_is_current_live_signal=False,
        current_reevaluation_required=recovered,
    )


def commit_canary_if_new(
    store: SqliteStore,
    snapshot: CanonicalSnapshot,
    *,
    broker: OutboxBroker | None = None,
) -> SignalRecord | None:
    if type(store) is not SqliteStore:
        raise ValueError("INVALID_CANARY_STORE_TYPE")
    evaluation = evaluate_canary(snapshot)
    if evaluation.status == "BLOCKED":
        return None

    identity_hash = evaluation.evaluation_key.removeprefix(
        "canary-evaluation:"
    )
    identity_key = f"canary-signal:{identity_hash}"
    existing = store.scalar(
        "SELECT signal_id FROM signals WHERE identity_key = ?",
        (identity_key,),
    )
    if existing is not None:
        return None

    signal_payload = {
        "canary_id": CANARY_ID,
        "canary_infrastructure_only": True,
        "event_type": "SIGNAL_CREATED",
        "evaluation_key": evaluation.evaluation_key,
        "execution_eligible": False,
        "historical_signal_is_current_live_signal": False,
        "origin": evaluation.origin,
        "paper_or_live_eligible": False,
        "reason_code": evaluation.reason_code,
        "status": evaluation.status,
    }
    signal = SignalRecord(
        identity_key=identity_key,
        evaluation_key=evaluation.evaluation_key,
        strategy_id=CANARY_ID,
        signal_type="INFRASTRUCTURE_CANARY",
        payload_json=json.dumps(
            signal_payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        created_at_ms=evaluation.evaluated_at_ms,
        origin=evaluation.origin,
        execution_eligible=False,
        infrastructure_only=True,
    )
    store.commit_evaluation_signal_and_outbox(
        evaluation,
        signal,
        topic="signal.created",
        broker=broker,
    )
    return signal
