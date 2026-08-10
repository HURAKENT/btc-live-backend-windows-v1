from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


if __package__ in (None, ""):
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    _PROJECT_ROOT_TEXT = str(_PROJECT_ROOT)
    if _PROJECT_ROOT_TEXT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT_TEXT)

from src.data_completion_source_receipt import (
    build_source_pack_receipt,
    validate_source_pack_receipt,
    write_source_pack_receipt,
)
from src.historical_source_pack_import import (
    AUTHORITATIVE_SOURCE_PACKS,
    import_source_pack,
    verify_authoritative_source_packs,
)
from src.storage import SqliteStore


NETWORK_MODE = "OFFLINE_LOCAL_SOURCE_IMPORT"
_HISTORICAL_ACCEPTANCE = (
    ("C1_ACCEPTANCE_PACK.zip", "9f6eaa9015f0698313c950571ae5f76be3f9c9d4327a461c4d504e8594b81a4f", 26026),
    ("C2_C3_ACCEPTANCE_PACK.zip", "07c0119585ead440036734926a3ad7bd86ff63002cc2d4a0e96df1a6c47062c9", 13471),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    if _sha256_file(source) != expected_sha256:
        raise RuntimeError("SOURCE_PACK_ARCHIVE_HASH_MISMATCH")
    if destination.exists():
        if not destination.is_file() or _sha256_file(destination) != expected_sha256:
            raise RuntimeError("SOURCE_PACK_ARCHIVE_CONFLICT")
        return
    temporary = destination.with_name(destination.name + ".copying")
    if temporary.exists():
        raise RuntimeError("SOURCE_PACK_INCOMPLETE_COPY_PRESENT")
    shutil.copyfile(source, temporary)
    if _sha256_file(temporary) != expected_sha256:
        raise RuntimeError("SOURCE_PACK_ARCHIVE_COPY_MISMATCH")
    os.replace(temporary, destination)


def _validate_paths(project_root: Path, data_root: Path, receipt_output: Path) -> None:
    project_root = project_root.resolve()
    data_root = data_root.resolve()
    receipt_output = receipt_output.resolve()
    if data_root == project_root or project_root in data_root.parents:
        raise RuntimeError("DATA_COMPLETION_ROOT_MUST_BE_OUTSIDE_REPOSITORY")
    if receipt_output == project_root or project_root in receipt_output.parents:
        raise RuntimeError("RECEIPT_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")
    if receipt_output != data_root and data_root not in receipt_output.parents:
        raise RuntimeError("RECEIPT_OUTPUT_MUST_BE_UNDER_DATA_ROOT")


def _verify_source_commit(project_root: Path, source_commit: str) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip() != source_commit:
        raise RuntimeError("SOURCE_COMMIT_HEAD_MISMATCH")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout:
        raise RuntimeError("SOURCE_WORKTREE_NOT_CLEAN")


def _load_existing_receipt(
    data_root: Path,
    receipt_output: Path,
    source_commit: str,
) -> dict[str, object] | None:
    if not receipt_output.exists():
        return None
    try:
        receipt_raw = receipt_output.read_bytes()
        receipt = json.loads(receipt_raw.decode("utf-8"))
        validate_source_pack_receipt(receipt, expected_source_commit=source_commit)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RuntimeError("SOURCE_PACK_EXISTING_RECEIPT_INVALID") from None
    expected_raw = (
        json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if receipt_raw != expected_raw:
        raise RuntimeError("SOURCE_PACK_EXISTING_RECEIPT_INVALID")
    for item in receipt["packs"]:
        path = data_root / "source_packs" / item["file_name"]
        if not path.is_file() or _sha256_file(path) != item["outer_sha256"]:
            raise RuntimeError("SOURCE_PACK_EXISTING_RECEIPT_ARTIFACT_MISMATCH")
    database = data_root / "historical_source.sqlite3"
    if (
        not database.is_file()
        or database.stat().st_size != receipt["database"]["file_size_bytes"]
        or _sha256_file(database) != receipt["database"]["database_sha256"]
    ):
        raise RuntimeError("SOURCE_PACK_EXISTING_RECEIPT_DATABASE_MISMATCH")
    for name, expected_sha256, expected_size in _HISTORICAL_ACCEPTANCE:
        path = data_root / "historical_acceptance" / name
        if (
            not path.is_file()
            or path.stat().st_size != expected_size
            or _sha256_file(path) != expected_sha256
        ):
            raise RuntimeError("SOURCE_PACK_EXISTING_RECEIPT_ARTIFACT_MISMATCH")
    return receipt


def run_import(
    *,
    project_root: Path,
    source_root: Path,
    data_root: Path,
    receipt_output: Path,
    source_commit: str,
    generated_at_utc: str,
) -> dict[str, object]:
    project_root = project_root.resolve()
    source_root = source_root.resolve()
    data_root = data_root.resolve()
    receipt_output = receipt_output.resolve()
    _validate_paths(project_root, data_root, receipt_output)
    _verify_source_commit(project_root, source_commit)
    existing_receipt = _load_existing_receipt(
        data_root,
        receipt_output,
        source_commit,
    )
    if existing_receipt is not None:
        return existing_receipt
    started = time.monotonic()
    pack_root = data_root / "source_packs"
    pack_root.mkdir(parents=True, exist_ok=True)
    for frozen in AUTHORITATIVE_SOURCE_PACKS:
        _copy_verified(
            source_root / frozen.filename,
            pack_root / frozen.filename,
            frozen.sha256,
        )
    plans = verify_authoritative_source_packs(pack_root)
    historical_root = data_root / "historical_acceptance"
    historical_root.mkdir(parents=True, exist_ok=True)
    historical_observations = []
    for name, expected_sha256, expected_size in _HISTORICAL_ACCEPTANCE:
        source_path = project_root / "artifacts" / name
        if not source_path.is_file():
            raise RuntimeError("HISTORICAL_ACCEPTANCE_ARTIFACT_MISSING")
        destination_path = historical_root / name
        _copy_verified(source_path, destination_path, expected_sha256)
        if destination_path.stat().st_size != expected_size:
            raise RuntimeError("HISTORICAL_ACCEPTANCE_ARTIFACT_SIZE_MISMATCH")
        historical_observations.append(
            {
                "source_path": f"artifacts/{name}",
                "external_path_class": f"BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_acceptance/{name}",
                "sha256": _sha256_file(destination_path),
                "size_bytes": destination_path.stat().st_size,
            }
        )
    database_path = data_root / "historical_source.sqlite3"
    store = SqliteStore.open(database_path)
    try:
        store.migrate()
        for plan in plans:
            import_source_pack(store, plan)
        integrity = store.integrity_report()
        pack_summaries = []
        for frozen, plan in zip(AUTHORITATIVE_SOURCE_PACKS, plans, strict=True):
            run_id = f"historical-source-pack:{plan.pack_id}:{plan.outer_sha256}"
            row = store._connection.execute(
                "SELECT inserted_row_count,replayed_row_count,conflict_row_count,dropped_row_count,status "
                "FROM data_import_runs WHERE import_run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("SOURCE_PACK_IMPORT_RUN_MISSING")
            pack_summaries.append(
                {
                    "pack_id": frozen.pack_id,
                    "file_name": frozen.filename,
                    "outer_sha256": plan.outer_sha256,
                    "member_set_sha256": plan.member_set_sha256,
                    "universe_dates_sha256": plan.universe_dates_sha256,
                    "market_identity_sequence_sha256": plan.market_identity_sequence_sha256,
                    "universe_date_count": plan.universe_date_count,
                    "market_row_count": plan.market_row_count,
                    "price_row_count": plan.price_row_count,
                    "binance_row_count": plan.binance_row_count,
                    "settlement_row_count": plan.settlement_row_count,
                    "inserted_row_count": row[0],
                    "replayed_row_count": row[1],
                    "conflict_row_count": row[2],
                    "dropped_row_count": row[3],
                    "status": row[4],
                }
            )
        database_summary = {
            "database_sha256": "0" * 64,
            "file_size_bytes": database_path.stat().st_size,
            "integrity_check": "ok" if integrity["status"] == "PASS" else "failed",
            "migration_version": integrity["migration_version"],
            "import_run_count": store.count("data_import_runs"),
            "source_event_count": store.count("source_events"),
            "source_range_count": store.count("data_source_ranges"),
        }
    finally:
        store.close()
    database_summary["database_sha256"] = _sha256_file(database_path)
    database_summary["file_size_bytes"] = database_path.stat().st_size
    receipt = build_source_pack_receipt(
        project_root=project_root,
        source_commit=source_commit,
        generated_at_utc=generated_at_utc,
        elapsed_seconds=round(time.monotonic() - started, 6),
        packs=tuple(pack_summaries),
        database=database_summary,
        historical_artifact_observations=tuple(historical_observations),
    )
    receipt_output.parent.mkdir(parents=True, exist_ok=True)
    write_source_pack_receipt(receipt, receipt_output)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify and import the two authoritative historical source packs offline."
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--receipt-output", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--generated-at-utc",
        default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    args = parser.parse_args(argv)
    receipt = run_import(
        project_root=args.project_root,
        source_root=args.source_root,
        data_root=args.data_root,
        receipt_output=args.receipt_output,
        source_commit=args.source_commit,
        generated_at_utc=args.generated_at_utc,
    )
    print(json.dumps({"status": receipt["status"], "receipt_sha256": receipt["receipt_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
