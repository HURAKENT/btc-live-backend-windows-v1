from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self

from src.lifecycle import STARTUP_SEQUENCE
from src.models import (
    CanonicalSnapshot,
    OutboxEvent,
    SignalRecord,
    SourceEvent,
    StrategyEvaluation,
)


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_COUNTABLE_TABLES = frozenset(
    {
        "schema_migrations",
        "source_events",
        "source_cursors",
        "market_catalog",
        "canonical_state",
        "strategy_evaluations",
        "signals",
        "outbox_events",
        "incidents",
        "strategy_checkpoint_schedules",
        "data_import_runs",
        "data_source_ranges",
        "data_source_range_assessments",
        "data_import_run_events",
        "paper_execution_readiness",
        "paper_intents",
        "paper_fills",
        "paper_positions",
        "paper_accounts",
    }
)


@dataclass(frozen=True, slots=True)
class AppendResult:
    event_id: int
    inserted: bool


@dataclass(frozen=True, slots=True)
class PersistResult:
    row_id: int
    inserted: bool


class CommittedPublisher(Protocol):
    def publish_committed(self, event_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class WriteCommand:
    operation: str
    event: SourceEvent | None
    future: asyncio.Future[AppendResult] | None

    @classmethod
    def append_source_event(
        cls,
        event: SourceEvent,
        future: asyncio.Future[AppendResult],
    ) -> Self:
        return cls("APPEND_SOURCE_EVENT", event, future)

    @classmethod
    def stop(cls) -> Self:
        return cls("STOP", None, None)


class SqliteStore:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self._path = path
        self._connection = connection
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> Self:
        if not isinstance(path, Path):
            raise ValueError("INVALID_DATABASE_PATH_TYPE")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return cls(path, connection)

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def commit_pending(self) -> None:
        if self._closed:
            raise RuntimeError("SQLITE_STORE_CLOSED")
        self._connection.commit()

    def migrate(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at_ms INTEGER NOT NULL
            )
            """
        )
        migration_paths = sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if not migration_paths:
            raise ValueError("NO_DATABASE_MIGRATIONS")

        for migration_path in migration_paths:
            version = int(migration_path.name[:4])
            already_applied = self.scalar(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (version,),
            )
            if already_applied == 1:
                continue

            script = migration_path.read_text(encoding="utf-8-sig")
            escaped_name = migration_path.name.replace("'", "''")
            applied_at_ms = time.time_ns() // 1_000_000
            transactional_script = (
                "BEGIN IMMEDIATE;\n"
                f"{script.rstrip()}\n"
                "INSERT INTO schema_migrations(version, name, applied_at_ms) "
                f"VALUES ({version}, '{escaped_name}', {applied_at_ms});\n"
                "COMMIT;\n"
            )
            try:
                self._connection.executescript(transactional_script)
            except sqlite3.Error:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise

    def append_source_event(self, event: SourceEvent) -> AppendResult:
        if type(event) is not SourceEvent:
            raise ValueError("INVALID_SOURCE_EVENT_TYPE")

        committed_at_ms = time.time_ns() // 1_000_000
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT INTO source_events(
                    source,
                    natural_key,
                    source_timestamp_ms,
                    received_timestamp_ms,
                    event_type,
                    payload_json,
                    payload_sha256,
                    recovery_origin,
                    committed_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(natural_key) DO NOTHING
                """,
                (
                    event.source,
                    event.natural_key,
                    event.source_timestamp_ms,
                    event.received_timestamp_ms,
                    event.event_type,
                    event.payload_json,
                    event.payload_sha256,
                    event.recovery_origin,
                    committed_at_ms,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._connection.execute(
                """
                SELECT
                    event_id,
                    source,
                    source_timestamp_ms,
                    received_timestamp_ms,
                    event_type,
                    payload_json,
                    payload_sha256,
                    recovery_origin
                FROM source_events
                WHERE natural_key = ?
                """,
                (event.natural_key,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("SOURCE_EVENT_INSERT_MISSING")

            # Acquisition metadata may legitimately differ when the same
            # authoritative event is observed through REST backfill and a live
            # stream. Natural key + canonical payload define event identity;
            # received timestamp and recovery origin describe how it arrived.
            #
            # A Polymarket book natural key is asset_id + provider book hash.
            # Re-observing that exact hashed state later is therefore an
            # idempotent replay even when the provider observation timestamp
            # advances. Other event types retain strict source-timestamp
            # equality because their frozen identities do not make that
            # timestamp redundant.
            if stored[4] == event.event_type == "POLYMARKET_BOOK":
                stored_authoritative = (
                    stored[1],
                    stored[4],
                    stored[5],
                    stored[6],
                )
                expected_authoritative = (
                    event.source,
                    event.event_type,
                    event.payload_json,
                    event.payload_sha256,
                )
            else:
                stored_authoritative = (
                    stored[1],
                    stored[2],
                    stored[4],
                    stored[5],
                    stored[6],
                )
                expected_authoritative = (
                    event.source,
                    event.source_timestamp_ms,
                    event.event_type,
                    event.payload_json,
                    event.payload_sha256,
                )
            if stored_authoritative != expected_authoritative:
                raise ValueError("SOURCE_EVENT_CONFLICT")

            self._connection.commit()
            return AppendResult(event_id=stored[0], inserted=inserted)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def upsert_source_cursor(
        self,
        *,
        source: str,
        cursor_json: str,
        updated_at_ms: int,
    ) -> bool:
        _require_nonempty_string(source, "INVALID_SOURCE_CURSOR_SOURCE")
        canonical_cursor = _canonical_json(
            cursor_json,
            "INVALID_CURSOR_JSON",
        )
        _require_nonnegative_integer(
            updated_at_ms,
            "INVALID_SOURCE_CURSOR_TIMESTAMP",
        )

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            stored = self._connection.execute(
                """
                SELECT cursor_json, updated_at_ms
                FROM source_cursors
                WHERE source = ?
                """,
                (source,),
            ).fetchone()
            if stored is None:
                self._connection.execute(
                    """
                    INSERT INTO source_cursors(
                        source,
                        cursor_json,
                        updated_at_ms
                    )
                    VALUES (?, ?, ?)
                    """,
                    (source, canonical_cursor, updated_at_ms),
                )
                changed = True
            else:
                stored_cursor, stored_timestamp = stored
                if updated_at_ms < stored_timestamp:
                    raise ValueError("SOURCE_CURSOR_REGRESSION")
                if updated_at_ms == stored_timestamp:
                    if canonical_cursor != stored_cursor:
                        raise ValueError("SOURCE_CURSOR_CONFLICT")
                    changed = False
                else:
                    self._connection.execute(
                        """
                        UPDATE source_cursors
                        SET cursor_json = ?, updated_at_ms = ?
                        WHERE source = ?
                        """,
                        (canonical_cursor, updated_at_ms, source),
                    )
                    changed = True
            self._connection.commit()
            return changed
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def read_source_cursor(
        self,
        source: str,
    ) -> dict[str, Any] | None:
        _require_nonempty_string(source, "INVALID_SOURCE_CURSOR_SOURCE")
        stored = self._connection.execute(
            """
            SELECT source, cursor_json, updated_at_ms
            FROM source_cursors
            WHERE source = ?
            """,
            (source,),
        ).fetchone()
        if stored is None:
            return None
        return {
            "source": stored[0],
            "cursor": _decode_stored_json(stored[1]),
            "cursor_json": stored[1],
            "updated_at_ms": stored[2],
        }

    def persist_market_identity(
        self,
        *,
        market_id: str,
        payload_json: str,
        payload_sha256: str,
        updated_at_ms: int,
    ) -> PersistResult:
        _require_nonempty_string(
            market_id,
            "INVALID_MARKET_IDENTITY_ID",
        )
        canonical_payload = _canonical_json(
            payload_json,
            "INVALID_MARKET_IDENTITY_JSON",
        )
        _require_payload_hash(canonical_payload, payload_sha256)
        _require_nonnegative_integer(
            updated_at_ms,
            "INVALID_MARKET_IDENTITY_TIMESTAMP",
        )

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT INTO market_catalog(
                    market_id,
                    payload_json,
                    payload_sha256,
                    updated_at_ms
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(market_id) DO NOTHING
                """,
                (
                    market_id,
                    canonical_payload,
                    payload_sha256,
                    updated_at_ms,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._connection.execute(
                """
                SELECT rowid, payload_json, payload_sha256, updated_at_ms
                FROM market_catalog
                WHERE market_id = ?
                """,
                (market_id,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("MARKET_IDENTITY_INSERT_MISSING")
            expected = (
                canonical_payload,
                payload_sha256,
                updated_at_ms,
            )
            if tuple(stored[1:]) != expected:
                raise ValueError("MARKET_IDENTITY_CONFLICT")
            self._connection.commit()
            return PersistResult(row_id=stored[0], inserted=inserted)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def append_canonical_snapshot(
        self,
        snapshot: CanonicalSnapshot,
    ) -> PersistResult:
        if type(snapshot) is not CanonicalSnapshot:
            raise ValueError("INVALID_CANONICAL_SNAPSHOT_TYPE")
        canonical_payload = _canonical_json(
            snapshot.payload_json,
            "INVALID_CANONICAL_SNAPSHOT_JSON",
        )
        if canonical_payload != snapshot.payload_json:
            raise ValueError("NONCANONICAL_SNAPSHOT_JSON")
        _require_payload_hash(
            snapshot.payload_json,
            snapshot.payload_sha256,
        )
        source_event_ids_json = json.dumps(
            list(snapshot.source_event_ids),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT INTO canonical_state(
                    snapshot_key,
                    source_event_ids_json,
                    payload_json,
                    payload_sha256,
                    recovery_origin,
                    created_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_key) DO NOTHING
                """,
                (
                    snapshot.snapshot_key,
                    source_event_ids_json,
                    snapshot.payload_json,
                    snapshot.payload_sha256,
                    snapshot.recovery_origin,
                    snapshot.created_at_ms,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._connection.execute(
                """
                SELECT
                    snapshot_id,
                    source_event_ids_json,
                    payload_json,
                    payload_sha256,
                    recovery_origin,
                    created_at_ms
                FROM canonical_state
                WHERE snapshot_key = ?
                """,
                (snapshot.snapshot_key,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("CANONICAL_SNAPSHOT_INSERT_MISSING")
            expected = (
                source_event_ids_json,
                snapshot.payload_json,
                snapshot.payload_sha256,
                snapshot.recovery_origin,
                snapshot.created_at_ms,
            )
            if tuple(stored[1:]) != expected:
                raise ValueError("CANONICAL_SNAPSHOT_CONFLICT")
            self._connection.commit()
            return PersistResult(row_id=stored[0], inserted=inserted)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def append_strategy_evaluation(
        self,
        evaluation: StrategyEvaluation,
    ) -> PersistResult:
        if type(evaluation) is not StrategyEvaluation:
            raise ValueError("INVALID_STRATEGY_EVALUATION_TYPE")
        canonical_payload = _canonical_json(
            evaluation.payload_json,
            "INVALID_STRATEGY_EVALUATION_JSON",
        )
        if canonical_payload != evaluation.payload_json:
            raise ValueError("NONCANONICAL_STRATEGY_EVALUATION_JSON")
        if (
            evaluation.origin == "RECOVERED_AFTER_DOWNTIME"
            and evaluation.execution_eligible
        ):
            raise ValueError("RECOVERED_EVALUATION_EXECUTION_FORBIDDEN")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT INTO strategy_evaluations(
                    evaluation_key,
                    strategy_id,
                    strategy_version,
                    status,
                    input_snapshot_hash,
                    evaluation_revision,
                    execution_eligible,
                    evaluated_at_ms,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._connection.execute(
                """
                SELECT
                    evaluation_id,
                    strategy_id,
                    strategy_version,
                    status,
                    input_snapshot_hash,
                    evaluation_revision,
                    execution_eligible,
                    evaluated_at_ms,
                    payload_json
                FROM strategy_evaluations
                WHERE evaluation_key = ?
                """,
                (evaluation.evaluation_key,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("STRATEGY_EVALUATION_INSERT_MISSING")
            expected = (
                evaluation.strategy_id,
                evaluation.strategy_version,
                evaluation.status,
                evaluation.input_snapshot_hash,
                evaluation.evaluation_revision,
                int(evaluation.execution_eligible),
                evaluation.evaluated_at_ms,
                evaluation.payload_json,
            )
            if tuple(stored[1:]) != expected:
                raise ValueError("STRATEGY_EVALUATION_CONFLICT")
            self._connection.commit()
            return PersistResult(row_id=stored[0], inserted=inserted)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def append_incident(
        self,
        *,
        incident_key: str,
        severity: str,
        status: str,
        payload_json: str,
        created_at_ms: int,
    ) -> PersistResult:
        _require_nonempty_string(incident_key, "INVALID_INCIDENT_KEY")
        _require_nonempty_string(severity, "INVALID_INCIDENT_SEVERITY")
        _require_nonempty_string(status, "INVALID_INCIDENT_STATUS")
        canonical_payload = _canonical_json(
            payload_json,
            "INVALID_INCIDENT_JSON",
        )
        _require_nonnegative_integer(
            created_at_ms,
            "INVALID_INCIDENT_TIMESTAMP",
        )

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                """
                INSERT INTO incidents(
                    incident_key,
                    severity,
                    status,
                    payload_json,
                    created_at_ms
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(incident_key) DO NOTHING
                """,
                (
                    incident_key,
                    severity,
                    status,
                    canonical_payload,
                    created_at_ms,
                ),
            )
            inserted = cursor.rowcount == 1
            stored = self._connection.execute(
                """
                SELECT
                    incident_id,
                    severity,
                    status,
                    payload_json,
                    created_at_ms
                FROM incidents
                WHERE incident_key = ?
                """,
                (incident_key,),
            ).fetchone()
            if stored is None:
                raise RuntimeError("INCIDENT_INSERT_MISSING")
            expected = (
                severity,
                status,
                canonical_payload,
                created_at_ms,
            )
            if tuple(stored[1:]) != expected:
                raise ValueError("INCIDENT_CONFLICT")
            self._connection.commit()
            return PersistResult(row_id=stored[0], inserted=inserted)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def latest_committed_rollover_identity(self) -> dict[str, str] | None:
        row = self._connection.execute(
            """
            SELECT payload_json
            FROM incidents
            WHERE status = 'CUTOVER_COMMITTED'
            ORDER BY incident_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        payload = _decode_stored_json(row[0])
        if (
            type(payload) is not dict
            or payload.get("record_type") != "MARKET_ROLLOVER_STATE"
            or payload.get("state") != "CUTOVER_COMMITTED"
            or type(payload.get("market_id")) is not str
            or not payload["market_id"]
            or type(payload.get("market_identity_sha256")) is not str
            or len(payload["market_identity_sha256"]) != 64
        ):
            raise ValueError("INVALID_DURABLE_ROLLOVER_COMMIT")
        market = self._connection.execute(
            """
            SELECT payload_json, payload_sha256
            FROM market_catalog
            WHERE market_id = ?
            """,
            (payload["market_id"],),
        ).fetchone()
        if market is None:
            raise ValueError("DURABLE_ROLLOVER_MARKET_MISSING")
        identity_json = _canonical_json(
            market[0], "INVALID_MARKET_IDENTITY_JSON"
        )
        identity_sha256 = hashlib.sha256(
            identity_json.encode("utf-8")
        ).hexdigest()
        if (
            market[0] != identity_json
            or market[1] != identity_sha256
            or payload["market_identity_sha256"] != identity_sha256
        ):
            raise ValueError("DURABLE_ROLLOVER_IDENTITY_CONFLICT")
        return {
            "market_id": payload["market_id"],
            "market_identity_json": identity_json,
            "market_identity_sha256": identity_sha256,
        }

    def append_lifecycle_state(
        self,
        *,
        run_id: str,
        state: str,
        sequence_index: int,
        created_at_ms: int,
        detail: str | None = None,
    ) -> PersistResult:
        _require_nonempty_string(run_id, "INVALID_LIFECYCLE_RUN_ID")
        _require_nonempty_string(state, "INVALID_LIFECYCLE_STATE")
        _require_nonnegative_integer(
            sequence_index,
            "INVALID_LIFECYCLE_SEQUENCE_INDEX",
        )
        _require_nonnegative_integer(
            created_at_ms,
            "INVALID_LIFECYCLE_TIMESTAMP",
        )
        if detail is not None and type(detail) is not str:
            raise ValueError("INVALID_LIFECYCLE_DETAIL")

        incident_key = f"lifecycle:{run_id}:{sequence_index}:{state}"
        if state == "RECOVERY_BLOCKED":
            existing = self.scalar(
                "SELECT 1 FROM incidents WHERE incident_key = ?",
                (incident_key,),
            )
            if (
                existing is None
                and sequence_index
                != self._next_lifecycle_sequence_index(run_id)
            ):
                raise ValueError("INVALID_LIFECYCLE_SEQUENCE_INDEX")
            severity = "CRITICAL"
        else:
            if (
                state not in STARTUP_SEQUENCE
                or sequence_index >= len(STARTUP_SEQUENCE)
                or STARTUP_SEQUENCE[sequence_index] != state
            ):
                raise ValueError("INVALID_LIFECYCLE_SEQUENCE_INDEX")
            severity = "INFO"

        payload = {
            "record_type": "LIFECYCLE_STATE",
            "run_id": run_id,
            "state": state,
            "sequence_index": sequence_index,
        }
        if detail is not None:
            payload["detail"] = detail
        return self.append_incident(
            incident_key=incident_key,
            severity=severity,
            status=state,
            payload_json=json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            created_at_ms=created_at_ms,
        )

    def _next_lifecycle_sequence_index(self, run_id: str) -> int:
        pattern = f"lifecycle:{_escape_like(run_id)}:%"
        rows = self.rows(
            """
            SELECT payload_json
            FROM incidents
            WHERE incident_key LIKE ? ESCAPE '\\'
            """,
            (pattern,),
        )
        observed_indices = []
        for row in rows:
            payload = _decode_stored_json(row[0])
            if (
                type(payload) is dict
                and payload.get("record_type") == "LIFECYCLE_STATE"
                and payload.get("state") in STARTUP_SEQUENCE
                and type(payload.get("sequence_index")) is int
            ):
                observed_indices.append(payload["sequence_index"])
        return max(observed_indices, default=-1) + 1

    def commit_signal_and_outbox(
        self,
        signal: SignalRecord,
        *,
        topic: str,
        broker: CommittedPublisher | None = None,
    ) -> tuple[int, int]:
        if type(signal) is not SignalRecord:
            raise ValueError("INVALID_SIGNAL_TYPE")
        if type(topic) is not str:
            raise ValueError("INVALID_OUTBOX_TOPIC_TYPE")
        if not topic:
            raise ValueError("INVALID_OUTBOX_TOPIC")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            signal_cursor = self._connection.execute(
                """
                INSERT INTO signals(
                    identity_key,
                    evaluation_key,
                    strategy_id,
                    signal_type,
                    payload_json,
                    created_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO NOTHING
                """,
                (
                    signal.identity_key,
                    signal.evaluation_key,
                    signal.strategy_id,
                    signal.signal_type,
                    signal.payload_json,
                    signal.created_at_ms,
                ),
            )
            signal_inserted = signal_cursor.rowcount == 1
            stored_signal = self._connection.execute(
                """
                SELECT
                    signal_id,
                    evaluation_key,
                    strategy_id,
                    signal_type,
                    payload_json,
                    created_at_ms
                FROM signals
                WHERE identity_key = ?
                """,
                (signal.identity_key,),
            ).fetchone()
            if stored_signal is None:
                raise RuntimeError("SIGNAL_INSERT_MISSING")
            expected_signal = (
                signal.evaluation_key,
                signal.strategy_id,
                signal.signal_type,
                signal.payload_json,
                signal.created_at_ms,
            )
            if tuple(stored_signal[1:]) != expected_signal:
                raise ValueError("SIGNAL_IDENTITY_CONFLICT")

            signal_id = stored_signal[0]
            outbox_cursor = self._connection.execute(
                """
                INSERT INTO outbox_events(
                    signal_id,
                    topic,
                    payload_json,
                    created_at_ms
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(signal_id) DO NOTHING
                """,
                (
                    signal_id,
                    topic,
                    signal.payload_json,
                    signal.created_at_ms,
                ),
            )
            outbox_inserted = outbox_cursor.rowcount == 1
            stored_outbox = self._connection.execute(
                """
                SELECT event_id, topic, payload_json, created_at_ms
                FROM outbox_events
                WHERE signal_id = ?
                """,
                (signal_id,),
            ).fetchone()
            if stored_outbox is None:
                raise RuntimeError("OUTBOX_INSERT_MISSING")
            expected_outbox = (
                topic,
                signal.payload_json,
                signal.created_at_ms,
            )
            if tuple(stored_outbox[1:]) != expected_outbox:
                raise ValueError("OUTBOX_SIGNAL_CONFLICT")
            if signal_inserted != outbox_inserted:
                raise RuntimeError("SIGNAL_OUTBOX_ATOMICITY_VIOLATION")

            outbox_id = stored_outbox[0]
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

        if outbox_inserted and broker is not None:
            broker.publish_committed(outbox_id)
        return signal_id, outbox_id

    def commit_evaluation_signal_and_outbox(
        self,
        evaluation: StrategyEvaluation,
        signal: SignalRecord,
        *,
        topic: str,
        broker: CommittedPublisher | None = None,
    ) -> tuple[int, int, int]:
        if type(evaluation) is not StrategyEvaluation:
            raise ValueError("INVALID_STRATEGY_EVALUATION_TYPE")
        if type(signal) is not SignalRecord:
            raise ValueError("INVALID_SIGNAL_TYPE")
        if type(topic) is not str or not topic:
            raise ValueError("INVALID_OUTBOX_TOPIC")
        if evaluation.execution_eligible or signal.execution_eligible:
            raise ValueError("CANARY_EXECUTION_ELIGIBILITY_FORBIDDEN")
        if not signal.infrastructure_only:
            raise ValueError("CANARY_INFRASTRUCTURE_ONLY_REQUIRED")
        if signal.evaluation_key != evaluation.evaluation_key:
            raise ValueError("CANARY_EVALUATION_SIGNAL_MISMATCH")
        canonical_evaluation = _canonical_json(
            evaluation.payload_json,
            "INVALID_STRATEGY_EVALUATION_JSON",
        )
        canonical_signal = _canonical_json(
            signal.payload_json,
            "INVALID_SIGNAL_JSON",
        )
        if canonical_evaluation != evaluation.payload_json:
            raise ValueError("NONCANONICAL_STRATEGY_EVALUATION_JSON")
        if canonical_signal != signal.payload_json:
            raise ValueError("NONCANONICAL_SIGNAL_JSON")
        if (
            evaluation.origin == "RECOVERED_AFTER_DOWNTIME"
            and evaluation.execution_eligible
        ):
            raise ValueError("RECOVERED_EVALUATION_EXECUTION_FORBIDDEN")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            evaluation_cursor = self._connection.execute(
                """
                INSERT INTO strategy_evaluations(
                    evaluation_key, strategy_id, strategy_version, status,
                    input_snapshot_hash, evaluation_revision,
                    execution_eligible, evaluated_at_ms, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            evaluation_inserted = evaluation_cursor.rowcount == 1
            stored_evaluation = self._connection.execute(
                """
                SELECT evaluation_id, strategy_id, strategy_version, status,
                       input_snapshot_hash, evaluation_revision,
                       execution_eligible, evaluated_at_ms, payload_json
                FROM strategy_evaluations WHERE evaluation_key = ?
                """,
                (evaluation.evaluation_key,),
            ).fetchone()
            expected_evaluation = (
                evaluation.strategy_id,
                evaluation.strategy_version,
                evaluation.status,
                evaluation.input_snapshot_hash,
                evaluation.evaluation_revision,
                int(evaluation.execution_eligible),
                evaluation.evaluated_at_ms,
                evaluation.payload_json,
            )
            if (
                stored_evaluation is None
                or tuple(stored_evaluation[1:]) != expected_evaluation
            ):
                raise ValueError("STRATEGY_EVALUATION_CONFLICT")

            signal_cursor = self._connection.execute(
                """
                INSERT INTO signals(
                    identity_key, evaluation_key, strategy_id, signal_type,
                    payload_json, created_at_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO NOTHING
                """,
                (
                    signal.identity_key,
                    signal.evaluation_key,
                    signal.strategy_id,
                    signal.signal_type,
                    signal.payload_json,
                    signal.created_at_ms,
                ),
            )
            signal_inserted = signal_cursor.rowcount == 1
            stored_signal = self._connection.execute(
                """
                SELECT signal_id, evaluation_key, strategy_id, signal_type,
                       payload_json, created_at_ms
                FROM signals WHERE identity_key = ?
                """,
                (signal.identity_key,),
            ).fetchone()
            expected_signal = (
                signal.evaluation_key,
                signal.strategy_id,
                signal.signal_type,
                signal.payload_json,
                signal.created_at_ms,
            )
            if stored_signal is None or tuple(stored_signal[1:]) != expected_signal:
                raise ValueError("SIGNAL_IDENTITY_CONFLICT")

            signal_id = stored_signal[0]
            outbox_cursor = self._connection.execute(
                """
                INSERT INTO outbox_events(
                    signal_id, topic, payload_json, created_at_ms
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(signal_id) DO NOTHING
                """,
                (
                    signal_id,
                    topic,
                    signal.payload_json,
                    signal.created_at_ms,
                ),
            )
            outbox_inserted = outbox_cursor.rowcount == 1
            stored_outbox = self._connection.execute(
                """
                SELECT event_id, topic, payload_json, created_at_ms
                FROM outbox_events WHERE signal_id = ?
                """,
                (signal_id,),
            ).fetchone()
            expected_outbox = (
                topic,
                signal.payload_json,
                signal.created_at_ms,
            )
            if stored_outbox is None or tuple(stored_outbox[1:]) != expected_outbox:
                raise ValueError("OUTBOX_SIGNAL_CONFLICT")
            if len(
                {
                    evaluation_inserted,
                    signal_inserted,
                    outbox_inserted,
                }
            ) != 1:
                raise RuntimeError("CANARY_ATOMICITY_VIOLATION")
            evaluation_id = stored_evaluation[0]
            outbox_id = stored_outbox[0]
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

        if outbox_inserted and broker is not None:
            broker.publish_committed(outbox_id)
        return evaluation_id, signal_id, outbox_id

    def read_outbox_after(
        self,
        event_id: int,
        limit: int,
    ) -> list[OutboxEvent]:
        if type(event_id) is not int:
            raise ValueError("INVALID_OUTBOX_EVENT_ID_TYPE")
        if event_id < 0:
            raise ValueError("INVALID_OUTBOX_EVENT_ID")
        if type(limit) is not int:
            raise ValueError("INVALID_OUTBOX_LIMIT_TYPE")
        if not 1 <= limit <= 1000:
            raise ValueError("INVALID_OUTBOX_LIMIT")

        rows = self.rows(
            """
            SELECT event_id, topic, payload_json, created_at_ms
            FROM outbox_events
            WHERE event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (event_id, limit),
        )
        return [
            OutboxEvent(
                event_id=row[0],
                topic=row[1],
                payload_json=row[2],
                created_at_ms=row[3],
            )
            for row in rows
        ]

    def open_read_only(self) -> sqlite3.Connection:
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def open_read_store(self) -> SqliteReadStore:
        return SqliteReadStore(self.open_read_only())

    def paper_ledger(self, *, publisher: CommittedPublisher | None = None):
        from src.paper import PaperLedger

        return PaperLedger(
            self._connection,
            publisher=publisher,
            owns_connection=False,
        )

    def has_paper_execution_for_date(self, market_date: str) -> bool:
        _require_nonempty_string(market_date, "INVALID_PAPER_MARKET_DATE")
        return self.scalar(
            "SELECT 1 FROM paper_intents WHERE market_date = ? LIMIT 1",
            (market_date,),
        ) == 1

    def integrity_report(self) -> dict[str, Any]:
        quick_check = self.scalar("PRAGMA quick_check")
        integrity_check = self.scalar("PRAGMA integrity_check")
        foreign_key_violations = len(self.rows("PRAGMA foreign_key_check"))
        foreign_keys = self.scalar("PRAGMA foreign_keys")
        journal_mode = self.scalar("PRAGMA journal_mode").lower()
        migration_version = self.scalar(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        status = (
            "PASS"
            if (
                quick_check == "ok"
                and integrity_check == "ok"
                and foreign_key_violations == 0
                and foreign_keys == 1
                and journal_mode == "wal"
                and migration_version == max(
                    int(path.name[:4])
                    for path in _MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
                )
            )
            else "FAIL"
        )
        return {
            "status": status,
            "quick_check": quick_check,
            "integrity_check": integrity_check,
            "foreign_keys": foreign_keys,
            "foreign_key_violations": foreign_key_violations,
            "journal_mode": journal_mode,
            "migration_version": migration_version,
        }

    def scalar(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> Any:
        row = self._connection.execute(sql, parameters).fetchone()
        return None if row is None else row[0]

    def rows(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        return self._connection.execute(sql, parameters).fetchall()

    def count(self, table: str) -> int:
        if table not in _COUNTABLE_TABLES:
            raise ValueError("INVALID_COUNT_TABLE")
        return self.scalar(f"SELECT COUNT(*) FROM {table}")


class SqliteReadStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def scalar(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> Any:
        row = self._connection.execute(sql, parameters).fetchone()
        return None if row is None else row[0]

    def rows(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        return self._connection.execute(sql, parameters).fetchall()

    def health(self) -> dict[str, Any]:
        quick_check = self.scalar("PRAGMA quick_check")
        foreign_keys = self.scalar("PRAGMA foreign_keys")
        journal_mode = self.scalar("PRAGMA journal_mode").lower()
        status = (
            "PASS"
            if quick_check == "ok"
            and foreign_keys == 1
            and journal_mode == "wal"
            else "FAIL"
        )
        return {
            "database": {
                "foreign_keys": foreign_keys,
                "journal_mode": journal_mode,
                "quick_check": quick_check,
            },
            "status": status,
        }

    def sources(self) -> list[dict[str, Any]]:
        rows = self.rows(
            """
            SELECT
                source,
                COUNT(*),
                MAX(source_timestamp_ms),
                MAX(received_timestamp_ms)
            FROM source_events
            GROUP BY source
            ORDER BY source ASC
            """
        )
        return [
            {
                "event_count": row[1],
                "last_received_timestamp_ms": row[3],
                "last_source_timestamp_ms": row[2],
                "source": row[0],
            }
            for row in rows
        ]

    def signals(self, *, limit: int) -> list[dict[str, Any]]:
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT
                signal_id,
                identity_key,
                evaluation_key,
                strategy_id,
                signal_type,
                payload_json,
                created_at_ms
            FROM signals
            ORDER BY signal_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "created_at_ms": row[6],
                "evaluation_key": row[2],
                "identity_key": row[1],
                "payload": _decode_stored_json(row[5]),
                "signal_id": row[0],
                "signal_type": row[4],
                "strategy_id": row[3],
            }
            for row in rows
        ]

    def incidents(self, *, limit: int) -> list[dict[str, Any]]:
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT
                incident_id,
                incident_key,
                severity,
                status,
                payload_json,
                created_at_ms
            FROM incidents
            ORDER BY incident_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "created_at_ms": row[5],
                "incident_id": row[0],
                "incident_key": row[1],
                "payload": _decode_stored_json(row[4]),
                "severity": row[2],
                "status": row[3],
            }
            for row in rows
        ]

    def current_market_identity(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT market_id, payload_json, updated_at_ms
            FROM market_catalog
            ORDER BY updated_at_ms DESC, market_id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "market_id": row[0],
            "payload": _decode_stored_json(row[1]),
            "updated_at_ms": row[2],
        }

    def latest_lifecycle_state(
        self,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        if run_id is not None:
            _require_nonempty_string(run_id, "INVALID_LIFECYCLE_RUN_ID")
            escaped_run_id = _escape_like(run_id)
            pattern = f"lifecycle:{escaped_run_id}:%"
        else:
            pattern = "lifecycle:%"
        row = self._connection.execute(
            """
            SELECT severity, status, payload_json, created_at_ms
            FROM incidents
            WHERE incident_key LIKE ? ESCAPE '\\'
            ORDER BY created_at_ms DESC, incident_id DESC
            LIMIT 1
            """,
            (pattern,),
        ).fetchone()
        if row is None:
            return None
        payload = _decode_stored_json(row[2])
        if (
            type(payload) is not dict
            or payload.get("record_type") != "LIFECYCLE_STATE"
        ):
            raise ValueError("INVALID_STORED_LIFECYCLE_STATE")
        return {
            **payload,
            "severity": row[0],
            "status": row[1],
            "created_at_ms": row[3],
        }

    def latest_canonical_snapshot(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT
                snapshot_id,
                snapshot_key,
                source_event_ids_json,
                payload_json,
                payload_sha256,
                recovery_origin,
                created_at_ms
            FROM canonical_state
            ORDER BY created_at_ms DESC, snapshot_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        source_event_ids = _decode_stored_json(row[2])
        if (
            type(source_event_ids) is not list
            or any(type(event_id) is not int for event_id in source_event_ids)
        ):
            raise ValueError("INVALID_STORED_SOURCE_EVENT_IDS")
        return {
            "snapshot_id": row[0],
            "snapshot_key": row[1],
            "source_event_ids": source_event_ids,
            "payload": _decode_stored_json(row[3]),
            "payload_sha256": row[4],
            "recovery_origin": row[5],
            "created_at_ms": row[6],
        }

    def strategy_evaluations(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT
                evaluation_id,
                evaluation_key,
                strategy_id,
                strategy_version,
                status,
                input_snapshot_hash,
                evaluation_revision,
                execution_eligible,
                evaluated_at_ms,
                payload_json
            FROM strategy_evaluations
            ORDER BY evaluated_at_ms DESC, evaluation_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "evaluation_id": row[0],
                "evaluation_key": row[1],
                "strategy_id": row[2],
                "strategy_version": row[3],
                "status": row[4],
                "input_snapshot_hash": row[5],
                "evaluation_revision": row[6],
                "execution_eligible": bool(row[7]),
                "evaluated_at_ms": row[8],
                "payload": _decode_stored_json(row[9]),
            }
            for row in rows
        ]

    def paper_account(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT schema_version, account_key, starting_bankroll_usd_micros,
                cash_usd_micros, open_cost_basis_usd_micros, equity_usd_micros,
                realized_pnl_usd_micros, unrealized_pnl_usd_micros, updated_at_ms
            FROM paper_accounts WHERE account_key = 'default'
            """
        ).fetchone()
        if row is None:
            return None
        names = (
            "schema_version", "account_key", "starting_bankroll_usd_micros",
            "cash_usd_micros", "open_cost_basis_usd_micros", "equity_usd_micros",
            "realized_pnl_usd_micros", "unrealized_pnl_usd_micros", "updated_at_ms",
        )
        return dict(zip(names, row))

    def paper_positions(self) -> list[dict[str, Any]]:
        rows = self.rows(
            """
            SELECT schema_version, position_key, intent_key, market_id,
                market_date, token_id, side, status, filled_shares_micros,
                average_price_micros, cost_basis_usd_micros, fees_paid_usd_micros,
                settlement_value_usd_micros, realized_pnl_usd_micros,
                unrealized_pnl_usd_micros, opened_at_ms, updated_at_ms, settled_at_ms
            FROM paper_positions ORDER BY market_date DESC
            """
        )
        names = (
            "schema_version", "position_key", "intent_key", "market_id",
            "market_date", "token_id", "side", "status", "filled_shares_micros",
            "average_price_micros", "cost_basis_usd_micros", "fees_paid_usd_micros",
            "settlement_value_usd_micros", "realized_pnl_usd_micros",
            "unrealized_pnl_usd_micros", "opened_at_ms", "updated_at_ms", "settled_at_ms",
        )
        return [dict(zip(names, row)) for row in rows]

    def paper_fills(self) -> list[dict[str, Any]]:
        rows = self.rows(
            """
            SELECT schema_version, fill_key, intent_key, fill_sequence,
                shares_micros, price_micros, fee_usd_micros, gross_cost_usd_micros,
                evidence_key, book_sha256, filled_at_ms
            FROM paper_fills ORDER BY filled_at_ms DESC, fill_key DESC
            """
        )
        names = (
            "schema_version", "fill_key", "intent_key", "fill_sequence",
            "shares_micros", "price_micros", "fee_usd_micros", "gross_cost_usd_micros",
            "evidence_key", "book_sha256", "filled_at_ms",
        )
        return [dict(zip(names, row)) for row in rows]

    def paper_readiness(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT schema_version, readiness_key, market_id, market_date,
                checkpoint_minutes, signal_key, evidence_key, ready, reason_code,
                requested_shares_micros, checked_at_ms
            FROM paper_execution_readiness
            ORDER BY checked_at_ms DESC, readiness_key DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        names = (
            "schema_version", "readiness_key", "market_id", "market_date",
            "checkpoint_minutes", "signal_key", "evidence_key", "ready", "reason_code",
            "requested_shares_micros", "checked_at_ms",
        )
        payload = dict(zip(names, row))
        payload["ready"] = bool(payload["ready"])
        return payload

    def latest_strict_a_signal(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT payload_json FROM signals
            WHERE strategy_id IN (
                'YES_STRICT_A_OPERATIONAL', 'YES_STRICT_A_T60', 'YES_STRICT_A_T30'
            )
              AND signal_type='STRICT_A_SIGNAL_V1'
            ORDER BY created_at_ms DESC, signal_id DESC LIMIT 1
            """
        ).fetchone()
        return None if row is None else _decode_stored_json(row[0])

    def last_event_id(self) -> int:
        return self.scalar(
            "SELECT COALESCE(MAX(event_id), 0) FROM outbox_events"
        )

    def read_outbox_after(
        self,
        event_id: int,
        limit: int,
    ) -> list[OutboxEvent]:
        if type(event_id) is not int:
            raise ValueError("INVALID_OUTBOX_EVENT_ID_TYPE")
        if event_id < 0:
            raise ValueError("INVALID_OUTBOX_EVENT_ID")
        _validate_read_limit(limit)
        rows = self.rows(
            """
            SELECT event_id, topic, payload_json, created_at_ms
            FROM outbox_events
            WHERE event_id > ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (event_id, limit),
        )
        return [
            OutboxEvent(
                event_id=row[0],
                topic=row[1],
                payload_json=row[2],
                created_at_ms=row[3],
            )
            for row in rows
        ]


def _validate_read_limit(limit: int) -> None:
    if type(limit) is not int:
        raise ValueError("INVALID_READ_LIMIT_TYPE")
    if not 1 <= limit <= 1000:
        raise ValueError("INVALID_READ_LIMIT")


def _require_nonempty_string(value: Any, error_code: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(error_code)


def _require_nonnegative_integer(value: Any, error_code: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(error_code)


def _canonical_json(value: Any, error_code: str) -> str:
    if type(value) is not str:
        raise ValueError(error_code)
    try:
        decoded = json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(constant)
            ),
        )
        return json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError(error_code) from None


def _require_payload_hash(value: str, expected_hash: Any) -> None:
    if (
        type(expected_hash) is not str
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
        or hashlib.sha256(value.encode("utf-8")).hexdigest() != expected_hash
    ):
        raise ValueError("PAYLOAD_SHA256_MISMATCH")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _decode_stored_json(value: str) -> Any:
    return json.loads(value)


class SqliteWriter:
    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError("INVALID_DATABASE_PATH_TYPE")
        self._path = path

    async def run(self, queue: asyncio.Queue[WriteCommand]) -> None:
        store = SqliteStore.open(self._path)
        try:
            store.migrate()
            while True:
                command = await queue.get()
                try:
                    if type(command) is not WriteCommand:
                        raise ValueError("INVALID_WRITE_COMMAND")
                    if command.operation == "STOP":
                        return
                    if command.operation != "APPEND_SOURCE_EVENT":
                        raise ValueError("UNKNOWN_WRITE_COMMAND")
                    if command.event is None or command.future is None:
                        raise ValueError("INCOMPLETE_WRITE_COMMAND")
                    try:
                        result = store.append_source_event(command.event)
                    except BaseException as error:
                        if not command.future.done():
                            command.future.set_exception(error)
                    else:
                        if not command.future.done():
                            command.future.set_result(result)
                finally:
                    queue.task_done()
        finally:
            store.close()
