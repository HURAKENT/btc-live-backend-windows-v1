from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CONFIG_KEYS = frozenset(
    {
        "bind_host",
        "bind_port",
        "real_orders_enabled",
        "wallet_enabled",
        "paper_enabled",
        "dashboard_enabled",
        "database_writer_count",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    bind_host: str
    bind_port: int
    real_orders_enabled: bool
    wallet_enabled: bool
    paper_enabled: bool
    dashboard_enabled: bool
    database_writer_count: int


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


def validate_runtime_config(config: RuntimeConfig) -> None:
    if config.bind_host != "127.0.0.1":
        raise ValueError("BIND_HOST_MUST_BE_LOOPBACK")
    if not 1024 <= config.bind_port <= 65535:
        raise ValueError("BIND_PORT_OUT_OF_RANGE")
    if config.real_orders_enabled:
        raise ValueError("REAL_ORDERS_MUST_BE_DISABLED")
    if config.wallet_enabled:
        raise ValueError("WALLET_MUST_BE_DISABLED")
    if config.paper_enabled:
        raise ValueError("PAPER_EXECUTION_MUST_BE_DISABLED")
    if config.dashboard_enabled:
        raise ValueError("DASHBOARD_MUST_BE_DISABLED")
    if config.database_writer_count != 1:
        raise ValueError("DATABASE_WRITER_COUNT_MUST_BE_ONE")


def _require_type(payload: dict[str, Any], key: str, expected_type: type[Any]) -> Any:
    value = payload[key]
    if type(value) is not expected_type:
        raise ValueError(f"INVALID_CONFIG_TYPE: {key}")
    return value
