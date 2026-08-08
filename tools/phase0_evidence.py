from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence


RECEIPT_PATH = Path("reports/PHASE0_COMMAND_RECEIPT.json")
REPORT_PATH = Path("reports/PHASE0_ACCEPTED_BASELINE.json")
PACK_PATH = Path("artifacts/PHASE0_ACCEPTANCE_PACK.zip")
OUTPUT_DIRECTORY = Path("artifacts/phase0_evidence")
REQUIRED_COMMANDS = (
    "focused_tests",
    "full_offline_tests",
    "compileall",
    "pip_check",
    "scope_audit",
)
HISTORICAL_PATHS = (
    "docs/C1_AUTONOMOUS_GOAL.md",
    "docs/C1_GOAL_PROGRESS.md",
    "reports/C1_DOWNTIME_ACCEPTANCE.json",
    "reports/C1_FINAL_ACCEPTANCE.json",
    "artifacts/C1_ACCEPTANCE_PACK.zip",
    "reports/C2_RECOVERY_ACCEPTANCE.json",
    "reports/C3_MARKET_ROLLOVER_ACCEPTANCE.json",
    "reports/C2_C3_FINAL_ACCEPTANCE.json",
    "artifacts/C2_C3_ACCEPTANCE_PACK.zip",
)
TEST_ONLY_HISTORICAL_PATHS = ("seed.txt",)
PACK_MANIFEST = "MANIFEST.json"
PACK_SHA256SUMS = "SHA256SUMS"
_TEST_COUNT = re.compile(r"Ran (\d+) tests?")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_WINDOWS_USER_HOME = re.compile(
    r"(?i)[a-z]:[\\/]+users[\\/]+[^\\/\r\n\"'<>]+"
)
_WSL_USER_HOME = re.compile(r"(?i)/mnt/[a-z]/users/[^/\r\n\"'<>]+")
_WINDOWS_USER_PATH = re.compile(r"(?i)[a-z]:[\\/]+users[\\/]")
_WSL_USER_PATH = re.compile(r"(?i)/mnt/[a-z]/users/")
OUTPUT_SANITIZATION_POLICY = "PROJECT_ROOT_AND_USER_HOME_REPLACEMENT_V2"
PROJECT_ROOT_PLACEHOLDER = "<PROJECT_ROOT>"
USER_HOME_PLACEHOLDER = "<USER_HOME>"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    expected_test_count: int | None = None

    def __post_init__(self) -> None:
        if self.name not in REQUIRED_COMMANDS:
            raise ValueError("INVALID_PHASE0_COMMAND_NAME")
        if not self.argv or any(type(value) is not str or not value for value in self.argv):
            raise ValueError("INVALID_PHASE0_COMMAND_ARGV")
        if self.expected_test_count is not None and (
            type(self.expected_test_count) is not int
            or self.expected_test_count < 1
        ):
            raise ValueError("INVALID_PHASE0_EXPECTED_TEST_COUNT")


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    receipt_path: Path
    report_path: Path
    pack_path: Path
    receipt_sha256: str
    command_output_paths: tuple[str, ...]


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\r\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _project_root_variants(root: Path) -> tuple[str, ...]:
    variants = {str(root), root.as_posix()}
    posix = root.as_posix()
    wsl_match = re.fullmatch(r"/mnt/([a-zA-Z])/(.+)", posix)
    if wsl_match is not None:
        drive = wsl_match.group(1)
        tail = wsl_match.group(2)
        variants.update(
            {
                f"{drive.upper()}:\\{tail.replace('/', chr(92))}",
                f"{drive.lower()}:\\{tail.replace('/', chr(92))}",
                f"{drive.upper()}:/{tail}",
                f"{drive.lower()}:/{tail}",
            }
        )
    windows_match = re.fullmatch(r"([a-zA-Z]):[\\/](.+)", str(root))
    if windows_match is not None:
        drive = windows_match.group(1)
        tail = windows_match.group(2).replace("\\", "/")
        variants.update(
            {
                f"{drive.upper()}:\\{tail.replace('/', chr(92))}",
                f"{drive.lower()}:\\{tail.replace('/', chr(92))}",
                f"{drive.upper()}:/{tail}",
                f"{drive.lower()}:/{tail}",
            }
        )
    return tuple(sorted(filter(None, variants), key=lambda value: (-len(value), value)))


def _sanitize_output(payload: bytes, *, root: Path) -> tuple[bytes, int]:
    text = payload.decode("utf-8", errors="replace")
    replacements = 0
    for variant in _project_root_variants(root):
        count = text.count(variant)
        if count:
            text = text.replace(variant, PROJECT_ROOT_PLACEHOLDER)
            replacements += count
    text, windows_home_count = _WINDOWS_USER_HOME.subn(
        USER_HOME_PLACEHOLDER,
        text,
    )
    text, wsl_home_count = _WSL_USER_HOME.subn(
        USER_HOME_PLACEHOLDER,
        text,
    )
    replacements += windows_home_count + wsl_home_count
    sanitized = text.encode("utf-8")
    _require_no_user_path(sanitized)
    return sanitized, replacements


def _sanitize_evidence_text(value: str, *, root: Path) -> str:
    sanitized, _ = _sanitize_output(value.encode("utf-8"), root=root)
    return sanitized.decode("utf-8")


def _require_no_user_path(payload: bytes) -> None:
    text = payload.decode("utf-8", errors="replace")
    if _WINDOWS_USER_PATH.search(text) or _WSL_USER_PATH.search(text):
        raise ValueError("PHASE0_UNSANITIZED_USER_PATH")


def _output_commitment(commands: Sequence[dict], *, raw: bool) -> str:
    if raw:
        fields = (
            {
                "name": item["name"],
                "stderr_sha256": item["raw_stderr_sha256"],
                "stdout_sha256": item["raw_stdout_sha256"],
            }
            for item in commands
        )
    else:
        fields = (
            {
                "name": item["name"],
                "stderr_sanitization_replacements": item[
                    "stderr_sanitization_replacements"
                ],
                "stderr_sha256": item["stderr_sha256"],
                "stdout_sanitization_replacements": item[
                    "stdout_sanitization_replacements"
                ],
                "stdout_sha256": item["stdout_sha256"],
            }
            for item in commands
        )
    return _sha256(_json_bytes(list(fields)))


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _require_commit(root: Path, commit: str, code: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(code)
    resolved = _git(root, "rev-parse", f"{commit}^{{commit}}")
    if resolved != commit:
        raise ValueError(code)


def _dirty_paths(root: Path) -> set[str]:
    paths = set(
        filter(
            None,
            _git(root, "diff", "--name-only").splitlines(),
        )
    )
    paths.update(
        filter(
            None,
            _git(root, "diff", "--cached", "--name-only").splitlines(),
        )
    )
    paths.update(
        filter(
            None,
            _git(root, "ls-files", "--others", "--exclude-standard").splitlines(),
        )
    )
    return paths


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _build_pack(
    *,
    root: Path,
    pack_path: Path,
    payload_paths: Sequence[Path],
) -> None:
    payloads = {
        path.as_posix(): (root / path).read_bytes()
        for path in payload_paths
    }
    member_hashes = {
        name: _sha256(payload) for name, payload in sorted(payloads.items())
    }
    manifest_bytes = _json_bytes(
        {
            "members": member_hashes,
            "schema_version": "BTC_DAILY_RANGE_PHASE0_PACK_MANIFEST_V1",
        }
    )
    sums = {
        **member_hashes,
        PACK_MANIFEST: _sha256(manifest_bytes),
    }
    sums_bytes = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(sums.items())
    ).encode("utf-8")
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        pack_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, payload in sorted(payloads.items()):
            archive.writestr(_zip_info(name), payload)
        archive.writestr(_zip_info(PACK_MANIFEST), manifest_bytes)
        archive.writestr(_zip_info(PACK_SHA256SUMS), sums_bytes)


def _verify_pack(
    *,
    root: Path,
    pack_path: Path,
    payload_paths: Sequence[str],
) -> None:
    if not pack_path.is_file():
        raise ValueError("PHASE0_PACK_MISSING")
    expected_payloads = set(payload_paths)
    expected_names = expected_payloads | {PACK_MANIFEST, PACK_SHA256SUMS}
    try:
        with zipfile.ZipFile(pack_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError("PHASE0_PACK_DUPLICATE_MEMBER")
            for name in names:
                pure = PurePosixPath(name)
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or "\\" in name
                ):
                    raise ValueError("PHASE0_PACK_UNSAFE_MEMBER")
            if set(names) != expected_names:
                raise ValueError("PHASE0_PACK_MEMBER_SET_MISMATCH")
            if archive.testzip() is not None:
                raise ValueError("PHASE0_PACK_CRC_FAILURE")
            archived = {name: archive.read(name) for name in names}
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        raise ValueError("INVALID_PHASE0_PACK") from error

    for name in expected_payloads:
        path = root / name
        if not path.is_file() or archived[name] != path.read_bytes():
            raise ValueError("PHASE0_PACK_PAYLOAD_MISMATCH")
    try:
        manifest = json.loads(archived[PACK_MANIFEST].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("INVALID_PHASE0_PACK_MANIFEST") from error
    if archived[PACK_MANIFEST] != _json_bytes(manifest):
        raise ValueError("NONCANONICAL_PHASE0_PACK_MANIFEST")
    expected_member_hashes = {
        name: _sha256(archived[name]) for name in sorted(expected_payloads)
    }
    if manifest != {
        "members": expected_member_hashes,
        "schema_version": "BTC_DAILY_RANGE_PHASE0_PACK_MANIFEST_V1",
    }:
        raise ValueError("PHASE0_PACK_MANIFEST_MISMATCH")
    expected_sums = {
        **expected_member_hashes,
        PACK_MANIFEST: _sha256(archived[PACK_MANIFEST]),
    }
    expected_sums_bytes = "".join(
        f"{digest}  {name}\n"
        for name, digest in sorted(expected_sums.items())
    ).encode("utf-8")
    if archived[PACK_SHA256SUMS] != expected_sums_bytes:
        raise ValueError("PHASE0_PACK_SHA256SUMS_MISMATCH")


def generate_phase0_evidence(
    *,
    root: Path,
    source_commit: str,
    harness_commit: str,
    command_specs: Sequence[CommandSpec],
    historical_paths: Sequence[str] = HISTORICAL_PATHS,
    test_only: bool = False,
    test_only_allow_custom_commands: bool = False,
) -> EvidenceResult:
    root = root.resolve()
    if _dirty_paths(root):
        raise ValueError("PHASE0_EVIDENCE_DIRTY_START")
    branch = _git(root, "branch", "--show-current")
    if not branch:
        raise ValueError("PHASE0_EVIDENCE_DETACHED_HEAD")
    _require_commit(root, source_commit, "INVALID_PHASE0_SOURCE_COMMIT")
    _require_commit(root, harness_commit, "INVALID_PHASE0_HARNESS_COMMIT")
    if _git(root, "rev-parse", "HEAD") != harness_commit:
        raise ValueError("PHASE0_HARNESS_COMMIT_STALE")
    if subprocess.run(
        ("git", "-C", str(root), "merge-base", "--is-ancestor", source_commit, harness_commit),
        check=False,
    ).returncode != 0:
        raise ValueError("PHASE0_SOURCE_NOT_ANCESTOR")
    command_specs = tuple(command_specs)
    historical_paths = tuple(historical_paths)
    if tuple(spec.name for spec in command_specs) != REQUIRED_COMMANDS:
        raise ValueError("INCOMPLETE_PHASE0_COMMAND_SET")
    if not command_specs:
        raise ValueError("INCOMPLETE_PHASE0_COMMAND_SET")
    python_executable = command_specs[0].argv[0]
    if test_only:
        contract_profile = "TEST_ONLY"
        if historical_paths != TEST_ONLY_HISTORICAL_PATHS:
            raise ValueError("INVALID_TEST_HISTORICAL_CONTRACT")
        expected_specs = test_only_command_specs(python_executable)
        if (
            not test_only_allow_custom_commands
            and command_specs != expected_specs
        ):
            raise ValueError("INVALID_TEST_COMMAND_CONTRACT")
    else:
        contract_profile = "PRODUCTION"
        if historical_paths != HISTORICAL_PATHS:
            raise ValueError("PHASE0_HISTORICAL_CONTRACT_WEAKENED")
        expected_specs = default_command_specs(
            python_executable,
            source_commit,
        )
        if command_specs != expected_specs:
            raise ValueError("PHASE0_COMMAND_CONTRACT_WEAKENED")

    output_directory = root / OUTPUT_DIRECTORY
    output_directory.mkdir(parents=True, exist_ok=True)
    command_receipts = []
    output_paths: list[str] = []
    receipt_started = datetime.now(timezone.utc)
    monotonic_started = time.monotonic_ns()
    for index, spec in enumerate(command_specs, start=1):
        started = datetime.now(timezone.utc)
        command_started = time.monotonic_ns()
        result = subprocess.run(spec.argv, cwd=root, capture_output=True)
        elapsed_ms = (time.monotonic_ns() - command_started) // 1_000_000
        ended = datetime.now(timezone.utc)
        stdout_path = OUTPUT_DIRECTORY / f"{index:02d}-{spec.name}.stdout.txt"
        stderr_path = OUTPUT_DIRECTORY / f"{index:02d}-{spec.name}.stderr.txt"
        raw_stdout_bytes = bytes(result.stdout)
        raw_stderr_bytes = bytes(result.stderr)
        stdout_bytes, stdout_replacements = _sanitize_output(
            raw_stdout_bytes,
            root=root,
        )
        stderr_bytes, stderr_replacements = _sanitize_output(
            raw_stderr_bytes,
            root=root,
        )
        (root / stdout_path).write_bytes(stdout_bytes)
        (root / stderr_path).write_bytes(stderr_bytes)
        output_paths.extend((stdout_path.as_posix(), stderr_path.as_posix()))
        unexpected_dirt = _dirty_paths(root) - set(output_paths)
        if unexpected_dirt:
            raise ValueError(
                "PHASE0_COMMAND_DIRTIED_REPOSITORY:"
                + ",".join(sorted(unexpected_dirt))
            )
        combined = (raw_stdout_bytes + b"\n" + raw_stderr_bytes).decode(
            "utf-8", errors="replace"
        )
        matches = _TEST_COUNT.findall(combined)
        test_count = int(matches[-1]) if matches else None
        if result.returncode != 0:
            raise ValueError(f"PHASE0_COMMAND_FAILED:{spec.name}:{result.returncode}")
        if spec.expected_test_count is not None and test_count != spec.expected_test_count:
            raise ValueError(f"PHASE0_TEST_COUNT_MISMATCH:{spec.name}")
        command_receipts.append(
            {
                "argv": [
                    _sanitize_evidence_text(value, root=root)
                    for value in spec.argv
                ],
                "command": _sanitize_evidence_text(
                    subprocess.list2cmdline(spec.argv),
                    root=root,
                ),
                "elapsed_ms": elapsed_ms,
                "ended_at_utc": ended.isoformat().replace("+00:00", "Z"),
                "exit_code": result.returncode,
                "name": spec.name,
                "expected_test_count": spec.expected_test_count,
                "raw_stderr_sha256": _sha256(raw_stderr_bytes),
                "raw_stdout_sha256": _sha256(raw_stdout_bytes),
                "started_at_utc": started.isoformat().replace("+00:00", "Z"),
                "stderr_path": stderr_path.as_posix(),
                "stderr_sanitization_replacements": stderr_replacements,
                "stderr_sha256": _sha256(stderr_bytes),
                "stdout_path": stdout_path.as_posix(),
                "stdout_sanitization_replacements": stdout_replacements,
                "stdout_sha256": _sha256(stdout_bytes),
                "test_count": test_count,
            }
        )

    historical_hashes = {}
    for relative in historical_paths:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"PHASE0_HISTORICAL_ARTIFACT_MISSING:{relative}")
        historical_hashes[relative] = _sha256(path.read_bytes())
    receipt_ended = datetime.now(timezone.utc)
    replacement_count = sum(
        item["stdout_sanitization_replacements"]
        + item["stderr_sanitization_replacements"]
        for item in command_receipts
    )
    raw_output_commitment = _output_commitment(command_receipts, raw=True)
    stored_output_commitment = _output_commitment(command_receipts, raw=False)
    receipt = {
        "branch": branch,
        "clean_state_before": True,
        "commands": command_receipts,
        "contract_profile": contract_profile,
        "elapsed_ms": (time.monotonic_ns() - monotonic_started) // 1_000_000,
        "ended_at_utc": receipt_ended.isoformat().replace("+00:00", "Z"),
        "harness_commit": harness_commit,
        "historical_sha256": historical_hashes,
        "network_mode": "OFFLINE_COMMAND_ALLOWLIST",
        "output_sanitization_policy": OUTPUT_SANITIZATION_POLICY,
        "output_sanitization_replacement_count": replacement_count,
        "provider_network_observation": "NOT_PERFORMED",
        "public_provider_requests": "NOT_OBSERVED",
        "python_executable": _sanitize_evidence_text(
            python_executable,
            root=root,
        ),
        "registry_executions": "NOT_RUN_BY_EVIDENCE_HARNESS",
        "raw_output_commitment_sha256": raw_output_commitment,
        "schema_version": "BTC_DAILY_RANGE_PHASE0_COMMAND_RECEIPT_V2",
        "source_commit": source_commit,
        "started_at_utc": receipt_started.isoformat().replace("+00:00", "Z"),
        "stored_output_commitment_sha256": stored_output_commitment,
        "task14_runs": "NOT_RUN_BY_EVIDENCE_HARNESS",
        "trading_approval": False,
    }
    receipt_bytes = _json_bytes(receipt)
    _require_no_user_path(receipt_bytes)
    receipt_path = root / RECEIPT_PATH
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)
    receipt_sha256 = _sha256(receipt_bytes)
    report = {
        "branch": branch,
        "command_count": len(command_receipts),
        "focused_test_count": command_receipts[0]["test_count"],
        "full_offline_test_count": command_receipts[1]["test_count"],
        "gate": "BTC_DAILY_RANGE_WINDOWS_V1_PHASE0_PASS",
        "harness_commit": harness_commit,
        "historical_sha256": historical_hashes,
        "network_mode": "OFFLINE_COMMAND_ALLOWLIST",
        "output_sanitization_policy": OUTPUT_SANITIZATION_POLICY,
        "output_sanitization_replacement_count": replacement_count,
        "provider_network_observation": "NOT_PERFORMED",
        "public_provider_requests": "NOT_OBSERVED",
        "receipt_path": RECEIPT_PATH.as_posix(),
        "receipt_sha256": receipt_sha256,
        "raw_output_commitment_sha256": raw_output_commitment,
        "registry_executions": "NOT_RUN_BY_EVIDENCE_HARNESS",
        "schema_version": "BTC_DAILY_RANGE_PHASE0_ACCEPTED_BASELINE_V2",
        "source_commit": source_commit,
        "status": "PASS",
        "stored_output_commitment_sha256": stored_output_commitment,
        "task14_runs": "NOT_RUN_BY_EVIDENCE_HARNESS",
        "trading_approval": False,
    }
    report_path = root / REPORT_PATH
    report_bytes = _json_bytes(report)
    _require_no_user_path(report_bytes)
    report_path.write_bytes(report_bytes)
    pack_path = root / PACK_PATH
    _build_pack(
        root=root,
        pack_path=pack_path,
        payload_paths=(
            RECEIPT_PATH,
            REPORT_PATH,
            *(Path(path) for path in output_paths),
        ),
    )
    verify_phase0_report(report_path, root=root, verify_repository=False)
    return EvidenceResult(
        receipt_path=receipt_path,
        report_path=report_path,
        pack_path=pack_path,
        receipt_sha256=receipt_sha256,
        command_output_paths=tuple(output_paths),
    )


def verify_phase0_report(
    report_path: Path,
    *,
    root: Path,
    verify_repository: bool = True,
) -> dict:
    root = root.resolve()
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes.decode("utf-8"))
    if report_bytes != _json_bytes(report):
        raise ValueError("NONCANONICAL_PHASE0_REPORT")
    _require_no_user_path(report_bytes)
    receipt_path = root / report.get("receipt_path", "")
    if not receipt_path.is_file():
        raise ValueError("PHASE0_RECEIPT_MISSING")
    receipt_bytes = receipt_path.read_bytes()
    if report.get("receipt_sha256") != _sha256(receipt_bytes):
        raise ValueError("RECEIPT_SHA256_MISMATCH")
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    if receipt_bytes != _json_bytes(receipt):
        raise ValueError("NONCANONICAL_PHASE0_RECEIPT")
    _require_no_user_path(receipt_bytes)
    profile = receipt.get("contract_profile")
    python_executable = receipt.get("python_executable")
    source_commit = receipt.get("source_commit")
    if type(python_executable) is not str or not python_executable:
        raise ValueError("INVALID_PHASE0_PYTHON_EXECUTABLE")
    if profile == "PRODUCTION":
        required_historical = HISTORICAL_PATHS
        expected_specs = default_command_specs(
            python_executable,
            source_commit,
        )
    elif profile == "TEST_ONLY":
        required_historical = TEST_ONLY_HISTORICAL_PATHS
        expected_specs = test_only_command_specs(python_executable)
    else:
        raise ValueError("INVALID_PHASE0_CONTRACT_PROFILE")
    if (
        receipt.get("schema_version") != "BTC_DAILY_RANGE_PHASE0_COMMAND_RECEIPT_V2"
        or receipt.get("clean_state_before") is not True
        or receipt.get("network_mode") != "OFFLINE_COMMAND_ALLOWLIST"
        or receipt.get("output_sanitization_policy")
        != OUTPUT_SANITIZATION_POLICY
        or receipt.get("provider_network_observation") != "NOT_PERFORMED"
        or receipt.get("public_provider_requests") != "NOT_OBSERVED"
        or receipt.get("task14_runs") != "NOT_RUN_BY_EVIDENCE_HARNESS"
        or receipt.get("registry_executions")
        != "NOT_RUN_BY_EVIDENCE_HARNESS"
        or receipt.get("trading_approval") is not False
    ):
        raise ValueError("INVALID_PHASE0_RECEIPT_CONTRACT")
    commands = receipt.get("commands")
    if type(commands) is not list or len(commands) != len(expected_specs):
        raise ValueError("INCOMPLETE_PHASE0_COMMAND_SET")
    command_keys = {
        "argv",
        "command",
        "elapsed_ms",
        "ended_at_utc",
        "exit_code",
        "expected_test_count",
        "name",
        "raw_stderr_sha256",
        "raw_stdout_sha256",
        "started_at_utc",
        "stderr_path",
        "stderr_sanitization_replacements",
        "stderr_sha256",
        "stdout_path",
        "stdout_sanitization_replacements",
        "stdout_sha256",
        "test_count",
    }
    for index, (item, spec) in enumerate(
        zip(commands, expected_specs, strict=True),
        start=1,
    ):
        expected_prefix = f"{index:02d}-{spec.name}"
        if (
            type(item) is not dict
            or set(item) != command_keys
            or item.get("name") != spec.name
            or item.get("argv") != list(spec.argv)
            or item.get("command") != subprocess.list2cmdline(spec.argv)
            or item.get("expected_test_count") != spec.expected_test_count
            or item.get("stdout_path")
            != (OUTPUT_DIRECTORY / f"{expected_prefix}.stdout.txt").as_posix()
            or item.get("stderr_path")
            != (OUTPUT_DIRECTORY / f"{expected_prefix}.stderr.txt").as_posix()
        ):
            raise ValueError("PHASE0_COMMAND_IDENTITY_MISMATCH")
        if item.get("exit_code") != 0:
            raise ValueError("FAILED_PHASE0_COMMAND_RECEIPT")
        for stream in ("stdout", "stderr"):
            path = root / item.get(f"{stream}_path", "")
            if not path.is_file():
                raise ValueError("COMMAND_OUTPUT_SHA256_MISMATCH")
            output_bytes = path.read_bytes()
            _require_no_user_path(output_bytes)
            if item.get(f"{stream}_sha256") != _sha256(output_bytes):
                raise ValueError("COMMAND_OUTPUT_SHA256_MISMATCH")
            raw_sha256 = item.get(f"raw_{stream}_sha256")
            replacements = item.get(f"{stream}_sanitization_replacements")
            if (
                type(raw_sha256) is not str
                or _HEX_64.fullmatch(raw_sha256) is None
                or type(replacements) is not int
                or replacements < 0
            ):
                raise ValueError("INVALID_PHASE0_OUTPUT_SANITIZATION")
        combined = (
            (root / item["stdout_path"]).read_bytes()
            + b"\n"
            + (root / item["stderr_path"]).read_bytes()
        ).decode("utf-8", errors="replace")
        matches = _TEST_COUNT.findall(combined)
        observed_count = int(matches[-1]) if matches else None
        if item.get("test_count") != observed_count:
            raise ValueError("COMMAND_TEST_COUNT_MISMATCH")
        if (
            spec.expected_test_count is not None
            and observed_count != spec.expected_test_count
        ):
            raise ValueError("PHASE0_EXPECTED_TEST_COUNT_MISMATCH")
    replacement_count = sum(
        item["stdout_sanitization_replacements"]
        + item["stderr_sanitization_replacements"]
        for item in commands
    )
    if receipt.get("output_sanitization_replacement_count") != replacement_count:
        raise ValueError("PHASE0_SANITIZATION_COUNTER_MISMATCH")
    raw_output_commitment = _output_commitment(commands, raw=True)
    if receipt.get("raw_output_commitment_sha256") != raw_output_commitment:
        raise ValueError("PHASE0_RAW_OUTPUT_COMMITMENT_MISMATCH")
    stored_output_commitment = _output_commitment(commands, raw=False)
    if receipt.get("stored_output_commitment_sha256") != stored_output_commitment:
        raise ValueError("PHASE0_STORED_OUTPUT_COMMITMENT_MISMATCH")
    historical = receipt.get("historical_sha256")
    if type(historical) is not dict or set(historical) != set(
        required_historical
    ):
        raise ValueError("PHASE0_HISTORICAL_CONTRACT_MISMATCH")
    for relative, expected_hash in historical.items():
        path = root / relative
        if not path.is_file() or not _HEX_64.fullmatch(expected_hash) or _sha256(path.read_bytes()) != expected_hash:
            raise ValueError("HISTORICAL_ARTIFACT_SHA256_MISMATCH")
    expected_report = {
        "branch": receipt["branch"],
        "command_count": len(commands),
        "focused_test_count": commands[0]["test_count"],
        "full_offline_test_count": commands[1]["test_count"],
        "gate": "BTC_DAILY_RANGE_WINDOWS_V1_PHASE0_PASS",
        "harness_commit": receipt["harness_commit"],
        "historical_sha256": historical,
        "network_mode": "OFFLINE_COMMAND_ALLOWLIST",
        "output_sanitization_policy": OUTPUT_SANITIZATION_POLICY,
        "output_sanitization_replacement_count": replacement_count,
        "provider_network_observation": "NOT_PERFORMED",
        "public_provider_requests": "NOT_OBSERVED",
        "receipt_path": RECEIPT_PATH.as_posix(),
        "receipt_sha256": _sha256(receipt_bytes),
        "raw_output_commitment_sha256": raw_output_commitment,
        "registry_executions": "NOT_RUN_BY_EVIDENCE_HARNESS",
        "schema_version": "BTC_DAILY_RANGE_PHASE0_ACCEPTED_BASELINE_V2",
        "source_commit": receipt["source_commit"],
        "status": "PASS",
        "stored_output_commitment_sha256": stored_output_commitment,
        "task14_runs": "NOT_RUN_BY_EVIDENCE_HARNESS",
        "trading_approval": False,
    }
    if report != expected_report:
        raise ValueError("PHASE0_REPORT_RECEIPT_MISMATCH")
    payload_paths = (
        RECEIPT_PATH.as_posix(),
        REPORT_PATH.as_posix(),
        *(item["stdout_path"] for item in commands),
        *(item["stderr_path"] for item in commands),
    )
    _verify_pack(
        root=root,
        pack_path=root / PACK_PATH,
        payload_paths=payload_paths,
    )
    if verify_repository:
        if _dirty_paths(root):
            raise ValueError("PHASE0_EVIDENCE_DIRTY")
        if _git(root, "branch", "--show-current") != receipt["branch"]:
            raise ValueError("PHASE0_BRANCH_MISMATCH")
        harness_commit = receipt["harness_commit"]
        source_commit = receipt["source_commit"]
        _require_commit(root, harness_commit, "INVALID_PHASE0_HARNESS_COMMIT")
        _require_commit(root, source_commit, "INVALID_PHASE0_SOURCE_COMMIT")
        if subprocess.run(
            (
                "git",
                "-C",
                str(root),
                "merge-base",
                "--is-ancestor",
                source_commit,
                harness_commit,
            ),
            check=False,
        ).returncode != 0:
            raise ValueError("PHASE0_SOURCE_NOT_ANCESTOR")
        if subprocess.run(
            ("git", "-C", str(root), "merge-base", "--is-ancestor", harness_commit, "HEAD"),
            check=False,
        ).returncode != 0:
            raise ValueError("PHASE0_HARNESS_COMMIT_STALE")
        allowed = {
            RECEIPT_PATH.as_posix(),
            REPORT_PATH.as_posix(),
            PACK_PATH.as_posix(),
            *(item["stdout_path"] for item in commands),
            *(item["stderr_path"] for item in commands),
        }
        changed = set(filter(None, _git(root, "diff", "--name-only", f"{harness_commit}..HEAD").splitlines()))
        if not changed <= allowed:
            raise ValueError("PHASE0_EVIDENCE_STALE")
        if profile == "PRODUCTION":
            audit_scope(root, "1197109", source_commit)
    return report


def test_only_command_specs(python_executable: str) -> tuple[CommandSpec, ...]:
    return (
        CommandSpec(
            "focused_tests",
            (python_executable, "-c", "print('Ran 7 tests')"),
            expected_test_count=7,
        ),
        CommandSpec(
            "full_offline_tests",
            (python_executable, "-c", "print('Ran 11 tests')"),
            expected_test_count=11,
        ),
        CommandSpec(
            "compileall",
            (python_executable, "-c", "print('compile ok')"),
        ),
        CommandSpec(
            "pip_check",
            (python_executable, "-c", "print('pip check ok')"),
        ),
        CommandSpec(
            "scope_audit",
            (python_executable, "-c", "print('scope audit ok')"),
        ),
    )


def default_command_specs(python_executable: str, source_commit: str) -> tuple[CommandSpec, ...]:
    focused = (
        "tests.test_market_rollover",
        "tests.test_phase0_rollover_recovery",
        "tests.test_runtime_persistence",
        "tests.test_runtime_restart_identity",
        "tests.test_process_runtime_integration",
        "tests.test_c2_recovery_hardening",
    )
    return (
        CommandSpec(
            "focused_tests",
            (python_executable, "-m", "unittest", "-v", *focused),
            expected_test_count=101,
        ),
        CommandSpec(
            "full_offline_tests",
            (python_executable, "-m", "unittest", "discover", "-s", "tests", "-v"),
            expected_test_count=647,
        ),
        CommandSpec("compileall", (python_executable, "-m", "compileall", "-q", "src", "tests", "tools", "run_backend.py")),
        CommandSpec("pip_check", (python_executable, "-m", "pip", "check")),
        CommandSpec("scope_audit", (python_executable, "tools/phase0_evidence.py", "audit-scope", "--baseline", "1197109", "--source-commit", source_commit)),
    )


def audit_scope(root: Path, baseline: str, source_commit: str) -> dict:
    changed = tuple(filter(None, _git(root, "diff", "--name-only", f"{baseline}..{source_commit}").splitlines()))
    forbidden_exact = set(HISTORICAL_PATHS) | {
        "docs/C1_AUTONOMOUS_HANDOFF.md",
        "docs/C2_C3_AUTONOMOUS_GOAL.md",
        "docs/C2_C3_DECISION_LOG.md",
        "docs/C2_C3_GOAL_PROGRESS.md",
    }
    forbidden = tuple(path for path in changed if path in forbidden_exact or path.startswith("migrations/") or path.startswith("registry/"))
    diff = _git(root, "diff", "--unified=0", f"{baseline}..{source_commit}", "--", "src").lower()
    forbidden_tokens = tuple(token for token in ("place_order", "private_key", "sign_order", "wallet_address") if token in diff)
    if forbidden or forbidden_tokens:
        raise ValueError("PHASE0_SCOPE_AUDIT_FAILED")
    return {"changed_files": list(changed), "forbidden_paths": [], "forbidden_tokens": [], "status": "PASS"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--python", required=True)
    generate.add_argument("--source-commit", required=True)
    generate.add_argument("--harness-commit", required=True)
    audit = subparsers.add_parser("audit-scope")
    audit.add_argument("--baseline", required=True)
    audit.add_argument("--source-commit", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--report", default=REPORT_PATH.as_posix())
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "generate":
        result = generate_phase0_evidence(
            root=root,
            source_commit=args.source_commit,
            harness_commit=args.harness_commit,
            command_specs=default_command_specs(args.python, args.source_commit),
        )
        print(json.dumps({"pack": str(result.pack_path), "receipt_sha256": result.receipt_sha256, "report": str(result.report_path)}, sort_keys=True))
        return 0
    if args.command == "audit-scope":
        print(json.dumps(audit_scope(root, args.baseline, args.source_commit), sort_keys=True))
        return 0
    verify_phase0_report(root / args.report, root=root)
    print("BTC_DAILY_RANGE_WINDOWS_V1_PHASE0_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
