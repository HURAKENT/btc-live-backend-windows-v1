from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.strategy_dispatch import load_strategy_dispatcher
from src.strategy_parity import verify_v2_overlay_parity
from src.strategy_v1_other_parity import verify_v1_other_parity
from src.strategy_v1_parity import verify_v1_historical_parity


_GIT_SHA40 = re.compile(r"[0-9a-f]{40}")
_REGISTRY_SHA256 = "88c54943cf84e4d123f979639cf30668f35a5dd6bc6792f8f50ab4c986ee3c2f"
_RULE_PACK_SHA256 = "9d402e9a1e2e0dd2fdae3443e3641dfb777efdc29b88ae14b529689972e8d727"


@dataclass(frozen=True, slots=True)
class C4StrategyAcceptanceReport:
    status: str
    strategy_count: int
    v1_strategy_count: int
    v2_strategy_count: int
    strategy_ids: tuple[str, ...]
    rule_pack_sha256: str
    v1_fixture_record_count: int
    v1_decision_count: int
    v2_fixture_record_count: int
    parity_fixture_sha256: tuple[tuple[str, str], ...]
    source_verified_count: int
    spec_frozen_count: int
    evaluator_implemented_count: int
    parity_pass_count: int
    external_provider_requests: int
    trading_approval: bool
    acceptance_pass: bool


def verify_c4_strategy_acceptance(project_root: Path) -> C4StrategyAcceptanceReport:
    if not isinstance(project_root, Path):
        raise ValueError("C4_INVALID_PROJECT_ROOT_TYPE")
    rule_pack_path = (
        project_root / "strategy_sources" / "frozen" / "STRATEGY_RULE_MAP_47.json"
    )
    if hashlib.sha256(rule_pack_path.read_bytes()).hexdigest() != _RULE_PACK_SHA256:
        raise ValueError("C4_ACCEPTANCE_RULE_PACK_HASH_MISMATCH")
    dispatcher = load_strategy_dispatcher(project_root)
    if tuple(binding.registry_index for binding in dispatcher.bindings) != tuple(
        range(1, 48)
    ):
        raise ValueError("C4_ACCEPTANCE_DISPATCH_ORDER_MISMATCH")

    parity_root = project_root / "strategy_sources" / "frozen" / "parity"
    early_horizon = verify_v1_historical_parity(
        parity_root / "V1_EARLY_HORIZON_FULL_DECISION_PARITY.jsonl"
    )
    early_confidence = verify_v1_historical_parity(
        parity_root / "V1_EARLY_CONFIDENCE_FULL_DECISION_PARITY.jsonl"
    )
    other_v1 = verify_v1_other_parity(
        parity_root / "V1_CONFIRMATION_BASKET_FULL_DECISION_PARITY.jsonl"
    )
    v2 = verify_v2_overlay_parity(
        parity_root / "VOL_OVERLAY_DECISION_PARITY.jsonl"
    )
    reports = (early_horizon, early_confidence, other_v1, v2)
    if not all(report.parity_pass for report in reports):
        raise ValueError("C4_ACCEPTANCE_PARITY_FAILED")

    dispatcher_ids = tuple(binding.strategy_id for binding in dispatcher.bindings)
    expected_v1_ids = {
        binding.strategy_id for binding in dispatcher.bindings if binding.version == "V1"
    }
    observed_v1_ids = (
        {item.strategy_id for item in early_horizon.identities}
        | {item.strategy_id for item in early_confidence.identities}
        | {item.strategy_id for item in other_v1.identities}
    )
    if observed_v1_ids != expected_v1_ids:
        raise ValueError("C4_ACCEPTANCE_V1_IDENTITY_MISMATCH")
    if (
        len(early_horizon.identities)
        + len(early_confidence.identities)
        + len(other_v1.identities)
        != len(observed_v1_ids)
    ):
        raise ValueError("C4_ACCEPTANCE_V1_IDENTITY_OVERLAP")
    expected_v2_ids = {
        binding.strategy_id for binding in dispatcher.bindings if binding.version == "V2"
    }
    if {item.strategy_id for item in v2.identities} != expected_v2_ids:
        raise ValueError("C4_ACCEPTANCE_V2_IDENTITY_MISMATCH")

    fixture_hashes = tuple(
        sorted(
            (
                ("V1_EARLY_HORIZON", str(early_horizon.fixture_sha256)),
                ("V1_EARLY_CONFIDENCE", str(early_confidence.fixture_sha256)),
                ("V1_CONFIRMATION_BASKET", str(other_v1.fixture_sha256)),
                ("V2_VOL_OVERLAY", v2.fixture_sha256),
            )
        )
    )
    return C4StrategyAcceptanceReport(
        status="C4_STRATEGY_47_ACCEPTANCE_PASS",
        strategy_count=47,
        v1_strategy_count=34,
        v2_strategy_count=13,
        strategy_ids=dispatcher_ids,
        rule_pack_sha256=_RULE_PACK_SHA256,
        v1_fixture_record_count=(
            early_horizon.fixture_record_count
            + early_confidence.fixture_record_count
            + other_v1.fixture_record_count
        ),
        v1_decision_count=(
            sum(item.fixture_decision_count for item in early_horizon.identities)
            + sum(item.fixture_decision_count for item in early_confidence.identities)
            + other_v1.decision_count
        ),
        v2_fixture_record_count=v2.fixture_row_count,
        parity_fixture_sha256=fixture_hashes,
        source_verified_count=47,
        spec_frozen_count=47,
        evaluator_implemented_count=47,
        parity_pass_count=47,
        external_provider_requests=0,
        trading_approval=False,
        acceptance_pass=True,
    )


def _source_commit(value: str) -> str:
    if type(value) is not str or _GIT_SHA40.fullmatch(value) is None:
        raise ValueError("C4_INVALID_SOURCE_COMMIT")
    return value


def acceptance_report_payload(
    report: C4StrategyAcceptanceReport, *, source_commit: str
) -> dict[str, Any]:
    if type(report) is not C4StrategyAcceptanceReport or not report.acceptance_pass:
        raise ValueError("C4_INVALID_ACCEPTANCE_REPORT")
    payload = asdict(report)
    payload["schema_version"] = "BTC_STRATEGY_C4_ACCEPTANCE_V1"
    payload["source_commit"] = _source_commit(source_commit)
    payload["parity_fixture_sha256"] = dict(report.parity_fixture_sha256)
    return payload


def build_strategy_status_matrix(
    report: C4StrategyAcceptanceReport, *, source_commit: str
) -> dict[str, Any]:
    if type(report) is not C4StrategyAcceptanceReport or not report.acceptance_pass:
        raise ValueError("C4_INVALID_ACCEPTANCE_REPORT")
    commit = _source_commit(source_commit)
    strategies = [
        {
            "strategy_id": strategy_id,
            "rule_source_status": "SOURCE_VERIFIED",
            "rule_spec_status": "SPEC_FROZEN",
            "evaluator_status": "EVALUATOR_IMPLEMENTED",
            "parity_status": "PARITY_PASS",
            "activation_status": "PENDING_C5_CLASSIFICATION",
            "paper_eligible": False,
        }
        for strategy_id in report.strategy_ids
    ]
    return {
        "schema_version": "BTC_STRATEGY_47_STATUS_MATRIX_V2",
        "source_registry_sha256": _REGISTRY_SHA256,
        "source_commit": commit,
        "strategy_count": 47,
        "status_counts": {
            "SOURCE_VERIFIED": 47,
            "SPEC_FROZEN": 47,
            "EVALUATOR_IMPLEMENTED": 47,
            "PARITY_PASS": 47,
            "PENDING_C5_CLASSIFICATION": 47,
        },
        "strategies": strategies,
        "trading_approval": False,
    }


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


def write_c4_acceptance_outputs(
    report: C4StrategyAcceptanceReport,
    *,
    report_path: Path,
    matrix_path: Path,
    source_commit: str,
) -> None:
    if not isinstance(report_path, Path) or not isinstance(matrix_path, Path):
        raise ValueError("C4_INVALID_OUTPUT_PATH")
    report_payload = acceptance_report_payload(report, source_commit=source_commit)
    matrix_payload = build_strategy_status_matrix(report, source_commit=source_commit)
    _atomic_write(report_path, _canonical_json(report_payload))
    _atomic_write(matrix_path, _canonical_json(matrix_payload))
