from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from src.strategy_c4_acceptance import (
    C4StrategyAcceptanceReport,
    verify_c4_strategy_acceptance,
)
from src.strategy_parity import verify_v2_overlay_parity
from src.strategy_registry import StrategyRulePack, load_strategy_rule_pack
from src.strategy_v1_other_parity import verify_v1_other_parity
from src.strategy_v1_parity import verify_v1_historical_parity


_GIT_SHA40 = re.compile(r"[0-9a-f]{40}")
_HISTORICAL_INPUT = "BTC_STRATEGY_HISTORICAL_INPUT_V1"
_EXECUTABLE_INPUT = "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1"
_OVERLAY_INPUT = "BTC_STRATEGY_VOL_OVERLAY_INPUT_V1"
_C4_REPORT = "reports/C4_STRATEGY_47_ACCEPTANCE.json"


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    activation_status: str
    reason_code: str
    paper_eligible: bool


@dataclass(frozen=True, slots=True)
class C5IdentityStatus:
    strategy_id: str
    registry_index: int
    version: str
    parent_strategy_id: str | None
    evaluator_key: str
    input_schema_version: str
    rule_spec_sha256: str
    rule_source_fingerprint_sha256: str
    rule_source_artifacts: tuple[tuple[str, str, str, str], ...]
    parity_population: str
    historical_reference_artifact: tuple[str, str, str, str]
    parity_fixture_artifact: tuple[str, str, str, str]
    parity_receipt_artifact: tuple[str, str, str, str]
    fixture_decision_count: int
    expected_trade_count: int
    actual_trade_count: int
    expected_wins: int
    actual_wins: int
    expected_losses: int
    actual_losses: int
    expected_turnover_micros: int
    actual_turnover_micros: int
    expected_pnl_micros: int
    actual_pnl_micros: int
    expected_roi: str
    actual_roi: str
    expected_trade_identity_sha256: str
    actual_trade_identity_sha256: str
    activation_status: str
    activation_reason_code: str
    paper_eligible: bool


@dataclass(frozen=True, slots=True)
class C5ActivationReport:
    status: str
    strategy_count: int
    classified_count: int
    activation_counts: tuple[tuple[str, int], ...]
    unknown_count: int
    identities: tuple[C5IdentityStatus, ...]
    c4_acceptance_sha256: str
    rule_pack_sha256: str
    external_provider_requests: int
    paper_execution_authorized: bool
    trading_approval: bool
    acceptance_pass: bool


def classify_activation(
    *,
    version: str,
    input_schema_version: str,
    registry_live_status: str,
    parity_status: str,
) -> ActivationDecision:
    for value in (
        version,
        input_schema_version,
        registry_live_status,
        parity_status,
    ):
        if type(value) is not str:
            raise ValueError("C5_INVALID_POLICY_INPUT_TYPE")
    policy_tuple = (version, input_schema_version, registry_live_status)
    allowed_policy_tuples = {
        ("V1", _EXECUTABLE_INPUT, "PRESERVE_EXISTING_STATUS"),
        ("V1", _HISTORICAL_INPUT, "PRESERVE_EXISTING_STATUS"),
        ("V2", _OVERLAY_INPUT, "DISABLED_RESEARCH_ONLY"),
    }
    if policy_tuple not in allowed_policy_tuples:
        raise ValueError("C5_UNREVIEWED_POLICY_COMBINATION")
    if parity_status == "PARITY_FAIL":
        return ActivationDecision(
            "DISABLED_PARITY_FAIL", "C4_PARITY_FAILED", False
        )
    if parity_status != "PARITY_PASS":
        raise ValueError("C5_UNREVIEWED_PARITY_STATUS")
    if policy_tuple == ("V1", _EXECUTABLE_INPUT, "PRESERVE_EXISTING_STATUS"):
        return ActivationDecision(
            "PAPER_EVALUATION_ENABLED",
            "V1_PARITY_PASS_EXECUTABLE_CHECKPOINT_SCHEMA",
            True,
        )
    if policy_tuple == ("V1", _HISTORICAL_INPUT, "PRESERVE_EXISTING_STATUS"):
        return ActivationDecision(
            "DISABLED_MISSING_EXECUTION_DATA",
            "HISTORICAL_INPUT_HAS_NO_LIVE_DEPTH_FEE_CONTRACT",
            False,
        )
    if policy_tuple == ("V2", _OVERLAY_INPUT, "DISABLED_RESEARCH_ONLY"):
        return ActivationDecision(
            "DISABLED_RESEARCH_ONLY",
            "V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY",
            False,
        )
    raise ValueError("C5_UNREVIEWED_POLICY_COMBINATION")


def verify_c5_activation(project_root: Path) -> C5ActivationReport:
    if not isinstance(project_root, Path):
        raise ValueError("C5_INVALID_PROJECT_ROOT_TYPE")
    c4 = verify_c4_strategy_acceptance(project_root)
    if not c4.acceptance_pass or c4.parity_pass_count != 47:
        raise ValueError("C5_C4_GATE_NOT_ACCEPTED")
    c4_path = project_root / _C4_REPORT
    c4_payload = _load_json(c4_path)
    validate_c4_acceptance_payload(c4_payload, c4)

    pack = _load_pack(project_root)
    registry = _load_json(project_root / "registry" / "STRATEGY_REGISTRY_47.json")
    registry_rows = registry.get("strategies")
    if type(registry_rows) is not list or len(registry_rows) != 47:
        raise ValueError("C5_REGISTRY_IDENTITY_MISMATCH")
    registry_by_id = {row.get("strategy_id"): row for row in registry_rows}
    if len(registry_by_id) != 47:
        raise ValueError("C5_REGISTRY_IDENTITY_MISMATCH")

    parity_by_id = _parity_results(project_root)
    if set(parity_by_id) != {rule.strategy_id for rule in pack.rules}:
        raise ValueError("C5_PARITY_IDENTITY_MISMATCH")

    identities: list[C5IdentityStatus] = []
    for rule in pack.rules:
        registry_row = registry_by_id.get(rule.strategy_id)
        if type(registry_row) is not dict:
            raise ValueError("C5_REGISTRY_IDENTITY_MISMATCH")
        decision = classify_activation(
            version=rule.version,
            input_schema_version=rule.input_schema_version,
            registry_live_status=_exact_str(registry_row, "live_status"),
            parity_status="PARITY_PASS",
        )
        parity = parity_by_id[rule.strategy_id]
        family = pack.family_spec(rule.spec_id)
        sources = _rule_source_artifacts(pack, family.artifact_ids)
        identities.append(
            C5IdentityStatus(
                strategy_id=rule.strategy_id,
                registry_index=rule.registry_index,
                version=rule.version,
                parent_strategy_id=rule.parent_strategy_id,
                evaluator_key=rule.evaluator_key,
                input_schema_version=rule.input_schema_version,
                rule_spec_sha256=family.spec_sha256,
                rule_source_fingerprint_sha256=_source_fingerprint(sources),
                rule_source_artifacts=sources,
                parity_population=parity["parity_population"],
                historical_reference_artifact=_artifact_tuple(
                    pack.artifact(rule.parity_artifact_id)
                ),
                parity_fixture_artifact=_artifact_tuple(
                    pack.artifact(parity["parity_fixture_artifact_id"])
                ),
                parity_receipt_artifact=_artifact_tuple(
                    pack.artifact(parity["parity_receipt_artifact_id"])
                ),
                fixture_decision_count=parity["fixture_decision_count"],
                expected_trade_count=parity["expected_trade_count"],
                actual_trade_count=parity["actual_trade_count"],
                expected_wins=parity["expected_wins"],
                actual_wins=parity["actual_wins"],
                expected_losses=parity["expected_losses"],
                actual_losses=parity["actual_losses"],
                expected_turnover_micros=parity["expected_turnover_micros"],
                actual_turnover_micros=parity["actual_turnover_micros"],
                expected_pnl_micros=parity["expected_pnl_micros"],
                actual_pnl_micros=parity["actual_pnl_micros"],
                expected_roi=parity["expected_roi"],
                actual_roi=parity["actual_roi"],
                expected_trade_identity_sha256=parity[
                    "expected_trade_identity_sha256"
                ],
                actual_trade_identity_sha256=parity[
                    "actual_trade_identity_sha256"
                ],
                activation_status=decision.activation_status,
                activation_reason_code=decision.reason_code,
                paper_eligible=decision.paper_eligible,
            )
        )

    if tuple(item.registry_index for item in identities) != tuple(range(1, 48)):
        raise ValueError("C5_IDENTITY_ORDER_MISMATCH")
    counts: dict[str, int] = {}
    for item in identities:
        counts[item.activation_status] = counts.get(item.activation_status, 0) + 1
    expected_counts = {
        "DISABLED_MISSING_EXECUTION_DATA": 26,
        "DISABLED_RESEARCH_ONLY": 13,
        "PAPER_EVALUATION_ENABLED": 8,
    }
    if counts != expected_counts:
        raise ValueError("C5_ACTIVATION_COUNT_MISMATCH")
    return C5ActivationReport(
        status="C5_STRATEGY_47_ACTIVATION_PASS",
        strategy_count=47,
        classified_count=47,
        activation_counts=tuple(sorted(counts.items())),
        unknown_count=0,
        identities=tuple(identities),
        c4_acceptance_sha256=hashlib.sha256(c4_path.read_bytes()).hexdigest(),
        rule_pack_sha256=c4.rule_pack_sha256,
        external_provider_requests=0,
        paper_execution_authorized=False,
        trading_approval=False,
        acceptance_pass=True,
    )


def validate_c4_acceptance_payload(
    payload: dict[str, Any], live: C4StrategyAcceptanceReport
) -> None:
    if type(payload) is not dict or type(live) is not C4StrategyAcceptanceReport:
        raise ValueError("C5_C4_REPORT_NOT_ACCEPTED")
    expected = {
        "acceptance_pass": live.acceptance_pass,
        "evaluator_implemented_count": live.evaluator_implemented_count,
        "external_provider_requests": live.external_provider_requests,
        "parity_fixture_sha256": dict(live.parity_fixture_sha256),
        "parity_pass_count": live.parity_pass_count,
        "rule_pack_sha256": live.rule_pack_sha256,
        "schema_version": "BTC_STRATEGY_C4_ACCEPTANCE_V1",
        "source_verified_count": live.source_verified_count,
        "spec_frozen_count": live.spec_frozen_count,
        "status": live.status,
        "strategy_count": live.strategy_count,
        "strategy_ids": list(live.strategy_ids),
        "trading_approval": live.trading_approval,
        "v1_decision_count": live.v1_decision_count,
        "v1_fixture_record_count": live.v1_fixture_record_count,
        "v1_strategy_count": live.v1_strategy_count,
        "v2_fixture_record_count": live.v2_fixture_record_count,
        "v2_strategy_count": live.v2_strategy_count,
    }
    if set(payload) != set(expected) | {"source_commit"}:
        raise ValueError("C5_C4_REPORT_NOT_ACCEPTED")
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("C5_C4_REPORT_NOT_ACCEPTED")
    source_commit = payload.get("source_commit")
    if type(source_commit) is not str or _GIT_SHA40.fullmatch(source_commit) is None:
        raise ValueError("C5_C4_REPORT_NOT_ACCEPTED")


def acceptance_report_payload(
    report: C5ActivationReport, *, source_commit: str, verified_at_utc: str
) -> dict[str, Any]:
    _validate_report(report)
    return {
        "schema_version": "BTC_STRATEGY_C5_ACTIVATION_ACCEPTANCE_V1",
        "source_commit": _source_commit(source_commit),
        "verified_at_utc": _verified_at_utc(verified_at_utc),
        "status": report.status,
        "strategy_count": report.strategy_count,
        "classified_count": report.classified_count,
        "activation_counts": dict(report.activation_counts),
        "unknown_count": report.unknown_count,
        "c4_acceptance_sha256": report.c4_acceptance_sha256,
        "rule_pack_sha256": report.rule_pack_sha256,
        "external_provider_requests": report.external_provider_requests,
        "paper_execution_authorized": report.paper_execution_authorized,
        "trading_approval": report.trading_approval,
        "acceptance_pass": report.acceptance_pass,
    }


def build_c5_status_matrix(
    report: C5ActivationReport, *, source_commit: str, verified_at_utc: str
) -> dict[str, Any]:
    _validate_report(report)
    commit = _source_commit(source_commit)
    verified = _verified_at_utc(verified_at_utc)
    rows: list[dict[str, Any]] = []
    for item in report.identities:
        row = asdict(item)
        row["rule_source_artifacts"] = [
            {
                "artifact_id": artifact_id,
                "relative_path": relative_path,
                "role": role,
                "sha256": sha256,
            }
            for artifact_id, relative_path, role, sha256 in item.rule_source_artifacts
        ]
        for field in (
            "historical_reference_artifact",
            "parity_fixture_artifact",
            "parity_receipt_artifact",
        ):
            artifact_id, relative_path, role, sha256 = getattr(item, field)
            row[field] = {
                "artifact_id": artifact_id,
                "relative_path": relative_path,
                "role": role,
                "sha256": sha256,
            }
        row.update(
            {
                "rule_source_status": "SOURCE_VERIFIED",
                "rule_spec_status": "SPEC_FROZEN",
                "evaluator_status": "EVALUATOR_IMPLEMENTED",
                "parity_status": "PARITY_PASS",
                "parity_tolerances": {
                    "counts": 0,
                    "pnl_micros": 0,
                    "roi": "EXACT_DECIMAL_RATIO",
                    "trade_identity": "EXACT_SHA256",
                },
                "last_verified_commit": commit,
                "last_verified_at_utc": verified,
            }
        )
        rows.append(row)
    return {
        "schema_version": "BTC_STRATEGY_47_STATUS_MATRIX_V3",
        "source_commit": commit,
        "verified_at_utc": verified,
        "strategy_count": 47,
        "status_counts": {
            "SOURCE_VERIFIED": 47,
            "SPEC_FROZEN": 47,
            "EVALUATOR_IMPLEMENTED": 47,
            "PARITY_PASS": 47,
            **dict(report.activation_counts),
        },
        "strategies": rows,
        "paper_execution_authorized": False,
        "trading_approval": False,
    }


def write_c5_acceptance_outputs(
    report: C5ActivationReport,
    *,
    report_path: Path,
    matrix_path: Path,
    source_commit: str,
    verified_at_utc: str,
) -> None:
    if not isinstance(report_path, Path) or not isinstance(matrix_path, Path):
        raise ValueError("C5_INVALID_OUTPUT_PATH")
    _atomic_write(
        report_path,
        _canonical_json(
            acceptance_report_payload(
                report,
                source_commit=source_commit,
                verified_at_utc=verified_at_utc,
            )
        ),
    )
    _atomic_write(
        matrix_path,
        _canonical_json(
            build_c5_status_matrix(
                report,
                source_commit=source_commit,
                verified_at_utc=verified_at_utc,
            )
        ),
    )


def _load_pack(project_root: Path) -> StrategyRulePack:
    return load_strategy_rule_pack(
        project_root / "strategy_sources" / "frozen" / "STRATEGY_RULE_MAP_47.json",
        lock_path=project_root / "contract" / "STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json",
        registry_json_path=project_root / "registry" / "STRATEGY_REGISTRY_47.json",
        registry_csv_path=project_root / "registry" / "STRATEGY_REGISTRY_47.csv",
    )


def _parity_results(project_root: Path) -> dict[str, dict[str, Any]]:
    root = project_root / "strategy_sources" / "frozen" / "parity"
    reports = (
        (
            "V1_EARLY_HORIZON",
            "PARITY_V1_EARLY_HORIZON_FULL",
            "PARITY_V1_EARLY_HORIZON_RECEIPT",
            verify_v1_historical_parity(
                root / "V1_EARLY_HORIZON_FULL_DECISION_PARITY.jsonl"
            ),
        ),
        (
            "V1_EARLY_CONFIDENCE",
            "PARITY_V1_EARLY_CONFIDENCE_FULL",
            "PARITY_V1_EARLY_CONFIDENCE_RECEIPT",
            verify_v1_historical_parity(
                root / "V1_EARLY_CONFIDENCE_FULL_DECISION_PARITY.jsonl"
            ),
        ),
        (
            "V1_CONFIRMATION_BASKET",
            "PARITY_V1_CONFIRMATION_BASKET_FULL",
            "PARITY_V1_CONFIRMATION_BASKET_RECEIPT",
            verify_v1_other_parity(
                root / "V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl"
            ),
        ),
        (
            "V2_VOL_OVERLAY",
            "PARITY_VOL_OVERLAY_DECISIONS",
            "PARITY_VOL_OVERLAY_RECEIPT",
            verify_v2_overlay_parity(root / "VOL_OVERLAY_DECISION_PARITY.jsonl"),
        ),
    )
    result: dict[str, dict[str, Any]] = {}
    for population, fixture_artifact_id, receipt_artifact_id, report in reports:
        if not report.parity_pass:
            raise ValueError("C5_PARITY_NOT_PASS")
        for identity in report.identities:
            if identity.strategy_id in result:
                raise ValueError("C5_PARITY_IDENTITY_OVERLAP")
            payload = asdict(identity)
            decision_count = payload.get(
                "fixture_decision_count",
                payload.get("decision_count", payload.get("fixture_row_count")),
            )
            expected_losses = payload.get(
                "expected_losses",
                payload["expected_trade_count"] - payload["expected_wins"],
            )
            actual_losses = payload.get(
                "actual_losses",
                payload["actual_trade_count"] - payload["actual_wins"],
            )
            expected_roi = payload.get(
                "expected_roi",
                _roi(payload["expected_pnl_micros"], payload["expected_turnover_micros"]),
            )
            actual_roi = payload.get(
                "actual_roi",
                _roi(payload["actual_pnl_micros"], payload["actual_turnover_micros"]),
            )
            result[identity.strategy_id] = {
                "parity_population": population,
                "parity_fixture_artifact_id": fixture_artifact_id,
                "parity_receipt_artifact_id": receipt_artifact_id,
                "fixture_decision_count": int(decision_count),
                "expected_trade_count": payload["expected_trade_count"],
                "actual_trade_count": payload["actual_trade_count"],
                "expected_wins": payload["expected_wins"],
                "actual_wins": payload["actual_wins"],
                "expected_losses": expected_losses,
                "actual_losses": actual_losses,
                "expected_turnover_micros": payload["expected_turnover_micros"],
                "actual_turnover_micros": payload["actual_turnover_micros"],
                "expected_pnl_micros": payload["expected_pnl_micros"],
                "actual_pnl_micros": payload["actual_pnl_micros"],
                "expected_roi": expected_roi,
                "actual_roi": actual_roi,
                "expected_trade_identity_sha256": payload[
                    "expected_trade_identity_sha256"
                ],
                "actual_trade_identity_sha256": payload[
                    "actual_trade_identity_sha256"
                ],
            }
    return result


def _rule_source_artifacts(
    pack: StrategyRulePack, artifact_ids: tuple[str, ...]
) -> tuple[tuple[str, str, str, str], ...]:
    rows = []
    for artifact_id in artifact_ids:
        artifact = pack.artifact(artifact_id)
        if "PARITY" in artifact.role or artifact.role == "IMMUTABLE_TRADE_LEDGER":
            continue
        rows.append(
            (
                artifact.artifact_id,
                artifact.relative_path,
                artifact.role,
                artifact.sha256,
            )
        )
    if not rows:
        raise ValueError("C5_RULE_SOURCE_ARTIFACT_MISSING")
    return tuple(sorted(rows))


def _artifact_tuple(artifact: Any) -> tuple[str, str, str, str]:
    return (
        artifact.artifact_id,
        artifact.relative_path,
        artifact.role,
        artifact.sha256,
    )


def _source_fingerprint(rows: tuple[tuple[str, str, str, str], ...]) -> str:
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _roi(pnl_micros: int, turnover_micros: int) -> str:
    if turnover_micros == 0:
        return "0"
    with localcontext() as context:
        context.prec = 50
        return str(Decimal(pnl_micros) / Decimal(turnover_micros))


def _validate_report(report: C5ActivationReport) -> None:
    if type(report) is not C5ActivationReport or not report.acceptance_pass:
        raise ValueError("C5_INVALID_ACCEPTANCE_REPORT")


def _source_commit(value: str) -> str:
    if type(value) is not str or _GIT_SHA40.fullmatch(value) is None:
        raise ValueError("C5_INVALID_SOURCE_COMMIT")
    return value


def _verified_at_utc(value: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("C5_INVALID_VERIFIED_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("C5_INVALID_VERIFIED_TIMESTAMP") from exc
    if parsed.tzinfo != UTC or parsed.isoformat().replace("+00:00", "Z") != value:
        raise ValueError("C5_INVALID_VERIFIED_TIMESTAMP")
    return value


def _exact_str(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if type(item) is not str:
        raise ValueError("C5_REGISTRY_FIELD_INVALID")
    return item


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError("C5_INVALID_JSON_OBJECT")
    return value


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
