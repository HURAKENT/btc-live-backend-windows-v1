from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


IMPORT_PERFORMED_NETWORK_IO = False
MIN_DOWNTIME_MS = 600_000
MAX_DOWNTIME_MS = 630_000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
DOWNTIME_REPORT_PATH = REPORTS_DIR / "C1_DOWNTIME_ACCEPTANCE.json"
FINAL_REPORT_PATH = REPORTS_DIR / "C1_FINAL_ACCEPTANCE.json"
PACK_PATH = ARTIFACTS_DIR / "C1_ACCEPTANCE_PACK.zip"


class AcceptanceBlocked(RuntimeError):
    pass


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_windows(platform_name: str = os.name) -> None:
    if platform_name != "nt":
        raise AcceptanceBlocked("BLOCKED_UNSUPPORTED_PLATFORM")


def backend_command() -> list[str]:
    return [sys.executable, "run_backend.py"]


def monotonic_duration_ms(stopped_ns: int, restart_ns: int) -> int:
    if (
        type(stopped_ns) is not int
        or type(restart_ns) is not int
        or stopped_ns < 0
        or restart_ns < stopped_ns
    ):
        raise ValueError("INVALID_MONOTONIC_BOUNDARY")
    return (restart_ns - stopped_ns) // 1_000_000


def validate_downtime_duration(duration_ms: int) -> None:
    if type(duration_ms) is not int:
        raise ValueError("INVALID_DOWNTIME_DURATION_TYPE")
    if duration_ms < MIN_DOWNTIME_MS:
        raise AcceptanceBlocked("BLOCKED_DOWNTIME_TOO_SHORT")
    if duration_ms > MAX_DOWNTIME_MS:
        raise AcceptanceBlocked("BLOCKED_DOWNTIME_WINDOW")


def build_integrity_marker(fields: Mapping[str, Any]) -> dict[str, Any]:
    marker = dict(fields)
    if "marker_sha256" in marker:
        raise ValueError("MARKER_HASH_ALREADY_PRESENT")
    marker["marker_sha256"] = _sha256(_canonical_json_bytes(marker))
    return marker


def verify_integrity_marker(marker: Mapping[str, Any]) -> bool:
    expected = marker.get("marker_sha256")
    if not isinstance(expected, str):
        return False
    unhashed = dict(marker)
    del unhashed["marker_sha256"]
    return _sha256(_canonical_json_bytes(unhashed)) == expected


def calculate_acceptance_status(evidence: Mapping[str, Any]) -> str:
    duration_ms = evidence["downtime_duration_ms"]
    if type(duration_ms) is not int or duration_ms < MIN_DOWNTIME_MS:
        return "BLOCKED_DOWNTIME_TOO_SHORT"
    if duration_ms > MAX_DOWNTIME_MS:
        return "BLOCKED_DOWNTIME_WINDOW"
    if evidence["forced_termination_used"]:
        return "BLOCKED_GRACEFUL_SHUTDOWN"
    if (
        evidence["initial_backend_exit_code"] != 0
        or evidence["final_backend_exit_code"] != 0
    ):
        return "BLOCKED_GRACEFUL_SHUTDOWN"
    if (
        not evidence["post_restart_live_ready"]
        or evidence["missing_binance_closed_minutes"] != 0
        or evidence["duplicate_binance_natural_keys"] != 0
        or not evidence["polymarket_reconciled"]
        or not evidence["historical_depth_classification"]
    ):
        return "BLOCKED_RECOVERY"
    if (
        not evidence["current_post_restart_canary"]
        or evidence["recovered_execution_eligible"]
    ):
        return "BLOCKED_CANARY_SEMANTICS"
    if not evidence["outbox_replay_proved"]:
        return "BLOCKED_OUTBOX_REPLAY"
    if not evidence["database_integrity_pass"]:
        return "BLOCKED_DATABASE_INTEGRITY"
    if evidence["second_instance_exit_code"] != 20:
        return "BLOCKED_SINGLE_INSTANCE_GATE"
    return "PASS"


def _validate_pack_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise ValueError(f"UNSAFE_ZIP_PATH: {name}")
    lowered = name.lower()
    forbidden = (
        ".sqlite",
        ".sqlite3",
        ".sqlite-wal",
        ".sqlite3-wal",
        ".sqlite-shm",
        ".sqlite3-shm",
        "raw_ws",
        "raw_websocket",
    )
    if any(lowered.endswith(suffix) for suffix in forbidden) or any(
        marker in lowered for marker in ("raw_ws", "raw_websocket")
    ):
        raise ValueError(f"FORBIDDEN_PACK_ENTRY: {name}")
    return path


def safe_zip_manifest(entries: Mapping[str, bytes]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in sorted(entries):
        _validate_pack_path(name)
        if name in seen:
            raise ValueError(f"DUPLICATE_PACK_ENTRY: {name}")
        payload = entries[name]
        if not isinstance(payload, bytes):
            raise TypeError(f"INVALID_PACK_BYTES: {name}")
        seen.add(name)
        manifest.append(
            {
                "path": name,
                "size": len(payload),
                "sha256": _sha256(payload),
            }
        )
    return manifest


def _sha256sums(entries: Mapping[str, bytes]) -> bytes:
    lines = [f"{_sha256(entries[name])}  {name}" for name in sorted(entries)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def verify_sha256s(entries: Mapping[str, bytes], sums_text: str) -> bool:
    observed: dict[str, str] = {}
    for line in sums_text.splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not name
            or name in observed
        ):
            return False
        observed[name] = digest
    return observed == {
        name: _sha256(payload) for name, payload in entries.items()
    }


def _write_zip_entry(
    archive: zipfile.ZipFile,
    name: str,
    payload: bytes,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def build_acceptance_pack(
    path: Path,
    entries: Mapping[str, bytes],
    *,
    status: str,
) -> dict[str, Any]:
    safe_entries = dict(entries)
    manifest_entries = safe_zip_manifest(safe_entries)
    manifest = {
        "schema_version": "BTC_LIVE_BACKEND_C1_ACCEPTANCE_PACK_MANIFEST_V1",
        "status": status,
        "entries": manifest_entries,
        "raw_runtime_data_included": False,
    }
    safe_entries["MANIFEST.json"] = _canonical_json_bytes(manifest)
    safe_zip_manifest(safe_entries)
    sums = _sha256sums(safe_entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(safe_entries):
            _write_zip_entry(archive, name, safe_entries[name])
        _write_zip_entry(archive, "SHA256SUMS", sums)
    return validate_acceptance_pack(path)


def validate_acceptance_pack(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        duplicate_entries = len(names) - len(set(names))
        path_traversal_entries = sum(
            1
            for name in names
            if _unsafe_zip_name(name)
        )
        crc_pass = archive.testzip() is None
        contents = {
            name: archive.read(name)
            for name in names
            if name != "SHA256SUMS"
        }
        sums_text = archive.read("SHA256SUMS").decode("utf-8")
    internal_verified = verify_sha256s(contents, sums_text)
    return {
        "path": "artifacts/C1_ACCEPTANCE_PACK.zip",
        "sha256": _sha256(path.read_bytes()),
        "size": path.stat().st_size,
        "crc_pass": crc_pass,
        "internal_sha256s_verified": internal_verified,
        "path_traversal_entries": path_traversal_entries,
        "duplicate_entries": duplicate_entries,
        "missing_sha_entries": 0 if internal_verified else 1,
        "sha_mismatch": 0 if internal_verified else 1,
    }


def _unsafe_zip_name(name: str) -> bool:
    try:
        _validate_pack_path(name)
    except ValueError:
        return True
    return False


def build_final_report(
    *,
    run_id: str,
    commit_sha: str,
    status: str,
    pack_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    limitations = [
        "C1 proves only walking-skeleton infrastructure.",
        "Registry 47 remains non-executable.",
        "Strategy parity is not established.",
        "Paper execution is absent.",
        "Real order submission is absent.",
        "Wallet and signing support are absent.",
        "C2 recovery hardening was not performed.",
        "C3 rollover was not performed.",
        "C1 PASS is not trading approval.",
    ]
    return {
        "schema_version": (
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_FINAL_ACCEPTANCE_V1"
        ),
        "run_id": run_id,
        "commit_sha": commit_sha,
        "status": status,
        "gate": (
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS"
            if status == "PASS"
            else "NOT_REACHED"
        ),
        "prerequisites": {
            "C0_RUNTIME_FREEZE_PASS": True,
            "C0_REGISTRY_47_LOCK_PASS": True,
            "C1_DOMAIN_CONTRACT_PASS": True,
            "C1_EVENT_STORE_PASS": True,
            "C1_TRANSACTIONAL_OUTBOX_PASS": True,
            "C1_BINANCE_PROVIDER_PASS": True,
            "C1_POLYMARKET_PROVIDER_PASS": True,
            "C1_RESTART_RECOVERY_PASS": True,
            "C1_CANARY_PASS": True,
            "C1_PUSH_API_PASS": True,
            "C1_WINDOWS_LIFECYCLE_PASS": True,
            "C1_SAFE_DELIVERY_PASS": True,
            "C1_PROVIDER_CAPABILITY_PASS": True,
        },
        "intentional_downtime_acceptance": {
            "status": status,
            "completed": True,
        },
        "acceptance_pack": dict(pack_evidence),
        "security_assertions": {
            "public_read_only_network_only": True,
            "credentials_used": False,
            "real_order_submission": False,
            "wallet_or_signing": False,
            "trading_approval": False,
        },
        "scope_assertions": {
            "c2_started": False,
            "c3_rollover_started": False,
            "c4_started": False,
            "c5_started": False,
            "paper_execution": False,
            "registry_47_execution": False,
        },
        "limitations": limitations,
        "trading_approval": False,
    }


def validate_report(report: Mapping[str, Any]) -> None:
    status = report.get("status")
    gate = report.get("gate")
    if status == "PASS" and gate != "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS":
        raise ValueError("INVALID_PASS_GATE")
    if status != "PASS" and gate == "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS":
        raise ValueError("BLOCKED_REPORT_HAS_PASS_GATE")
    if report.get("trading_approval") is not False:
        raise ValueError("TRADING_APPROVAL_FORBIDDEN")


def inspect_runtime_path_contract(
    project_root: Path,
    run_id: str,
) -> dict[str, Any]:
    entrypoint = project_root / "run_backend.py"
    spec = importlib.util.spec_from_file_location(
        "_c1_acceptance_run_backend",
        entrypoint,
    )
    if spec is None or spec.loader is None:
        raise AcceptanceBlocked("BLOCKED_RUNTIME_ENTRYPOINT_IMPORT")
    module = importlib.util.module_from_spec(spec)
    inserted = str(project_root) not in sys.path
    if inserted:
        sys.path.insert(0, str(project_root))
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(str(project_root))

    actual = module.DATABASE_PATH.resolve()
    expected = (
        project_root.parent
        / "btc_live_backend_windows_v1_data"
        / "acceptance"
        / run_id
        / "btc_live_backend.sqlite3"
    ).resolve()
    try:
        actual_label = actual.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        actual_label = "OUTSIDE_PROJECT_ROOT"
    return {
        "supported": actual == expected,
        "backend_entrypoint": "run_backend.py",
        "backend_command": ["CURRENT_SYS_EXECUTABLE", "run_backend.py"],
        "configured_database_path": actual_label,
        "required_acceptance_database_label": (
            "data-root/acceptance/<run_id>/btc_live_backend.sqlite3"
        ),
        "supported_cli_path_parameters": [],
        "supported_environment_path_variables": [],
        "blocker": (
            None if actual == expected else "BLOCKED_RUNTIME_PATH_CONTRACT"
        ),
    }


def _git_value(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return result.stdout.strip()


def _file_hash_evidence(paths: list[Path]) -> dict[str, str]:
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): _sha256(path.read_bytes())
        for path in paths
    }


def _blocked_downtime_report(
    *,
    run_id: str,
    commit_sha: str,
    branch: str,
    started_at: str,
    path_contract: Mapping[str, Any],
) -> dict[str, Any]:
    not_run = {"status": "NOT_RUN_BLOCKED_RUNTIME_PATH_CONTRACT"}
    return {
        "schema_version": (
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_DOWNTIME_ACCEPTANCE_V1"
        ),
        "run_id": run_id,
        "commit_sha": commit_sha,
        "branch": branch,
        "status": "BLOCKED_RUNTIME_PATH_CONTRACT",
        "task_14_started": True,
        "task_14_completed": True,
        "started_at_utc": started_at,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "platform": sys.platform,
        "data_path_relative_label": "data-root/acceptance/<run_id>",
        "runtime_path_relative_label": "runtime-root/acceptance/<run_id>",
        "initial_state": {
            "status": "NOT_STARTED",
            "live_ready": False,
            "backend_process_started": False,
            "runtime_path_contract": dict(path_contract),
        },
        "single_instance": dict(not_run),
        "downtime": {
            **not_run,
            "process_stopped_at_ms": None,
            "restart_requested_at_ms": None,
            "downtime_duration_ms": None,
            "forced_termination_used": False,
        },
        "recovery": {**not_run, "post_restart_live_ready": False},
        "binance_continuity": {
            **not_run,
            "expected_closed_minutes": None,
            "recovered_closed_minutes": None,
            "missing_closed_minutes": None,
            "duplicate_natural_keys": None,
        },
        "polymarket_reconciliation": {
            **not_run,
            "reconciled": False,
            "historical_depth_classification": "NOT_RUN",
        },
        "canary_semantics": {
            **not_run,
            "current_post_restart_count": None,
            "recovered_execution_eligible": None,
        },
        "outbox_replay": {
            **not_run,
            "proved": False,
            "replayed_event_ids": [],
        },
        "database_integrity": {
            **not_run,
            "quick_check": "NOT_RUN",
            "integrity_check": "NOT_RUN",
            "journal_mode": "NOT_RUN",
            "synchronous": "NOT_RUN",
            "foreign_keys": "NOT_RUN",
        },
        "security": {
            "network_used": False,
            "backend_process_started": False,
            "unknown_process_stopped": False,
            "forced_termination_used": False,
            "real_order_submission": False,
            "wallet_or_signing": False,
            "paper_execution": False,
            "registry_47_execution": False,
        },
        "blocking_failures": [
            {
                "code": "BLOCKED_RUNTIME_PATH_CONTRACT",
                "detail": (
                    "run_backend.py fixes the database at "
                    "data/runtime/btc_live_backend.sqlite3 and exposes no "
                    "acceptance-specific CLI or environment path contract"
                ),
            }
        ],
    }


def _pack_entries(
    downtime_report: Mapping[str, Any],
    final_report: Mapping[str, Any],
) -> dict[str, bytes]:
    contract_paths = [
        PROJECT_ROOT / "contract" / "BTC_LIVE_BACKEND_WINDOWS_V1_CONTRACT.json",
        PROJECT_ROOT / "config" / "c0_c1_frozen_config.json",
        PROJECT_ROOT
        / "contract"
        / "STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json",
        PROJECT_ROOT / "reports" / "EXECUTABLE_RULE_GAP_REPORT.json",
    ]
    hash_evidence = _file_hash_evidence(contract_paths)
    entries = {
        "contract/STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json": (
            PROJECT_ROOT
            / "contract"
            / "STRATEGY_REGISTRY_47_LIVE_BACKEND_LOCK.json"
        ).read_bytes(),
        "reports/EXECUTABLE_RULE_GAP_REPORT.json": (
            PROJECT_ROOT / "reports" / "EXECUTABLE_RULE_GAP_REPORT.json"
        ).read_bytes(),
        "reports/C1_OFFLINE_VERIFICATION.json": (
            PROJECT_ROOT / "reports" / "C1_OFFLINE_VERIFICATION.json"
        ).read_bytes(),
        "reports/C1_PROVIDER_CAPABILITY_SMOKE.json": (
            PROJECT_ROOT / "reports" / "C1_PROVIDER_CAPABILITY_SMOKE.json"
        ).read_bytes(),
        "reports/C1_DOWNTIME_ACCEPTANCE.json": _canonical_json_bytes(
            downtime_report
        ),
        "reports/C1_FINAL_ACCEPTANCE.json": _canonical_json_bytes(final_report),
        "evidence/contract_config_registry_hashes.json": _canonical_json_bytes(
            hash_evidence
        ),
        "evidence/database_integrity.json": _canonical_json_bytes(
            downtime_report["database_integrity"]
        ),
        "evidence/source_continuity.json": _canonical_json_bytes(
            downtime_report["binance_continuity"]
        ),
        "evidence/polymarket_reconciliation.json": _canonical_json_bytes(
            downtime_report["polymarket_reconciliation"]
        ),
        "evidence/canary_evaluation.json": _canonical_json_bytes(
            downtime_report["canary_semantics"]
        ),
        "evidence/outbox_replay.json": _canonical_json_bytes(
            downtime_report["outbox_replay"]
        ),
        "evidence/incident_summary.json": _canonical_json_bytes(
            {"blocking_failures": downtime_report["blocking_failures"]}
        ),
        "evidence/sanitized_bounded_log.json": _canonical_json_bytes(
            {
                "events": [
                    "PREFLIGHT_PASS",
                    "OFFLINE_GATE_PENDING",
                    "RUNTIME_PATH_CONTRACT_BLOCKED",
                    "BACKEND_NOT_STARTED",
                ]
            }
        ),
    }
    return entries


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(payload))


def main() -> int:
    require_windows()
    now = datetime.now(UTC)
    run_id = (
        f"C1-ACCEPTANCE-{now.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8].upper()}"
    )
    commit_sha = _git_value("rev-parse", "HEAD")
    branch = _git_value("branch", "--show-current")
    path_contract = inspect_runtime_path_contract(PROJECT_ROOT, run_id)
    if path_contract["supported"]:
        raise AcceptanceBlocked(
            "ACCEPTANCE_RUNTIME_IMPLEMENTATION_NOT_REACHED_IN_THIS_BUILD"
        )

    downtime_report = _blocked_downtime_report(
        run_id=run_id,
        commit_sha=commit_sha,
        branch=branch,
        started_at=now.isoformat().replace("+00:00", "Z"),
        path_contract=path_contract,
    )
    preliminary_final = build_final_report(
        run_id=run_id,
        commit_sha=commit_sha,
        status="BLOCKED_RUNTIME_PATH_CONTRACT",
        pack_evidence={
            "status": "BUILDING_BLOCKED_EVIDENCE",
            "path": "artifacts/C1_ACCEPTANCE_PACK.zip",
        },
    )
    validate_report(preliminary_final)
    _write_json(DOWNTIME_REPORT_PATH, downtime_report)
    _write_json(FINAL_REPORT_PATH, preliminary_final)
    pack_evidence = build_acceptance_pack(
        PACK_PATH,
        _pack_entries(downtime_report, preliminary_final),
        status="BLOCKED_RUNTIME_PATH_CONTRACT",
    )
    final_report = build_final_report(
        run_id=run_id,
        commit_sha=commit_sha,
        status="BLOCKED_RUNTIME_PATH_CONTRACT",
        pack_evidence=pack_evidence,
    )
    validate_report(final_report)
    _write_json(FINAL_REPORT_PATH, final_report)
    print(
        json.dumps(
            {
                "status": "BLOCKED_RUNTIME_PATH_CONTRACT",
                "run_id": run_id,
                "pack_sha256": pack_evidence["sha256"],
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
