from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.registry_lock import verify_registry_lock


_EXPECTED_RULE_PACK_SHA256 = (
    "58384baf72c619aa030b08a960e13db6b552aef87e24b6d966387dadaca5a940"
)
_EXPECTED_SCHEMA_VERSION = "BTC_STRATEGY_RULE_MAP_47_V2"
_EXPECTED_STATUS = "C4_RULE_SOURCES_FROZEN_PARITY_PENDING"
_EXPECTED_COUNTS = (47, 34, 13)
_EXPECTED_CANONICAL_SOURCES = {
    "registry_lock_sha256": (
        "5f4dbf5936c8b456dbe15aceeb4210342a452d6b379e0ee820d7b5de27b92957"
    ),
    "registry_json_sha256": (
        "88c54943cf84e4d123f979639cf30668f35a5dd6bc6792f8f50ab4c986ee3c2f"
    ),
    "registry_csv_sha256": (
        "77fc26814e3c05182b0b13dbb3d162c41536328517a40b4a6713d7d6e9bad8fd"
    ),
    "source_map_34_sha256": (
        "f568beb0aaf35507c035ae008b09c576ef7361f735a3e249230d253e3e2eeb1e"
    ),
    "v2_analysis_pack_sha256": (
        "75b2048911202790cfbeef4632e2c07cb8d89d123eb57ecddbf599eae44e6927"
    ),
}
_PACK_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "strategy_count",
        "v1_count",
        "v2_count",
        "trading_approval",
        "canonical_sources",
        "artifacts",
        "family_specs",
        "identities",
    }
)
_ARTIFACT_KEYS = frozenset(
    {"artifact_id", "relative_path", "role", "sha256", "size_bytes"}
)
_FAMILY_SPEC_KEYS = frozenset(
    {
        "evaluator_key",
        "input_schema_version",
        "artifact_ids",
        "semantic_contract",
        "spec_sha256",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "strategy_id",
        "registry_index",
        "version",
        "spec_id",
        "evaluator_key",
        "input_schema_version",
        "parent_strategy_id",
        "parity_artifact_id",
        "schedule",
    }
)
_EXPECTED_INPUT_SCHEMAS = frozenset(
    {
        "BTC_STRATEGY_HISTORICAL_INPUT_V1",
        "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1",
        "BTC_STRATEGY_VOL_OVERLAY_INPUT_V1",
    }
)
_EXPECTED_V2_MODES = {"VOL_CONFIRMATION": 20_000, "VOL_VETO_ONLY": 0}


@dataclass(frozen=True, slots=True)
class FrozenRuleArtifact:
    artifact_id: str
    relative_path: str
    role: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StrategyFamilySpec:
    spec_id: str
    evaluator_key: str
    input_schema_version: str
    artifact_ids: tuple[str, ...]
    spec_sha256: str
    _semantic_contract_json: str

    @property
    def semantic_contract(self) -> dict[str, Any]:
        return _decoded_object(self._semantic_contract_json)


@dataclass(frozen=True, slots=True)
class StrategyRuleSpec:
    strategy_id: str
    registry_index: int
    version: str
    spec_id: str
    evaluator_key: str
    input_schema_version: str
    parent_strategy_id: str | None
    parity_artifact_id: str
    _schedule_json: str

    @property
    def schedule(self) -> dict[str, Any]:
        return _decoded_object(self._schedule_json)


@dataclass(frozen=True, slots=True)
class StrategyRulePack:
    schema_version: str
    status: str
    strategy_count: int
    v1_count: int
    v2_count: int
    trading_approval: bool
    artifacts: tuple[FrozenRuleArtifact, ...]
    family_specs: tuple[StrategyFamilySpec, ...]
    rules: tuple[StrategyRuleSpec, ...]

    def artifact(self, artifact_id: str) -> FrozenRuleArtifact:
        for artifact in self.artifacts:
            if artifact.artifact_id == artifact_id:
                return artifact
        raise KeyError(artifact_id)

    def family_spec(self, spec_id: str) -> StrategyFamilySpec:
        for spec in self.family_specs:
            if spec.spec_id == spec_id:
                return spec
        raise KeyError(spec_id)


def expected_strategy_rule_pack_sha256() -> str:
    return _EXPECTED_RULE_PACK_SHA256


def load_strategy_rule_pack(
    rule_pack_path: Path,
    *,
    lock_path: Path,
    registry_json_path: Path,
    registry_csv_path: Path,
) -> StrategyRulePack:
    for name, value in (
        ("rule_pack_path", rule_pack_path),
        ("lock_path", lock_path),
        ("registry_json_path", registry_json_path),
        ("registry_csv_path", registry_csv_path),
    ):
        if not isinstance(value, Path):
            _fail("INVALID_STRATEGY_RULE_TYPE", f"{name} must be Path")

    pack_bytes = rule_pack_path.read_bytes()
    actual_pack_sha256 = hashlib.sha256(pack_bytes).hexdigest()
    if actual_pack_sha256 != _EXPECTED_RULE_PACK_SHA256:
        _fail(
            "STRATEGY_RULE_PACK_HASH_MISMATCH",
            f"expected={_EXPECTED_RULE_PACK_SHA256} actual={actual_pack_sha256}",
        )

    verification = verify_registry_lock(
        lock_path, registry_json_path, registry_csv_path
    )
    payload = _parse_json(pack_bytes)
    _require_exact_keys(payload, _PACK_KEYS, "pack")
    _validate_pack_header(payload)
    _validate_canonical_sources(
        _required(payload, "canonical_sources", dict, "pack"),
        lock_path=lock_path,
        registry_json_path=registry_json_path,
        registry_csv_path=registry_csv_path,
    )

    artifacts = _parse_artifacts(
        _required(payload, "artifacts", list, "pack"), rule_pack_path.parent
    )
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    family_specs = _parse_family_specs(
        _required(payload, "family_specs", dict, "pack"), artifacts_by_id
    )
    family_specs_by_id = {spec.spec_id: spec for spec in family_specs}
    rules = _parse_identities(
        _required(payload, "identities", list, "pack"),
        family_specs_by_id,
        artifacts_by_id,
    )

    observed_ids = tuple(rule.strategy_id for rule in rules)
    if observed_ids != verification.ordered_strategy_ids:
        _fail(
            "STRATEGY_RULE_IDENTITY_MISMATCH",
            "identity order/set differs from registry lock",
        )
    if len(set(observed_ids)) != _EXPECTED_COUNTS[0]:
        _fail("STRATEGY_RULE_IDENTITY_MISMATCH", "duplicate identity")

    registry = _parse_json(registry_json_path.read_bytes())
    registry_rows = _required(registry, "strategies", list, "registry")
    registry_versions = {
        _required(row, "strategy_id", str, "registry.row"): _required(
            row, "version", str, "registry.row"
        )
        for row in registry_rows
    }
    by_id = {rule.strategy_id: rule for rule in rules}
    for rule in rules:
        if registry_versions.get(rule.strategy_id) != rule.version:
            _fail(
                "STRATEGY_RULE_VERSION_MISMATCH",
                f"{rule.strategy_id}: {rule.version}",
            )
        if rule.version == "V1":
            if rule.parent_strategy_id is not None:
                _fail(
                    "STRATEGY_RULE_PARENT_MISMATCH",
                    f"{rule.strategy_id}: V1 parent must be null",
                )
        elif rule.version == "V2":
            _validate_v2_identity(rule, by_id)
        else:
            _fail(
                "STRATEGY_RULE_VERSION_MISMATCH",
                f"{rule.strategy_id}: unsupported version",
            )

    return StrategyRulePack(
        schema_version=_EXPECTED_SCHEMA_VERSION,
        status=_EXPECTED_STATUS,
        strategy_count=_EXPECTED_COUNTS[0],
        v1_count=_EXPECTED_COUNTS[1],
        v2_count=_EXPECTED_COUNTS[2],
        trading_approval=False,
        artifacts=artifacts,
        family_specs=family_specs,
        rules=rules,
    )


def _validate_pack_header(payload: dict[str, Any]) -> None:
    schema = _required(payload, "schema_version", str, "pack")
    status = _required(payload, "status", str, "pack")
    counts = tuple(
        _required(payload, name, int, "pack")
        for name in ("strategy_count", "v1_count", "v2_count")
    )
    trading_approval = _required(payload, "trading_approval", bool, "pack")
    if schema != _EXPECTED_SCHEMA_VERSION or status != _EXPECTED_STATUS:
        _fail(
            "STRATEGY_RULE_PACK_CONTRACT_MISMATCH",
            f"schema={schema!r} status={status!r}",
        )
    if counts != _EXPECTED_COUNTS:
        _fail("STRATEGY_RULE_COUNT_MISMATCH", f"counts={counts}")
    if trading_approval:
        _fail("STRATEGY_RULE_TRADING_APPROVAL_FORBIDDEN", "must remain false")


def _validate_canonical_sources(
    value: dict[str, Any],
    *,
    lock_path: Path,
    registry_json_path: Path,
    registry_csv_path: Path,
) -> None:
    _require_exact_keys(
        value, frozenset(_EXPECTED_CANONICAL_SOURCES), "canonical_sources"
    )
    for field, expected in _EXPECTED_CANONICAL_SOURCES.items():
        observed = _required(value, field, str, "canonical_sources")
        if observed != expected:
            _fail(
                "STRATEGY_RULE_PROVENANCE_HASH_MISMATCH",
                f"{field}: expected={expected} observed={observed}",
            )
    for field, path in (
        ("registry_lock_sha256", lock_path),
        ("registry_json_sha256", registry_json_path),
        ("registry_csv_sha256", registry_csv_path),
    ):
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != _EXPECTED_CANONICAL_SOURCES[field]:
            _fail(
                "STRATEGY_RULE_PROVENANCE_HASH_MISMATCH",
                f"{field}: actual={actual}",
            )


def _parse_artifacts(
    values: list[Any], frozen_root: Path
) -> tuple[FrozenRuleArtifact, ...]:
    parsed: list[FrozenRuleArtifact] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    resolved_root = frozen_root.resolve(strict=True)
    for index, value in enumerate(values):
        source = f"artifacts[{index}]"
        _require_exact_keys(value, _ARTIFACT_KEYS, source)
        artifact_id = _nonempty_string(value, "artifact_id", source)
        relative_path = _nonempty_string(value, "relative_path", source)
        role = _nonempty_string(value, "role", source)
        sha256_value = _nonempty_string(value, "sha256", source)
        size_bytes = _required(value, "size_bytes", int, source)
        _validate_sha256(sha256_value, f"{source}.sha256")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or relative_path != relative.as_posix():
            _fail(
                "STRATEGY_RULE_ARTIFACT_PATH_FORBIDDEN",
                f"{artifact_id}: {relative_path!r}",
            )
        if artifact_id in seen_ids or relative_path in seen_paths:
            _fail("STRATEGY_RULE_ARTIFACT_DUPLICATE", artifact_id)
        artifact_path = (frozen_root / relative).resolve(strict=True)
        if artifact_path == resolved_root or resolved_root not in artifact_path.parents:
            _fail("STRATEGY_RULE_ARTIFACT_PATH_FORBIDDEN", artifact_id)
        artifact_bytes = artifact_path.read_bytes()
        actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        if len(artifact_bytes) != size_bytes or actual_sha256 != sha256_value:
            _fail(
                "STRATEGY_RULE_ARTIFACT_HASH_MISMATCH",
                f"{artifact_id}: expected={sha256_value}/{size_bytes} actual={actual_sha256}/{len(artifact_bytes)}",
            )
        seen_ids.add(artifact_id)
        seen_paths.add(relative_path)
        parsed.append(
            FrozenRuleArtifact(
                artifact_id=artifact_id,
                relative_path=relative_path,
                role=role,
                sha256=sha256_value,
                size_bytes=size_bytes,
            )
        )
    if not parsed:
        _fail("STRATEGY_RULE_ARTIFACT_MISSING", "artifact catalog is empty")
    return tuple(parsed)


def _parse_family_specs(
    values: dict[str, Any], artifacts: dict[str, FrozenRuleArtifact]
) -> tuple[StrategyFamilySpec, ...]:
    parsed: list[StrategyFamilySpec] = []
    for spec_id, value in values.items():
        if type(spec_id) is not str or not spec_id:
            _fail("INVALID_STRATEGY_RULE_VALUE", "family spec ID")
        source = f"family_specs.{spec_id}"
        _require_exact_keys(value, _FAMILY_SPEC_KEYS, source)
        evaluator_key = _nonempty_string(value, "evaluator_key", source)
        input_schema = _nonempty_string(value, "input_schema_version", source)
        if input_schema not in _EXPECTED_INPUT_SCHEMAS:
            _fail("STRATEGY_RULE_INPUT_SCHEMA_MISMATCH", spec_id)
        artifact_ids_value = _required(value, "artifact_ids", list, source)
        artifact_ids = tuple(
            _exact_nonempty_string(item, f"{source}.artifact_ids")
            for item in artifact_ids_value
        )
        if not artifact_ids or len(set(artifact_ids)) != len(artifact_ids):
            _fail("STRATEGY_RULE_ARTIFACT_MISSING", spec_id)
        for artifact_id in artifact_ids:
            if artifact_id not in artifacts:
                _fail(
                    "STRATEGY_RULE_ARTIFACT_MISSING",
                    f"{spec_id}: {artifact_id}",
                )
        semantic_contract = _required(value, "semantic_contract", dict, source)
        semantic_json = _canonical_json(semantic_contract)
        expected_spec_sha256 = _nonempty_string(value, "spec_sha256", source)
        actual_spec_sha256 = hashlib.sha256(semantic_json.encode("utf-8")).hexdigest()
        if expected_spec_sha256 != actual_spec_sha256:
            _fail(
                "STRATEGY_RULE_SPEC_HASH_MISMATCH",
                f"{spec_id}: expected={expected_spec_sha256} actual={actual_spec_sha256}",
            )
        parsed.append(
            StrategyFamilySpec(
                spec_id=spec_id,
                evaluator_key=evaluator_key,
                input_schema_version=input_schema,
                artifact_ids=artifact_ids,
                spec_sha256=expected_spec_sha256,
                _semantic_contract_json=semantic_json,
            )
        )
    if not parsed:
        _fail("STRATEGY_RULE_SPEC_MISSING", "family specs are empty")
    return tuple(parsed)


def _parse_identities(
    values: list[Any],
    family_specs: dict[str, StrategyFamilySpec],
    artifacts: dict[str, FrozenRuleArtifact],
) -> tuple[StrategyRuleSpec, ...]:
    if len(values) != _EXPECTED_COUNTS[0]:
        _fail("STRATEGY_RULE_COUNT_MISMATCH", f"identities={len(values)}")
    parsed: list[StrategyRuleSpec] = []
    for index, value in enumerate(values, start=1):
        source = f"identities[{index - 1}]"
        _require_exact_keys(value, _IDENTITY_KEYS, source)
        strategy_id = _nonempty_string(value, "strategy_id", source)
        registry_index = _required(value, "registry_index", int, source)
        version = _nonempty_string(value, "version", source)
        spec_id = _nonempty_string(value, "spec_id", source)
        evaluator_key = _nonempty_string(value, "evaluator_key", source)
        input_schema = _nonempty_string(value, "input_schema_version", source)
        parent = _required(value, "parent_strategy_id", (str, type(None)), source)
        parity_artifact_id = _nonempty_string(value, "parity_artifact_id", source)
        schedule = _required(value, "schedule", dict, source)
        if registry_index != index:
            _fail(
                "STRATEGY_RULE_IDENTITY_MISMATCH",
                f"{strategy_id}: registry_index={registry_index} expected={index}",
            )
        family_spec = family_specs.get(spec_id)
        if family_spec is None:
            _fail("STRATEGY_RULE_SPEC_MISSING", f"{strategy_id}: {spec_id}")
        if (
            evaluator_key != family_spec.evaluator_key
            or input_schema != family_spec.input_schema_version
        ):
            _fail("STRATEGY_RULE_SPEC_MISMATCH", strategy_id)
        parity_artifact = artifacts.get(parity_artifact_id)
        if parity_artifact is None or parity_artifact.role != "IMMUTABLE_TRADE_LEDGER":
            _fail(
                "STRATEGY_RULE_ARTIFACT_MISSING",
                f"{strategy_id}: parity={parity_artifact_id}",
            )
        parsed.append(
            StrategyRuleSpec(
                strategy_id=strategy_id,
                registry_index=registry_index,
                version=version,
                spec_id=spec_id,
                evaluator_key=evaluator_key,
                input_schema_version=input_schema,
                parent_strategy_id=parent,
                parity_artifact_id=parity_artifact_id,
                _schedule_json=_canonical_json(schedule),
            )
        )
    return tuple(parsed)


def _validate_v2_identity(
    rule: StrategyRuleSpec, rules_by_id: dict[str, StrategyRuleSpec]
) -> None:
    parent = rules_by_id.get(rule.parent_strategy_id or "")
    if parent is None or parent.version != "V1":
        _fail(
            "STRATEGY_RULE_PARENT_MISMATCH",
            f"{rule.strategy_id}: {rule.parent_strategy_id!r}",
        )
    schedule = rule.schedule
    mode = schedule.get("overlay_mode")
    threshold = schedule.get("threshold_micros")
    if (
        type(mode) is not str
        or mode not in _EXPECTED_V2_MODES
        or type(threshold) is not int
        or threshold != _EXPECTED_V2_MODES.get(mode)
    ):
        _fail("STRATEGY_RULE_OVERLAY_MISMATCH", rule.strategy_id)


def _parse_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite {constant}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("INVALID_STRATEGY_RULE_JSON") from exc
    if type(value) is not dict:
        _fail("INVALID_STRATEGY_RULE_TYPE", "top-level value must be object")
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_STRATEGY_RULE_JSON") from exc


def _decoded_object(payload_json: str) -> dict[str, Any]:
    value = json.loads(payload_json)
    if type(value) is not dict:
        raise AssertionError("validated payload is not an object")
    return value


def _required(
    value: dict[str, Any],
    key: str,
    expected_type: type[Any] | tuple[type[Any], ...],
    source: str,
) -> Any:
    item = value.get(key)
    allowed_types = expected_type if type(expected_type) is tuple else (expected_type,)
    if type(item) not in allowed_types:
        _fail(
            "INVALID_STRATEGY_RULE_TYPE",
            f"{source}.{key} has type {type(item).__name__}",
        )
    return item


def _nonempty_string(value: dict[str, Any], key: str, source: str) -> str:
    return _exact_nonempty_string(_required(value, key, str, source), f"{source}.{key}")


def _exact_nonempty_string(value: Any, source: str) -> str:
    if type(value) is not str or not value:
        _fail("INVALID_STRATEGY_RULE_VALUE", f"{source} must be non-empty str")
    return value


def _require_exact_keys(
    value: Any, expected_keys: frozenset[str], source: str
) -> None:
    if type(value) is not dict:
        _fail("INVALID_STRATEGY_RULE_TYPE", f"{source} must be object")
    observed = frozenset(value)
    if observed != expected_keys:
        _fail(
            "STRATEGY_RULE_PACK_CONTRACT_MISMATCH",
            f"{source} missing={sorted(expected_keys - observed)} extra={sorted(observed - expected_keys)}",
        )


def _validate_sha256(value: str, source: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        _fail("INVALID_STRATEGY_RULE_SHA256", source)


def _fail(code: str, detail: str) -> None:
    raise ValueError(f"{code}: {detail}")
