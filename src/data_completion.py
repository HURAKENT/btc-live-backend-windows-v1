from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Any

from src.models import SourceEvent
from src.storage import SqliteStore


COMPLETENESS_STATES = (
    "COMPLETE",
    "EXPECTED_ABSENT",
    "SOURCE_UNAVAILABLE_RETRYABLE",
    "SOURCE_CONFLICT_FATAL",
    "NOT_REQUIRED_BY_CONTRACT",
)
_SHA64 = re.compile(r"[0-9a-f]{64}")
BACKEND_MUTEX_NAME = "BTC_LIVE_BACKEND_WINDOWS_V1"


def _canonical(value: str, code: str) -> str:
    if type(value) is not str:
        raise ValueError(code)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(code) from None
    encoded = json.dumps(
        decoded,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if encoded != value:
        raise ValueError(code)
    return encoded


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(code)
    return value


def _sha(value: object, code: str) -> str:
    if type(value) is not str or _SHA64.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _integer(value: object, code: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(code)
    return value


def _identity(domain: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"domain": domain, "payload": payload},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ImportRunRecord:
    import_run_id: str
    artifact_sha256: str
    source_path_fingerprint: str
    dataset_start_ms: int
    dataset_end_ms: int
    declared_row_count: int
    schema_mapping_sha256: str
    inserted_row_count: int
    replayed_row_count: int
    conflict_row_count: int
    dropped_row_count: int
    status: str
    created_at_ms: int

    def __post_init__(self) -> None:
        _text(self.import_run_id, "INVALID_IMPORT_RUN_ID")
        for value in (
            self.artifact_sha256,
            self.source_path_fingerprint,
            self.schema_mapping_sha256,
        ):
            _sha(value, "INVALID_IMPORT_PROVENANCE_SHA256")
        for value in (
            self.dataset_start_ms,
            self.dataset_end_ms,
            self.declared_row_count,
            self.inserted_row_count,
            self.replayed_row_count,
            self.conflict_row_count,
            self.dropped_row_count,
            self.created_at_ms,
        ):
            _integer(value, "INVALID_IMPORT_RUN_BOUNDARY")
        if self.dataset_start_ms >= self.dataset_end_ms:
            raise ValueError("INVALID_IMPORT_RUN_BOUNDARY")
        if self.status not in COMPLETENESS_STATES:
            raise ValueError("INVALID_COMPLETENESS_STATUS")
        if self.dropped_row_count != 0:
            raise ValueError("IMPORT_DROPPED_ROWS_FORBIDDEN")
        if self.declared_row_count != (
            self.inserted_row_count
            + self.replayed_row_count
            + self.conflict_row_count
        ):
            raise ValueError("IMPORT_ROW_COUNTS_MISMATCH")
        if self.status != "COMPLETE" or self.conflict_row_count != 0:
            raise ValueError("IMPORT_STATUS_SEMANTICS_INVALID")


@dataclass(frozen=True, slots=True)
class SourceRangeRecord:
    import_run_id: str | None
    source: str
    canonical_scope_json: str
    canonical_scope_sha256: str
    requested_start_ms: int
    requested_end_ms: int
    granularity_ms: int
    contract_version: str
    status: str
    expected_row_count: int
    observed_row_count: int
    missing_row_count: int
    reason_code: str | None
    evidence_metadata_json: str
    evidence_sha256: str
    updated_at_ms: int

    def __post_init__(self) -> None:
        if self.import_run_id is not None:
            _text(self.import_run_id, "INVALID_IMPORT_RUN_ID")
        _text(self.source, "INVALID_SOURCE_RANGE_SOURCE")
        _text(self.contract_version, "INVALID_SOURCE_RANGE_CONTRACT")
        scope = _canonical(self.canonical_scope_json, "INVALID_SOURCE_RANGE_SCOPE")
        if hashlib.sha256(scope.encode()).hexdigest() != self.canonical_scope_sha256:
            raise ValueError("SOURCE_RANGE_SCOPE_HASH_MISMATCH")
        _integer(self.requested_start_ms, "INVALID_SOURCE_RANGE_BOUNDARY")
        _integer(self.requested_end_ms, "INVALID_SOURCE_RANGE_BOUNDARY")
        _integer(self.granularity_ms, "INVALID_SOURCE_RANGE_BOUNDARY", positive=True)
        if (
            self.requested_start_ms >= self.requested_end_ms
            or self.requested_start_ms % self.granularity_ms != 0
            or self.requested_end_ms % self.granularity_ms != 0
        ):
            raise ValueError("INVALID_SOURCE_RANGE_BOUNDARY")
        if self.status not in COMPLETENESS_STATES:
            raise ValueError("INVALID_COMPLETENESS_STATUS")
        for value in (
            self.expected_row_count,
            self.observed_row_count,
            self.missing_row_count,
            self.updated_at_ms,
        ):
            _integer(value, "INVALID_SOURCE_RANGE_COUNT")
        evidence = _canonical(
            self.evidence_metadata_json,
            "INVALID_SOURCE_RANGE_EVIDENCE",
        )
        if hashlib.sha256(evidence.encode()).hexdigest() != self.evidence_sha256:
            raise ValueError("SOURCE_RANGE_EVIDENCE_HASH_MISMATCH")
        decoded_evidence = json.loads(evidence)
        if self.status == "COMPLETE" and (
            self.missing_row_count != 0
            or self.expected_row_count != self.observed_row_count
        ):
            raise ValueError("INCOMPLETE_RANGE_CANNOT_BE_COMPLETE")
        if self.status == "EXPECTED_ABSENT":
            inventory = decoded_evidence.get("inventory") if type(decoded_evidence) is dict else None
            if (
                self.observed_row_count != 0
                or self.missing_row_count != self.expected_row_count
                or type(inventory) is not dict
                or inventory.get("absence_confirmed") is not True
                or _SHA64.fullmatch(str(inventory.get("artifact_sha256", ""))) is None
                or inventory.get("artifact_type") != "SANITIZED_PROVIDER_INVENTORY_V1"
                or type(inventory.get("reason_code")) is not str
                or not inventory["reason_code"]
                or inventory["reason_code"] != self.reason_code
            ):
                raise ValueError("EXPECTED_ABSENT_EVIDENCE_REQUIRED")
        if self.status in {
            "SOURCE_UNAVAILABLE_RETRYABLE",
            "SOURCE_CONFLICT_FATAL",
        } and (type(self.reason_code) is not str or not self.reason_code):
            raise ValueError("SOURCE_RANGE_REASON_REQUIRED")
        if self.status == "NOT_REQUIRED_BY_CONTRACT" and (
            self.expected_row_count or self.observed_row_count or self.missing_row_count
        ):
            raise ValueError("SOURCE_RANGE_STATUS_SEMANTICS_INVALID")
        if self.status in {"SOURCE_UNAVAILABLE_RETRYABLE", "SOURCE_CONFLICT_FATAL"} and (
            self.observed_row_count + self.missing_row_count != self.expected_row_count
        ):
            raise ValueError("SOURCE_RANGE_STATUS_SEMANTICS_INVALID")

    @property
    def range_key(self) -> str:
        return _identity(
            "DATA_SOURCE_RANGE_V1",
            {
                "canonical_scope_sha256": self.canonical_scope_sha256,
                "contract_version": self.contract_version,
                "granularity_ms": self.granularity_ms,
                "requested_end_ms": self.requested_end_ms,
                "requested_start_ms": self.requested_start_ms,
                "source": self.source,
            },
        )

    @property
    def assessment_key(self) -> str:
        return _identity(
            "DATA_SOURCE_RANGE_ASSESSMENT_V1",
            {
                "evidence_sha256": self.evidence_sha256,
                "import_run_id": self.import_run_id,
                "range_key": self.range_key,
                "reason_code": self.reason_code,
                "status": self.status,
                "updated_at_ms": self.updated_at_ms,
            },
        )


@dataclass(frozen=True, slots=True)
class ImportCommitResult:
    import_run_row_id: int
    inserted: bool


class DataCompletionLedger:
    def __init__(self, store: SqliteStore) -> None:
        if type(store) is not SqliteStore:
            raise ValueError("INVALID_DATA_COMPLETION_STORE")
        self._connection = store._connection

    def commit_import(
        self,
        run: ImportRunRecord,
        ranges: tuple[SourceRangeRecord, ...],
        events: tuple[SourceEvent, ...],
    ) -> ImportCommitResult:
        self._validate_import(run, ranges, events)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            result = self._commit_import_in_transaction(run, ranges, events)
            self._connection.commit()
            return result
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def commit_import_pack(
        self,
        imports: tuple[
            tuple[ImportRunRecord, tuple[SourceRangeRecord, ...], tuple[SourceEvent, ...]], ...
        ],
    ) -> tuple[ImportCommitResult, ...]:
        if type(imports) is not tuple or not imports:
            raise ValueError("INVALID_IMPORT_PACK")
        for item in imports:
            if type(item) is not tuple or len(item) != 3:
                raise ValueError("INVALID_IMPORT_PACK")
            self._validate_import(*item)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            results = tuple(self._commit_import_in_transaction(*item) for item in imports)
            self._connection.commit()
            return results
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _validate_import(
        self,
        run: ImportRunRecord,
        ranges: tuple[SourceRangeRecord, ...],
        events: tuple[SourceEvent, ...],
    ) -> None:
        if type(run) is not ImportRunRecord:
            raise ValueError("INVALID_IMPORT_RUN")
        if type(ranges) is not tuple or any(type(x) is not SourceRangeRecord for x in ranges):
            raise ValueError("INVALID_SOURCE_RANGES")
        if type(events) is not tuple or any(type(x) is not SourceEvent for x in events):
            raise ValueError("INVALID_IMPORT_EVENTS")
        if any(item.import_run_id != run.import_run_id for item in ranges):
            raise ValueError("SOURCE_RANGE_IMPORT_RUN_MISMATCH")
        if len(events) != run.declared_row_count:
            raise ValueError("IMPORT_INPUT_COUNT_MISMATCH")
        if not ranges or any(item.status != "COMPLETE" for item in ranges):
            raise ValueError("IMPORT_COMPLETE_RANGES_REQUIRED")
        event_matches: dict[str, list[SourceEvent]] = {item.range_key: [] for item in ranges}
        for event in events:
            matches = [
                item for item in ranges if (
                item.source == event.source
                and item.requested_start_ms <= event.source_timestamp_ms < item.requested_end_ms
                )
            ]
            if not matches:
                raise ValueError("IMPORT_EVENT_OUTSIDE_DECLARED_RANGE")
            if len(matches) != 1:
                raise ValueError("IMPORT_EVENT_RANGE_AMBIGUOUS")
            event_matches[matches[0].range_key].append(event)
            if not run.dataset_start_ms <= event.source_timestamp_ms < run.dataset_end_ms:
                raise ValueError("IMPORT_EVENT_OUTSIDE_DATASET_BOUNDARY")
        for item in ranges:
            observed = len({event.natural_key for event in event_matches[item.range_key]})
            if item.observed_row_count != observed:
                raise ValueError("SOURCE_RANGE_OBSERVED_COUNT_MISMATCH")
            if item.source == "binance":
                expected = (item.requested_end_ms - item.requested_start_ms) // item.granularity_ms
                if item.expected_row_count != expected:
                    raise ValueError("SOURCE_RANGE_EXPECTED_COUNT_MISMATCH")
                expected_timestamps = tuple(
                    range(item.requested_start_ms, item.requested_end_ms, item.granularity_ms)
                )
                actual_timestamps = tuple(
                    sorted(event.source_timestamp_ms for event in event_matches[item.range_key])
                )
                if actual_timestamps != expected_timestamps:
                    raise ValueError("DENSE_RANGE_TIMESTAMP_COVERAGE_MISMATCH")
            else:
                evidence = json.loads(item.evidence_metadata_json)
                expected_hashes = evidence.get("expected_event_natural_key_sha256") if type(evidence) is dict else None
                if (
                    type(expected_hashes) is not list
                    or any(type(value) is not str or _SHA64.fullmatch(value) is None for value in expected_hashes)
                    or expected_hashes != sorted(set(expected_hashes))
                    or len(expected_hashes) != item.expected_row_count
                ):
                    raise ValueError("SPARSE_RANGE_EXPECTED_KEYS_REQUIRED")
                actual_hashes = sorted(
                    hashlib.sha256(event.natural_key.encode()).hexdigest()
                    for event in event_matches[item.range_key]
                )
                if actual_hashes != expected_hashes:
                    raise ValueError("SPARSE_RANGE_EXPECTED_KEYS_MISMATCH")

    def _commit_import_in_transaction(
        self,
        run: ImportRunRecord,
        ranges: tuple[SourceRangeRecord, ...],
        events: tuple[SourceEvent, ...],
    ) -> ImportCommitResult:
        existing = self._read_run(run.import_run_id)
        if existing is not None:
            row_id = self._verify_run(run, existing)
            self._verify_existing_import(run, ranges, events)
            return ImportCommitResult(row_id, False)
        outcomes = tuple(self._classify_event(event) for event in events)
        inserted_count = sum(item[0] for item in outcomes)
        replayed_count = len(outcomes) - inserted_count
        if (run.inserted_row_count, run.replayed_row_count, run.conflict_row_count) != (
            inserted_count, replayed_count, 0
        ):
            raise ValueError("IMPORT_ROW_COUNTS_MISMATCH")
        inserted, row_id = self._insert_run(run)
        for item in ranges:
            self._insert_range(item)
        for event, (will_insert, _) in zip(events, outcomes, strict=True):
            self._insert_event(run.import_run_id, event, will_insert)
        return ImportCommitResult(row_id, inserted)

    def _insert_run(self, run: ImportRunRecord) -> tuple[bool, int]:
        names = tuple(field.name for field in fields(run))
        values = tuple(getattr(run, name) for name in names)
        cursor = self._connection.execute(
            f"INSERT INTO data_import_runs({','.join(names)}) "
            f"VALUES ({','.join('?' for _ in names)}) "
            "ON CONFLICT(import_run_id) DO NOTHING",
            values,
        )
        stored = self._connection.execute(
            f"SELECT import_run_row_id,{','.join(names[1:])} "
            "FROM data_import_runs WHERE import_run_id=?",
            (run.import_run_id,),
        ).fetchone()
        if stored is None or tuple(stored[1:]) != values[1:]:
            raise ValueError("DATA_IMPORT_RUN_CONFLICT")
        return cursor.rowcount == 1, stored[0]

    def _read_run(self, import_run_id: str):
        names = tuple(field.name for field in fields(ImportRunRecord))
        return self._connection.execute(
            f"SELECT import_run_row_id,{','.join(names)} FROM data_import_runs WHERE import_run_id=?",
            (import_run_id,),
        ).fetchone()

    def _verify_run(self, run: ImportRunRecord, stored) -> int:
        values = tuple(getattr(run, field.name) for field in fields(run))
        if tuple(stored[1:]) != values:
            raise ValueError("DATA_IMPORT_RUN_CONFLICT")
        return stored[0]

    def _insert_range(self, item: SourceRangeRecord) -> None:
        definition_names = (
            "source","canonical_scope_json","canonical_scope_sha256",
            "requested_start_ms","requested_end_ms","granularity_ms","contract_version",
        )
        values = tuple(getattr(item, name) for name in definition_names)
        self._connection.execute(
            f"INSERT INTO data_source_ranges(range_key,{','.join(definition_names)}) "
            f"VALUES ({','.join('?' for _ in range(len(definition_names) + 1))}) "
            "ON CONFLICT(range_key) DO NOTHING",
            (item.range_key, *values),
        )
        stored = self._connection.execute(
            f"SELECT {','.join(definition_names)} FROM data_source_ranges WHERE range_key=?",
            (item.range_key,),
        ).fetchone()
        if stored is None or tuple(stored) != values:
            raise ValueError("SOURCE_RANGE_CONFLICT")

        self._insert_assessment(item)

    def _insert_assessment(self, item: SourceRangeRecord) -> None:
        names = (
            "range_key","import_run_id","status","expected_row_count",
            "observed_row_count","missing_row_count","reason_code",
            "evidence_metadata_json","evidence_sha256","updated_at_ms",
        )
        values = (item.range_key, *(getattr(item, name) for name in names[1:]))
        exact = self._connection.execute(
            f"SELECT {','.join(names)} FROM data_source_range_assessments WHERE assessment_key=?",
            (item.assessment_key,),
        ).fetchone()
        if exact is not None:
            if tuple(exact) != values:
                raise ValueError("SOURCE_RANGE_ASSESSMENT_CONFLICT")
            return
        latest = self._connection.execute(
            "SELECT status,updated_at_ms FROM data_source_range_assessments "
            "WHERE range_key=? ORDER BY updated_at_ms DESC,assessment_row_id DESC LIMIT 1",
            (item.range_key,),
        ).fetchone()
        if latest is not None:
            if item.updated_at_ms <= latest[1]:
                raise ValueError("SOURCE_RANGE_ASSESSMENT_NOT_MONOTONIC")
            if latest[0] in {"COMPLETE", "EXPECTED_ABSENT", "NOT_REQUIRED_BY_CONTRACT", "SOURCE_CONFLICT_FATAL"} and not (
                latest[0] == "COMPLETE" and item.status == "COMPLETE" and item.import_run_id is not None
            ):
                raise ValueError("SOURCE_RANGE_TERMINAL_REGRESSION")
        self._connection.execute(
            f"INSERT INTO data_source_range_assessments(assessment_key,{','.join(names)}) "
            f"VALUES ({','.join('?' for _ in range(len(names) + 1))}) "
            "ON CONFLICT(assessment_key) DO NOTHING",
            (item.assessment_key, *values),
        )
        stored = self._connection.execute(
            f"SELECT {','.join(names)} FROM data_source_range_assessments WHERE assessment_key=?",
            (item.assessment_key,),
        ).fetchone()
        if stored is None or tuple(stored) != values:
            raise ValueError("SOURCE_RANGE_ASSESSMENT_CONFLICT")

    def record_range_assessment(
        self,
        item: SourceRangeRecord,
        *,
        inventory_artifact_bytes: bytes | None = None,
    ) -> None:
        if type(item) is not SourceRangeRecord or item.import_run_id is not None:
            raise ValueError("INVALID_STANDALONE_RANGE_ASSESSMENT")
        if item.status == "EXPECTED_ABSENT":
            if type(inventory_artifact_bytes) is not bytes:
                raise ValueError("EXPECTED_ABSENT_INVENTORY_BYTES_REQUIRED")
            inventory = json.loads(item.evidence_metadata_json)["inventory"]
            if hashlib.sha256(inventory_artifact_bytes).hexdigest() != inventory["artifact_sha256"]:
                raise ValueError("EXPECTED_ABSENT_INVENTORY_HASH_MISMATCH")
            try:
                artifact_text = inventory_artifact_bytes.decode("utf-8")
                artifact = json.loads(artifact_text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("EXPECTED_ABSENT_INVENTORY_CONTRACT_MISMATCH") from None
            if (
                json.dumps(
                    artifact,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ) != artifact_text
                or artifact != {
                    "canonical_scope_sha256": item.canonical_scope_sha256,
                    "observed_row_count": 0,
                    "reason_code": item.reason_code,
                    "requested_end_ms": item.requested_end_ms,
                    "requested_start_ms": item.requested_start_ms,
                    "schema_version": inventory["artifact_type"],
                    "source": item.source,
                }
            ):
                raise ValueError("EXPECTED_ABSENT_INVENTORY_CONTRACT_MISMATCH")
        elif inventory_artifact_bytes is not None:
            raise ValueError("UNEXPECTED_INVENTORY_ARTIFACT_BYTES")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_range(item)
            self._connection.commit()
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _classify_event(self, event: SourceEvent) -> tuple[bool, int | None]:
        stored = self._connection.execute(
            "SELECT event_id,source,source_timestamp_ms,received_timestamp_ms,event_type,payload_json,payload_sha256,recovery_origin FROM source_events WHERE natural_key=?",
            (event.natural_key,),
        ).fetchone()
        if stored is None:
            return True, None
        expected = (
            event.source,event.source_timestamp_ms,event.received_timestamp_ms,
            event.event_type,event.payload_json,event.payload_sha256,event.recovery_origin,
        )
        if tuple(stored[1:]) != expected:
            raise ValueError("SOURCE_EVENT_CONFLICT")
        return False, stored[0]

    def _insert_event(self, import_run_id: str, event: SourceEvent, will_insert: bool) -> None:
        self._connection.execute(
            """
            INSERT INTO source_events(
                source,natural_key,source_timestamp_ms,received_timestamp_ms,
                event_type,payload_json,payload_sha256,recovery_origin,
                committed_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(natural_key) DO NOTHING
            """,
            (
                event.source,event.natural_key,event.source_timestamp_ms,
                event.received_timestamp_ms,event.event_type,event.payload_json,
                event.payload_sha256,event.recovery_origin,
                event.received_timestamp_ms,
            ),
        )
        stored = self._connection.execute(
            "SELECT event_id,source,source_timestamp_ms,received_timestamp_ms,event_type,payload_json,"
            "payload_sha256,recovery_origin FROM source_events WHERE natural_key=?",
            (event.natural_key,),
        ).fetchone()
        expected = (
            event.source,event.source_timestamp_ms,event.received_timestamp_ms,event.event_type,
            event.payload_json,event.payload_sha256,event.recovery_origin,
        )
        if stored is None or tuple(stored[1:]) != expected:
            raise ValueError("SOURCE_EVENT_CONFLICT")
        self._connection.execute(
            "INSERT INTO data_import_run_events(import_run_id,event_id,outcome) VALUES (?,?,?)",
            (import_run_id, stored[0], "INSERTED" if will_insert else "REPLAYED"),
        )

    def _verify_existing_import(self, run, ranges, events) -> None:
        stored_assessments = self._connection.execute(
            "SELECT assessment_key FROM data_source_range_assessments WHERE import_run_id=? ORDER BY assessment_key",
            (run.import_run_id,),
        ).fetchall()
        expected_assessments = tuple(sorted(item.assessment_key for item in ranges))
        if tuple(row[0] for row in stored_assessments) != expected_assessments:
            raise ValueError("DATA_IMPORT_RUN_RANGE_SET_CONFLICT")
        for item in ranges:
            definition = self._connection.execute(
                "SELECT source,canonical_scope_json,canonical_scope_sha256,requested_start_ms,requested_end_ms,granularity_ms,contract_version FROM data_source_ranges WHERE range_key=?",
                (item.range_key,),
            ).fetchone()
            expected_definition = (
                item.source,item.canonical_scope_json,item.canonical_scope_sha256,
                item.requested_start_ms,item.requested_end_ms,item.granularity_ms,item.contract_version,
            )
            if definition is None or tuple(definition) != expected_definition:
                raise ValueError("SOURCE_RANGE_CONFLICT")
        linked = self._connection.execute(
            "SELECT se.natural_key,se.source,se.source_timestamp_ms,se.received_timestamp_ms,se.event_type,se.payload_json,se.payload_sha256,se.recovery_origin,l.outcome "
            "FROM data_import_run_events l JOIN source_events se ON se.event_id=l.event_id WHERE l.import_run_id=? ORDER BY se.natural_key",
            (run.import_run_id,),
        ).fetchall()
        expected_by_key = {
            event.natural_key: (
                event.natural_key,event.source,event.source_timestamp_ms,
                event.received_timestamp_ms,event.event_type,event.payload_json,
                event.payload_sha256,event.recovery_origin,
            )
            for event in events
        }
        if tuple(row[0] for row in linked) != tuple(sorted(expected_by_key)):
            raise ValueError("DATA_IMPORT_RUN_EVENT_SET_CONFLICT")
        for row in linked:
            if tuple(row[:-1]) != expected_by_key[row[0]]:
                raise ValueError("SOURCE_EVENT_CONFLICT")
        outcomes = tuple(row[-1] for row in linked)
        if outcomes.count("INSERTED") != run.inserted_row_count or outcomes.count("REPLAYED") != run.replayed_row_count:
            raise ValueError("DATA_IMPORT_RUN_OUTCOME_CONFLICT")


def execute_offline_import(
    database_path: Path,
    run: ImportRunRecord,
    ranges: tuple[SourceRangeRecord, ...],
    events: tuple[SourceEvent, ...],
    *,
    artifact_bytes: bytes,
    schema_mapping_bytes: bytes,
    mutex_factory: Callable[[str], Any] | None = None,
    trace: list[str] | None = None,
) -> ImportCommitResult:
    if not isinstance(database_path, Path):
        raise ValueError("INVALID_DATABASE_PATH_TYPE")
    if type(artifact_bytes) is not bytes or hashlib.sha256(artifact_bytes).hexdigest() != run.artifact_sha256:
        raise ValueError("IMPORT_ARTIFACT_HASH_MISMATCH")
    if type(schema_mapping_bytes) is not bytes or hashlib.sha256(schema_mapping_bytes).hexdigest() != run.schema_mapping_sha256:
        raise ValueError("IMPORT_SCHEMA_MAPPING_HASH_MISMATCH")
    if mutex_factory is None:
        from src.single_instance import WindowsMutex

        mutex_factory = WindowsMutex.acquire
    mutex = mutex_factory(BACKEND_MUTEX_NAME)
    store: SqliteStore | None = None
    try:
        store = SqliteStore.open(database_path)
        store.migrate()
        result = DataCompletionLedger(store).commit_import(run, ranges, events)
        if trace is not None:
            trace.append("transaction-complete")
        return result
    finally:
        try:
            if store is not None:
                store.close()
        finally:
            mutex.close()
