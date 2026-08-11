from __future__ import annotations

import re
import sqlite3
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from src.current_input import CurrentExecutionEvidenceV1


FIVE_SHARES_MICROS = 5_000_000
DEFAULT_BANKROLL_USD_MICROS = 1_000_000_000
_MICROS = 1_000_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _require_int(name: str, value: object, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_nonempty(name: str, value: object) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_probability(name: str, value: object) -> None:
    _require_int(name, value)
    if value > _MICROS:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_sha256(name: str, value: object) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"INVALID_{name.upper()}")


def _require_market_date(value: object) -> None:
    if type(value) is not str:
        raise ValueError("INVALID_MARKET_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("INVALID_MARKET_DATE") from None
    if parsed.isoformat() != value:
        raise ValueError("INVALID_MARKET_DATE")


def _round_half_up_product(left: int, right: int) -> int:
    return (left * right + (_MICROS // 2)) // _MICROS


@dataclass(frozen=True, slots=True)
class StrictASignalV1:
    schema_version: str
    signal_key: str
    evaluation_key: str
    strategy_id: str
    market_id: str
    market_date: str
    checkpoint_minutes: int
    side: str
    bucket_index: int
    token_id: str
    model_probability_micros: int
    market_q_micros: int
    vwap5_micros: int
    fee_per_share_usd_micros: int
    evidence_key: str
    input_snapshot_hash: str
    execution_eligible: bool
    reason_code: str
    origin: str
    evaluated_at_ms: int

    def __post_init__(self) -> None:
        if self.schema_version != "STRICT_A_SIGNAL_V1":
            raise ValueError("INVALID_SIGNAL_SCHEMA_VERSION")
        if self.strategy_id != "YES_STRICT_A_OPERATIONAL":
            raise ValueError("INVALID_STRATEGY_ID")
        if self.side != "YES":
            raise ValueError("INVALID_SIDE")
        for name in ("signal_key", "evaluation_key", "market_id", "token_id", "reason_code"):
            _require_nonempty(name, getattr(self, name))
        _require_market_date(self.market_date)
        if self.checkpoint_minutes not in (30, 60):
            raise ValueError("INVALID_CHECKPOINT_MINUTES")
        if type(self.bucket_index) is not int or not 0 <= self.bucket_index <= 10:
            raise ValueError("INVALID_BUCKET_INDEX")
        for name in (
            "model_probability_micros",
            "market_q_micros",
            "vwap5_micros",
        ):
            _require_probability(name, getattr(self, name))
        _require_int("fee_per_share_usd_micros", self.fee_per_share_usd_micros)
        _require_sha256("evidence_key", self.evidence_key)
        _require_sha256("input_snapshot_hash", self.input_snapshot_hash)
        if type(self.execution_eligible) is not bool:
            raise ValueError("INVALID_EXECUTION_ELIGIBLE")
        if self.origin not in (
            "CURRENT_LIVE_REEVALUATION",
            "RECOVERED_AFTER_DOWNTIME",
        ):
            raise ValueError("INVALID_SIGNAL_ORIGIN")
        _require_int("evaluated_at_ms", self.evaluated_at_ms)


@dataclass(frozen=True, slots=True)
class PaperExecutionReadinessV1:
    schema_version: str
    readiness_key: str
    market_id: str
    market_date: str
    checkpoint_minutes: int
    signal_key: str | None
    evidence_key: str | None
    ready: bool
    reason_code: str
    requested_shares_micros: int
    checked_at_ms: int


@dataclass(frozen=True, slots=True)
class PaperIntentV1:
    schema_version: str
    intent_key: str
    signal_key: str
    evidence_key: str
    market_id: str
    market_date: str
    token_id: str
    side: str
    checkpoint_minutes: int
    requested_shares_micros: int
    status: str
    reason_code: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class PaperFillV1:
    schema_version: str
    fill_key: str
    intent_key: str
    fill_sequence: int
    shares_micros: int
    price_micros: int
    fee_usd_micros: int
    gross_cost_usd_micros: int
    evidence_key: str
    book_sha256: str
    filled_at_ms: int


@dataclass(frozen=True, slots=True)
class PaperPositionV1:
    schema_version: str
    position_key: str
    intent_key: str
    market_id: str
    market_date: str
    token_id: str
    side: str
    status: str
    filled_shares_micros: int
    average_price_micros: int
    cost_basis_usd_micros: int
    fees_paid_usd_micros: int
    settlement_value_usd_micros: int | None
    realized_pnl_usd_micros: int
    unrealized_pnl_usd_micros: int
    opened_at_ms: int
    updated_at_ms: int
    settled_at_ms: int | None


@dataclass(frozen=True, slots=True)
class PaperAccountV1:
    schema_version: str
    account_key: str
    starting_bankroll_usd_micros: int
    cash_usd_micros: int
    open_cost_basis_usd_micros: int
    equity_usd_micros: int
    realized_pnl_usd_micros: int
    unrealized_pnl_usd_micros: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    readiness: PaperExecutionReadinessV1
    intent: PaperIntentV1 | None
    fill: PaperFillV1 | None
    position: PaperPositionV1 | None
    account: PaperAccountV1
    replayed: bool = False


class PaperLedger:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        publisher: Any | None = None,
        owns_connection: bool = True,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("INVALID_SQLITE_CONNECTION")
        self._connection = connection
        self._publisher = publisher
        self._owns_connection = owns_connection

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    @staticmethod
    def _canonical_payload(value: object) -> str:
        return json.dumps(
            asdict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _insert_outbox(
        self,
        *,
        identity_key: str,
        topic: str,
        value: object,
        created_at_ms: int,
    ) -> int | None:
        payload_json = self._canonical_payload(value)
        cursor = self._connection.execute(
            """
            INSERT INTO outbox_events(
                signal_id, event_identity_key, topic, payload_json, created_at_ms
            ) VALUES (NULL, ?, ?, ?, ?)
            ON CONFLICT(event_identity_key) DO NOTHING
            """,
            (identity_key, topic, payload_json, created_at_ms),
        )
        row = self._connection.execute(
            """
            SELECT event_id, topic, payload_json, created_at_ms
            FROM outbox_events WHERE event_identity_key = ?
            """,
            (identity_key,),
        ).fetchone()
        if row is None or tuple(row[1:]) != (topic, payload_json, created_at_ms):
            raise ValueError("PAPER_OUTBOX_IDENTITY_CONFLICT")
        return row[0] if cursor.rowcount == 1 else None

    def _result_outbox_ids(self, result: PaperExecutionResult) -> list[int]:
        values = [("paper.readiness", result.readiness)]
        for topic, value in (
            ("paper.intent", result.intent),
            ("paper.fill", result.fill),
            ("paper.position", result.position),
            ("paper.account", result.account),
        ):
            if value is not None:
                values.append((topic, value))
        ids: list[int] = []
        for topic, value in values:
            updated_at_ms = getattr(
                value,
                "updated_at_ms",
                getattr(value, "filled_at_ms", getattr(value, "checked_at_ms", 0)),
            )
            identity = getattr(
                value,
                "readiness_key",
                getattr(
                    value,
                    "intent_key",
                    getattr(
                        value,
                        "fill_key",
                        getattr(value, "position_key", getattr(value, "account_key", "")),
                    ),
                ),
            )
            event_id = self._insert_outbox(
                identity_key=f"{topic}:{identity}:{updated_at_ms}",
                topic=topic,
                value=value,
                created_at_ms=updated_at_ms,
            )
            if event_id is not None:
                ids.append(event_id)
        return ids

    def _notify(self, event_ids: list[int]) -> None:
        if self._publisher is None:
            return
        for event_id in event_ids:
            self._publisher.publish_committed(event_id)

    def _state_outbox_ids(
        self, position: PaperPositionV1, account: PaperAccountV1
    ) -> list[int]:
        ids: list[int] = []
        for topic, identity, value in (
            ("paper.position", position.position_key, position),
            ("paper.account", account.account_key, account),
        ):
            event_id = self._insert_outbox(
                identity_key=f"{topic}:{identity}:{value.updated_at_ms}",
                topic=topic,
                value=value,
                created_at_ms=value.updated_at_ms,
            )
            if event_id is not None:
                ids.append(event_id)
        return ids

    def initialize_account(self, *, updated_at_ms: int) -> PaperAccountV1:
        _require_int("updated_at_ms", updated_at_ms)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._ensure_account_row(updated_at_ms)
            account = self.get_account()
            self._connection.commit()
            return account
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _ensure_account_row(self, updated_at_ms: int) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_accounts(
                account_key, schema_version, starting_bankroll_usd_micros,
                cash_usd_micros, open_cost_basis_usd_micros, equity_usd_micros,
                realized_pnl_usd_micros, unrealized_pnl_usd_micros, updated_at_ms
            ) VALUES ('default', 'PAPER_ACCOUNT_V1', ?, ?, 0, ?, 0, 0, ?)
            ON CONFLICT(account_key) DO NOTHING
            """,
            (
                DEFAULT_BANKROLL_USD_MICROS,
                DEFAULT_BANKROLL_USD_MICROS,
                DEFAULT_BANKROLL_USD_MICROS,
                updated_at_ms,
            ),
        )

    def execute(
        self,
        signal: StrictASignalV1,
        evidence: CurrentExecutionEvidenceV1 | None,
        *,
        checked_at_ms: int,
        max_fill_shares_micros: int = FIVE_SHARES_MICROS,
    ) -> PaperExecutionResult:
        if type(signal) is not StrictASignalV1:
            raise ValueError("INVALID_STRICT_A_SIGNAL")
        if evidence is not None and type(evidence) is not CurrentExecutionEvidenceV1:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE")
        _require_int("checked_at_ms", checked_at_ms)
        if max_fill_shares_micros != FIVE_SHARES_MICROS:
            raise ValueError("INVALID_FILL_SHARES")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._ensure_account_row(checked_at_ms)
            existing = self._intent_for_date(signal.market_date)
            if existing is not None:
                if (
                    existing.signal_key == signal.signal_key
                    and existing.evidence_key == signal.evidence_key
                ):
                    self._validate_replay_identity(existing, signal, evidence)
                    result = self._replay_result(existing)
                    self._connection.commit()
                    return result
                reason = (
                    "T30_BLOCKED_EXISTING_LOGICAL_EXECUTION"
                    if signal.checkpoint_minutes == 30
                    else "DUPLICATE_LOGICAL_EXECUTION"
                )
                result = self._blocked_result(signal, evidence, checked_at_ms, reason)
                event_ids = self._result_outbox_ids(result)
                self._connection.commit()
                self._notify(event_ids)
                return result

            reason = self._readiness_reason(signal, evidence)
            if reason == "READY":
                assert evidence is not None
                fill_shares = max_fill_shares_micros
                gross_cost = _round_half_up_product(
                    evidence.vwap5_micros, fill_shares
                )
                fee = _round_half_up_product(
                    evidence.fee_per_share_usd_micros, fill_shares
                )
                if self.get_account().cash_usd_micros < gross_cost + fee:
                    reason = "INSUFFICIENT_PAPER_CASH"

            readiness = self._persist_readiness(
                signal=signal,
                evidence=evidence,
                checked_at_ms=checked_at_ms,
                reason_code=reason,
            )
            if not readiness.ready:
                result = PaperExecutionResult(
                    readiness=readiness,
                    intent=None,
                    fill=None,
                    position=None,
                    account=self.get_account(),
                )
                event_ids = self._result_outbox_ids(result)
                self._connection.commit()
                self._notify(event_ids)
                return result

            assert evidence is not None
            result = self._create_execution(
                signal=signal,
                evidence=evidence,
                readiness=readiness,
                fill_shares_micros=max_fill_shares_micros,
                created_at_ms=checked_at_ms,
            )
            event_ids = self._result_outbox_ids(result)
            self._connection.commit()
            self._notify(event_ids)
            return result
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _validate_replay_identity(
        self,
        intent: PaperIntentV1,
        signal: StrictASignalV1,
        evidence: CurrentExecutionEvidenceV1 | None,
    ) -> None:
        if (
            intent.market_id,
            intent.market_date,
            intent.token_id,
            intent.checkpoint_minutes,
            intent.side,
        ) != (
            signal.market_id,
            signal.market_date,
            signal.token_id,
            signal.checkpoint_minutes,
            signal.side,
        ):
            raise ValueError("PAPER_EXECUTION_IDENTITY_CONFLICT")
        if evidence is None:
            return
        if self._readiness_reason(signal, evidence) != "READY":
            raise ValueError("PAPER_EXECUTION_IDENTITY_CONFLICT")
        fill = self._fill_for_intent(intent.intent_key)
        position = self._position_for_date(intent.market_date)
        if fill is None or position is None:
            raise RuntimeError("PAPER_EXECUTION_STATE_INCOMPLETE")
        expected_gross = _round_half_up_product(
            evidence.vwap5_micros, fill.shares_micros
        )
        expected_fee = _round_half_up_product(
            evidence.fee_per_share_usd_micros, fill.shares_micros
        )
        if (
            fill.evidence_key,
            fill.book_sha256,
            fill.price_micros,
            fill.gross_cost_usd_micros,
            fill.fee_usd_micros,
            position.cost_basis_usd_micros,
        ) != (
            evidence.evidence_key,
            evidence.book_sha256,
            evidence.vwap5_micros,
            expected_gross,
            expected_fee,
            expected_gross + expected_fee,
        ):
            raise ValueError("PAPER_EXECUTION_IDENTITY_CONFLICT")

    @staticmethod
    def _readiness_reason(
        signal: StrictASignalV1,
        evidence: CurrentExecutionEvidenceV1 | None,
    ) -> str:
        if signal.origin == "RECOVERED_AFTER_DOWNTIME":
            return "RECOVERED_EXECUTION_FORBIDDEN"
        if not signal.execution_eligible:
            return "NO_STRICT_A_SIGNAL"
        if evidence is None:
            return "MISSING_CURRENT_BOOK"
        signal_tuple = (
            signal.evidence_key,
            signal.market_id,
            signal.market_date,
            signal.token_id,
            signal.checkpoint_minutes,
            signal.bucket_index,
            signal.model_probability_micros,
            signal.market_q_micros,
            signal.vwap5_micros,
            signal.fee_per_share_usd_micros,
        )
        evidence_tuple = (
            evidence.evidence_key,
            evidence.market_id,
            evidence.market_date,
            evidence.token_id,
            evidence.checkpoint_minutes,
            evidence.bucket_index,
            evidence.model_probability_micros,
            evidence.market_q_micros,
            evidence.vwap5_micros,
            evidence.fee_per_share_usd_micros,
        )
        if signal_tuple != evidence_tuple:
            return "MODEL_INPUT_INVALID"
        if evidence.closed_candle_count != 181:
            return "MISSING_181_CLOSED_CANDLES"
        if evidence.available_depth_shares_micros < FIVE_SHARES_MICROS:
            return "INSUFFICIENT_FIVE_SHARE_DEPTH"
        return "READY"

    def _blocked_result(
        self,
        signal: StrictASignalV1,
        evidence: CurrentExecutionEvidenceV1 | None,
        checked_at_ms: int,
        reason: str,
    ) -> PaperExecutionResult:
        readiness = self._persist_readiness(
            signal=signal,
            evidence=evidence,
            checked_at_ms=checked_at_ms,
            reason_code=reason,
        )
        return PaperExecutionResult(
            readiness=readiness,
            intent=None,
            fill=None,
            position=None,
            account=self.get_account(),
        )

    def _persist_readiness(
        self,
        *,
        signal: StrictASignalV1,
        evidence: CurrentExecutionEvidenceV1 | None,
        checked_at_ms: int,
        reason_code: str,
    ) -> PaperExecutionReadinessV1:
        readiness = PaperExecutionReadinessV1(
            schema_version="PAPER_READINESS_V1",
            readiness_key=(
                f"strict-a:{signal.market_date}:{signal.checkpoint_minutes}"
            ),
            market_id=signal.market_id,
            market_date=signal.market_date,
            checkpoint_minutes=signal.checkpoint_minutes,
            signal_key=signal.signal_key,
            evidence_key=evidence.evidence_key if evidence is not None else None,
            ready=reason_code == "READY",
            reason_code=reason_code,
            requested_shares_micros=FIVE_SHARES_MICROS,
            checked_at_ms=checked_at_ms,
        )
        self._connection.execute(
            """
            INSERT INTO paper_execution_readiness(
                readiness_key, schema_version, market_id, market_date,
                checkpoint_minutes, signal_key, evidence_key, ready, reason_code,
                requested_shares_micros, checked_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(readiness_key) DO UPDATE SET
                market_id=excluded.market_id,
                signal_key=excluded.signal_key,
                evidence_key=excluded.evidence_key,
                ready=excluded.ready,
                reason_code=excluded.reason_code,
                checked_at_ms=excluded.checked_at_ms
            """,
            (
                readiness.readiness_key,
                readiness.schema_version,
                readiness.market_id,
                readiness.market_date,
                readiness.checkpoint_minutes,
                readiness.signal_key,
                readiness.evidence_key,
                int(readiness.ready),
                readiness.reason_code,
                readiness.requested_shares_micros,
                readiness.checked_at_ms,
            ),
        )
        return readiness

    def _create_execution(
        self,
        *,
        signal: StrictASignalV1,
        evidence: CurrentExecutionEvidenceV1,
        readiness: PaperExecutionReadinessV1,
        fill_shares_micros: int,
        created_at_ms: int,
    ) -> PaperExecutionResult:
        intent_key = f"strict-a:{signal.market_date}"
        intent_status = (
            "FILLED" if fill_shares_micros == FIVE_SHARES_MICROS else "PARTIAL"
        )
        intent = PaperIntentV1(
            schema_version="PAPER_INTENT_V1",
            intent_key=intent_key,
            signal_key=signal.signal_key,
            evidence_key=evidence.evidence_key,
            market_id=signal.market_id,
            market_date=signal.market_date,
            token_id=signal.token_id,
            side="YES",
            checkpoint_minutes=signal.checkpoint_minutes,
            requested_shares_micros=FIVE_SHARES_MICROS,
            status=intent_status,
            reason_code="READY",
            created_at_ms=created_at_ms,
            updated_at_ms=created_at_ms,
        )
        self._connection.execute(
            """
            INSERT INTO paper_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.intent_key,
                intent.schema_version,
                intent.signal_key,
                intent.evidence_key,
                intent.market_id,
                intent.market_date,
                intent.token_id,
                intent.side,
                intent.checkpoint_minutes,
                intent.requested_shares_micros,
                intent.status,
                intent.reason_code,
                intent.created_at_ms,
                intent.updated_at_ms,
            ),
        )
        gross_cost = _round_half_up_product(
            evidence.vwap5_micros, fill_shares_micros
        )
        fee = _round_half_up_product(
            evidence.fee_per_share_usd_micros, fill_shares_micros
        )
        fill = PaperFillV1(
            schema_version="PAPER_FILL_V1",
            fill_key=f"{intent_key}:1",
            intent_key=intent_key,
            fill_sequence=1,
            shares_micros=fill_shares_micros,
            price_micros=evidence.vwap5_micros,
            fee_usd_micros=fee,
            gross_cost_usd_micros=gross_cost,
            evidence_key=evidence.evidence_key,
            book_sha256=evidence.book_sha256,
            filled_at_ms=created_at_ms,
        )
        self._connection.execute(
            "INSERT INTO paper_fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.fill_key,
                fill.schema_version,
                fill.intent_key,
                fill.fill_sequence,
                fill.shares_micros,
                fill.price_micros,
                fill.fee_usd_micros,
                fill.gross_cost_usd_micros,
                fill.evidence_key,
                fill.book_sha256,
                fill.filled_at_ms,
            ),
        )
        cost_basis = gross_cost + fee
        marked_value = _round_half_up_product(
            evidence.vwap5_micros, fill_shares_micros
        )
        position = PaperPositionV1(
            schema_version="PAPER_POSITION_V1",
            position_key=intent_key,
            intent_key=intent_key,
            market_id=signal.market_id,
            market_date=signal.market_date,
            token_id=signal.token_id,
            side="YES",
            status="OPEN" if intent_status == "FILLED" else "PARTIAL",
            filled_shares_micros=fill_shares_micros,
            average_price_micros=evidence.vwap5_micros,
            cost_basis_usd_micros=cost_basis,
            fees_paid_usd_micros=fee,
            settlement_value_usd_micros=None,
            realized_pnl_usd_micros=0,
            unrealized_pnl_usd_micros=marked_value - cost_basis,
            opened_at_ms=created_at_ms,
            updated_at_ms=created_at_ms,
            settled_at_ms=None,
        )
        self._connection.execute(
            """
            INSERT INTO paper_positions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                position.position_key,
                position.schema_version,
                position.intent_key,
                position.market_id,
                position.market_date,
                position.token_id,
                position.side,
                position.status,
                position.filled_shares_micros,
                position.average_price_micros,
                position.cost_basis_usd_micros,
                position.fees_paid_usd_micros,
                position.settlement_value_usd_micros,
                position.realized_pnl_usd_micros,
                position.unrealized_pnl_usd_micros,
                position.opened_at_ms,
                position.updated_at_ms,
                position.settled_at_ms,
            ),
        )
        account = self.get_account()
        self._connection.execute(
            "UPDATE paper_accounts SET cash_usd_micros = ?, updated_at_ms = ? WHERE account_key = 'default'",
            (account.cash_usd_micros - cost_basis, created_at_ms),
        )
        account = self._recalculate_account(created_at_ms)
        return PaperExecutionResult(readiness, intent, fill, position, account)

    def _replay_result(self, intent: PaperIntentV1) -> PaperExecutionResult:
        readiness = self._readiness_for(
            intent.market_date, intent.checkpoint_minutes
        )
        if readiness is None:
            raise RuntimeError("PAPER_READINESS_MISSING")
        return PaperExecutionResult(
            readiness=readiness,
            intent=intent,
            fill=self._fill_for_intent(intent.intent_key),
            position=self._position_for_date(intent.market_date),
            account=self.get_account(),
            replayed=True,
        )

    def mark_to_market(
        self, *, market_date: str, mark_price_micros: int, updated_at_ms: int
    ) -> tuple[PaperPositionV1, PaperAccountV1]:
        _require_market_date(market_date)
        if type(mark_price_micros) is not int or not 0 <= mark_price_micros <= _MICROS:
            raise ValueError("INVALID_MARK_PRICE")
        _require_int("updated_at_ms", updated_at_ms)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            position = self._position_for_date(market_date)
            if position is None or position.status == "SETTLED":
                raise ValueError("NO_OPEN_PAPER_POSITION")
            marked_value = _round_half_up_product(
                mark_price_micros, position.filled_shares_micros
            )
            self._connection.execute(
                """
                UPDATE paper_positions
                SET unrealized_pnl_usd_micros = ?, updated_at_ms = ?
                WHERE market_date = ? AND status <> 'SETTLED'
                """,
                (marked_value - position.cost_basis_usd_micros, updated_at_ms, market_date),
            )
            position = self._position_for_date(market_date)
            account = self._recalculate_account(updated_at_ms)
            assert position is not None
            event_ids = self._state_outbox_ids(position, account)
            self._connection.commit()
            self._notify(event_ids)
            return position, account
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def settle(
        self, *, market_date: str, settlement_price_micros: int, settled_at_ms: int
    ) -> tuple[PaperPositionV1, PaperAccountV1]:
        _require_market_date(market_date)
        if (
            type(settlement_price_micros) is not int
            or settlement_price_micros not in (0, _MICROS)
        ):
            raise ValueError("INVALID_SETTLEMENT_PRICE")
        _require_int("settled_at_ms", settled_at_ms)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            position = self._position_for_date(market_date)
            if position is None:
                raise ValueError("PAPER_POSITION_NOT_FOUND")
            settlement_value = _round_half_up_product(
                settlement_price_micros, position.filled_shares_micros
            )
            if position.status == "SETTLED":
                if position.settlement_value_usd_micros != settlement_value:
                    raise ValueError("SETTLEMENT_CONFLICT")
                account = self.get_account()
                self._connection.commit()
                return position, account
            realized = settlement_value - position.cost_basis_usd_micros
            self._connection.execute(
                """
                UPDATE paper_positions SET
                    status = 'SETTLED', settlement_value_usd_micros = ?,
                    realized_pnl_usd_micros = ?, unrealized_pnl_usd_micros = 0,
                    updated_at_ms = ?, settled_at_ms = ?
                WHERE market_date = ? AND status <> 'SETTLED'
                """,
                (settlement_value, realized, settled_at_ms, settled_at_ms, market_date),
            )
            self._connection.execute(
                """
                UPDATE paper_intents SET status = 'SETTLED', updated_at_ms = ?
                WHERE intent_key = ?
                """,
                (settled_at_ms, position.intent_key),
            )
            account = self.get_account()
            self._connection.execute(
                "UPDATE paper_accounts SET cash_usd_micros = ?, updated_at_ms = ? WHERE account_key = 'default'",
                (account.cash_usd_micros + settlement_value, settled_at_ms),
            )
            account = self._recalculate_account(settled_at_ms)
            position = self._position_for_date(market_date)
            assert position is not None
            event_ids = self._state_outbox_ids(position, account)
            self._connection.commit()
            self._notify(event_ids)
            return position, account
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _recalculate_account(self, updated_at_ms: int) -> PaperAccountV1:
        open_values = self._connection.execute(
            """
            SELECT
                COALESCE(SUM(cost_basis_usd_micros), 0),
                COALESCE(SUM(unrealized_pnl_usd_micros), 0)
            FROM paper_positions WHERE status <> 'SETTLED'
            """
        ).fetchone()
        realized = self._connection.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd_micros), 0) FROM paper_positions WHERE status = 'SETTLED'"
        ).fetchone()[0]
        account = self.get_account()
        open_cost, unrealized = open_values
        equity = account.cash_usd_micros + open_cost + unrealized
        if account.cash_usd_micros < 0 or open_cost < 0 or equity < 0:
            raise ValueError("IMPOSSIBLE_PAPER_ACCOUNT_STATE")
        self._connection.execute(
            """
            UPDATE paper_accounts SET
                open_cost_basis_usd_micros = ?, equity_usd_micros = ?,
                realized_pnl_usd_micros = ?, unrealized_pnl_usd_micros = ?,
                updated_at_ms = ? WHERE account_key = 'default'
            """,
            (open_cost, equity, realized, unrealized, updated_at_ms),
        )
        return self.get_account()

    def get_account(self) -> PaperAccountV1:
        row = self._connection.execute(
            """
            SELECT schema_version, account_key, starting_bankroll_usd_micros,
                cash_usd_micros, open_cost_basis_usd_micros, equity_usd_micros,
                realized_pnl_usd_micros, unrealized_pnl_usd_micros, updated_at_ms
            FROM paper_accounts WHERE account_key = 'default'
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("PAPER_ACCOUNT_NOT_INITIALIZED")
        return PaperAccountV1(*row)

    def row_count(self, table: str) -> int:
        allowed = {
            "paper_execution_readiness",
            "paper_intents",
            "paper_fills",
            "paper_positions",
            "paper_accounts",
        }
        if table not in allowed:
            raise ValueError("INVALID_PAPER_TABLE")
        return self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _intent_for_date(self, market_date: str) -> PaperIntentV1 | None:
        row = self._connection.execute(
            """
            SELECT schema_version, intent_key, signal_key, evidence_key, market_id,
                market_date, token_id, side, checkpoint_minutes,
                requested_shares_micros, status, reason_code, created_at_ms,
                updated_at_ms FROM paper_intents WHERE market_date = ?
            """,
            (market_date,),
        ).fetchone()
        return None if row is None else PaperIntentV1(*row)

    def _fill_for_intent(self, intent_key: str) -> PaperFillV1 | None:
        row = self._connection.execute(
            """
            SELECT schema_version, fill_key, intent_key, fill_sequence,
                shares_micros, price_micros, fee_usd_micros,
                gross_cost_usd_micros, evidence_key, book_sha256, filled_at_ms
            FROM paper_fills WHERE intent_key = ? ORDER BY fill_sequence LIMIT 1
            """,
            (intent_key,),
        ).fetchone()
        return None if row is None else PaperFillV1(*row)

    def _position_for_date(self, market_date: str) -> PaperPositionV1 | None:
        row = self._connection.execute(
            """
            SELECT schema_version, position_key, intent_key, market_id,
                market_date, token_id, side, status, filled_shares_micros,
                average_price_micros, cost_basis_usd_micros,
                fees_paid_usd_micros, settlement_value_usd_micros,
                realized_pnl_usd_micros, unrealized_pnl_usd_micros,
                opened_at_ms, updated_at_ms, settled_at_ms
            FROM paper_positions WHERE market_date = ?
            """,
            (market_date,),
        ).fetchone()
        return None if row is None else PaperPositionV1(*row)

    def _readiness_for(
        self, market_date: str, checkpoint_minutes: int
    ) -> PaperExecutionReadinessV1 | None:
        row = self._connection.execute(
            """
            SELECT schema_version, readiness_key, market_id, market_date,
                checkpoint_minutes, signal_key, evidence_key, ready, reason_code,
                requested_shares_micros, checked_at_ms
            FROM paper_execution_readiness
            WHERE readiness_key = ?
            """,
            (f"strict-a:{market_date}:{checkpoint_minutes}",),
        ).fetchone()
        if row is None:
            return None
        values = list(row)
        values[7] = bool(values[7])
        return PaperExecutionReadinessV1(*values)
