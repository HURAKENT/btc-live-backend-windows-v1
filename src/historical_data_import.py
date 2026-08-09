from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from src.data_completion import ImportRunRecord, SourceRangeRecord
from src.models import SourceEvent, payload_sha256


_FIXTURES = (
    ("V1_CONFIRMATION_BASKET", "V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl", "V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY_RECEIPT.json"),
    ("V1_EARLY_CONFIDENCE", "V1_EARLY_CONFIDENCE_FULL_DECISION_PARITY.jsonl", "V1_EARLY_CONFIDENCE_FULL_DECISION_PARITY_RECEIPT.json"),
    ("V1_EARLY_HORIZON", "V1_EARLY_HORIZON_FULL_DECISION_PARITY.jsonl", "V1_EARLY_HORIZON_FULL_DECISION_PARITY_RECEIPT.json"),
    ("V2_VOL_OVERLAY", "VOL_OVERLAY_DECISION_PARITY.jsonl", "VOL_OVERLAY_DECISION_PARITY_RECEIPT.json"),
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalImportPlan:
    run: ImportRunRecord
    source_range: SourceRangeRecord
    events: tuple[SourceEvent, ...]
    artifact_bytes: bytes
    schema_mapping_bytes: bytes
    receipt: dict[str, object]


def build_historical_import_plan(
    fixture_path: Path,
    receipt_path: Path,
    *,
    ordinal: int,
    authoritative_fixture_sha256: str | None = None,
) -> HistoricalImportPlan:
    if not isinstance(fixture_path, Path) or not isinstance(receipt_path, Path):
        raise ValueError("INVALID_HISTORICAL_IMPORT_PATH")
    artifact_bytes = fixture_path.read_bytes()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if _hash(artifact_bytes) != receipt.get("fixture_sha256"):
        raise ValueError("HISTORICAL_FIXTURE_HASH_MISMATCH")
    if (
        authoritative_fixture_sha256 is not None
        and receipt.get("fixture_sha256") != authoritative_fixture_sha256
    ):
        raise ValueError("HISTORICAL_FIXTURE_AUTHORITATIVE_HASH_MISMATCH")
    if receipt.get("network_requests") != 0 or receipt.get("trading_approval") is not False:
        raise ValueError("HISTORICAL_FIXTURE_RECEIPT_SCOPE_INVALID")
    lines = artifact_bytes.decode("utf-8").splitlines()
    if not lines:
        raise ValueError("HISTORICAL_FIXTURE_EMPTY")
    source = f"historical_strategy_parity_{ordinal}"
    start_ms = ordinal * 10_000_000
    events: list[SourceEvent] = []
    for index, line in enumerate(lines):
        decoded = json.loads(line)
        if _canonical(decoded) != line:
            raise ValueError("HISTORICAL_FIXTURE_NOT_CANONICAL")
        timestamp_ms = start_ms + index
        natural_key = f"historical-parity:{receipt['fixture_sha256']}:{index}"
        events.append(
            SourceEvent(
                source=source,
                natural_key=natural_key,
                source_timestamp_ms=timestamp_ms,
                received_timestamp_ms=timestamp_ms,
                event_type="HISTORICAL_STRATEGY_PARITY_RECORD",
                payload_json=line,
                payload_sha256=payload_sha256(line),
                recovery_origin="HISTORICAL_IMPORT",
            )
        )
    event_tuple = tuple(events)
    schema_mapping_bytes = _canonical(
        {
            "event_type": "HISTORICAL_STRATEGY_PARITY_RECORD",
            "fixture_schema_version": json.loads(lines[0]).get("schema_version", "ROW_SCHEMA_FROM_RECEIPT"),
            "mapping_version": "HISTORICAL_PARITY_JSONL_IMPORT_V1",
            "payload_policy": "CANONICAL_JSON_LINE_UNCHANGED",
        }
    ).encode()
    run_id = f"historical-parity-import:{receipt['fixture_sha256']}"
    run = ImportRunRecord(
        import_run_id=run_id,
        artifact_sha256=receipt["fixture_sha256"],
        source_path_fingerprint=_hash(fixture_path.name),
        dataset_start_ms=start_ms,
        dataset_end_ms=start_ms + len(event_tuple),
        declared_row_count=len(event_tuple),
        schema_mapping_sha256=_hash(schema_mapping_bytes),
        inserted_row_count=len(event_tuple),
        replayed_row_count=0,
        conflict_row_count=0,
        dropped_row_count=0,
        status="COMPLETE",
        created_at_ms=start_ms,
    )
    scope = _canonical(
        {
            "artifact_sha256": receipt["fixture_sha256"],
            "evidence_class": "SANITIZED_C4_FULL_DECISION_PARITY",
            "source": source,
        }
    )
    evidence = _canonical(
        {
            "expected_event_natural_key_sha256": sorted(
                _hash(event.natural_key) for event in event_tuple
            ),
            "fixture_sha256": receipt["fixture_sha256"],
            "receipt_sha256": _hash(receipt_path.read_bytes()),
        }
    )
    source_range = SourceRangeRecord(
        import_run_id=run_id,
        source=source,
        canonical_scope_json=scope,
        canonical_scope_sha256=_hash(scope),
        requested_start_ms=start_ms,
        requested_end_ms=start_ms + len(event_tuple),
        granularity_ms=1,
        contract_version="BTC_DATA_COMPLETENESS_V1",
        status="COMPLETE",
        expected_row_count=len(event_tuple),
        observed_row_count=len(event_tuple),
        missing_row_count=0,
        reason_code=None,
        evidence_metadata_json=evidence,
        evidence_sha256=_hash(evidence),
        updated_at_ms=start_ms,
    )
    return HistoricalImportPlan(
        run=run,
        source_range=source_range,
        events=event_tuple,
        artifact_bytes=artifact_bytes,
        schema_mapping_bytes=schema_mapping_bytes,
        receipt=receipt,
    )


def build_historical_import_plans(
    project_root: Path,
    *,
    authoritative_fixture_sha256: dict[str, str],
) -> tuple[HistoricalImportPlan, ...]:
    if not isinstance(project_root, Path):
        raise ValueError("INVALID_PROJECT_ROOT")
    root = project_root / "strategy_sources/frozen/parity"
    expected_keys = tuple(item[0] for item in _FIXTURES)
    if (
        type(authoritative_fixture_sha256) is not dict
        or tuple(sorted(authoritative_fixture_sha256)) != tuple(sorted(expected_keys))
        or any(type(value) is not str or len(value) != 64 for value in authoritative_fixture_sha256.values())
    ):
        raise ValueError("INVALID_AUTHORITATIVE_C4_FIXTURE_HASH_MAP")
    return tuple(
        build_historical_import_plan(
            root / fixture,
            root / receipt,
            ordinal=index,
            authoritative_fixture_sha256=authoritative_fixture_sha256[population],
        )
        for index, (population, fixture, receipt) in enumerate(_FIXTURES, start=1)
    )
