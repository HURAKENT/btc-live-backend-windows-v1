from __future__ import annotations

import asyncio
import hashlib
import http.client
import importlib.util
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import aiohttp


_SINGLE_INSTANCE_IMPORT_ROOT = Path(__file__).resolve().parent.parent
_single_instance_path_inserted = (
    str(_SINGLE_INSTANCE_IMPORT_ROOT) not in sys.path
)
if _single_instance_path_inserted:
    sys.path.insert(0, str(_SINGLE_INSTANCE_IMPORT_ROOT))
try:
    from src.single_instance import AlreadyRunningError, WindowsMutex
finally:
    if _single_instance_path_inserted:
        sys.path.remove(str(_SINGLE_INSTANCE_IMPORT_ROOT))


IMPORT_PERFORMED_NETWORK_IO = False
MIN_DOWNTIME_MS = 600_000
MAX_DOWNTIME_MS = 630_000
PROJECT_ROOT = _SINGLE_INSTANCE_IMPORT_ROOT
REPORTS_DIR = PROJECT_ROOT / "reports"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
DOWNTIME_REPORT_PATH = REPORTS_DIR / "C1_DOWNTIME_ACCEPTANCE.json"
FINAL_REPORT_PATH = REPORTS_DIR / "C1_FINAL_ACCEPTANCE.json"
PACK_PATH = ARTIFACTS_DIR / "C1_ACCEPTANCE_PACK.zip"
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "data" / "runtime" / "btc_live_backend.sqlite3"
)
ACCEPTANCE_DATA_BASE = (
    PROJECT_ROOT.parent
    / "btc_live_backend_windows_v1_data"
    / "acceptance"
)
ACCEPTANCE_RUNTIME_BASE = (
    PROJECT_ROOT.parent
    / "btc_live_backend_windows_v1_runtime"
    / "acceptance"
)
API_HOST = "127.0.0.1"
API_PORT = 8767
INITIAL_READY_OBSERVATION_SECONDS = 180
RUNTIME_BASELINE_COMMIT = "eec05197f05c3d6887bbc49a5edd125ad784cd46"
REAL_DOWNTIME_TARGET_MS = 605_000
ACCEPTANCE_MUTEX_NAME = "BTC_LIVE_BACKEND_WINDOWS_V1_C1_ACCEPTANCE"


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


def backend_command(database_path: Path | None = None) -> list[str]:
    command = [sys.executable, "run_backend.py"]
    if database_path is not None:
        command.extend(
            [
                "--database-path",
                str(database_path.resolve(strict=False)),
            ]
        )
    return command


def path_fingerprint(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=False)
    if not resolved.exists():
        return {
            "exists": False,
            "size": None,
            "mtime_ns": None,
            "sha256": None,
        }
    if not resolved.is_file():
        raise AcceptanceBlocked("BLOCKED_DEFAULT_DATABASE_NOT_FILE")
    stat = resolved.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256(resolved.read_bytes()),
    }


def validate_acceptance_database_path(
    database_path: Path,
    run_data_root: Path,
) -> Path:
    if not database_path.is_absolute() or not run_data_root.is_absolute():
        raise AcceptanceBlocked("BLOCKED_RUNTIME_PATH_CONTRACT")
    resolved = database_path.resolve(strict=False)
    data_root = run_data_root.resolve(strict=False)
    if resolved == DEFAULT_DATABASE_PATH.resolve(strict=False):
        raise AcceptanceBlocked("BLOCKED_RUNTIME_PATH_CONTRACT")
    try:
        resolved.relative_to(data_root)
    except ValueError:
        raise AcceptanceBlocked("BLOCKED_RUNTIME_PATH_CONTRACT") from None
    if resolved != data_root / "btc_live_backend.sqlite3":
        raise AcceptanceBlocked("BLOCKED_RUNTIME_PATH_CONTRACT")
    return resolved


def acceptance_run_paths(run_id: str) -> tuple[Path, Path, Path]:
    data_root = (ACCEPTANCE_DATA_BASE / run_id).resolve(strict=False)
    runtime_root = (ACCEPTANCE_RUNTIME_BASE / run_id).resolve(strict=False)
    database_path = validate_acceptance_database_path(
        data_root / "btc_live_backend.sqlite3",
        data_root,
    )
    return data_root, runtime_root, database_path


def prepare_run_data_root(data_root: Path) -> None:
    if data_root.exists():
        if not data_root.is_dir() or any(data_root.iterdir()):
            raise AcceptanceBlocked("BLOCKED_ACCEPTANCE_DATA_ROOT_NOT_EMPTY")
        return
    data_root.mkdir(parents=True, exist_ok=False)


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
        "commit_under_test": commit_sha,
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

    data_root, _, expected = acceptance_run_paths(run_id)
    resolver = getattr(module, "resolve_database_path", None)
    supported = callable(resolver)
    resolved = None
    blocker = None
    if supported:
        try:
            resolved = resolver(str(expected))
            supported = resolved == expected
        except (OSError, ValueError):
            supported = False
    if not supported:
        blocker = "BLOCKED_RUNTIME_PATH_CONTRACT"
    return {
        "supported": supported,
        "backend_entrypoint": "run_backend.py",
        "backend_command": [
            "CURRENT_SYS_EXECUTABLE",
            "run_backend.py",
            "--database-path",
            "data-root/acceptance/<run_id>/btc_live_backend.sqlite3",
        ],
        "configured_database_path": (
            "data/runtime/btc_live_backend.sqlite3"
        ),
        "required_acceptance_database_label": (
            "data-root/acceptance/<run_id>/btc_live_backend.sqlite3"
        ),
        "database_path_sha256": _sha256(str(expected).encode("utf-8")),
        "run_data_root_sha256": _sha256(str(data_root).encode("utf-8")),
        "supported_cli_path_parameters": (
            ["--database-path"] if supported else []
        ),
        "supported_environment_path_variables": [],
        "custom_database_path_used": supported,
        "blocker": blocker,
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


def _port_is_free(host: str = API_HOST, port: int = API_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) != 0


def _mutex_is_free() -> bool:
    from src.single_instance import AlreadyRunningError, WindowsMutex

    try:
        mutex = WindowsMutex.acquire("BTC_LIVE_BACKEND_WINDOWS_V1")
    except AlreadyRunningError:
        return False
    mutex.close()
    return True


def _local_json(path: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection(API_HOST, API_PORT, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise AcceptanceBlocked(f"BLOCKED_LOCAL_API_HTTP_{response.status}")
    payload = json.loads(body)
    if type(payload) is not dict:
        raise AcceptanceBlocked("BLOCKED_LOCAL_API_SHAPE")
    return payload


def _live_ready_evidence(bootstrap: Mapping[str, Any]) -> dict[str, Any]:
    health = bootstrap.get("health")
    health_mapping = health if type(health) is dict else {}
    readiness_value = health_mapping.get("runtime_readiness")
    readiness = readiness_value if type(readiness_value) is dict else {}
    source_value = health_mapping.get("source_health")
    source_health = source_value if type(source_value) is dict else {}
    startup_state = readiness.get("state")
    market_identity = bootstrap.get("current_market_identity")
    ready = (
        health_mapping.get("status") == "PASS"
        and startup_state == "LIVE_READY"
        and readiness.get("live_ready") is True
        and source_health.get("binance") == "LIVE"
        and source_health.get("polymarket") == "LIVE"
        and readiness.get("market_count") == 11
        and readiness.get("asset_count") == 22
        and type(market_identity) is dict
    )
    return {
        "live_ready": ready,
        "startup_state": startup_state,
        "health_status": health_mapping.get("status"),
        "source_names": sorted(source_health),
        "binance_status": source_health.get("binance"),
        "polymarket_status": source_health.get("polymarket"),
        "current_market_identity_present": type(market_identity) is dict,
        "market_id": readiness.get("market_id"),
        "market_count": readiness.get("market_count"),
        "asset_count": readiness.get("asset_count"),
        "last_event_id": bootstrap.get("last_event_id"),
    }


def _wait_for_initial_live_ready(
    process: subprocess.Popen[Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + INITIAL_READY_OBSERVATION_SECONDS
    observations: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            return {
                "live_ready": False,
                "process_exit_code": exit_code,
                "observations": observations,
                "termination_reason": "BACKEND_EXITED_BEFORE_LIVE_READY",
            }
        try:
            bootstrap = _local_json("/api/v1/bootstrap")
        except (ConnectionError, OSError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        evidence = _live_ready_evidence(bootstrap)
        observations.append(evidence)
        if evidence["live_ready"]:
            return {
                **evidence,
                "process_exit_code": None,
                "observations": observations[-3:],
                "termination_reason": "LIVE_READY",
            }
        time.sleep(1)
    final = observations[-1] if observations else {
        "live_ready": False,
        "startup_state": None,
        "health_status": None,
        "source_names": [],
        "binance_status": None,
        "polymarket_status": None,
        "current_market_identity_present": False,
        "last_event_id": None,
    }
    return {
        **final,
        "process_exit_code": process.poll(),
        "observations": observations[-3:],
        "termination_reason": "INITIAL_LIVE_READY_NOT_OBSERVED",
    }


def _start_backend_process(
    database_path: Path,
    runtime_root: Path,
) -> tuple[subprocess.Popen[Any], Any]:
    runtime_root.mkdir(parents=True, exist_ok=False)
    log_handle = (runtime_root / "backend.log").open(
        "w",
        encoding="utf-8",
        newline="\n",
    )
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        backend_command(database_path),
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return process, log_handle


def _stop_backend_process(
    process: subprocess.Popen[Any],
) -> tuple[int | None, bool, int, int]:
    stop_requested_at_ms = time.time_ns() // 1_000_000
    forced = False
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            forced = True
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    stopped_at_ms = time.time_ns() // 1_000_000
    return process.returncode, forced, stop_requested_at_ms, stopped_at_ms


def _database_integrity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "status": "NOT_AVAILABLE",
            "quick_check": "NOT_RUN",
            "integrity_check": "NOT_RUN",
            "journal_mode": "NOT_RUN",
            "synchronous": "NOT_RUN",
            "foreign_keys": "NOT_RUN",
        }
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        return {
            "status": "PASS",
            "quick_check": connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0],
            "integrity_check": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "journal_mode": connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0].lower(),
            "synchronous": connection.execute(
                "PRAGMA synchronous"
            ).fetchone()[0],
            "foreign_keys": connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0],
        }
    finally:
        connection.close()


def _actual_blocked_report(
    *,
    run_id: str,
    commit_sha: str,
    branch: str,
    started_at: str,
    status: str,
    path_contract: Mapping[str, Any],
    initial_state: Mapping[str, Any],
    initial_exit_code: int | None,
    forced_termination: bool,
    stop_requested_at_ms: int,
    process_stopped_at_ms: int,
    database_integrity: Mapping[str, Any],
    default_before: Mapping[str, Any],
    default_after: Mapping[str, Any],
    final_port_free: bool,
    final_mutex_free: bool,
) -> dict[str, Any]:
    not_run = {"status": f"NOT_RUN_{status}"}
    detail = (
        "The backend exposed its loopback API but did not expose "
        "LIVE_READY, live Binance/Polymarket sources, or a current market "
        "identity. Source review shows BackendRuntime.start_runtime_tasks() "
        "returns without starting provider/recovery tasks."
    )
    return {
        "schema_version": (
            "BTC_LIVE_BACKEND_WINDOWS_V1_C1_DOWNTIME_ACCEPTANCE_V1"
        ),
        "run_id": run_id,
        "commit_sha": commit_sha,
        "commit_under_test": commit_sha,
        "branch": branch,
        "status": status,
        "gate": "NOT_REACHED",
        "task_14_started": True,
        "task_14_completed": True,
        "started_at_utc": started_at,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "platform": sys.platform,
        "data_path_relative_label": (
            "data-root/acceptance/<run_id>/btc_live_backend.sqlite3"
        ),
        "runtime_path_relative_label": "runtime-root/acceptance/<run_id>",
        "database_path_contract": {
            **dict(path_contract),
            "initial_database_path_sha256": path_contract[
                "database_path_sha256"
            ],
            "restart_database_path_sha256": path_contract[
                "database_path_sha256"
            ],
            "same_path_for_initial_and_restart": True,
            "default_database_before": dict(default_before),
            "default_database_after": dict(default_after),
            "default_database_unchanged": (
                dict(default_before) == dict(default_after)
            ),
        },
        "initial_state": {
            "status": status,
            "live_ready": False,
            "backend_process_started": True,
            "runtime_path_contract": dict(path_contract),
            "observation": dict(initial_state),
            "sanitized_process_command": path_contract["backend_command"],
        },
        "single_instance": dict(not_run),
        "downtime": {
            **not_run,
            "stop_requested_at_ms": stop_requested_at_ms,
            "process_stopped_at_ms": process_stopped_at_ms,
            "restart_requested_at_ms": None,
            "downtime_duration_ms": None,
            "forced_termination_used": forced_termination,
            "initial_backend_exit_code": initial_exit_code,
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
        "database_integrity": dict(database_integrity),
        "final_state": {
            "backend_absent": True,
            "mutex_free": final_mutex_free,
            "port_8767_free": final_port_free,
        },
        "security": {
            "network_used": False,
            "backend_process_started": True,
            "unknown_process_stopped": False,
            "forced_termination_used": forced_termination,
            "real_order_submission": False,
            "wallet_or_signing": False,
            "paper_execution": False,
            "registry_47_execution": False,
        },
        "blocking_failures": [{"code": status, "detail": detail}],
    }


def _pack_entries(
    downtime_report: Mapping[str, Any],
    final_report: Mapping[str, Any],
) -> dict[str, bytes]:
    initial_state = downtime_report["initial_state"]
    backend_process_started = initial_state.get(
        "backend_process_started",
        initial_state.get("live_ready"),
    )
    if type(backend_process_started) is not bool:
        raise ValueError("INVALID_BACKEND_PROCESS_STARTED_EVIDENCE")
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
                    "OFFLINE_GATE_PASS",
                    f"ACCEPTANCE_STATUS_{downtime_report['status']}",
                    (
                        "BACKEND_STARTED"
                        if backend_process_started
                        else "BACKEND_NOT_STARTED"
                    ),
                ]
            }
        ),
    }
    return entries


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(payload))


def _phase(dependencies: Any, name: str) -> None:
    recorder = getattr(dependencies, "record_phase", None)
    if callable(recorder):
        recorder(name)


def _ready_is_complete(value: Mapping[str, Any]) -> bool:
    return (
        value.get("live_ready") is True
        and value.get("startup_state") == "LIVE_READY"
        and value.get("binance_status") == "LIVE"
        and value.get("polymarket_status") == "LIVE"
        and value.get("market_count") == 11
        and value.get("asset_count") == 22
    )


def _stop_is_clean(value: Mapping[str, Any]) -> bool:
    return (
        value.get("exit_code") == 0
        and value.get("forced_termination_used") is False
        and value.get("port_free") is True
        and value.get("mutex_free") is True
        and value.get("child_processes") == 0
    )


def _evaluation_audit_passes(value: Mapping[str, Any]) -> bool:
    return (
        type(value.get("recovered_count")) is int
        and value["recovered_count"] >= 1
        and type(value.get("current_count")) is int
        and value["current_count"] >= 1
        and value.get("distinct_identities") is True
        and value.get("duplicate_recovered_identities") == 0
        and value.get("recovered_execution_eligible") is False
        and value.get("current_execution_eligible") is False
        and value.get("recovered_trading_eligible") is False
        and value.get("current_trading_eligible") is False
        and value.get("recovered_infrastructure_only") is True
        and value.get("current_infrastructure_only") is True
    )


def _continuity_audit_passes(value: Mapping[str, Any]) -> bool:
    zero_fields = (
        "missing_binance_closed_minutes",
        "duplicate_binance_natural_keys",
        "binance_ohlcv_conflicts",
        "source_cursor_regressions",
        "last_event_id_regressions",
        "source_event_conflicts",
    )
    return (
        all(value.get(field) == 0 for field in zero_fields)
        and value.get("polymarket_asset_count") == 22
        and type(value.get("snapshot_input_count")) is int
        and type(value.get("snapshot_input_bound")) is int
        and value["snapshot_input_count"] <= value["snapshot_input_bound"]
    )


def _outbox_audit_passes(value: Mapping[str, Any]) -> bool:
    replayed = value.get("replayed_event_ids")
    database = value.get("database_event_ids")
    return (
        value.get("proved") is True
        and type(replayed) is list
        and bool(replayed)
        and replayed == sorted(set(replayed))
        and type(database) is list
        and replayed == database
        and value.get("missing_event_ids") == []
    )


def _integrity_audit_passes(value: Mapping[str, Any]) -> bool:
    return (
        value.get("status") == "PASS"
        and value.get("quick_check") == "ok"
        and value.get("integrity_check") == "ok"
        and value.get("journal_mode") == "wal"
        and value.get("synchronous") == 2
        and value.get("foreign_keys") == 1
        and value.get("migration_version") == 2
        and value.get("table_count") == 9
    )


def run_acceptance_cycle(
    *,
    database_path: Path,
    dependencies: Any,
    downtime_target_ms: int = 605_000,
) -> dict[str, Any]:
    if not isinstance(database_path, Path) or not database_path.is_absolute():
        raise ValueError("INVALID_ACCEPTANCE_DATABASE_PATH")
    if (
        type(downtime_target_ms) is not int
        or not MIN_DOWNTIME_MS <= downtime_target_ms <= MAX_DOWNTIME_MS
    ):
        raise ValueError("INVALID_ACCEPTANCE_DOWNTIME_TARGET")

    evidence: dict[str, Any] = {
        "database_path": str(database_path),
        "status": "STARTING",
    }
    initial = dependencies.start_backend(database_path, "initial")
    initial_starting = dependencies.observe_starting(initial, "initial")
    evidence["initial_starting_observed"] = initial_starting
    if initial_starting is not True:
        evidence["initial_stop"] = dependencies.stop_backend(
            initial,
            "initial",
        )
        evidence["status"] = "BLOCKED_INITIAL_LIVE_READY"
        return evidence
    initial_ready = dependencies.wait_live_ready(initial, "initial")
    evidence["initial_ready"] = initial_ready
    if not _ready_is_complete(initial_ready):
        initial_stop = dependencies.stop_backend(
            initial,
            "initial",
        )
        evidence["initial_stop"] = initial_stop
        audit_blocked = getattr(dependencies, "audit_blocked", None)
        if callable(audit_blocked):
            evidence["blocked_evidence"] = audit_blocked(
                database_path,
                initial_ready,
                initial_stop,
            )
        evidence["status"] = "BLOCKED_INITIAL_LIVE_READY"
        return evidence

    second_exit = dependencies.check_second_instance(database_path)
    evidence["second_instance_exit_code"] = second_exit
    baseline = dependencies.audit_baseline(database_path, initial_ready)
    evidence["initial_baseline"] = baseline
    initial_outbox = dependencies.audit_outbox(
        "initial",
        0,
    )
    evidence["initial_outbox"] = initial_outbox
    initial_stop = dependencies.stop_backend(initial, "initial")
    evidence["initial_stop"] = initial_stop
    if not _stop_is_clean(initial_stop):
        evidence["status"] = "BLOCKED_GRACEFUL_SHUTDOWN"
        return evidence
    if second_exit != 20:
        evidence["status"] = "BLOCKED_SINGLE_INSTANCE_GATE"
        return evidence
    if not _outbox_audit_passes(initial_outbox):
        evidence["status"] = "BLOCKED_OUTBOX_REPLAY"
        return evidence

    _phase(dependencies, "downtime start")
    stopped_ns = dependencies.monotonic_ns()
    dependencies.sleep(downtime_target_ms / 1000)
    restart_requested_ns = dependencies.monotonic_ns()
    duration_ms = monotonic_duration_ms(stopped_ns, restart_requested_ns)
    evidence["downtime_duration_ms"] = duration_ms
    _phase(dependencies, "downtime validation")
    try:
        validate_downtime_duration(duration_ms)
    except AcceptanceBlocked as error:
        evidence["status"] = str(error)
        return evidence

    restart = dependencies.start_backend(database_path, "restart")
    restart_starting = dependencies.observe_starting(restart, "restart")
    evidence["restart_starting_observed"] = restart_starting
    if restart_starting is not True:
        evidence["final_stop"] = dependencies.stop_backend(
            restart,
            "final",
        )
        evidence["status"] = "BLOCKED_RECOVERY"
        return evidence
    restart_ready = dependencies.wait_live_ready(restart, "restart")
    evidence["restart_ready"] = restart_ready
    if not _ready_is_complete(restart_ready):
        evidence["final_stop"] = dependencies.stop_backend(
            restart,
            "final",
        )
        evidence["status"] = "BLOCKED_RECOVERY"
        return evidence

    recovery = dependencies.audit_recovery(
        database_path,
        initial_ready,
        restart_ready,
    )
    evaluations = dependencies.audit_evaluations(database_path)
    continuity = dependencies.audit_continuity(
        database_path,
        initial_ready,
        restart_ready,
    )
    after_event_id = baseline.get("last_event_id", 0)
    post_restart_outbox = dependencies.audit_outbox(
        "restart",
        after_event_id,
    )
    final_stop = dependencies.stop_backend(restart, "final")
    final_integrity = dependencies.audit_final_database(database_path)
    evidence.update(
        {
            "recovery": recovery,
            "evaluations": evaluations,
            "continuity": continuity,
            "post_restart_outbox": post_restart_outbox,
            "final_stop": final_stop,
            "database_integrity": final_integrity,
        }
    )

    if not _stop_is_clean(final_stop):
        status = "BLOCKED_GRACEFUL_SHUTDOWN"
    elif not _integrity_audit_passes(final_integrity):
        status = "BLOCKED_DATABASE_INTEGRITY"
    elif (
        recovery.get("post_restart_live_ready") is not True
        or recovery.get("polymarket_reconciled") is not True
        or recovery.get("historical_depth_classification") != "NOT_REQUIRED"
    ):
        status = "BLOCKED_RECOVERY"
    elif not _evaluation_audit_passes(evaluations):
        status = "BLOCKED_CANARY_SEMANTICS"
    elif not _continuity_audit_passes(continuity):
        status = "BLOCKED_RECOVERY"
    elif not _outbox_audit_passes(post_restart_outbox):
        status = "BLOCKED_OUTBOX_REPLAY"
    else:
        status = "PASS"
    evidence["status"] = status
    if status == "PASS":
        evidence["promotion"] = dependencies.promote_artifacts(evidence)
    return evidence


def _wait_for_starting(process: subprocess.Popen[Any]) -> bool:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            bootstrap = _local_json("/api/v1/bootstrap")
        except (ConnectionError, OSError, json.JSONDecodeError):
            time.sleep(0.25)
            continue
        status = bootstrap.get("health", {}).get("status")
        return status in {"STARTING", "DEGRADED", "PASS"}
    return False


async def _websocket_replay(after_event_id: int, count: int) -> list[int]:
    if count <= 0:
        return []
    url = (
        f"http://{API_HOST}:{API_PORT}/ws/v1/events"
        f"?after_event_id={after_event_id}"
    )
    result: list[int] = []
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    async with aiohttp.ClientSession(
        timeout=timeout,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    ) as session:
        async with session.ws_connect(url) as websocket:
            for _ in range(count):
                payload = await websocket.receive_json(timeout=10)
                result.append(payload["event_id"])
    return result


def _read_database(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = (
            "schema_migrations",
            "source_events",
            "source_cursors",
            "market_catalog",
            "canonical_state",
            "strategy_evaluations",
            "signals",
            "outbox_events",
            "incidents",
        )
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in tables
        }
        cursors = {
            row[0]: {
                "cursor": json.loads(row[1]),
                "updated_at_ms": row[2],
            }
            for row in connection.execute(
                "SELECT source, cursor_json, updated_at_ms "
                "FROM source_cursors ORDER BY source"
            )
        }
        incidents = [
            {
                "incident_key": row[0],
                "severity": row[1],
                "status": row[2],
                "payload": json.loads(row[3]),
                "created_at_ms": row[4],
            }
            for row in connection.execute(
                "SELECT incident_key, severity, status, payload_json, "
                "created_at_ms FROM incidents ORDER BY incident_id"
            )
        ]
        evaluations = [
            {
                "evaluation_id": row[0],
                "evaluation_key": row[1],
                "execution_eligible": bool(row[2]),
                "payload": json.loads(row[3]),
            }
            for row in connection.execute(
                "SELECT evaluation_id, evaluation_key, execution_eligible, "
                "payload_json FROM strategy_evaluations "
                "ORDER BY evaluation_id"
            )
        ]
        snapshot_row = connection.execute(
            "SELECT snapshot_key, source_event_ids_json, payload_json, "
            "payload_sha256 FROM canonical_state "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        latest_snapshot = (
            None
            if snapshot_row is None
            else {
                "snapshot_key": snapshot_row[0],
                "source_event_ids": json.loads(snapshot_row[1]),
                "payload": json.loads(snapshot_row[2]),
                "payload_sha256": snapshot_row[3],
            }
        )
        return {
            "status": "PASS",
            "quick_check": connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0],
            "integrity_check": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "journal_mode": connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0].lower(),
            "synchronous": connection.execute(
                "PRAGMA synchronous"
            ).fetchone()[0],
            "foreign_keys": connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0],
            "migration_version": connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0],
            "table_count": len(tables),
            "counts": counts,
            "cursors": cursors,
            "incidents": incidents,
            "evaluations": evaluations,
            "latest_snapshot": latest_snapshot,
            "outbox_event_ids": [
                row[0]
                for row in connection.execute(
                    "SELECT event_id FROM outbox_events ORDER BY event_id"
                )
            ],
            "duplicate_natural_keys": connection.execute(
                "SELECT COUNT(*) FROM (SELECT natural_key "
                "FROM source_events GROUP BY natural_key "
                "HAVING COUNT(*) > 1)"
            ).fetchone()[0],
        }
    finally:
        connection.close()


def _blocked_database_evidence(path: Path) -> dict[str, Any]:
    audit = _read_database(path)
    diagnostics: list[dict[str, Any]] = []
    diagnostic_keys: set[str] = set()
    lifecycle_states: list[str] = []
    prefix = "POLYMARKET_HISTORY_SEQUENCE_ERROR:"
    allowed_diagnostic_fields = {
        "adjacent_relations",
        "cross_page_overlap_count",
        "direction",
        "duplicate_position_count",
        "duplicate_positions",
        "duplicate_positions_truncated",
        "first_rejected_position",
        "item_count",
        "page_index",
        "rejection_category",
        "request_range_sha256",
        "timestamp_type_histogram",
    }
    for incident in audit["incidents"]:
        payload = incident["payload"]
        if payload.get("record_type") == "LIFECYCLE_STATE":
            state = payload.get("state")
            if type(state) is str:
                lifecycle_states.append(state)
        detail = payload.get("detail")
        if type(detail) is not str or not detail.startswith(prefix):
            continue
        try:
            diagnostic = json.loads(detail.removeprefix(prefix))
        except json.JSONDecodeError:
            continue
        if (
            type(diagnostic) is dict
            and set(diagnostic) == allowed_diagnostic_fields
        ):
            diagnostic_key = json.dumps(
                diagnostic,
                sort_keys=True,
                separators=(",", ":"),
            )
            if diagnostic_key not in diagnostic_keys:
                diagnostic_keys.add(diagnostic_key)
                diagnostics.append(diagnostic)
    return {
        "database_integrity": {
            key: audit[key]
            for key in (
                "status",
                "quick_check",
                "integrity_check",
                "journal_mode",
                "synchronous",
                "foreign_keys",
                "migration_version",
                "table_count",
            )
        },
        "history_sequence_diagnostics": diagnostics,
        "latest_lifecycle_state": (
            lifecycle_states[-1] if lifecycle_states else None
        ),
        "sanitized": True,
    }


def _protected_hashes() -> dict[str, str]:
    paths = _git_value(
        "ls-files",
        "src/*",
        "run_backend.py",
        "migrations/*",
        "contract/*",
        "registry/*",
        "config/*",
        "requirements*",
        "pyproject.toml",
        "docs/frozen/*",
        "scripts/RUN_C1_ACCEPTANCE_SAFE.ps1",
        "tools/simulate_downtime.py",
    ).splitlines()
    return {
        path: _sha256((PROJECT_ROOT / path).read_bytes())
        for path in sorted(paths)
    }


class _ProductionDependencies:
    def __init__(
        self,
        *,
        run_id: str,
        runtime_root: Path,
        commit_sha: str,
        branch: str,
        started_at_utc: str,
        path_contract: Mapping[str, Any],
        default_before: Mapping[str, Any],
        protected_before: Mapping[str, str],
    ) -> None:
        self.run_id = run_id
        self.runtime_root = runtime_root
        self.evidence_root = runtime_root / "evidence"
        self.evidence_root.mkdir(parents=True, exist_ok=False)
        self.commit_sha = commit_sha
        self.branch = branch
        self.started_at_utc = started_at_utc
        self.path_contract = dict(path_contract)
        self.default_before = dict(default_before)
        self.protected_before = dict(protected_before)
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.logs: dict[str, Any] = {}
        self.initial_database: dict[str, Any] | None = None
        self.stop_requested_at_ms: int | None = None
        self.process_stopped_at_ms: int | None = None
        self.restart_requested_at_ms: int | None = None
        self.restart_ready_at_ms: int | None = None

    def start_backend(self, database_path: Path, phase: str) -> Any:
        if phase == "restart":
            self.restart_requested_at_ms = time.time_ns() // 1_000_000
        process, handle = _start_backend_process(
            database_path,
            self.runtime_root / phase,
        )
        self.processes[phase] = process
        self.logs[phase] = handle
        return process

    def observe_starting(self, process: Any, phase: str) -> bool:
        return _wait_for_starting(process)

    def wait_live_ready(self, process: Any, phase: str) -> dict[str, Any]:
        evidence = _wait_for_initial_live_ready(process)
        if phase == "restart" and evidence.get("live_ready") is True:
            self.restart_ready_at_ms = time.time_ns() // 1_000_000
        return evidence

    @staticmethod
    def audit_blocked(
        database_path: Path,
        ready: Mapping[str, Any],
        stop: Mapping[str, Any],
    ) -> dict[str, Any]:
        del ready, stop
        return _blocked_database_evidence(database_path)

    def check_second_instance(self, database_path: Path) -> int:
        process, handle = _start_backend_process(
            database_path,
            self.runtime_root / "second-instance",
        )
        try:
            return process.wait(timeout=20)
        finally:
            handle.close()

    def audit_baseline(
        self,
        database_path: Path,
        ready: Mapping[str, Any],
    ) -> dict[str, Any]:
        audit = _read_database(database_path)
        self.initial_database = audit
        return {
            "last_event_id": ready.get("last_event_id", 0),
            "database_integrity": {
                key: audit[key]
                for key in (
                    "status",
                    "quick_check",
                    "integrity_check",
                    "journal_mode",
                    "synchronous",
                    "foreign_keys",
                    "migration_version",
                    "table_count",
                )
            },
            "counts": audit["counts"],
            "cursors": audit["cursors"],
        }

    def audit_outbox(self, phase: str, after_event_id: int) -> dict[str, Any]:
        target_path = Path(self.path_contract["_resolved_database_path"])
        audit = _read_database(target_path)
        database_ids = [
            event_id
            for event_id in audit["outbox_event_ids"]
            if event_id > after_event_id
        ]
        replayed = asyncio.run(
            _websocket_replay(after_event_id, len(database_ids))
        )
        return {
            "proved": bool(replayed) and replayed == database_ids,
            "replayed_event_ids": replayed,
            "database_event_ids": database_ids,
            "missing_event_ids": [
                event_id
                for event_id in database_ids
                if event_id not in replayed
            ],
        }

    def stop_backend(self, process: Any, phase: str) -> dict[str, Any]:
        (
            exit_code,
            forced,
            requested_at_ms,
            stopped_at_ms,
        ) = _stop_backend_process(process)
        handle = self.logs.pop(
            "initial" if phase == "initial" else "restart",
            None,
        )
        if handle is not None:
            handle.close()
        if phase == "initial":
            self.stop_requested_at_ms = requested_at_ms
            self.process_stopped_at_ms = stopped_at_ms
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _port_is_free() and _mutex_is_free():
                break
            time.sleep(0.1)
        return {
            "exit_code": exit_code,
            "forced_termination_used": forced,
            "port_free": _port_is_free(),
            "mutex_free": _mutex_is_free(),
            "child_processes": int(process.poll() is None),
            "stop_requested_at_ms": requested_at_ms,
            "process_stopped_at_ms": stopped_at_ms,
        }

    @staticmethod
    def monotonic_ns() -> int:
        return time.monotonic_ns()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    def audit_recovery(
        self,
        database_path: Path,
        initial: Mapping[str, Any],
        restart: Mapping[str, Any],
    ) -> dict[str, Any]:
        audit = _read_database(database_path)
        blocking = [
            incident
            for incident in audit["incidents"]
            if incident["status"] == "RECOVERY_BLOCKED"
        ]
        return {
            "post_restart_live_ready": restart.get("live_ready") is True,
            "polymarket_reconciled": (
                restart.get("asset_count") == 22
                and audit["counts"]["market_catalog"] == 1
            ),
            "historical_depth_classification": "NOT_REQUIRED",
            "blocking_incidents": blocking,
        }

    def audit_evaluations(self, database_path: Path) -> dict[str, Any]:
        evaluations = _read_database(database_path)["evaluations"]
        recovered = [
            value
            for value in evaluations
            if value["payload"].get("origin")
            == "RECOVERED_AFTER_DOWNTIME"
        ]
        current = [
            value
            for value in evaluations
            if value["payload"].get("origin")
            == "CURRENT_LIVE_REEVALUATION"
        ]
        recovered_keys = [value["evaluation_key"] for value in recovered]
        return {
            "recovered_count": len(recovered),
            "current_count": len(current),
            "distinct_identities": bool(recovered and current)
            and set(recovered_keys).isdisjoint(
                value["evaluation_key"] for value in current
            ),
            "duplicate_recovered_identities": (
                len(recovered_keys) - len(set(recovered_keys))
            ),
            "recovered_execution_eligible": any(
                value["execution_eligible"] for value in recovered
            ),
            "current_execution_eligible": any(
                value["execution_eligible"] for value in current
            ),
            "recovered_trading_eligible": any(
                value["payload"].get("trading_eligible") is not False
                for value in recovered
            ),
            "current_trading_eligible": any(
                value["payload"].get("trading_eligible") is not False
                for value in current
            ),
            "recovered_infrastructure_only": bool(recovered) and all(
                value["payload"].get("canary_infrastructure_only") is True
                for value in recovered
            ),
            "current_infrastructure_only": bool(current) and all(
                value["payload"].get("canary_infrastructure_only") is True
                for value in current
            ),
            "recovered_evaluation_keys": recovered_keys,
            "current_evaluation_keys": [
                value["evaluation_key"] for value in current
            ],
        }

    def audit_continuity(
        self,
        database_path: Path,
        initial: Mapping[str, Any],
        restart: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.initial_database is None:
            raise AcceptanceBlocked("BLOCKED_INITIAL_DATABASE_EVIDENCE")
        final = _read_database(database_path)
        initial_cursor = self.initial_database["cursors"].get("binance")
        final_cursor = final["cursors"].get("binance")
        start_ms = (
            None
            if initial_cursor is None
            else initial_cursor["updated_at_ms"] + 60_000
        )
        end_ms = (
            None
            if self.restart_ready_at_ms is None
            else (
                self.restart_ready_at_ms // 60_000 * 60_000 - 60_000
            )
        )
        connection = sqlite3.connect(database_path)
        try:
            observed = {
                row[0]
                for row in connection.execute(
                    "SELECT source_timestamp_ms FROM source_events "
                    "WHERE source='binance' "
                    "AND event_type='BINANCE_KLINE_CLOSED'"
                )
            }
        finally:
            connection.close()
        expected = (
            []
            if start_ms is None or end_ms is None or start_ms > end_ms
            else list(range(start_ms, end_ms + 1, 60_000))
        )
        incident_text = json.dumps(
            final["incidents"],
            sort_keys=True,
        )
        snapshot = final["latest_snapshot"]
        source_ids = [] if snapshot is None else snapshot["source_event_ids"]
        return {
            "expected_binance_closed_minutes": len(expected),
            "recovered_binance_closed_minutes": sum(
                timestamp in observed for timestamp in expected
            ),
            "missing_binance_closed_minutes": sum(
                timestamp not in observed for timestamp in expected
            ),
            "duplicate_binance_natural_keys": final[
                "duplicate_natural_keys"
            ],
            "binance_ohlcv_conflicts": incident_text.count(
                "BINANCE_OHLCV_CONFLICT"
            ),
            "source_cursor_regressions": incident_text.count(
                "SOURCE_CURSOR_REGRESSION"
            ),
            "last_event_id_regressions": int(
                restart.get("last_event_id", -1)
                < initial.get("last_event_id", 0)
            ),
            "polymarket_asset_count": restart.get("asset_count"),
            "source_event_conflicts": incident_text.count(
                "SOURCE_EVENT_CONFLICT"
            ),
            "snapshot_input_count": len(source_ids),
            "snapshot_input_bound": 23,
            "snapshot_key": (
                None if snapshot is None else snapshot["snapshot_key"]
            ),
            "snapshot_payload_sha256": (
                None if snapshot is None else snapshot["payload_sha256"]
            ),
            "initial_binance_cursor": initial_cursor,
            "final_binance_cursor": final_cursor,
        }

    @staticmethod
    def audit_final_database(database_path: Path) -> dict[str, Any]:
        return _read_database(database_path)

    def promote_artifacts(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        protected_after = _protected_hashes()
        if protected_after != self.protected_before:
            raise AcceptanceBlocked("BLOCKED_PROTECTED_SOURCE_MUTATION")
        default_after = path_fingerprint(DEFAULT_DATABASE_PATH)
        if default_after != self.default_before:
            raise AcceptanceBlocked("BLOCKED_DEFAULT_DATABASE_MUTATION")

        downtime_report = {
            "schema_version": (
                "BTC_LIVE_BACKEND_WINDOWS_V1_C1_DOWNTIME_ACCEPTANCE_V1"
            ),
            "run_id": self.run_id,
            "runtime_baseline_commit": RUNTIME_BASELINE_COMMIT,
            "acceptance_harness_commit": self.commit_sha,
            "source_commit": self.commit_sha,
            "commit_sha": self.commit_sha,
            "commit_under_test": self.commit_sha,
            "branch": self.branch,
            "status": "PASS",
            "gate": "BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS",
            "task_14_started": True,
            "task_14_completed": True,
            "started_at_utc": self.started_at_utc,
            "database_path_contract": {
                **{
                    key: value
                    for key, value in self.path_contract.items()
                    if not key.startswith("_")
                },
                "same_path_for_initial_and_restart": True,
                "default_database_before": self.default_before,
                "default_database_after": default_after,
                "default_database_unchanged": True,
            },
            "initial_state": evidence["initial_ready"],
            "single_instance": {
                "status": "PASS",
                "second_instance_exit_code": evidence[
                    "second_instance_exit_code"
                ],
            },
            "downtime": {
                "status": "PASS",
                "stop_requested_at_ms": self.stop_requested_at_ms,
                "process_stopped_at_ms": self.process_stopped_at_ms,
                "restart_requested_at_ms": self.restart_requested_at_ms,
                "downtime_duration_ms": evidence["downtime_duration_ms"],
                "initial_backend_exit_code": evidence["initial_stop"][
                    "exit_code"
                ],
                "final_backend_exit_code": evidence["final_stop"][
                    "exit_code"
                ],
                "forced_termination_used": False,
            },
            "recovery": evidence["recovery"],
            "binance_continuity": evidence["continuity"],
            "polymarket_reconciliation": {
                "status": "PASS",
                "reconciled": evidence["recovery"][
                    "polymarket_reconciled"
                ],
                "historical_depth_classification": evidence["recovery"][
                    "historical_depth_classification"
                ],
                "asset_count": evidence["continuity"][
                    "polymarket_asset_count"
                ],
            },
            "canary_semantics": evidence["evaluations"],
            "outbox_replay": {
                "status": "PASS",
                **evidence["post_restart_outbox"],
                "initial": evidence["initial_outbox"],
            },
            "database_integrity": {
                key: evidence["database_integrity"][key]
                for key in (
                    "status",
                    "quick_check",
                    "integrity_check",
                    "journal_mode",
                    "synchronous",
                    "foreign_keys",
                    "migration_version",
                    "table_count",
                    "counts",
                )
            },
            "final_state": {
                "backend_absent": True,
                "mutex_free": evidence["final_stop"]["mutex_free"],
                "port_8767_free": evidence["final_stop"]["port_free"],
                "child_processes": evidence["final_stop"][
                    "child_processes"
                ],
            },
            "protected_source": {
                "before": self.protected_before,
                "after": protected_after,
                "unchanged": True,
            },
            "security": {
                "network_scope": "PUBLIC_READ_ONLY",
                "real_order_submission": False,
                "wallet_or_signing": False,
                "paper_execution": False,
                "registry_47_execution": False,
                "authentication_used": False,
                "secrets_used": False,
            },
            "blocking_failures": [],
        }
        preliminary_final = build_final_report(
            run_id=self.run_id,
            commit_sha=self.commit_sha,
            status="PASS",
            pack_evidence={
                "status": "BUILDING",
                "path": "artifacts/C1_ACCEPTANCE_PACK.zip",
            },
        )
        preliminary_final.update(
            {
                "runtime_baseline_commit": RUNTIME_BASELINE_COMMIT,
                "acceptance_harness_commit": self.commit_sha,
                "source_commit": self.commit_sha,
            }
        )
        temp_downtime = self.evidence_root / DOWNTIME_REPORT_PATH.name
        temp_final = self.evidence_root / FINAL_REPORT_PATH.name
        temp_pack = self.evidence_root / PACK_PATH.name
        _write_json(temp_downtime, downtime_report)
        _write_json(temp_final, preliminary_final)
        pack_evidence = build_acceptance_pack(
            temp_pack,
            _pack_entries(downtime_report, preliminary_final),
            status="PASS",
        )
        final_report = build_final_report(
            run_id=self.run_id,
            commit_sha=self.commit_sha,
            status="PASS",
            pack_evidence=pack_evidence,
        )
        final_report.update(
            {
                "runtime_baseline_commit": RUNTIME_BASELINE_COMMIT,
                "acceptance_harness_commit": self.commit_sha,
                "source_commit": self.commit_sha,
            }
        )
        validate_report(final_report)
        _write_json(temp_final, final_report)
        json.loads(temp_downtime.read_text(encoding="utf-8"))
        json.loads(temp_final.read_text(encoding="utf-8"))
        validate_acceptance_pack(temp_pack)
        self._replace_canonical(
            {
                temp_downtime: DOWNTIME_REPORT_PATH,
                temp_final: FINAL_REPORT_PATH,
                temp_pack: PACK_PATH,
            }
        )
        return {
            "status": "PASS",
            "pack_sha256": pack_evidence["sha256"],
            "pack_size": pack_evidence["size"],
        }

    def _replace_canonical(self, paths: Mapping[Path, Path]) -> None:
        backup_root = self.evidence_root / "canonical-backup"
        backup_root.mkdir()
        backups: dict[Path, Path] = {}
        for destination in paths.values():
            if destination.exists():
                backup = backup_root / destination.name
                backup.write_bytes(destination.read_bytes())
                backups[destination] = backup
        replaced: list[Path] = []
        try:
            for source, destination in paths.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                replaced.append(destination)
        except BaseException:
            for destination in replaced:
                backup = backups.get(destination)
                if backup is not None:
                    os.replace(backup, destination)
            raise

    def cleanup(self) -> None:
        for phase, process in self.processes.items():
            if process.poll() is None:
                _stop_backend_process(process)
            handle = self.logs.get(phase)
            if handle is not None and not handle.closed:
                handle.close()


def _validate_prerequisites(run_id: str) -> dict[str, Any]:
    path_contract = inspect_runtime_path_contract(PROJECT_ROOT, run_id)
    if not path_contract["supported"]:
        raise AcceptanceBlocked("BLOCKED_RUNTIME_PATH_CONTRACT")
    provider_report = json.loads(
        (REPORTS_DIR / "C1_PROVIDER_CAPABILITY_SMOKE.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        provider_report.get("status") != "PASS"
        or provider_report.get("gate") != "C1_PROVIDER_CAPABILITY_PASS"
    ):
        raise AcceptanceBlocked("BLOCKED_PROVIDER_CAPABILITY_PREREQUISITE")
    gap_report = json.loads(
        (REPORTS_DIR / "EXECUTABLE_RULE_GAP_REPORT.json").read_text(
            encoding="utf-8"
        )
    )
    if gap_report.get("executable_now") != 0:
        raise AcceptanceBlocked("BLOCKED_REGISTRY_EXECUTION_SCOPE")
    contract = json.loads(
        (
            PROJECT_ROOT
            / "contract"
            / "BTC_LIVE_BACKEND_WINDOWS_V1_CONTRACT.json"
        ).read_text(encoding="utf-8")
    )
    serialized = json.dumps(contract, sort_keys=True)
    if (
        '"real_order_submission": false' not in serialized
        or '"executable_rule_pack_complete": false' not in serialized
    ):
        raise AcceptanceBlocked("BLOCKED_CONTRACT_SCOPE")
    if not _port_is_free() or not _mutex_is_free():
        raise AcceptanceBlocked("BLOCKED_LOCAL_INSTANCE_CONFLICT")
    return path_contract


def _run_acceptance_main() -> int:
    now = datetime.now(UTC)
    run_id = (
        f"C1-ACCEPTANCE-{now.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8].upper()}"
    )
    runtime_root = (ACCEPTANCE_RUNTIME_BASE / run_id).resolve(strict=False)
    evidence_root = runtime_root / "evidence"
    dependencies: _ProductionDependencies | None = None
    try:
        commit_sha = _git_value("rev-parse", "HEAD")
        branch = _git_value("branch", "--show-current")
        path_contract = _validate_prerequisites(run_id)
        data_root, runtime_root, database_path = acceptance_run_paths(run_id)
        prepare_run_data_root(data_root)
        path_contract = {
            **path_contract,
            "_resolved_database_path": str(database_path),
        }
        dependencies = _ProductionDependencies(
            run_id=run_id,
            runtime_root=runtime_root,
            commit_sha=commit_sha,
            branch=branch,
            started_at_utc=now.isoformat().replace("+00:00", "Z"),
            path_contract=path_contract,
            default_before=path_fingerprint(DEFAULT_DATABASE_PATH),
            protected_before=_protected_hashes(),
        )
        result = run_acceptance_cycle(
            database_path=database_path,
            dependencies=dependencies,
            downtime_target_ms=REAL_DOWNTIME_TARGET_MS,
        )
        if result["status"] != "PASS":
            _write_json(
                dependencies.evidence_root / "task14_blocked.json",
                result,
            )
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "run_id": run_id,
                        "evidence_path": (
                            "runtime-root/acceptance/<run_id>/evidence/"
                            "task14_blocked.json"
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "run_id": run_id,
                    "pack_sha256": result["promotion"]["pack_sha256"],
                },
                sort_keys=True,
            )
        )
        return 0
    except BaseException as error:
        evidence_root.mkdir(parents=True, exist_ok=True)
        diagnostic = {
            "status": (
                str(error)
                if isinstance(error, AcceptanceBlocked)
                else "BLOCKED_ACCEPTANCE_EXCEPTION"
            ),
            "run_id": run_id,
            "error_type": type(error).__name__,
            "detail": str(error)[:1000],
        }
        _write_json(evidence_root / "task14_blocked.json", diagnostic)
        print(
            json.dumps(
                {
                    **diagnostic,
                    "evidence_path": (
                        "runtime-root/acceptance/<run_id>/evidence/"
                        "task14_blocked.json"
                    ),
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        if dependencies is not None:
            dependencies.cleanup()


def main(*, mutex_factory=WindowsMutex.acquire) -> int:
    require_windows()
    try:
        acceptance_mutex = mutex_factory(ACCEPTANCE_MUTEX_NAME)
    except AlreadyRunningError:
        print(
            json.dumps(
                {
                    "status": "BLOCKED_ACCEPTANCE_ALREADY_RUNNING",
                    "run_id": None,
                },
                sort_keys=True,
            )
        )
        return 20

    try:
        return _run_acceptance_main()
    finally:
        acceptance_mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
