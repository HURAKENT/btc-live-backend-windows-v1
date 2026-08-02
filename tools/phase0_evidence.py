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
from pathlib import Path
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
_TEST_COUNT = re.compile(r"Ran (\d+) tests?")
_HEX_64 = re.compile(r"[0-9a-f]{64}")


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
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def generate_phase0_evidence(
    *,
    root: Path,
    source_commit: str,
    harness_commit: str,
    command_specs: Sequence[CommandSpec],
    historical_paths: Sequence[str] = HISTORICAL_PATHS,
) -> EvidenceResult:
    root = root.resolve()
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
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
    if tuple(spec.name for spec in command_specs) != REQUIRED_COMMANDS:
        raise ValueError("INCOMPLETE_PHASE0_COMMAND_SET")

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
        stdout_bytes = bytes(result.stdout)
        stderr_bytes = bytes(result.stderr)
        (root / stdout_path).write_bytes(stdout_bytes)
        (root / stderr_path).write_bytes(stderr_bytes)
        output_paths.extend((stdout_path.as_posix(), stderr_path.as_posix()))
        combined = (stdout_bytes + b"\n" + stderr_bytes).decode(
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
                "argv": list(spec.argv),
                "command": subprocess.list2cmdline(spec.argv),
                "elapsed_ms": elapsed_ms,
                "ended_at_utc": ended.isoformat().replace("+00:00", "Z"),
                "exit_code": result.returncode,
                "name": spec.name,
                "started_at_utc": started.isoformat().replace("+00:00", "Z"),
                "stderr_path": stderr_path.as_posix(),
                "stderr_sha256": _sha256(stderr_bytes),
                "stdout_path": stdout_path.as_posix(),
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
    receipt = {
        "branch": branch,
        "clean_state_before": True,
        "commands": command_receipts,
        "elapsed_ms": (time.monotonic_ns() - monotonic_started) // 1_000_000,
        "ended_at_utc": receipt_ended.isoformat().replace("+00:00", "Z"),
        "harness_commit": harness_commit,
        "historical_sha256": historical_hashes,
        "network_mode": "OFFLINE_LOOPBACK_ONLY",
        "public_provider_requests": 0,
        "registry_executions": 0,
        "schema_version": "BTC_DAILY_RANGE_PHASE0_COMMAND_RECEIPT_V1",
        "source_commit": source_commit,
        "started_at_utc": receipt_started.isoformat().replace("+00:00", "Z"),
        "task14_runs": 0,
        "trading_approval": False,
    }
    receipt_bytes = _json_bytes(receipt)
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
        "network_mode": "OFFLINE_LOOPBACK_ONLY",
        "public_provider_requests": 0,
        "receipt_path": RECEIPT_PATH.as_posix(),
        "receipt_sha256": receipt_sha256,
        "registry_executions": 0,
        "schema_version": "BTC_DAILY_RANGE_PHASE0_ACCEPTED_BASELINE_V1",
        "source_commit": source_commit,
        "status": "PASS",
        "task14_runs": 0,
        "trading_approval": False,
    }
    report_path = root / REPORT_PATH
    report_path.write_bytes(_json_bytes(report))
    verify_phase0_report(report_path, root=root, verify_repository=False)
    pack_path = root / PACK_PATH
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    members = (RECEIPT_PATH, REPORT_PATH, *(Path(path) for path in output_paths))
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in members:
            info = zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / relative).read_bytes())
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
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "PASS" or report.get("trading_approval") is not False:
        raise ValueError("INVALID_PHASE0_PASS_REPORT")
    receipt_path = root / report.get("receipt_path", "")
    if not receipt_path.is_file():
        raise ValueError("PHASE0_RECEIPT_MISSING")
    receipt_bytes = receipt_path.read_bytes()
    if report.get("receipt_sha256") != _sha256(receipt_bytes):
        raise ValueError("RECEIPT_SHA256_MISMATCH")
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    if receipt_bytes != _json_bytes(receipt):
        raise ValueError("NONCANONICAL_PHASE0_RECEIPT")
    if (
        receipt.get("schema_version") != "BTC_DAILY_RANGE_PHASE0_COMMAND_RECEIPT_V1"
        or receipt.get("clean_state_before") is not True
        or receipt.get("network_mode") != "OFFLINE_LOOPBACK_ONLY"
        or receipt.get("public_provider_requests") != 0
        or receipt.get("task14_runs") != 0
        or receipt.get("registry_executions") != 0
        or receipt.get("trading_approval") is not False
    ):
        raise ValueError("INVALID_PHASE0_RECEIPT_CONTRACT")
    commands = receipt.get("commands")
    if type(commands) is not list or tuple(item.get("name") for item in commands) != REQUIRED_COMMANDS:
        raise ValueError("INCOMPLETE_PHASE0_COMMAND_SET")
    for item in commands:
        if item.get("exit_code") != 0:
            raise ValueError("FAILED_PHASE0_COMMAND_RECEIPT")
        for stream in ("stdout", "stderr"):
            path = root / item.get(f"{stream}_path", "")
            if not path.is_file() or item.get(f"{stream}_sha256") != _sha256(path.read_bytes()):
                raise ValueError("COMMAND_OUTPUT_SHA256_MISMATCH")
        combined = (
            (root / item["stdout_path"]).read_bytes()
            + b"\n"
            + (root / item["stderr_path"]).read_bytes()
        ).decode("utf-8", errors="replace")
        matches = _TEST_COUNT.findall(combined)
        observed_count = int(matches[-1]) if matches else None
        if item.get("test_count") != observed_count:
            raise ValueError("COMMAND_TEST_COUNT_MISMATCH")
    for relative, expected_hash in receipt.get("historical_sha256", {}).items():
        path = root / relative
        if not path.is_file() or not _HEX_64.fullmatch(expected_hash) or _sha256(path.read_bytes()) != expected_hash:
            raise ValueError("HISTORICAL_ARTIFACT_SHA256_MISMATCH")
    if verify_repository:
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("PHASE0_EVIDENCE_DIRTY")
        if _git(root, "branch", "--show-current") != receipt["branch"]:
            raise ValueError("PHASE0_BRANCH_MISMATCH")
        harness_commit = receipt["harness_commit"]
        _require_commit(root, harness_commit, "INVALID_PHASE0_HARNESS_COMMIT")
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
    return report


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
        CommandSpec("focused_tests", (python_executable, "-m", "unittest", "-v", *focused)),
        CommandSpec("full_offline_tests", (python_executable, "-m", "unittest", "discover", "-s", "tests", "-v")),
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
