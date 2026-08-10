from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_PHASE0_REPORT_SHA256 = "c7172837fc337a5d991c50d8c95833b081b5a5e006c2733693ef98100102574b"
_PHASE0_RECEIPT_SHA256 = "4e1ceb2a1276f46abf5a8d2cdb3e60d0cdf6095a770ffc54e4704a4c2954155d"
_HISTORICAL_ACCEPTANCE_ARTIFACTS = (
    ("artifacts/C1_ACCEPTANCE_PACK.zip", "9f6eaa9015f0698313c950571ae5f76be3f9c9d4327a461c4d504e8594b81a4f", 26026),
    ("artifacts/C2_C3_ACCEPTANCE_PACK.zip", "07c0119585ead440036734926a3ad7bd86ff63002cc2d4a0e96df1a6c47062c9", 13471),
)
_PACK_BINDINGS = {
    "HISTORICAL_70_V1_10": {
        "file_name": "btc_daily_range_merged_70_v1_10.zip",
        "outer_sha256": "911f4108cd8f6fe8d14c3bcbc435062caa0fa850c9f51a9712687507ac8691ce",
        "member_set_sha256": "065b7294bd0b1ca9bf1726891598ebce9160d281043c803ae5430d8d02985c32",
        "universe_dates_sha256": "c5d57431c006a6be71c6c7ec2f2e3c780339a29cfcc179febf59ebbcd6ec08ca",
        "market_identity_sequence_sha256": "432e35c6d4b5f73d8b8f8dd66779052854551746d0d13409f11144c3335dfb71",
        "universe_date_count": 70,
        "market_row_count": 770,
        "price_row_count": 2_853_424,
        "binance_row_count": 113_221,
        "settlement_row_count": 70,
        "total_row_count": 2_967_485,
    },
    "VALIDATION_100_V2_2": {
        "file_name": "btc_daily_range_validation_100_merged_v2_2(1).zip",
        "outer_sha256": "cfcf8b47acb87c3efec9a0d0f86f86dd83c7eeed919975c80dac9094ed7d1dc2",
        "member_set_sha256": "c41cbe90df0afe1c749af23f1f3ed9e4e45b9a08b1f51db3a265deb8bee4367a",
        "universe_dates_sha256": "a0978f29c94555927c03ad4116bbf53c6d78a772501937a377df672a4fd5a782",
        "market_identity_sequence_sha256": "12ebad4dd4122b5b0b636f657f912503cca3a4a3fd150c4e329bc52c94f5d01a",
        "universe_date_count": 100,
        "market_row_count": 1_100,
        "price_row_count": 4_144_919,
        "binance_row_count": 163_621,
        "settlement_row_count": 100,
        "total_row_count": 4_309_740,
    },
}


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _verify_phase0(
    project_root: Path,
    observations: tuple[dict[str, object], ...],
) -> dict[str, object]:
    report_path = project_root / "reports/PHASE0_ACCEPTED_BASELINE.json"
    receipt_path = project_root / "reports/PHASE0_COMMAND_RECEIPT.json"
    report_raw = report_path.read_bytes()
    receipt_raw = receipt_path.read_bytes()
    if _hash(report_raw) != _PHASE0_REPORT_SHA256 or _hash(receipt_raw) != _PHASE0_RECEIPT_SHA256:
        raise ValueError("PHASE0_HISTORICAL_ACCEPTANCE_BINDING_MISMATCH")
    report = json.loads(report_raw)
    historical = report.get("historical_sha256")
    if type(historical) is not dict or any(historical.get(path) != sha for path, sha, _ in _HISTORICAL_ACCEPTANCE_ARTIFACTS):
        raise ValueError("PHASE0_HISTORICAL_ACCEPTANCE_BINDING_MISMATCH")
    expected = tuple(
        {
            "source_path": path,
            "external_path_class": f"BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_acceptance/{Path(path).name}",
            "sha256": sha,
            "size_bytes": size,
        }
        for path, sha, size in _HISTORICAL_ACCEPTANCE_ARTIFACTS
    )
    if observations != expected:
        raise ValueError("PHASE0_HISTORICAL_ARTIFACT_OBSERVATION_MISMATCH")
    return {
        "phase0_report_sha256": _PHASE0_REPORT_SHA256,
        "phase0_receipt_sha256": _PHASE0_RECEIPT_SHA256,
        "artifacts": [
            dict(item)
            for item in expected
        ],
    }


def _validate_pack_summaries(packs: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    if type(packs) is not tuple or tuple(item.get("pack_id") for item in packs if type(item) is dict) != tuple(_PACK_BINDINGS):
        raise ValueError("SOURCE_PACK_RECEIPT_PACK_SET_MISMATCH")
    normalized: list[dict[str, object]] = []
    for item in packs:
        if type(item) is not dict:
            raise ValueError("SOURCE_PACK_RECEIPT_PACK_SET_MISMATCH")
        binding = _PACK_BINDINGS[str(item["pack_id"])]
        for key, value in binding.items():
            if key != "total_row_count" and item.get(key) != value:
                raise ValueError("SOURCE_PACK_RECEIPT_PACK_BINDING_MISMATCH")
        expected_keys = set(binding) - {"total_row_count"}
        expected_keys.update(
            {
                "pack_id",
                "inserted_row_count",
                "replayed_row_count",
                "conflict_row_count",
                "dropped_row_count",
                "status",
            }
        )
        if set(item) != expected_keys:
            raise ValueError("SOURCE_PACK_RECEIPT_PACK_SCHEMA_MISMATCH")
        inserted = item["inserted_row_count"]
        replayed = item["replayed_row_count"]
        if (
            type(inserted) is not int
            or type(replayed) is not int
            or inserted < 0
            or replayed < 0
            or inserted + replayed != binding["total_row_count"]
            or item["conflict_row_count"] != 0
            or item["dropped_row_count"] != 0
            or item["status"] != "COMPLETE"
        ):
            raise ValueError("SOURCE_PACK_RECEIPT_IMPORT_OUTCOME_MISMATCH")
        normalized.append(json.loads(_canonical(item)))
    return normalized


def _validate_database(database: object) -> dict[str, object]:
    required = {
        "database_sha256",
        "file_size_bytes",
        "integrity_check",
        "migration_version",
        "import_run_count",
        "source_event_count",
        "source_range_count",
    }
    if type(database) is not dict or set(database) != required:
        raise ValueError("SOURCE_PACK_RECEIPT_DATABASE_SCHEMA_MISMATCH")
    if (
        _SHA64.fullmatch(str(database["database_sha256"])) is None
        or type(database["file_size_bytes"]) is not int
        or database["file_size_bytes"] <= 0
        or database["integrity_check"] != "ok"
        or database["migration_version"] != 4
        or database["import_run_count"] != 2
        or database["source_event_count"] != 7_277_225
        or database["source_range_count"] != 8
    ):
        raise ValueError("SOURCE_PACK_RECEIPT_DATABASE_MISMATCH")
    return json.loads(_canonical(database))


def _validate_historical_acceptance(value: object) -> None:
    expected = {
        "phase0_report_sha256": _PHASE0_REPORT_SHA256,
        "phase0_receipt_sha256": _PHASE0_RECEIPT_SHA256,
        "artifacts": [
            {
                "source_path": path,
                "external_path_class": f"BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_acceptance/{Path(path).name}",
                "sha256": sha,
                "size_bytes": size,
            }
            for path, sha, size in _HISTORICAL_ACCEPTANCE_ARTIFACTS
        ],
    }
    if value != expected:
        raise ValueError("SOURCE_PACK_RECEIPT_CONTRACT_MISMATCH")


def build_source_pack_receipt(
    *,
    project_root: Path,
    source_commit: str,
    generated_at_utc: str,
    elapsed_seconds: float,
    packs: tuple[dict[str, object], ...],
    database: dict[str, object],
    historical_artifact_observations: tuple[dict[str, object], ...],
) -> dict[str, object]:
    if not isinstance(project_root, Path) or _SHA40.fullmatch(source_commit) is None:
        raise ValueError("INVALID_SOURCE_PACK_RECEIPT_CONFIGURATION")
    if type(generated_at_utc) is not str or not generated_at_utc.endswith("Z"):
        raise ValueError("INVALID_SOURCE_PACK_RECEIPT_CONFIGURATION")
    if type(elapsed_seconds) not in {int, float} or elapsed_seconds < 0:
        raise ValueError("INVALID_SOURCE_PACK_RECEIPT_CONFIGURATION")
    normalized_packs = _validate_pack_summaries(packs)
    normalized_database = _validate_database(database)
    payload: dict[str, object] = {
        "schema_version": "BTC_DATA_COMPLETION_SOURCE_PACK_RECEIPT_V1",
        "status": "HISTORICAL_SOURCE_PACK_IMPORT_PASS",
        "source_commit": source_commit,
        "generated_at_utc": generated_at_utc,
        "elapsed_seconds": elapsed_seconds,
        "source_pack_external_root_class": "BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/source_packs",
        "database_external_path_class": "BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_source.sqlite3",
        "historical_acceptance": _verify_phase0(
            project_root,
            historical_artifact_observations,
        ),
        "packs": normalized_packs,
        "database": normalized_database,
        "source_pack_row_count": sum(int(item["inserted_row_count"]) + int(item["replayed_row_count"]) for item in normalized_packs),
        "universe_date_count": sum(int(item["universe_date_count"]) for item in normalized_packs),
        "universe_overlap_count": 0,
        "conflict_row_count": 0,
        "dropped_row_count": 0,
        "bounded_streaming_max_materialized_rows": 1,
        "network": {"mode": "OFFLINE_LOCAL_SOURCE_IMPORT", "provider_requests": 0},
        "paper_execution_authorized": False,
        "trading_approval": False,
    }
    payload["receipt_sha256"] = _hash(_canonical(payload).encode())
    validate_source_pack_receipt(payload, expected_source_commit=source_commit)
    return payload


def validate_source_pack_receipt(
    receipt: dict[str, object],
    *,
    expected_source_commit: str,
) -> None:
    if type(receipt) is not dict or _SHA40.fullmatch(expected_source_commit) is None:
        raise ValueError("SOURCE_PACK_RECEIPT_INVALID")
    expected_keys = {
        "schema_version", "status", "source_commit", "generated_at_utc",
        "elapsed_seconds", "source_pack_external_root_class",
        "database_external_path_class", "historical_acceptance", "packs",
        "database", "source_pack_row_count", "universe_date_count",
        "universe_overlap_count", "conflict_row_count", "dropped_row_count",
        "bounded_streaming_max_materialized_rows", "network",
        "paper_execution_authorized", "trading_approval", "receipt_sha256",
    }
    if set(receipt) != expected_keys:
        raise ValueError("SOURCE_PACK_RECEIPT_CONTRACT_MISMATCH")
    stored_hash = receipt.get("receipt_sha256")
    without_hash = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if _SHA64.fullmatch(str(stored_hash)) is None or _hash(_canonical(without_hash).encode()) != stored_hash:
        raise ValueError("SOURCE_PACK_RECEIPT_HASH_MISMATCH")
    if (
        receipt.get("schema_version") != "BTC_DATA_COMPLETION_SOURCE_PACK_RECEIPT_V1"
        or receipt.get("status") != "HISTORICAL_SOURCE_PACK_IMPORT_PASS"
        or receipt.get("source_commit") != expected_source_commit
        or receipt.get("source_pack_row_count") != 7_277_225
        or receipt.get("universe_date_count") != 170
        or receipt.get("universe_overlap_count") != 0
        or receipt.get("conflict_row_count") != 0
        or receipt.get("dropped_row_count") != 0
        or receipt.get("bounded_streaming_max_materialized_rows") != 1
        or receipt.get("network") != {"mode": "OFFLINE_LOCAL_SOURCE_IMPORT", "provider_requests": 0}
        or receipt.get("paper_execution_authorized") is not False
        or receipt.get("trading_approval") is not False
        or receipt.get("source_pack_external_root_class") != "BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/source_packs"
        or receipt.get("database_external_path_class") != "BTC_DAILY_RANGE_WINDOWS_V1_DATA_COMPLETION/historical_source.sqlite3"
    ):
        raise ValueError("SOURCE_PACK_RECEIPT_CONTRACT_MISMATCH")
    _validate_pack_summaries(tuple(receipt.get("packs", ())))
    try:
        _validate_database(receipt.get("database"))
    except ValueError as error:
        raise ValueError("SOURCE_PACK_RECEIPT_CONTRACT_MISMATCH") from error
    _validate_historical_acceptance(receipt.get("historical_acceptance"))


def write_source_pack_receipt(receipt: dict[str, object], output_path: Path) -> None:
    if not isinstance(output_path, Path):
        raise ValueError("INVALID_SOURCE_PACK_RECEIPT_PATH")
    validate_source_pack_receipt(
        receipt,
        expected_source_commit=str(receipt.get("source_commit", "")),
    )
    encoded = json.dumps(
        receipt,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    raw = encoded.encode("utf-8")
    if output_path.exists():
        if not output_path.is_file() or output_path.read_bytes() != raw:
            raise ValueError("SOURCE_PACK_RECEIPT_CONFLICT")
        return
    temporary = output_path.with_name(output_path.name + ".writing")
    if temporary.exists():
        raise ValueError("SOURCE_PACK_RECEIPT_INCOMPLETE_WRITE_PRESENT")
    temporary.write_bytes(raw)
    temporary.replace(output_path)
