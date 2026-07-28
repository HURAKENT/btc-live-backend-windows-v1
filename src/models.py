from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Self


def canonical_payload_json(payload: bytes) -> str:
    if type(payload) is not bytes:
        raise ValueError("INVALID_EVENT_PAYLOAD_TYPE")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"INVALID_JSON_CONSTANT: {constant}")
            ),
        )
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("INVALID_EVENT_PAYLOAD_JSON") from None


def payload_sha256(payload_json: str) -> str:
    if type(payload_json) is not str:
        raise ValueError("INVALID_PAYLOAD_JSON_TYPE")
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _require_exact_type(name: str, value: Any, expected_type: type[Any]) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"INVALID_DOMAIN_TYPE: {name}")


def _require_nonnegative_integer(name: str, value: Any) -> None:
    _require_exact_type(name, value, int)
    if value < 0:
        raise ValueError(f"INVALID_DOMAIN_VALUE: {name}")


@dataclass(frozen=True, slots=True)
class SourceEvent:
    source: str
    natural_key: str
    source_timestamp_ms: int
    received_timestamp_ms: int
    event_type: str
    payload_json: str
    payload_sha256: str
    recovery_origin: str

    def __post_init__(self) -> None:
        for name in (
            "source",
            "natural_key",
            "event_type",
            "payload_json",
            "payload_sha256",
            "recovery_origin",
        ):
            _require_exact_type(name, getattr(self, name), str)
            if not getattr(self, name):
                raise ValueError(f"INVALID_DOMAIN_VALUE: {name}")
        _require_nonnegative_integer(
            "source_timestamp_ms", self.source_timestamp_ms
        )
        _require_nonnegative_integer(
            "received_timestamp_ms", self.received_timestamp_ms
        )
        if payload_sha256(self.payload_json) != self.payload_sha256:
            raise ValueError("PAYLOAD_SHA256_MISMATCH")

    @property
    def created_at_ms(self) -> int:
        return self.received_timestamp_ms

    @classmethod
    def binance_closed_kline(
        cls,
        *,
        symbol: str,
        interval: str,
        open_time_ms: int,
        payload: bytes,
        received_timestamp_ms: int | None = None,
        recovery_origin: str = "LIVE",
    ) -> Self:
        _require_exact_type("symbol", symbol, str)
        _require_exact_type("interval", interval, str)
        _require_nonnegative_integer("open_time_ms", open_time_ms)
        if received_timestamp_ms is None:
            received_timestamp_ms = open_time_ms
        canonical_json = canonical_payload_json(payload)
        return cls(
            source="binance",
            natural_key=f"binance:{symbol}:{interval}:{open_time_ms}",
            source_timestamp_ms=open_time_ms,
            received_timestamp_ms=received_timestamp_ms,
            event_type="BINANCE_KLINE_CLOSED",
            payload_json=canonical_json,
            payload_sha256=payload_sha256(canonical_json),
            recovery_origin=recovery_origin,
        )


@dataclass(frozen=True, slots=True)
class CanonicalSnapshot:
    snapshot_key: str
    source_event_ids: tuple[int, ...]
    payload_json: str
    payload_sha256: str
    created_at_ms: int
    recovery_origin: str

    def __post_init__(self) -> None:
        _require_exact_type("snapshot_key", self.snapshot_key, str)
        _require_exact_type("source_event_ids", self.source_event_ids, tuple)
        for event_id in self.source_event_ids:
            _require_nonnegative_integer("source_event_id", event_id)
        _require_exact_type("payload_json", self.payload_json, str)
        _require_exact_type("payload_sha256", self.payload_sha256, str)
        _require_nonnegative_integer("created_at_ms", self.created_at_ms)
        _require_exact_type("recovery_origin", self.recovery_origin, str)
        if payload_sha256(self.payload_json) != self.payload_sha256:
            raise ValueError("PAYLOAD_SHA256_MISMATCH")


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    evaluation_key: str
    strategy_id: str
    strategy_version: str
    status: str
    input_snapshot_hash: str
    evaluation_revision: int
    execution_eligible: bool
    evaluated_at_ms: int
    payload_json: str

    def __post_init__(self) -> None:
        for name in (
            "evaluation_key",
            "strategy_id",
            "strategy_version",
            "status",
            "input_snapshot_hash",
            "payload_json",
        ):
            _require_exact_type(name, getattr(self, name), str)
        _require_nonnegative_integer(
            "evaluation_revision", self.evaluation_revision
        )
        _require_nonnegative_integer("evaluated_at_ms", self.evaluated_at_ms)
        if type(self.execution_eligible) is not bool:
            raise ValueError("INVALID_EXECUTION_ELIGIBILITY_TYPE")


@dataclass(frozen=True, slots=True)
class SignalRecord:
    identity_key: str
    evaluation_key: str
    strategy_id: str
    signal_type: str
    payload_json: str
    created_at_ms: int

    def __post_init__(self) -> None:
        for name in (
            "identity_key",
            "evaluation_key",
            "strategy_id",
            "signal_type",
            "payload_json",
        ):
            _require_exact_type(name, getattr(self, name), str)
            if not getattr(self, name):
                raise ValueError(f"INVALID_DOMAIN_VALUE: {name}")
        _require_nonnegative_integer("created_at_ms", self.created_at_ms)


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    event_id: int
    topic: str
    payload_json: str
    created_at_ms: int

    def __post_init__(self) -> None:
        _require_nonnegative_integer("event_id", self.event_id)
        for name in ("topic", "payload_json"):
            _require_exact_type(name, getattr(self, name), str)
            if not getattr(self, name):
                raise ValueError(f"INVALID_DOMAIN_VALUE: {name}")
        _require_nonnegative_integer("created_at_ms", self.created_at_ms)
