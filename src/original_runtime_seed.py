from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.historical_input_builder import EXPECTED_SOURCE_SHA256
from src.performance_historical import (
    PINNED_AHR_ACCEPTANCE_SHA256,
    PINNED_AHR_MANIFEST_SEMANTIC_SHA256,
    PINNED_AHR_MANIFEST_SHA256,
    PINNED_AHR_RESULTS_SHA256,
    PINNED_AHR_RUN_ID,
    PINNED_SETTLEMENTS_SHA256,
)
from src.performance_models import (
    PerformanceObservation,
    PerformanceResolution,
    canonical_json,
)


ORIGINAL_RUNTIME_SEED_ID = "ORIGINAL_RUNTIME_SEED_V1"
ORIGINAL_RUNTIME_SEED_MANIFEST_SCHEMA = "ORIGINAL_RUNTIME_SEED_MANIFEST_V1"
ORIGINAL_RUNTIME_SEED_PACKAGER_VERSION = "ORIGINAL_RUNTIME_SEED_PACKAGER_V1"
PINNED_AHR_PARITY_REPORT_SHA256 = (
    "9e5a5b0e64d84e98bf813b0dbfa6662c63e9901b00cf342b7748b3cbe9d764c2"
)
PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256 = (
    "855d71af43361339a5cbeb7edd476c15db2de00e8bc0c157112dc93851886617"
)
EXPECTED_EXCLUDED_DATA = frozenset(
    {
        "RECOVERED",
        "FORWARD",
        "EMITTED_SIGNALS",
        "PAPER_INTENTS",
        "PAPER_FILLS",
        "MUTABLE_CURSORS",
        "RUNTIME_INCIDENTS",
    }
)


@dataclass(frozen=True, slots=True)
class OriginalRuntimeSeedBundle:
    seed_id: str
    seed_dir: Path
    manifest_sha256: str
    acceptance_sha256: str
    result_artifact_sha256: str
    input_manifest_sha256: str
    settlement_artifact_sha256: str
    source_sha256: Mapping[str, str]
    expected_migration_version: int
    observations: tuple[PerformanceObservation, ...]
    resolutions: tuple[PerformanceResolution, ...]


class OriginalRuntimeSeedLoader:
    """Fail-closed loader for the Git-controlled accepted ORIGINAL seed."""

    def __init__(self, *, project_root: Path, seed_dir: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.seed_dir = (
            Path(seed_dir).resolve()
            if seed_dir is not None
            else self.project_root
            / "strategy_sources"
            / "frozen"
            / "original_runtime_seed_v1"
        )

    def load(self) -> OriginalRuntimeSeedBundle:
        manifest_path = self.seed_dir / "manifest.json"
        manifest_hash = _required_hash(
            manifest_path, missing_code="ORIGINAL_RUNTIME_SEED_MANIFEST_MISSING"
        )
        if manifest_hash != PINNED_ORIGINAL_RUNTIME_SEED_MANIFEST_SHA256:
            raise ValueError("ORIGINAL_RUNTIME_SEED_MANIFEST_HASH_MISMATCH")
        manifest = _read_object(
            manifest_path, invalid_code="ORIGINAL_RUNTIME_SEED_MANIFEST_INVALID"
        )
        self._validate_manifest_identity(manifest)
        baseline = _required_mapping(
            manifest.get("source_accepted_baseline"),
            "ORIGINAL_RUNTIME_SEED_BASELINE_INVALID",
        )
        self._validate_baseline(baseline)
        self._validate_frozen_contracts(manifest.get("frozen_contracts"))
        self._validate_security_guards(manifest.get("security_guards"))

        payloads = self._payload_descriptors(manifest.get("payload_files"))
        observations = tuple(
            _observation_from_line(line)
            for line in self._read_payload(payloads["performance_observations"])
        )
        resolutions = tuple(
            _resolution_from_line(line)
            for line in self._read_payload(payloads["performance_resolutions"])
        )
        self._validate_content(manifest, observations, resolutions)
        self._validate_strategy_identities(
            manifest.get("strategy_identities"), observations
        )
        compatibility = _required_mapping(
            manifest.get("schema_compatibility"),
            "ORIGINAL_RUNTIME_SEED_SCHEMA_COMPATIBILITY_INVALID",
        )
        migration_version = compatibility.get("migration_version")
        if (
            type(migration_version) is not int
            or migration_version <= 0
            or compatibility.get("observation_schema") != "PERFORMANCE_OBSERVATION_V1"
            or compatibility.get("resolution_schema") != "PERFORMANCE_RESOLUTION_V1"
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_SCHEMA_COMPATIBILITY_INVALID")
        return OriginalRuntimeSeedBundle(
            seed_id=ORIGINAL_RUNTIME_SEED_ID,
            seed_dir=self.seed_dir,
            manifest_sha256=manifest_hash,
            acceptance_sha256=str(baseline["acceptance_sha256"]),
            result_artifact_sha256=str(baseline["strategy_results_sha256"]),
            input_manifest_sha256=str(baseline["input_manifest_semantic_sha256"]),
            settlement_artifact_sha256=str(baseline["settlements_sha256"]),
            source_sha256=dict(baseline["source_sha256"]),
            expected_migration_version=migration_version,
            observations=observations,
            resolutions=resolutions,
        )

    @staticmethod
    def _validate_manifest_identity(manifest: Mapping[str, Any]) -> None:
        if (
            manifest.get("schema_version") != ORIGINAL_RUNTIME_SEED_MANIFEST_SCHEMA
            or manifest.get("seed_id") != ORIGINAL_RUNTIME_SEED_ID
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_MANIFEST_INVALID")
        if manifest.get("provenance_classification") != "ORIGINAL":
            raise ValueError("ORIGINAL_RUNTIME_SEED_PROVENANCE_INVALID")
        creation = _required_mapping(
            manifest.get("creation_method"),
            "ORIGINAL_RUNTIME_SEED_CREATION_METHOD_INVALID",
        )
        if (
            creation.get("version") != ORIGINAL_RUNTIME_SEED_PACKAGER_VERSION
            or creation.get("script") != "tools/build_original_runtime_seed_v1.py"
            or creation.get("ordering") != "ACCEPTED_AHR_RESULT_ORDER"
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_CREATION_METHOD_INVALID")
        excluded = manifest.get("excluded_data")
        if (
            type(excluded) is not list
            or len(excluded) != len(EXPECTED_EXCLUDED_DATA)
            or set(excluded) != EXPECTED_EXCLUDED_DATA
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_EXCLUSIONS_INVALID")

    @staticmethod
    def _validate_baseline(baseline: Mapping[str, Any]) -> None:
        expected = {
            "path_name": f"historical_revalidation/{PINNED_AHR_RUN_ID}",
            "run_id": PINNED_AHR_RUN_ID,
            "acceptance_sha256": PINNED_AHR_ACCEPTANCE_SHA256,
            "input_manifest_file_sha256": PINNED_AHR_MANIFEST_SHA256,
            "input_manifest_semantic_sha256": PINNED_AHR_MANIFEST_SEMANTIC_SHA256,
            "parity_report_sha256": PINNED_AHR_PARITY_REPORT_SHA256,
            "strategy_results_sha256": PINNED_AHR_RESULTS_SHA256,
            "settlements_sha256": PINNED_SETTLEMENTS_SHA256,
            "source_sha256": dict(sorted(EXPECTED_SOURCE_SHA256.items())),
        }
        if dict(baseline) != expected:
            raise ValueError("ORIGINAL_RUNTIME_SEED_BASELINE_INVALID")

    def _validate_frozen_contracts(self, value: object) -> None:
        if type(value) is not list or len(value) != 2:
            raise ValueError("ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACTS_INVALID")
        expected_paths = {
            "strategy_sources/frozen/STRATEGY_RULE_MAP_47.json": (
                "BTC_STRATEGY_RULE_MAP_47_V2"
            ),
            "reports/STRATEGY_47_STATUS_MATRIX.json": (
                "BTC_STRATEGY_47_STATUS_MATRIX_V3"
            ),
        }
        observed_paths: set[str] = set()
        for descriptor in value:
            item = _required_mapping(
                descriptor, "ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACTS_INVALID"
            )
            relative = item.get("relative_path")
            if type(relative) is not str or relative not in expected_paths:
                raise ValueError("ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACTS_INVALID")
            if relative in observed_paths:
                raise ValueError("ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACTS_INVALID")
            observed_paths.add(relative)
            path = _contained_path(self.project_root, relative)
            if _required_hash(
                path, missing_code="ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACT_MISSING"
            ) != item.get("sha256"):
                raise ValueError(
                    "ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACT_HASH_MISMATCH"
                )
            payload = _read_object(
                path, invalid_code="ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACT_INVALID"
            )
            if (
                item.get("schema_version") != expected_paths[relative]
                or payload.get("schema_version") != expected_paths[relative]
            ):
                raise ValueError("ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACT_INVALID")
        if observed_paths != set(expected_paths):
            raise ValueError("ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACTS_INVALID")

    @staticmethod
    def _payload_descriptors(value: object) -> dict[str, Mapping[str, Any]]:
        if type(value) is not list or len(value) != 2:
            raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_MANIFEST_INVALID")
        result: dict[str, Mapping[str, Any]] = {}
        for descriptor in value:
            item = _required_mapping(
                descriptor, "ORIGINAL_RUNTIME_SEED_PAYLOAD_MANIFEST_INVALID"
            )
            logical_type = item.get("logical_type")
            if logical_type not in {
                "performance_observations",
                "performance_resolutions",
            } or logical_type in result:
                raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_MANIFEST_INVALID")
            if (
                type(item.get("relative_path")) is not str
                or type(item.get("record_count")) is not int
                or item["record_count"] < 0
                or not _is_sha256(item.get("sha256"))
            ):
                raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_MANIFEST_INVALID")
            result[str(logical_type)] = item
        return result

    @staticmethod
    def _validate_security_guards(value: object) -> None:
        expected = {
            "authenticated_CLOB_writes": False,
            "real_orders": False,
            "signing": False,
            "trading_approval": False,
            "wallet": False,
        }
        if type(value) is not dict or value != expected:
            raise ValueError("ORIGINAL_RUNTIME_SEED_SECURITY_GUARDS_INVALID")

    def _validate_strategy_identities(
        self,
        value: object,
        observations: tuple[PerformanceObservation, ...],
    ) -> None:
        if type(value) is not list or any(type(item) is not dict for item in value):
            raise ValueError("ORIGINAL_RUNTIME_SEED_STRATEGY_IDENTITIES_INVALID")
        status = _read_object(
            self.project_root / "reports" / "STRATEGY_47_STATUS_MATRIX.json",
            invalid_code="ORIGINAL_RUNTIME_SEED_FROZEN_CONTRACT_INVALID",
        )
        active_ids = {row.strategy_id for row in observations}
        expected = sorted(
            (
                {
                    "registry_index": row["registry_index"],
                    "rule_spec_sha256": row["rule_spec_sha256"],
                    "strategy_id": row["strategy_id"],
                    "version": row["version"],
                }
                for row in status.get("strategies", [])
                if row.get("strategy_id") in active_ids
            ),
            key=lambda row: row["strategy_id"],
        )
        if value != expected:
            raise ValueError("ORIGINAL_RUNTIME_SEED_STRATEGY_IDENTITIES_INVALID")

    def _read_payload(self, descriptor: Mapping[str, Any]) -> tuple[str, ...]:
        path = _contained_path(self.seed_dir, str(descriptor["relative_path"]))
        actual_hash = _required_hash(
            path, missing_code="ORIGINAL_RUNTIME_SEED_PAYLOAD_MISSING"
        )
        if actual_hash != descriptor["sha256"]:
            raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_HASH_MISMATCH")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_INVALID") from None
        if not text.endswith("\n") or "\r" in text:
            raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_INVALID")
        lines = tuple(text[:-1].split("\n")) if text != "\n" else ()
        if (
            any(not line for line in lines)
            or len(lines) != descriptor["record_count"]
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_PAYLOAD_COUNT_MISMATCH")
        return lines

    @staticmethod
    def _validate_content(
        manifest: Mapping[str, Any],
        observations: tuple[PerformanceObservation, ...],
        resolutions: tuple[PerformanceResolution, ...],
    ) -> None:
        counts = _required_mapping(
            manifest.get("record_counts"),
            "ORIGINAL_RUNTIME_SEED_RECORD_COUNTS_INVALID",
        )
        actual_counts = {
            "observations": len(observations),
            "resolutions": len(resolutions),
            "accepted_observations": sum(row.accepted for row in observations),
            "rejected_observations": sum(not row.accepted for row in observations),
            "strategies": len({row.strategy_id for row in observations}),
        }
        if dict(counts) != actual_counts:
            raise ValueError("ORIGINAL_RUNTIME_SEED_RECORD_COUNTS_INVALID")
        if (
            len({row.observation_key for row in observations}) != len(observations)
            or len({row.logical_decision_key for row in observations})
            != len(observations)
        ):
            raise ValueError(
                "ORIGINAL_RUNTIME_SEED_OBSERVATION_IDENTITY_CONFLICT"
            )
        if any(
            row.source_layer != "HISTORICAL"
            or row.provenance_run_id != PINNED_AHR_RUN_ID
            or row.emitted
            or row.signal_identity_key is not None
            or row.evaluation_key is not None
            for row in observations
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_OBSERVATION_PROVENANCE_INVALID")
        observation_by_key = {row.observation_key: row for row in observations}
        if (
            len({row.resolution_key for row in resolutions}) != len(resolutions)
            or len({(row.observation_key, row.revision) for row in resolutions})
            != len(resolutions)
        ):
            raise ValueError("ORIGINAL_RUNTIME_SEED_RESOLUTION_IDENTITY_CONFLICT")
        if any(
            row.observation_key not in observation_by_key
            or not observation_by_key[row.observation_key].accepted
            for row in resolutions
        ) or len(resolutions) != sum(row.accepted for row in observations):
            raise ValueError("ORIGINAL_RUNTIME_SEED_RESOLUTION_SCOPE_INVALID")


def _observation_from_line(line: str) -> PerformanceObservation:
    payload = _parse_line(line, "ORIGINAL_RUNTIME_SEED_OBSERVATION_INVALID")
    values = dict(payload)
    for name in (
        "decision_semantic_sha256",
        "selected_bucket_identity_sha256",
        "shares_micros",
    ):
        values.pop(name, None)
    selected = values.get("selected_buckets")
    if type(selected) is list:
        values["selected_buckets"] = tuple(selected)
    try:
        observation = PerformanceObservation.create(**values)
    except (TypeError, ValueError):
        raise ValueError("ORIGINAL_RUNTIME_SEED_OBSERVATION_INVALID") from None
    if observation.payload_json != line:
        raise ValueError("ORIGINAL_RUNTIME_SEED_OBSERVATION_PAYLOAD_MISMATCH")
    return observation


def _resolution_from_line(line: str) -> PerformanceResolution:
    payload = _parse_line(line, "ORIGINAL_RUNTIME_SEED_RESOLUTION_INVALID")
    values = dict(payload)
    provenance = values.pop("provenance", None)
    if type(provenance) is not dict:
        raise ValueError("ORIGINAL_RUNTIME_SEED_RESOLUTION_INVALID")
    values["provenance_json"] = canonical_json(provenance)
    try:
        resolution = PerformanceResolution(**values)
    except (TypeError, ValueError):
        raise ValueError("ORIGINAL_RUNTIME_SEED_RESOLUTION_INVALID") from None
    if resolution.payload_json != line:
        raise ValueError("ORIGINAL_RUNTIME_SEED_RESOLUTION_PAYLOAD_MISMATCH")
    return resolution


def _parse_line(line: str, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        raise ValueError(error_code) from None
    if type(value) is not dict or canonical_json(value) != line:
        raise ValueError(error_code)
    return value


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ValueError("ORIGINAL_RUNTIME_SEED_PATH_INVALID") from None
    if Path(relative).is_absolute() or relative in {"", "."}:
        raise ValueError("ORIGINAL_RUNTIME_SEED_PATH_INVALID")
    return candidate


def _required_hash(path: Path, *, missing_code: str) -> str:
    if not path.is_file():
        raise ValueError(missing_code)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ValueError(missing_code) from None
    return digest.hexdigest()


def _read_object(path: Path, *, invalid_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(invalid_code) from None
    if type(value) is not dict:
        raise ValueError(invalid_code)
    return value


def _required_mapping(value: object, error_code: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise ValueError(error_code)
    return value


def _is_sha256(value: object) -> bool:
    if type(value) is not str or len(value) != 64 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
