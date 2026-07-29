from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


INTEGRATION_ENDPOINTS_SCHEMA_VERSION = (
    "C1_LOOPBACK_INTEGRATION_ENDPOINTS_V1"
)
_EXPECTED_KEYS = frozenset(
    {
        "schema_version",
        "api_bind_host",
        "api_bind_port",
        "binance_rest_bases",
        "binance_websocket_url",
        "gamma_events_url",
        "polymarket_clob_base_url",
        "polymarket_websocket_url",
    }
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


@dataclass(frozen=True, slots=True)
class IntegrationEndpoints:
    api_bind_host: str
    api_bind_port: int
    binance_rest_bases: tuple[str, ...]
    binance_websocket_url: str
    gamma_events_url: str
    polymarket_clob_base_url: str
    polymarket_websocket_url: str


def load_integration_endpoints(path: Path) -> IntegrationEndpoints:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("INTEGRATION_ENDPOINTS_PATH_NOT_ABSOLUTE")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("INVALID_INTEGRATION_ENDPOINTS_JSON") from None
    if type(payload) is not dict or frozenset(payload) != _EXPECTED_KEYS:
        raise ValueError("INVALID_INTEGRATION_ENDPOINTS_SCHEMA")
    if (
        type(payload["schema_version"]) is not str
        or payload["schema_version"] != INTEGRATION_ENDPOINTS_SCHEMA_VERSION
        or type(payload["api_bind_host"]) is not str
        or payload["api_bind_host"] != "127.0.0.1"
        or type(payload["api_bind_port"]) is not int
        or not 1 <= payload["api_bind_port"] <= 65535
        or type(payload["binance_rest_bases"]) is not list
        or not payload["binance_rest_bases"]
        or any(
            type(value) is not str
            for value in payload["binance_rest_bases"]
        )
    ):
        raise ValueError("INVALID_INTEGRATION_ENDPOINTS_TYPE")

    rest_bases = tuple(
        _validate_url(value, schemes={"http"})
        for value in payload["binance_rest_bases"]
    )
    return IntegrationEndpoints(
        api_bind_host=payload["api_bind_host"],
        api_bind_port=payload["api_bind_port"],
        binance_rest_bases=rest_bases,
        binance_websocket_url=_validate_url(
            payload["binance_websocket_url"],
            schemes={"ws"},
        ),
        gamma_events_url=_validate_url(
            payload["gamma_events_url"],
            schemes={"http"},
        ),
        polymarket_clob_base_url=_validate_url(
            payload["polymarket_clob_base_url"],
            schemes={"http"},
        ),
        polymarket_websocket_url=_validate_url(
            payload["polymarket_websocket_url"],
            schemes={"ws"},
        ),
    )


def _validate_url(value: Any, *, schemes: set[str]) -> str:
    if type(value) is not str or not value:
        raise ValueError("INVALID_INTEGRATION_ENDPOINTS_TYPE")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in schemes
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.port is None
    ):
        raise ValueError("INTEGRATION_ENDPOINT_NOT_LOOPBACK")
    return value
