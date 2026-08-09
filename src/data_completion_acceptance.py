from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from src.data_completion import DataCompletionLedger, ImportRunRecord, SourceRangeRecord
from src.models import SourceEvent, payload_sha256
from src.storage import SqliteStore
from src.historical_data_import import build_historical_import_plans


REQUIRED_INVENTORY_IDS = (
    "MARKET_CATALOG_DAILY",
    "BINANCE_CLOSED_1M",
    "POLYMARKET_PRICE_HISTORY",
    "POLYMARKET_CURRENT_BOOK",
    "POLYMARKET_LIVE_BOOK_STREAM",
    "POLYMARKET_HISTORICAL_DEPTH_C7",
    "PUBLIC_FEE_SCHEDULE_C7",
    "C4_SANITIZED_PARITY_FIXTURE_IMPORT",
    "HISTORICAL_SOURCE_PACK_IMPORT",
)
_SHA40 = re.compile(r"[0-9a-f]{40}")
_AUTHORITATIVE_REPORT_SHA256 = {
    "C1_DOWNTIME_ACCEPTANCE.json": "f3f331e23c6e011220bae0fcf70fc6ae0bb6322264065458ae6a08d27a8618cc",
    "C4_STRATEGY_47_ACCEPTANCE.json": "641daca9fc68689f60baaf857a3a8711006802d9d1e28c8b5b09062f73180e5b",
    "C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json": "6cb7a2aef0443b24b571789299886e89059777f48e0cbc32403004ea6ec3cf16",
}


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _event(source: str, key: str, timestamp_ms: int, event_type: str) -> SourceEvent:
    payload = _canonical(
        {
            "event_type": event_type,
            "structural_value_sha256": _hash(f"{source}:{key}:{timestamp_ms}"),
        }
    )
    return SourceEvent(
        source=source,
        natural_key=key,
        source_timestamp_ms=timestamp_ms,
        received_timestamp_ms=timestamp_ms + 1,
        event_type=event_type,
        payload_json=payload,
        payload_sha256=payload_sha256(payload),
        recovery_origin="HISTORICAL_IMPORT",
    )


@dataclass(frozen=True, slots=True)
class RangeAcceptanceSummary:
    inventory_id: str
    range_key: str
    status: str
    expected_row_count: int
    observed_row_count: int
    missing_row_count: int
    evidence_sha256: str
    evidence_class: str
    boundary_sha256: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class ImportAcceptanceSummary:
    import_run_id_sha256: str
    artifact_sha256: str
    schema_mapping_sha256: str
    declared_row_count: int
    inserted_row_count: int
    replayed_row_count: int
    conflict_row_count: int
    dropped_row_count: int
    status: str


@dataclass(frozen=True, slots=True)
class DataCompletionAcceptanceReport:
    status: str
    acceptance_pass: bool
    migration_version: int
    source_range_count: int
    import_run_count: int
    unknown_range_count: int
    unknown_inventory_class_count: int
    source_ranges: tuple[RangeAcceptanceSummary, ...]
    import_runs: tuple[ImportAcceptanceSummary, ...]
    append_pass: bool
    bounded_backfill_pass: bool
    reconcile_pass: bool
    import_pass: bool
    import_replay_pass: bool
    expected_absent_pass: bool
    future_market_pass: bool
    backup_restore_pass: bool
    integrity_pass: bool
    free_space_preflight_pass: bool
    bounded_batch_plan_pass: bool
    production_historical_depth_ready: bool
    production_historical_depth_status: str
    production_data_complete: bool
    c7_entry_authorized: bool
    blocking_inventory_ids: tuple[str, ...]
    historical_imported_row_count: int
    external_provider_requests: int
    paper_execution_authorized: bool
    trading_approval: bool


def _range(
    *,
    inventory_id: str,
    source: str,
    start_ms: int,
    end_ms: int,
    granularity_ms: int,
    status: str,
    events: tuple[SourceEvent, ...] = (),
    import_run_id: str | None = None,
    updated_at_ms: int,
) -> SourceRangeRecord:
    scope = _canonical({"inventory_id": inventory_id, "scope_class": source})
    evidence: dict[str, object] = {"inventory_id": inventory_id}
    expected = len(events)
    observed = len(events)
    missing = 0
    reason = None
    if status == "COMPLETE" and source != "binance":
        evidence["expected_event_natural_key_sha256"] = sorted(
            _hash(event.natural_key) for event in events
        )
    elif status == "EXPECTED_ABSENT":
        expected = 1
        observed = 0
        missing = 1
        reason = "CONFIRMED_INVENTORY_ABSENCE"
        evidence["inventory"] = {
            "absence_confirmed": True,
            "provenance_sha256": _hash("deterministic-loopback-inventory"),
            "reason_code": reason,
        }
    elif status == "NOT_REQUIRED_BY_CONTRACT":
        expected = observed = missing = 0
        reason = "RESEARCH_ONLY_DISABLED"
    encoded_evidence = _canonical(evidence)
    return SourceRangeRecord(
        import_run_id=import_run_id,
        source=source,
        canonical_scope_json=scope,
        canonical_scope_sha256=_hash(scope),
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        granularity_ms=granularity_ms,
        contract_version="BTC_DATA_COMPLETENESS_V1",
        status=status,
        expected_row_count=expected,
        observed_row_count=observed,
        missing_row_count=missing,
        reason_code=reason,
        evidence_metadata_json=encoded_evidence,
        evidence_sha256=_hash(encoded_evidence),
        updated_at_ms=updated_at_ms,
    )


def _run(
    *,
    import_run_id: str,
    events: tuple[SourceEvent, ...],
    inserted: int,
    replayed: int,
    created_at_ms: int,
) -> ImportRunRecord:
    artifact = _canonical(
        [
            {
                "event_type": event.event_type,
                "natural_key_sha256": _hash(event.natural_key),
                "payload_sha256": event.payload_sha256,
                "source": event.source,
                "source_timestamp_ms": event.source_timestamp_ms,
            }
            for event in events
        ]
    )
    timestamps = [event.source_timestamp_ms for event in events]
    return ImportRunRecord(
        import_run_id=import_run_id,
        artifact_sha256=_hash(artifact),
        source_path_fingerprint=_hash(f"sanitized-source:{import_run_id}"),
        dataset_start_ms=min(timestamps),
        dataset_end_ms=max(timestamps) + 1,
        declared_row_count=len(events),
        schema_mapping_sha256=_hash("DATA_COMPLETION_ACCEPTANCE_EVENT_V1"),
        inserted_row_count=inserted,
        replayed_row_count=replayed,
        conflict_row_count=0,
        dropped_row_count=0,
        status="COMPLETE",
        created_at_ms=created_at_ms,
    )


def _commit_fixture(
    ledger: DataCompletionLedger,
    *,
    inventory_id: str,
    source: str,
    timestamps: tuple[int, ...],
    event_type: str,
    ordinal: int,
) -> tuple[ImportRunRecord, SourceRangeRecord, tuple[SourceEvent, ...]]:
    events = tuple(
        _event(source, f"dc:{ordinal}:{index}", timestamp, event_type)
        for index, timestamp in enumerate(timestamps)
    )
    run = _run(
        import_run_id=f"dc-import-{ordinal}",
        events=events,
        inserted=len(events),
        replayed=0,
        created_at_ms=100_000 + ordinal,
    )
    granularity = 60_000 if source == "binance" else 1_000
    source_range = _range(
        inventory_id=inventory_id,
        source=source,
        start_ms=min(timestamps),
        end_ms=max(timestamps) + granularity,
        granularity_ms=granularity,
        status="COMPLETE",
        events=events,
        import_run_id=run.import_run_id,
        updated_at_ms=100_000 + ordinal,
    )
    ledger.commit_import(run, (source_range,), events)
    return run, source_range, events


def _actual_inventory(
    project_root: Path,
    *,
    historical_imported_row_count: int,
    historical_import_evidence_sha256: str,
) -> tuple[RangeAcceptanceSummary, ...]:
    c1_path = project_root / "reports/C1_DOWNTIME_ACCEPTANCE.json"
    c6_path = project_root / "reports/C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json"
    c4_path = project_root / "reports/C4_STRATEGY_47_ACCEPTANCE.json"
    c1 = json.loads(c1_path.read_text(encoding="utf-8"))
    c6 = json.loads(c6_path.read_text(encoding="utf-8"))
    c4 = json.loads(c4_path.read_text(encoding="utf-8"))
    if not (
        c1.get("status") == "PASS"
        and c1.get("binance_continuity", {}).get("expected_binance_closed_minutes") == 10
        and c1.get("binance_continuity", {}).get("missing_binance_closed_minutes") == 0
        and c1.get("polymarket_reconciliation", {}).get("reconciled") is True
        and c1.get("polymarket_reconciliation", {}).get("asset_count") == 22
        and c1.get("initial_state", {}).get("market_count") == 11
        and c1.get("initial_state", {}).get("asset_count") == 22
        and c1.get("initial_state", {}).get("live_ready") is True
    ):
        raise RuntimeError("C1_DATA_COMPLETENESS_EVIDENCE_INVALID")
    if not (
        c6.get("status") == "C6_CHECKPOINT_SCHEDULER_PASS"
        and c6.get("missing_depth_block_pass") is True
        and c6.get("production_input_ready") is False
    ):
        raise RuntimeError("C6_DATA_COMPLETENESS_EVIDENCE_INVALID")
    if not (
        c4.get("status") == "C4_STRATEGY_47_ACCEPTANCE_PASS"
        and c4.get("parity_pass_count") == 47
    ):
        raise RuntimeError("C4_DATA_COMPLETENESS_EVIDENCE_INVALID")
    evidence = {
        "c1": _hash(c1_path.read_bytes()),
        "c6": _hash(c6_path.read_bytes()),
        "c4": _hash(c4_path.read_bytes()),
    }
    boundaries = {
        "MARKET_CATALOG_DAILY": _hash(_canonical({"asset_count": 22, "market_count": 11, "identity_present": True})),
        "BINANCE_CLOSED_1M": _hash(_canonical(c1["binance_continuity"])),
        "POLYMARKET_PRICE_HISTORY": _hash(_canonical({"required_assets": 22, "evidence_gap": "EXPLICIT_HISTORY_RANGE_RECEIPT_MISSING"})),
        "POLYMARKET_CURRENT_BOOK": _hash(_canonical(c1["polymarket_reconciliation"])),
        "POLYMARKET_LIVE_BOOK_STREAM": _hash(_canonical({"live_ready": True, "polymarket_status": c1["initial_state"]["polymarket_status"]})),
        "POLYMARKET_HISTORICAL_DEPTH_C7": _hash(_canonical({"missing_depth_block_pass": True, "production_input_ready": False})),
        "PUBLIC_FEE_SCHEDULE_C7": _hash(_canonical({"fee_schedule_receipt": "MISSING", "production_input_ready": False})),
        "C4_SANITIZED_PARITY_FIXTURE_IMPORT": historical_import_evidence_sha256,
        "HISTORICAL_SOURCE_PACK_IMPORT": _hash(_canonical({"source_pack_receipt": "MISSING"})),
    }
    rows = (
        ("MARKET_CATALOG_DAILY", "COMPLETE", 1, 1, 0, evidence["c1"], "IMMUTABLE_C1_ACCEPTANCE", None),
        ("BINANCE_CLOSED_1M", "COMPLETE", 10, 10, 0, evidence["c1"], "IMMUTABLE_C1_ACCEPTANCE", None),
        ("POLYMARKET_PRICE_HISTORY", "SOURCE_UNAVAILABLE_RETRYABLE", 22, 0, 22, evidence["c1"], "IMMUTABLE_C1_ACCEPTANCE_DATA_GAP", "EXPLICIT_HISTORY_RANGE_RECEIPT_MISSING"),
        ("POLYMARKET_CURRENT_BOOK", "COMPLETE", 22, 22, 0, evidence["c1"], "IMMUTABLE_C1_ACCEPTANCE", None),
        ("POLYMARKET_LIVE_BOOK_STREAM", "COMPLETE", 1, 1, 0, evidence["c1"], "IMMUTABLE_C1_ACCEPTANCE", None),
        ("POLYMARKET_HISTORICAL_DEPTH_C7", "SOURCE_UNAVAILABLE_RETRYABLE", 22, 0, 22, evidence["c6"], "IMMUTABLE_C6_FAIL_CLOSED_GAP", "CONTEMPORANEOUS_DEPTH_NOT_CAPTURED"),
        ("PUBLIC_FEE_SCHEDULE_C7", "SOURCE_UNAVAILABLE_RETRYABLE", 22, 0, 22, evidence["c6"], "IMMUTABLE_C6_FAIL_CLOSED_GAP", "PUBLIC_FEE_SCHEDULE_NOT_CAPTURED"),
        ("C4_SANITIZED_PARITY_FIXTURE_IMPORT", "COMPLETE", historical_imported_row_count, historical_imported_row_count, 0, evidence["c4"], "IMMUTABLE_C4_SANITIZED_PARITY_FIXTURES", None),
        ("HISTORICAL_SOURCE_PACK_IMPORT", "SOURCE_UNAVAILABLE_RETRYABLE", 1, 0, 1, evidence["c4"], "HISTORICAL_SOURCE_PACK_DATA_GAP", "HISTORICAL_SOURCE_PACK_RECEIPT_MISSING"),
    )
    return tuple(
        RangeAcceptanceSummary(
            inventory_id=inventory_id,
            range_key=_hash(_canonical({"inventory_id": inventory_id, "evidence_sha256": evidence_sha})),
            status=status,
            expected_row_count=expected,
            observed_row_count=observed,
            missing_row_count=missing,
            evidence_sha256=evidence_sha,
            evidence_class=evidence_class,
            boundary_sha256=boundaries[inventory_id],
            reason_code=reason_code,
        )
        for inventory_id, status, expected, observed, missing, evidence_sha, evidence_class, reason_code in rows
    )


def verify_data_completion_acceptance(project_root: Path) -> DataCompletionAcceptanceReport:
    if not isinstance(project_root, Path):
        raise ValueError("INVALID_PROJECT_ROOT")
    for name, expected_sha256 in _AUTHORITATIVE_REPORT_SHA256.items():
        actual_sha256 = _hash((project_root / "reports" / name).read_bytes())
        if actual_sha256 != expected_sha256:
            prefix = name.split("_", 1)[0]
            raise RuntimeError(f"{prefix}_DATA_COMPLETENESS_EVIDENCE_INVALID")
    c6 = json.loads(
        (project_root / "reports/C6_CHECKPOINT_SCHEDULER_ACCEPTANCE.json").read_text(
            encoding="utf-8"
        )
    )
    c4 = json.loads(
        (project_root / "reports/C4_STRATEGY_47_ACCEPTANCE.json").read_text(
            encoding="utf-8"
        )
    )
    future_market_pass = (
        c6.get("status") == "C6_CHECKPOINT_SCHEDULER_PASS"
        and c6.get("market_count") == 3
        and c6.get("schedule_count") == 30
        and c6.get("trading_approval") is False
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database_path = root / "data-completion.sqlite3"
        store = SqliteStore.open(database_path)
        try:
            store.migrate()
            ledger = DataCompletionLedger(store)
            fixtures = (
                ("BINANCE_BOUNDED_BACKFILL", "binance", (60_000, 120_000, 180_000), "BINANCE_KLINE_CLOSED"),
                ("POLYMARKET_CURRENT_BOOK_RECONCILE", "polymarket_book", (1_000, 2_000), "POLYMARKET_BOOK"),
                ("POLYMARKET_PRICE_HISTORY_IMPORT", "polymarket_history", (3_000, 4_000), "POLYMARKET_HISTORY_POINT"),
                ("GAMMA_DAILY_EVENT_INVENTORY", "gamma_inventory", (5_000,), "GAMMA_EVENT_IDENTITY"),
                ("FUTURE_MARKET_REGISTRATION", "future_market_schedule", (6_000,), "FUTURE_MARKET_SCHEDULE"),
            )
            committed = [
                _commit_fixture(
                    ledger,
                    inventory_id=inventory_id,
                    source=source,
                    timestamps=timestamps,
                    event_type=event_type,
                    ordinal=index,
                )
                for index, (inventory_id, source, timestamps, event_type) in enumerate(fixtures, start=1)
            ]
            historical_plans = build_historical_import_plans(
                project_root,
                authoritative_fixture_sha256=c4["parity_fixture_sha256"],
            )
            ledger.commit_import_pack(
                tuple(
                    (plan.run, (plan.source_range,), plan.events)
                    for plan in historical_plans
                )
            )
            historical_imported_row_count = sum(
                len(plan.events) for plan in historical_plans
            )
            historical_import_evidence_sha256 = _hash(
                _canonical(
                    [
                        {
                            "artifact_sha256": plan.run.artifact_sha256,
                            "receipt_fixture_sha256": plan.receipt["fixture_sha256"],
                            "row_count": len(plan.events),
                        }
                        for plan in historical_plans
                    ]
                )
            )

            first_run, first_range, first_events = committed[0]
            exact_replay = ledger.commit_import(first_run, (first_range,), first_events)
            replay_run = _run(
                import_run_id="dc-import-replay",
                events=first_events,
                inserted=0,
                replayed=len(first_events),
                created_at_ms=200_000,
            )
            replay_range = dataclass_replace_range(
                first_range,
                import_run_id=replay_run.import_run_id,
                updated_at_ms=200_000,
            )
            ledger.commit_import(replay_run, (replay_range,), first_events)

            absent = _range(
                inventory_id="EXPECTED_ABSENT_DAILY_EVENT",
                source="gamma_inventory",
                start_ms=10_000,
                end_ms=11_000,
                granularity_ms=1_000,
                status="EXPECTED_ABSENT",
                updated_at_ms=300_000,
            )
            not_required = _range(
                inventory_id="V2_RUNTIME_DATA_NOT_REQUIRED",
                source="v2_runtime_input",
                start_ms=20_000,
                end_ms=21_000,
                granularity_ms=1_000,
                status="NOT_REQUIRED_BY_CONTRACT",
                updated_at_ms=300_001,
            )
            ledger.record_range_assessment(absent)
            ledger.record_range_assessment(not_required)

            live_event = _event("binance", "dc:live:append", 240_000, "BINANCE_KLINE_CLOSED")
            append_first = store.append_source_event(live_event)
            append_replay = store.append_source_event(live_event)

            backup_path = root / "backup.sqlite3"
            backup_connection = sqlite3.connect(backup_path)
            try:
                store._connection.backup(backup_connection)
                backup_quick_check = backup_connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                backup_connection.close()

            integrity = store.integrity_report()
            free_space_preflight_pass = shutil.disk_usage(root).free > max(
                1_048_576,
                database_path.stat().st_size * 3,
            )
            bounded_batch_plan_pass = sum(
                len(events) for _, _, events in committed
            ) <= 1_000
            range_summaries = _actual_inventory(
                project_root,
                historical_imported_row_count=historical_imported_row_count,
                historical_import_evidence_sha256=historical_import_evidence_sha256,
            )
            import_summaries = tuple(
                ImportAcceptanceSummary(
                    import_run_id_sha256=_hash(row[0]),
                    artifact_sha256=row[1],
                    schema_mapping_sha256=row[2],
                    declared_row_count=row[3],
                    inserted_row_count=row[4],
                    replayed_row_count=row[5],
                    conflict_row_count=row[6],
                    dropped_row_count=row[7],
                    status=row[8],
                )
                for row in store.rows(
                    "SELECT import_run_id,artifact_sha256,schema_mapping_sha256,declared_row_count,inserted_row_count,replayed_row_count,conflict_row_count,dropped_row_count,status FROM data_import_runs ORDER BY import_run_row_id"
                )
            )
            inventory_exact = tuple(item.inventory_id for item in range_summaries) == REQUIRED_INVENTORY_IDS
            bounded_backfill_pass = (
                committed[0][0].inserted_row_count == 3
                and committed[0][1].status == "COMPLETE"
                and committed[0][1].missing_row_count == 0
            )
            reconcile_pass = (
                committed[1][0].inserted_row_count == 2
                and committed[1][1].status == "COMPLETE"
                and committed[1][1].observed_row_count == 2
            )
            import_pass = (
                historical_imported_row_count == 2341
                and all(plan.run.dropped_row_count == 0 for plan in historical_plans)
                and all(plan.run.conflict_row_count == 0 for plan in historical_plans)
            )
            expected_absent_pass = store.scalar(
                "SELECT COUNT(*) FROM data_source_range_assessments WHERE status='EXPECTED_ABSENT' AND observed_row_count=0 AND missing_row_count=expected_row_count"
            ) == 1
            capability_pass = all(
                (
                    inventory_exact,
                    integrity["status"] == "PASS",
                    future_market_pass,
                    backup_quick_check == "ok",
                    free_space_preflight_pass,
                    bounded_batch_plan_pass,
                    append_first.inserted,
                    not append_replay.inserted,
                    not exact_replay.inserted,
                    bounded_backfill_pass,
                    reconcile_pass,
                    import_pass,
                    expected_absent_pass,
                )
            )
            blocking_inventory_ids = tuple(
                item.inventory_id
                for item in range_summaries
                if item.status == "SOURCE_UNAVAILABLE_RETRYABLE"
            )
            return DataCompletionAcceptanceReport(
                status="DATA_COMPLETION_CAPABILITY_ACCEPTANCE_PASS" if capability_pass else "DATA_COMPLETION_CAPABILITY_ACCEPTANCE_BLOCKED",
                acceptance_pass=capability_pass,
                migration_version=integrity["migration_version"],
                source_range_count=len(range_summaries),
                import_run_count=len(import_summaries),
                unknown_range_count=len(blocking_inventory_ids),
                unknown_inventory_class_count=0 if inventory_exact else 1,
                source_ranges=range_summaries,
                import_runs=import_summaries,
                append_pass=append_first.inserted and not append_replay.inserted,
                bounded_backfill_pass=bounded_backfill_pass,
                reconcile_pass=reconcile_pass,
                import_pass=import_pass,
                import_replay_pass=not exact_replay.inserted and replay_run.replayed_row_count == len(first_events),
                expected_absent_pass=expected_absent_pass,
                future_market_pass=future_market_pass,
                backup_restore_pass=backup_quick_check == "ok",
                integrity_pass=integrity["status"] == "PASS",
                free_space_preflight_pass=free_space_preflight_pass,
                bounded_batch_plan_pass=bounded_batch_plan_pass,
                production_historical_depth_ready=False,
                production_historical_depth_status="FAIL_CLOSED_NOT_IMPORTED",
                production_data_complete=not blocking_inventory_ids,
                c7_entry_authorized=False,
                blocking_inventory_ids=blocking_inventory_ids,
                historical_imported_row_count=historical_imported_row_count,
                external_provider_requests=0,
                paper_execution_authorized=False,
                trading_approval=False,
            )
        finally:
            store.close()


def dataclass_replace_range(value: SourceRangeRecord, **changes) -> SourceRangeRecord:
    fields = {name: getattr(value, name) for name in value.__dataclass_fields__}
    fields.update(changes)
    return SourceRangeRecord(**fields)


def acceptance_report_payload(
    report: DataCompletionAcceptanceReport,
    *,
    source_commit: str,
    verified_at_utc: str,
) -> dict[str, object]:
    if _SHA40.fullmatch(source_commit) is None:
        raise ValueError("INVALID_SOURCE_COMMIT")
    payload = asdict(report)
    payload.update(
        {
            "schema_version": "BTC_DATA_COMPLETENESS_STATUS_V2",
            "source_commit": source_commit,
            "verified_at_utc": verified_at_utc,
        }
    )
    return json.loads(_canonical(payload))


def write_acceptance_report(
    report: DataCompletionAcceptanceReport,
    *,
    output_path: Path,
    source_commit: str,
    verified_at_utc: str,
) -> None:
    payload = acceptance_report_payload(
        report,
        source_commit=source_commit,
        verified_at_utc=verified_at_utc,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    output_path.write_text(encoded, encoding="utf-8", newline="\n")
