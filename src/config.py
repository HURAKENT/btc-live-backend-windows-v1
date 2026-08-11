from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CONFIG_FIELD_TYPES: tuple[tuple[str, type[Any]], ...] = (
    ("bind_host", str),
    ("bind_port", int),
    ("real_orders_enabled", bool),
    ("wallet_enabled", bool),
    ("paper_enabled", bool),
    ("dashboard_enabled", bool),
    ("database_writer_count", int),
)
_CONFIG_KEYS = frozenset(field for field, _ in _CONFIG_FIELD_TYPES)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    bind_host: str
    bind_port: int
    real_orders_enabled: bool
    wallet_enabled: bool
    paper_enabled: bool
    dashboard_enabled: bool
    database_writer_count: int
    strict_a_current_model_authorized: bool = False


def load_runtime_config(path: Path) -> RuntimeConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CONFIG_TOP_LEVEL_MUST_BE_OBJECT")

    keys = frozenset(payload)
    unknown_keys = sorted(keys - _CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(f"UNKNOWN_CONFIG_KEYS: {','.join(unknown_keys)}")

    missing_keys = sorted(_CONFIG_KEYS - keys)
    if missing_keys:
        raise ValueError(f"MISSING_CONFIG_KEYS: {','.join(missing_keys)}")

    config = RuntimeConfig(
        bind_host=_require_type(payload, "bind_host", str),
        bind_port=_require_type(payload, "bind_port", int),
        real_orders_enabled=_require_type(payload, "real_orders_enabled", bool),
        wallet_enabled=_require_type(payload, "wallet_enabled", bool),
        paper_enabled=_require_type(payload, "paper_enabled", bool),
        dashboard_enabled=_require_type(payload, "dashboard_enabled", bool),
        database_writer_count=_require_type(payload, "database_writer_count", int),
    )
    validate_runtime_config(config)
    return config


def load_versioned_runtime_config(path: Path) -> RuntimeConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CONFIG_TOP_LEVEL_MUST_BE_OBJECT")
    if payload.get("schema_version") != "BTC_DAILY_RANGE_MVP_RUNTIME_V1":
        return load_runtime_config(path)
    required = {
        "schema_version", "bind_host", "bind_port", "real_orders_enabled",
        "wallet_enabled", "paper_enabled", "dashboard_enabled",
        "database_writer_count", "strict_a_current_model_authorized",
        "trading_approval", "signing", "authenticated_clob_writes",
    }
    if set(payload) != required:
        unknown = sorted(set(payload) - required)
        missing = sorted(required - set(payload))
        detail = unknown or missing
        raise ValueError("INVALID_MVP_CONFIG_KEYS: " + ",".join(detail))
    if any(
        payload[key] is not False
        for key in (
            "real_orders_enabled", "wallet_enabled", "trading_approval",
            "signing", "authenticated_clob_writes",
        )
    ):
        raise ValueError("MVP_REAL_MONEY_SURFACE_FORBIDDEN")
    if (
        payload.get("paper_enabled") is not True
        or payload.get("dashboard_enabled") is not True
        or payload.get("strict_a_current_model_authorized") is not True
    ):
        raise ValueError("MVP_RUNTIME_AUTHORIZATION_REQUIRED")
    config = RuntimeConfig(
        bind_host=_require_type(payload, "bind_host", str),
        bind_port=_require_type(payload, "bind_port", int),
        real_orders_enabled=False,
        wallet_enabled=False,
        paper_enabled=True,
        dashboard_enabled=True,
        database_writer_count=_require_type(payload, "database_writer_count", int),
        strict_a_current_model_authorized=True,
    )
    _validate_common_runtime_config(config)
    return config


def validate_runtime_config(config: RuntimeConfig) -> None:
    for field, expected_type in _CONFIG_FIELD_TYPES:
        if type(getattr(config, field)) is not expected_type:
            raise ValueError(f"INVALID_CONFIG_TYPE: {field}")

    if type(config.strict_a_current_model_authorized) is not bool:
        raise ValueError("INVALID_CONFIG_TYPE: strict_a_current_model_authorized")
    _validate_common_runtime_config(config)
    if config.strict_a_current_model_authorized:
        raise ValueError("STRICT_A_CURRENT_MODEL_MUST_BE_DISABLED")
    if config.paper_enabled:
        raise ValueError("PAPER_EXECUTION_MUST_BE_DISABLED")
    if config.dashboard_enabled:
        raise ValueError("DASHBOARD_MUST_BE_DISABLED")


def _validate_common_runtime_config(config: RuntimeConfig) -> None:
    if config.bind_host != "127.0.0.1":
        raise ValueError("BIND_HOST_MUST_BE_LOOPBACK")
    if not 1024 <= config.bind_port <= 65535:
        raise ValueError("BIND_PORT_OUT_OF_RANGE")
    if config.real_orders_enabled:
        raise ValueError("REAL_ORDERS_MUST_BE_DISABLED")
    if config.wallet_enabled:
        raise ValueError("WALLET_MUST_BE_DISABLED")
    if config.database_writer_count != 1:
        raise ValueError("DATABASE_WRITER_COUNT_MUST_BE_ONE")


def _require_type(payload: dict[str, Any], key: str, expected_type: type[Any]) -> Any:
    value = payload[key]
    if type(value) is not expected_type:
        raise ValueError(f"INVALID_CONFIG_TYPE: {key}")
    return value
