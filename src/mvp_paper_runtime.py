from __future__ import annotations

import json
from dataclasses import asdict

from src.current_input import CurrentExecutionEvidenceV1
from src.models import SignalRecord
from src.outbox import OutboxBroker
from src.paper import PaperExecutionResult, StrictASignalV1
from src.storage import SqliteStore
from src.strategy_v1 import V1Evaluation


class MvpPaperRuntime:
    def __init__(self, store: SqliteStore, broker: OutboxBroker) -> None:
        if type(store) is not SqliteStore or not isinstance(broker, OutboxBroker):
            raise ValueError("INVALID_MVP_PAPER_RUNTIME")
        self._store = store
        self._broker = broker
        self._ledger = store.paper_ledger(publisher=broker)

    def execute(
        self,
        *,
        evaluation_key: str,
        input_snapshot_hash: str,
        evaluation: V1Evaluation,
        evidence: CurrentExecutionEvidenceV1,
        evaluated_at_ms: int,
        origin: str = "CURRENT_LIVE_REEVALUATION",
    ) -> PaperExecutionResult:
        if type(evaluation) is not V1Evaluation:
            raise ValueError("INVALID_STRICT_A_EVALUATION")
        if type(evidence) is not CurrentExecutionEvidenceV1:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE")
        signal = StrictASignalV1(
            schema_version="STRICT_A_SIGNAL_V1",
            signal_key="strict-a-signal:" + evaluation_key,
            evaluation_key=evaluation_key,
            strategy_id="YES_STRICT_A_OPERATIONAL",
            market_id=evidence.market_id,
            market_date=evidence.market_date,
            checkpoint_minutes=evidence.checkpoint_minutes,
            side="YES",
            bucket_index=evidence.bucket_index,
            token_id=evidence.token_id,
            model_probability_micros=evidence.model_probability_micros,
            market_q_micros=evidence.market_q_micros,
            vwap5_micros=evidence.vwap5_micros,
            fee_per_share_usd_micros=evidence.fee_per_share_usd_micros,
            evidence_key=evidence.evidence_key,
            input_snapshot_hash=input_snapshot_hash,
            execution_eligible=evaluation.accepted and origin == "CURRENT_LIVE_REEVALUATION",
            reason_code="READY" if evaluation.accepted else evaluation.reason,
            origin=origin,
            evaluated_at_ms=evaluated_at_ms,
        )
        payload_json = json.dumps(
            asdict(signal),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._store.commit_signal_and_outbox(
            SignalRecord(
                identity_key=signal.signal_key,
                evaluation_key=signal.evaluation_key,
                strategy_id=signal.strategy_id,
                signal_type="STRICT_A_SIGNAL_V1",
                payload_json=payload_json,
                created_at_ms=signal.evaluated_at_ms,
                origin=signal.origin,
                execution_eligible=signal.execution_eligible,
                infrastructure_only=False,
            ),
            topic="signal.strict_a",
            broker=self._broker,
        )
        return self._ledger.execute(
            signal,
            evidence,
            checked_at_ms=evaluated_at_ms,
        )
