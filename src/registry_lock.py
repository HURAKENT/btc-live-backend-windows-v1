from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_EXPECTED_STRATEGY_COUNT = 47
_EXPECTED_V1_COUNT = 34
_EXPECTED_V2_COUNT = 13
_VALID_VERSIONS = frozenset({"V1", "V2"})
_LINEAGE_STRING_FIELDS = (
    "strategy_generation",
    "parent_strategy_id",
    "stage_origin",
)


@dataclass(frozen=True, slots=True)
class RegistryLockReport:
    strategy_count: int
    unique_strategy_ids: int
    v1_count: int
    v2_count: int
    csv_sha256: str
    json_sha256: str
    ordered_strategy_ids: tuple[str, ...]


def verify_registry_lock(
    lock_path: Path,
    registry_json_path: Path,
    registry_csv_path: Path,
) -> RegistryLockReport:
    lock_bytes = lock_path.read_bytes()
    registry_json_bytes = registry_json_path.read_bytes()
    registry_csv_bytes = registry_csv_path.read_bytes()

    lock = _parse_json(lock_bytes, "lock")
    (
        ordered_lock_ids,
        expected_json_sha256,
        expected_csv_sha256,
        expected_v1_count,
        expected_v2_count,
    ) = _validate_lock(lock)

    json_sha256 = hashlib.sha256(registry_json_bytes).hexdigest()
    if json_sha256 != expected_json_sha256:
        _fail(
            "REGISTRY_JSON_HASH_MISMATCH",
            f"expected={expected_json_sha256} actual={json_sha256}",
        )

    csv_sha256 = hashlib.sha256(registry_csv_bytes).hexdigest()
    if csv_sha256 != expected_csv_sha256:
        _fail(
            "REGISTRY_CSV_HASH_MISMATCH",
            f"expected={expected_csv_sha256} actual={csv_sha256}",
        )

    registry_json = _parse_json(registry_json_bytes, "registry_json")
    json_ids, json_versions, json_declared_count = _validate_registry_json(
        registry_json
    )
    csv_ids, csv_versions = _validate_registry_csv(registry_csv_bytes)

    _ensure_unique_ids(ordered_lock_ids, "lock")
    _ensure_unique_ids(json_ids, "registry_json")
    _ensure_unique_ids(csv_ids, "registry_csv")

    lock_id_set = set(ordered_lock_ids)
    json_id_set = set(json_ids)
    csv_id_set = set(csv_ids)
    _validate_identity_set(lock_id_set, json_id_set, "registry_json")
    _validate_identity_set(lock_id_set, csv_id_set, "registry_csv")

    if json_id_set != csv_id_set:
        _fail("REGISTRY_LINEAGE_MISMATCH", "JSON and CSV identity sets differ")

    if (
        json_declared_count != _EXPECTED_STRATEGY_COUNT
        or len(json_ids) != _EXPECTED_STRATEGY_COUNT
        or len(csv_ids) != _EXPECTED_STRATEGY_COUNT
        or len(json_id_set) != _EXPECTED_STRATEGY_COUNT
        or len(csv_id_set) != _EXPECTED_STRATEGY_COUNT
    ):
        _fail(
            "REGISTRY_COUNT_MISMATCH",
            (
                f"json_declared={json_declared_count} json_rows={len(json_ids)} "
                f"csv_rows={len(csv_ids)}"
            ),
        )

    json_version_counts = Counter(json_versions.values())
    csv_version_counts = Counter(csv_versions.values())
    expected_version_counts = Counter(
        {"V1": expected_v1_count, "V2": expected_v2_count}
    )
    if json_version_counts != expected_version_counts:
        _fail(
            "REGISTRY_LINEAGE_MISMATCH",
            f"registry_json version_counts={dict(json_version_counts)}",
        )
    if csv_version_counts != expected_version_counts:
        _fail(
            "REGISTRY_LINEAGE_MISMATCH",
            f"registry_csv version_counts={dict(csv_version_counts)}",
        )
    if json_versions != csv_versions:
        _fail("REGISTRY_LINEAGE_MISMATCH", "per-strategy JSON/CSV versions differ")

    return RegistryLockReport(
        strategy_count=_EXPECTED_STRATEGY_COUNT,
        unique_strategy_ids=len(json_id_set),
        v1_count=json_version_counts["V1"],
        v2_count=json_version_counts["V2"],
        csv_sha256=csv_sha256,
        json_sha256=json_sha256,
        ordered_strategy_ids=ordered_lock_ids,
    )


def build_executable_gap_report(
    lock_path: Path,
    registry_json_path: Path,
    registry_csv_path: Path,
) -> dict[str, Any]:
    verification = verify_registry_lock(
        lock_path, registry_json_path, registry_csv_path
    )
    strategies = [
        {
            "strategy_id": strategy_id,
            "registry_locked": True,
            "exact_rule_source_locked": False,
            "historical_parity": "NOT_RUN",
            "runtime_status": "DISABLED_EXECUTABLE_RULE_GAP",
        }
        for strategy_id in verification.ordered_strategy_ids
    ]
    return {
        "schema_version": "BTC_STRATEGY_EXECUTABLE_GAP_REPORT_V1",
        "registry_locked": True,
        "strategy_count": verification.strategy_count,
        "executable_now": 0,
        "requires_parity": verification.strategy_count,
        "strategies": strategies,
    }


def _validate_lock(
    lock: Any,
) -> tuple[tuple[str, ...], str, str, int, int]:
    if type(lock) is not dict:
        _fail("INVALID_REGISTRY_SHAPE", "lock top level must be an object")

    strategy_count = _require_exact_type(lock, "strategy_count", int, "lock")
    v1_count = _require_exact_type(lock, "v1_count", int, "lock")
    v2_count = _require_exact_type(lock, "v2_count", int, "lock")
    strategy_ids = _require_exact_type(lock, "strategy_ids", list, "lock")
    registry_json = _require_exact_type(lock, "registry_json", dict, "lock")
    registry_csv = _require_exact_type(lock, "registry_csv", dict, "lock")
    json_sha256 = _require_exact_type(
        registry_json, "sha256", str, "lock.registry_json"
    )
    csv_sha256 = _require_exact_type(
        registry_csv, "sha256", str, "lock.registry_csv"
    )

    if (
        strategy_count != _EXPECTED_STRATEGY_COUNT
        or v1_count != _EXPECTED_V1_COUNT
        or v2_count != _EXPECTED_V2_COUNT
        or len(strategy_ids) != _EXPECTED_STRATEGY_COUNT
    ):
        _fail(
            "REGISTRY_COUNT_MISMATCH",
            (
                f"strategy_count={strategy_count} v1_count={v1_count} "
                f"v2_count={v2_count} strategy_ids={len(strategy_ids)}"
            ),
        )

    ordered_ids: list[str] = []
    for index, strategy_id in enumerate(strategy_ids):
        if type(strategy_id) is not str:
            _fail(
                "INVALID_REGISTRY_TYPE",
                f"lock.strategy_ids[{index}] must be str",
            )
        if not strategy_id:
            _fail("MISSING_REGISTRY_ID", f"lock.strategy_ids[{index}] is empty")
        ordered_ids.append(strategy_id)

    for source, sha256_value in (
        ("lock.registry_json.sha256", json_sha256),
        ("lock.registry_csv.sha256", csv_sha256),
    ):
        if len(sha256_value) != 64 or any(
            character not in "0123456789abcdef" for character in sha256_value
        ):
            _fail("INVALID_REGISTRY_TYPE", f"{source} must be lowercase SHA-256")

    return (
        tuple(ordered_ids),
        json_sha256,
        csv_sha256,
        v1_count,
        v2_count,
    )


def _validate_registry_json(
    registry: Any,
) -> tuple[list[str], dict[str, str], int]:
    if type(registry) is not dict:
        _fail(
            "INVALID_REGISTRY_SHAPE",
            "registry_json top level must be an object",
        )
    strategies = _require_exact_type(
        registry, "strategies", list, "registry_json"
    )
    declared_count = _require_exact_type(
        registry, "strategy_count", int, "registry_json"
    )

    strategy_ids: list[str] = []
    versions: dict[str, str] = {}
    for index, row in enumerate(strategies):
        source = f"registry_json.strategies[{index}]"
        if type(row) is not dict:
            _fail("INVALID_REGISTRY_SHAPE", f"{source} must be an object")
        strategy_id = _required_strategy_id(row, source)
        version = _required_json_lineage(row, source)
        strategy_ids.append(strategy_id)
        versions[strategy_id] = version
    return strategy_ids, versions, declared_count


def _validate_registry_csv(
    registry_csv_bytes: bytes,
) -> tuple[list[str], dict[str, str]]:
    try:
        csv_text = registry_csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("INVALID_REGISTRY_SHAPE: registry_csv is not UTF-8") from exc

    try:
        with io.StringIO(csv_text, newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            rows = list(reader)
    except csv.Error as exc:
        raise ValueError("INVALID_REGISTRY_SHAPE: registry_csv parse failed") from exc

    if type(fieldnames) is not list:
        _fail("INVALID_REGISTRY_SHAPE", "registry_csv header is missing")
    required_fields = {
        "registry_index",
        "strategy_id",
        "version",
        *_LINEAGE_STRING_FIELDS,
    }
    missing_fields = sorted(required_fields - set(fieldnames))
    if missing_fields:
        code = (
            "REGISTRY_LINEAGE_MISSING"
            if any(field != "strategy_id" for field in missing_fields)
            else "INVALID_REGISTRY_SHAPE"
        )
        _fail(code, f"registry_csv missing fields={','.join(missing_fields)}")

    strategy_ids: list[str] = []
    versions: dict[str, str] = {}
    for index, row in enumerate(rows):
        source = f"registry_csv row {index + 2}"
        if type(row) is not dict or None in row:
            _fail("INVALID_REGISTRY_SHAPE", f"{source} has invalid columns")
        strategy_id = _required_strategy_id(row, source)
        version = _required_csv_lineage(row, source)
        strategy_ids.append(strategy_id)
        versions[strategy_id] = version
    return strategy_ids, versions


def _required_strategy_id(row: dict[str, Any], source: str) -> str:
    if "strategy_id" not in row:
        _fail("MISSING_REGISTRY_ID", f"{source}.strategy_id is missing")
    strategy_id = row["strategy_id"]
    if type(strategy_id) is not str:
        _fail("INVALID_REGISTRY_TYPE", f"{source}.strategy_id must be str")
    if not strategy_id:
        _fail("MISSING_REGISTRY_ID", f"{source}.strategy_id is empty")
    return strategy_id


def _required_json_lineage(row: dict[str, Any], source: str) -> str:
    if "version" not in row:
        _fail("REGISTRY_LINEAGE_MISSING", f"{source}.version is missing")
    version = row["version"]
    if type(version) is not str:
        _fail("INVALID_REGISTRY_TYPE", f"{source}.version must be str")
    if not version:
        _fail("REGISTRY_LINEAGE_MISSING", f"{source}.version is empty")
    if version not in _VALID_VERSIONS:
        _fail("REGISTRY_LINEAGE_MISMATCH", f"{source}.version={version}")

    registry_index = _require_exact_type(row, "registry_index", int, source)
    if registry_index <= 0:
        _fail(
            "REGISTRY_LINEAGE_MISMATCH",
            f"{source}.registry_index={registry_index}",
        )
    for field in _LINEAGE_STRING_FIELDS:
        if field not in row:
            _fail("REGISTRY_LINEAGE_MISSING", f"{source}.{field} is missing")
        value = row[field]
        if type(value) is not str:
            _fail("INVALID_REGISTRY_TYPE", f"{source}.{field} must be str")
        if field != "parent_strategy_id" and not value:
            _fail("REGISTRY_LINEAGE_MISSING", f"{source}.{field} is empty")
    return version


def _required_csv_lineage(row: dict[str, Any], source: str) -> str:
    version = row.get("version")
    if type(version) is not str:
        _fail("INVALID_REGISTRY_TYPE", f"{source}.version must be str")
    if not version:
        _fail("REGISTRY_LINEAGE_MISSING", f"{source}.version is empty")
    if version not in _VALID_VERSIONS:
        _fail("REGISTRY_LINEAGE_MISMATCH", f"{source}.version={version}")

    registry_index = row.get("registry_index")
    if type(registry_index) is not str:
        _fail("INVALID_REGISTRY_TYPE", f"{source}.registry_index must be str")
    if not registry_index:
        _fail("REGISTRY_LINEAGE_MISSING", f"{source}.registry_index is empty")
    if not registry_index.isdigit() or int(registry_index) <= 0:
        _fail(
            "REGISTRY_LINEAGE_MISMATCH",
            f"{source}.registry_index={registry_index}",
        )
    for field in _LINEAGE_STRING_FIELDS:
        value = row.get(field)
        if type(value) is not str:
            _fail("INVALID_REGISTRY_TYPE", f"{source}.{field} must be str")
        if field != "parent_strategy_id" and not value:
            _fail("REGISTRY_LINEAGE_MISSING", f"{source}.{field} is empty")
    return version


def _ensure_unique_ids(strategy_ids: tuple[str, ...] | list[str], source: str) -> None:
    duplicates = sorted(
        strategy_id
        for strategy_id, count in Counter(strategy_ids).items()
        if count > 1
    )
    if duplicates:
        _fail(
            "DUPLICATE_STRATEGY_ID",
            f"{source} duplicates={','.join(duplicates)}",
        )


def _validate_identity_set(
    locked_ids: set[str],
    registry_ids: set[str],
    source: str,
) -> None:
    missing_ids = sorted(locked_ids - registry_ids)
    if missing_ids:
        _fail(
            "MISSING_REGISTRY_ID",
            f"{source} missing={','.join(missing_ids)}",
        )
    unexpected_ids = sorted(registry_ids - locked_ids)
    if unexpected_ids:
        _fail(
            "UNEXPECTED_REGISTRY_ID",
            f"{source} unexpected={','.join(unexpected_ids)}",
        )


def _require_exact_type(
    mapping: dict[str, Any],
    key: str,
    expected_type: type[Any],
    source: str,
) -> Any:
    if key not in mapping:
        _fail("INVALID_REGISTRY_SHAPE", f"{source}.{key} is missing")
    value = mapping[key]
    if type(value) is not expected_type:
        _fail(
            "INVALID_REGISTRY_TYPE",
            f"{source}.{key} must be {expected_type.__name__}",
        )
    return value


def _parse_json(raw_bytes: bytes, source: str) -> Any:
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"INVALID_REGISTRY_SHAPE: {source} is not valid JSON") from exc


def _fail(code: str, detail: str) -> None:
    raise ValueError(f"{code}: {detail}")
