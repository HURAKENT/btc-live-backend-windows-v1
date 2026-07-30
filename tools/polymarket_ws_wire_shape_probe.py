from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parents[1]
    project_root_text = str(project_root)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)

import aiohttp
from aiohttp import WSMsgType

from src.market_discovery import discover_active_btc_daily_range
from src.polymarket_provider import MARKET_WEBSOCKET_URL


# Kept in sync with C1 production composition in src/runtime_orchestrator.py.
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events/keyset"
SUBSCRIPTION_STATIC_FIELDS = {
    "type": "market",
    "custom_feature_enabled": True,
}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_FRAME_BYTES = 1_048_576
MAX_TOTAL_BYTES = 4_194_304
HARD_MAX_FRAMES = 10
DEFAULT_MAX_FRAMES = 10
MIN_MAX_FRAMES = 1
HARD_TIMEOUT_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 30
MIN_TIMEOUT_SECONDS = 5
IMPORT_PERFORMED_NETWORK_IO = False
SQLITE_USED = False

EXIT_ESTABLISHED = 0
EXIT_INSUFFICIENT = 2
EXIT_BLOCKED = 3
EXIT_CONFIGURATION = 30
EXIT_SECURITY = 40
EXIT_INTERNAL = 50

_TRANSPORT_TYPES = {
    "TEXT",
    "BINARY",
    "PING",
    "PONG",
    "CLOSE",
    "CLOSED",
    "ERROR",
}
_SERVICE_TYPES = {
    "connected",
    "connection",
    "subscribed",
    "subscription",
    "ack",
    "pong",
}
_PASSTHROUGH_MARKET_TYPES = {
    "last_trade_price",
    "best_bid_ask",
    "tick_size_change",
    "market_resolved",
}
_RAW_REPORT_KEYS = {
    "raw_frame",
    "raw_frames",
    "raw_payload",
    "raw_subscription",
    "subscription_json",
}
_LONG_DIGITS = re.compile(r"\d{12,}")
_LONG_HEX = re.compile(r"(?:0x)?[0-9a-fA-F]{32,}")


class ProbeConfigurationError(ValueError):
    pass


class ProbeSecurityError(ValueError):
    pass


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("INVALID_CANONICAL_JSON") from None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity_sha256(kind: str, value: object) -> str:
    return _sha256_bytes(
        kind.encode("ascii") + b"\x00" + _canonical_bytes(value)
    )


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in {int, float}:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    raise ValueError("UNSUPPORTED_JSON_VALUE")


def _reject_json_constant(_value: str) -> None:
    raise ValueError("NON_FINITE_JSON_NUMBER")


def sanitize_discriminator(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or len(value) > 64
        or not value.isprintable()
        or _LONG_DIGITS.search(value)
        or _LONG_HEX.fullmatch(value)
    ):
        return None
    return value


def _is_market_object(value: dict[str, object]) -> bool:
    event_type = value.get("event_type")
    if event_type == "book":
        return {
            "asset_id",
            "timestamp",
            "hash",
            "bids",
            "asks",
        }.issubset(value)
    if event_type == "price_change":
        return {
            "timestamp",
            "price_changes",
        }.issubset(value)
    if event_type in _PASSTHROUGH_MARKET_TYPES:
        return {"asset_id", "timestamp"}.issubset(value)
    return False


def _is_service_object(value: dict[str, object]) -> bool:
    for name in ("event_type", "type"):
        discriminator = sanitize_discriminator(value.get(name))
        if discriminator is not None and discriminator.lower() in _SERVICE_TYPES:
            return True
    return False


def _nested_counts(value: object, *, depth: int = 0) -> tuple[int, int]:
    if depth >= 2:
        return (0, 0)
    objects = 0
    arrays = 0
    children: Sequence[object]
    if type(value) is dict:
        children = list(value.values())
    elif type(value) is list:
        children = value
    else:
        return (0, 0)
    for child in children:
        if type(child) is dict:
            objects += 1
        elif type(child) is list:
            arrays += 1
        nested_objects, nested_arrays = _nested_counts(
            child,
            depth=depth + 1,
        )
        objects += nested_objects
        arrays += nested_arrays
    return objects, arrays


def _object_structure(value: dict[str, object]) -> dict[str, object]:
    fields = sorted(value)
    nested_objects, nested_arrays = _nested_counts(value)
    discriminators = {}
    for field in ("event_type", "type"):
        discriminator = sanitize_discriminator(value.get(field))
        if discriminator is not None:
            discriminators[field] = discriminator
    return {
        "top_level_field_names": fields,
        "top_level_field_count": len(fields),
        "top_level_field_types": {
            field: _json_type(value[field]) for field in fields
        },
        "nested_object_count": nested_objects,
        "nested_array_count": nested_arrays,
        "discriminators": discriminators,
    }


def _array_structure(value: list[object]) -> dict[str, object]:
    types = [_json_type(element) for element in value]
    histogram = dict(sorted(Counter(types).items()))
    objects = [element for element in value if type(element) is dict]
    field_sets = [tuple(sorted(element)) for element in objects]
    distinct_field_sets = sorted(set(field_sets))
    union = sorted({field for fields in field_sets for field in fields})
    intersection = (
        sorted(set(field_sets[0]).intersection(*map(set, field_sets[1:])))
        if field_sets
        else []
    )
    discriminators: dict[str, list[str]] = {}
    for field in ("event_type", "type"):
        values = sorted(
            {
                sanitized
                for element in objects
                if (
                    sanitized := sanitize_discriminator(element.get(field))
                )
                is not None
            }
        )
        if values:
            discriminators[field] = values
    return {
        "array_length": len(value),
        "element_type_histogram": histogram,
        "empty": not value,
        "mixed_type": len(histogram) > 1,
        "object_element_count": len(objects),
        "nested_array_element_count": sum(
            type(element) is list for element in value
        ),
        "null_element_count": sum(element is None for element in value),
        "object_field_union": union,
        "object_field_intersection": intersection,
        "distinct_object_shape_count": len(distinct_field_sets),
        "distinct_object_field_set_sha256": [
            _sha256_bytes(_canonical_bytes(list(fields)))
            for fields in distinct_field_sets
        ],
        "discriminators": discriminators,
    }


def classify_json_structure(payload: object) -> dict[str, object]:
    top_level_type = _json_type(payload)
    if type(payload) is dict:
        if _is_market_object(payload):
            classification = "SINGLE_MARKET_OBJECT"
        elif _is_service_object(payload):
            classification = "SINGLE_SERVICE_OBJECT"
        else:
            classification = "SINGLE_UNKNOWN_OBJECT"
        structure = _object_structure(payload)
    elif type(payload) is list:
        structure = _array_structure(payload)
        if not payload:
            classification = "ARRAY_EMPTY"
        elif len(structure["element_type_histogram"]) > 1:
            classification = "ARRAY_MIXED"
        elif all(type(element) is dict for element in payload):
            if all(_is_market_object(element) for element in payload):
                classification = "ARRAY_OF_MARKET_OBJECTS"
            elif all(_is_service_object(element) for element in payload):
                classification = "ARRAY_OF_SERVICE_OBJECTS"
            else:
                classification = "ARRAY_OF_OBJECTS_UNKNOWN"
        else:
            classification = "ARRAY_MIXED"
    else:
        classification = "JSON_PRIMITIVE"
        structure = {"primitive_type": top_level_type}
    structural_hash = _sha256_bytes(
        _canonical_bytes(
            {
                "classification": classification,
                "structure": structure,
                "top_level_json_type": top_level_type,
            }
        )
    )
    return {
        "top_level_json_type": top_level_type,
        "classification": classification,
        "structure": structure,
        "structural_summary_sha256": structural_hash,
    }


def summarize_ws_frame(
    *,
    sequence: int,
    transport_type: str,
    raw_bytes: bytes,
    monotonic_offset_ms: int,
) -> dict[str, object]:
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("INVALID_FRAME_SEQUENCE")
    if transport_type not in _TRANSPORT_TYPES:
        raise ValueError("INVALID_TRANSPORT_TYPE")
    if type(raw_bytes) is not bytes:
        raise ValueError("INVALID_FRAME_BYTES")
    if type(monotonic_offset_ms) is not int or monotonic_offset_ms < 0:
        raise ValueError("INVALID_MONOTONIC_OFFSET")
    if len(raw_bytes) > MAX_FRAME_BYTES:
        raise ValueError("FRAME_BYTES_BOUND")

    result: dict[str, object] = {
        "sequence": sequence,
        "monotonic_offset_ms": monotonic_offset_ms,
        "transport_type": transport_type,
        "byte_length": len(raw_bytes),
        "raw_frame_sha256": _sha256_bytes(raw_bytes),
        "json_decode_status": "NOT_APPLICABLE",
        "top_level_json_type": None,
    }
    if transport_type == "TEXT":
        try:
            payload = json.loads(
                raw_bytes.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            result.update(
                {
                    "classification": "NON_JSON_TEXT",
                    "json_decode_status": "ERROR",
                    "decode_error_class": type(error).__name__,
                }
            )
        else:
            result.update(classify_json_structure(payload))
            result["json_decode_status"] = "PASS"
    elif transport_type == "BINARY":
        result["classification"] = "BINARY_FRAME"
    elif transport_type in {"PING", "PONG", "CLOSE", "CLOSED"}:
        result["classification"] = "CONTROL_FRAME"
    else:
        result["classification"] = "ERROR_FRAME"

    if "structural_summary_sha256" not in result:
        result["structural_summary_sha256"] = _sha256_bytes(
            _canonical_bytes(
                {
                    "classification": result["classification"],
                    "json_decode_status": result["json_decode_status"],
                    "top_level_json_type": result["top_level_json_type"],
                    "transport_type": transport_type,
                }
            )
        )
    return result


def validate_bounds(*, max_frames: int, timeout_seconds: int) -> None:
    if (
        type(max_frames) is not int
        or not MIN_MAX_FRAMES <= max_frames <= HARD_MAX_FRAMES
    ):
        raise ValueError("INVALID_MAX_FRAMES")
    if (
        type(timeout_seconds) is not int
        or not MIN_TIMEOUT_SECONDS
        <= timeout_seconds
        <= HARD_TIMEOUT_SECONDS
    ):
        raise ValueError("INVALID_TIMEOUT_SECONDS")


def enforce_frame_bounds(
    *,
    frame_count: int,
    frame_bytes: int,
    total_bytes: int,
    max_frames: int,
) -> None:
    validate_bounds(
        max_frames=max_frames,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )
    if type(frame_count) is not int or frame_count > max_frames:
        raise ValueError("FRAME_COUNT_BOUND")
    if (
        type(frame_bytes) is not int
        or frame_bytes < 0
        or frame_bytes > MAX_FRAME_BYTES
    ):
        raise ValueError("FRAME_BYTES_BOUND")
    if (
        type(total_bytes) is not int
        or total_bytes < 0
        or total_bytes > MAX_TOTAL_BYTES
    ):
        raise ValueError("TOTAL_BYTES_BOUND")


def _is_market_classification(value: object) -> bool:
    return value in {
        "SINGLE_MARKET_OBJECT",
        "ARRAY_OF_MARKET_OBJECTS",
    }


def build_probe_report(
    *,
    probe_metadata: dict[str, object],
    discovery_summary: dict[str, object],
    subscription_summary: dict[str, object],
    frames: list[dict[str, object]],
    counters: dict[str, int],
) -> dict[str, object]:
    max_frames = probe_metadata.get("max_frames", DEFAULT_MAX_FRAMES)
    timeout_seconds = probe_metadata.get(
        "timeout_seconds",
        DEFAULT_TIMEOUT_SECONDS,
    )
    validate_bounds(
        max_frames=max_frames,
        timeout_seconds=timeout_seconds,
    )
    market_frames = [
        frame
        for frame in frames
        if _is_market_classification(frame.get("classification"))
    ]
    json_frames = [
        frame
        for frame in frames
        if frame.get("json_decode_status") == "PASS"
    ]
    selected = discovery_summary.get("selected") is True
    discovery_exact = (
        selected
        and discovery_summary.get("market_count") == 11
        and discovery_summary.get("asset_count") == 22
        and discovery_summary.get("subscribed_asset_count") == 2
    )
    established = discovery_exact and bool(json_frames) and bool(market_frames)
    if established:
        status = "POLYMARKET_WS_WIRE_SHAPE_ESTABLISHED"
        remaining: list[str] = []
    elif frames and counters.get("websocket_connections") == 1:
        status = "POLYMARKET_WS_WIRE_SHAPE_INSUFFICIENT"
        remaining = ["MARKET_DATA_CONTAINER_NOT_OBSERVED"]
    else:
        status = "POLYMARKET_WS_WIRE_SHAPE_PROBE_BLOCKED"
        remaining = ["DISCOVERY_CONNECTION_OR_FRAME_EVIDENCE_MISSING"]

    first_market = market_frames[0] if market_frames else None
    observed_compatible = (
        first_market is not None
        and first_market.get("classification") == "SINGLE_MARKET_OBJECT"
    )
    report: dict[str, object] = {
        "schema_version": "POLYMARKET_WS_WIRE_SHAPE_PROBE_V1",
        "probe_id": probe_metadata["probe_id"],
        "generated_at_utc": probe_metadata["generated_at_utc"],
        "source_commit": probe_metadata["source_commit"],
        "python_version": probe_metadata["python_version"],
        "aiohttp_version": probe_metadata["aiohttp_version"],
        "scope": {
            "public_read_only": True,
            "manual_windows_run": True,
            "codex_network_run": False,
            "backend_started": False,
            "task14_started": False,
            "sqlite_used": False,
            "auth_used": False,
            "order_surface_used": False,
            "wallet_surface_used": False,
        },
        "bounds": {
            "gamma_discovery_sequences": counters.get(
                "gamma_discovery_sequences",
                0,
            ),
            "websocket_connections": counters.get(
                "websocket_connections",
                0,
            ),
            "subscriptions_sent": counters.get("subscriptions_sent", 0),
            "frames_received": counters.get("frames_received", 0),
            "inbound_bytes": counters.get("inbound_bytes", 0),
            "max_frames": max_frames,
            "timeout_seconds": timeout_seconds,
            "reconnect_attempts": counters.get("reconnect_attempts", 0),
        },
        "endpoint_summary": {
            "gamma_host": "gamma-api.polymarket.com",
            "gamma_path_class": "/events/keyset",
            "websocket_host": "ws-subscriptions-clob.polymarket.com",
            "websocket_path_class": "/ws/market",
            "tls": True,
            "authenticated": False,
        },
        "discovery": dict(discovery_summary),
        "subscription": dict(subscription_summary),
        "frames": frames,
        "observed_transport_types": sorted(
            {
                frame["transport_type"]
                for frame in frames
                if "transport_type" in frame
            }
        ),
        "observed_top_level_json_types": sorted(
            {
                frame["top_level_json_type"]
                for frame in json_frames
                if frame.get("top_level_json_type") is not None
            }
        ),
        "observed_classifications": sorted(
            {
                frame["classification"]
                for frame in frames
                if "classification" in frame
            }
        ),
        "first_json_frame_classification": (
            None if not json_frames else json_frames[0]["classification"]
        ),
        "first_market_data_candidate_classification": (
            None if first_market is None else first_market["classification"]
        ),
        "current_parser_contract": {
            "accepted_top_level_json_types": ["object"],
            "observed_market_data_compatible": observed_compatible,
            "mismatch_reason": (
                None
                if first_market is None or observed_compatible
                else "MARKET_DATA_TOP_LEVEL_CONTAINER_IS_NOT_OBJECT"
            ),
        },
        "conclusion": {
            "exact_wire_container_established": established,
            "likely_required_parser_change": (
                "NOT_INDICATED"
                if observed_compatible
                else (
                    "REVIEW_ARRAY_CONTAINER_SUPPORT"
                    if first_market is not None
                    else "INSUFFICIENT_EVIDENCE"
                )
            ),
            "remaining_data_gaps": remaining,
            "status": status,
        },
        "network_counters": {
            **counters,
            "clob_book_requests": 0,
            "price_history_requests": 0,
            "automatic_retries": 0,
        },
        "security_counters": {
            "raw_frames_persisted": 0,
            "raw_identifiers_persisted": 0,
            "authentication_uses": 0,
            "order_surface_uses": 0,
            "wallet_surface_uses": 0,
        },
    }
    report["report_sha256"] = _sha256_bytes(_canonical_bytes(report))
    return report


def canonical_report_bytes(report: dict[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("INVALID_REPORT_JSON") from None


def verify_report_sha256(report: dict[str, object]) -> bool:
    expected = report.get("report_sha256")
    if type(expected) is not str:
        return False
    unhashed = dict(report)
    del unhashed["report_sha256"]
    return _sha256_bytes(_canonical_bytes(unhashed)) == expected


def _walk_report(value: object):
    if type(value) is dict:
        for key, child in value.items():
            yield key, child
            yield from _walk_report(child)
    elif type(value) is list:
        for child in value:
            yield from _walk_report(child)


def validate_report_security(
    *,
    report: dict[str, object],
    forbidden_identifiers: set[str],
    raw_frames: list[bytes],
) -> None:
    if not verify_report_sha256(report):
        raise ProbeSecurityError("INVALID_REPORT_SHA256")
    serialized = canonical_report_bytes(report)
    text = serialized.decode("utf-8")
    for key, _value in _walk_report(report):
        if key in _RAW_REPORT_KEYS:
            raise ProbeSecurityError(f"RAW_REPORT_FIELD: {key}")
    for identifier in forbidden_identifiers:
        if identifier and identifier in text:
            raise ProbeSecurityError("RAW_IDENTIFIER")
    for raw_frame in raw_frames:
        if not raw_frame:
            continue
        try:
            raw_text = raw_frame.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if raw_text and raw_text in text:
            raise ProbeSecurityError("RAW_FRAME_PERSISTED")


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("INVALID_SOURCE_COMMIT")
    return commit


def _transport_name(message_type: WSMsgType) -> str:
    mapping = {
        WSMsgType.TEXT: "TEXT",
        WSMsgType.BINARY: "BINARY",
        WSMsgType.PING: "PING",
        WSMsgType.PONG: "PONG",
        WSMsgType.CLOSE: "CLOSE",
        WSMsgType.CLOSED: "CLOSED",
        WSMsgType.ERROR: "ERROR",
    }
    return mapping.get(message_type, "ERROR")


def _message_bytes(message: aiohttp.WSMessage) -> bytes:
    if message.type == WSMsgType.TEXT:
        return message.data.encode("utf-8")
    if message.type == WSMsgType.BINARY:
        return bytes(message.data)
    if type(message.data) is bytes:
        return message.data
    if type(message.data) is str:
        return message.data.encode("utf-8")
    return b""


def _empty_discovery() -> dict[str, object]:
    return {
        "selected": False,
        "event_identity_sha256": None,
        "market_count": 0,
        "asset_count": 0,
        "selected_market_identity_sha256": None,
        "selected_market_order_index": None,
        "subscribed_asset_count": 0,
        "subscribed_asset_set_sha256": None,
        "token_role_count": {"YES": 0, "NO": 0},
    }


async def run_probe(
    *,
    max_frames: int = DEFAULT_MAX_FRAMES,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, object]]:
    validate_bounds(
        max_frames=max_frames,
        timeout_seconds=timeout_seconds,
    )
    started = time.monotonic()
    metadata = {
        "probe_id": (
            f"POLYMARKET-WS-PROBE-"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:8].upper()}"
        ),
        "generated_at_utc": datetime.now(UTC).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "source_commit": _source_commit(),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "aiohttp_version": aiohttp.__version__,
        "max_frames": max_frames,
        "timeout_seconds": timeout_seconds,
    }
    counters = {
        "gamma_discovery_sequences": 0,
        "websocket_connections": 0,
        "subscriptions_sent": 0,
        "frames_received": 0,
        "inbound_bytes": 0,
        "reconnect_attempts": 0,
    }
    discovery = _empty_discovery()
    subscription_summary: dict[str, object] = {
        "sent": False,
        "asset_count": 0,
        "payload_sha256": None,
    }
    frames: list[dict[str, object]] = []
    raw_frames: list[bytes] = []
    forbidden_identifiers: set[str] = set()

    timeout = aiohttp.ClientTimeout(
        total=timeout_seconds,
        connect=min(10, timeout_seconds),
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            ) as session:
                if GAMMA_EVENTS_URL != (
                    "https://gamma-api.polymarket.com/events/keyset"
                ):
                    raise ProbeSecurityError("GAMMA_ALLOWLIST_VIOLATION")
                now = datetime.now(UTC)
                counters["gamma_discovery_sequences"] = 1
                async with session.get(
                    GAMMA_EVENTS_URL,
                    params={
                        "closed": "false",
                        "title_search": "Bitcoin price on",
                        "end_date_min": (now - timedelta(hours=6)).isoformat(),
                        "end_date_max": (now + timedelta(days=14)).isoformat(),
                        "order": "endDate",
                        "ascending": "true",
                        "limit": 500,
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"GAMMA_DISCOVERY_HTTP_{response.status}"
                        )
                    payload = await response.json()
                if type(payload) is not dict or type(payload.get("events")) is not list:
                    raise ValueError("INVALID_GAMMA_DISCOVERY_RESPONSE")
                identity = discover_active_btc_daily_range(
                    payload["events"],
                    now_utc=datetime.now(UTC),
                )
                if (
                    len(identity.market_ids) != 11
                    or len(set(identity.market_ids)) != 11
                    or len(identity.asset_ids) != 22
                    or len(set(identity.asset_ids)) != 22
                ):
                    raise ValueError("INCOMPLETE_MARKET_IDENTITY")
                selected_assets = list(identity.asset_ids[:2])
                forbidden_identifiers.update(
                    {
                        identity.event_id,
                        *identity.market_ids,
                        *identity.asset_ids,
                    }
                )
                discovery = {
                    "selected": True,
                    "event_identity_sha256": _identity_sha256(
                        "event",
                        identity.event_id,
                    ),
                    "market_count": len(identity.market_ids),
                    "asset_count": len(identity.asset_ids),
                    "selected_market_identity_sha256": _identity_sha256(
                        "market",
                        identity.market_ids[0],
                    ),
                    "selected_market_order_index": 0,
                    "subscribed_asset_count": len(selected_assets),
                    "subscribed_asset_set_sha256": _identity_sha256(
                        "asset-set",
                        selected_assets,
                    ),
                    "token_role_count": {"YES": 1, "NO": 1},
                }
                subscription = {
                    "assets_ids": selected_assets,
                    **SUBSCRIPTION_STATIC_FIELDS,
                }
                subscription_summary = {
                    "sent": False,
                    "asset_count": 2,
                    "payload_sha256": _identity_sha256(
                        "subscription",
                        subscription,
                    ),
                }

                if MARKET_WEBSOCKET_URL != (
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
                ):
                    raise ProbeSecurityError("WEBSOCKET_ALLOWLIST_VIOLATION")
                counters["websocket_connections"] = 1
                async with session.ws_connect(
                    MARKET_WEBSOCKET_URL,
                    max_msg_size=MAX_FRAME_BYTES,
                    autoping=True,
                    heartbeat=None,
                ) as websocket:
                    await websocket.send_json(subscription)
                    counters["subscriptions_sent"] = 1
                    subscription_summary["sent"] = True
                    while counters["frames_received"] < max_frames:
                        remaining = timeout_seconds - (
                            time.monotonic() - started
                        )
                        if remaining <= 0:
                            break
                        message = await asyncio.wait_for(
                            websocket.receive(),
                            timeout=remaining,
                        )
                        transport_type = _transport_name(message.type)
                        raw = _message_bytes(message)
                        next_count = counters["frames_received"] + 1
                        next_total = counters["inbound_bytes"] + len(raw)
                        enforce_frame_bounds(
                            frame_count=next_count,
                            frame_bytes=len(raw),
                            total_bytes=next_total,
                            max_frames=max_frames,
                        )
                        counters["frames_received"] = next_count
                        counters["inbound_bytes"] = next_total
                        raw_frames.append(raw)
                        summary = summarize_ws_frame(
                            sequence=next_count,
                            transport_type=transport_type,
                            raw_bytes=raw,
                            monotonic_offset_ms=int(
                                (time.monotonic() - started) * 1000
                            ),
                        )
                        frames.append(summary)
                        if _is_market_classification(
                            summary["classification"]
                        ):
                            break
                        if transport_type in {"CLOSE", "CLOSED", "ERROR"}:
                            break
    except ProbeSecurityError:
        raise
    except (
        TimeoutError,
        asyncio.TimeoutError,
        aiohttp.ClientError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        report = build_probe_report(
            probe_metadata=metadata,
            discovery_summary=discovery,
            subscription_summary=subscription_summary,
            frames=frames,
            counters=counters,
        )
        validate_report_security(
            report=report,
            forbidden_identifiers=forbidden_identifiers,
            raw_frames=raw_frames,
        )
        return EXIT_BLOCKED, report

    report = build_probe_report(
        probe_metadata=metadata,
        discovery_summary=discovery,
        subscription_summary=subscription_summary,
        frames=frames,
        counters=counters,
    )
    validate_report_security(
        report=report,
        forbidden_identifiers=forbidden_identifiers,
        raw_frames=raw_frames,
    )
    status = report["conclusion"]["status"]
    if status == "POLYMARKET_WS_WIRE_SHAPE_ESTABLISHED":
        return EXIT_ESTABLISHED, report
    if status == "POLYMARKET_WS_WIRE_SHAPE_INSUFFICIENT":
        return EXIT_INSUFFICIENT, report
    return EXIT_BLOCKED, report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded sanitized Polymarket Market WebSocket wire-shape probe"
        )
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    return parser


def _validate_output_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.suffix != ".json" or not path.name:
        raise ProbeConfigurationError("INVALID_OUTPUT_PATH")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        pass
    else:
        raise ProbeSecurityError("OUTPUT_INSIDE_REPOSITORY")
    if resolved.exists() and not resolved.is_file():
        raise ProbeConfigurationError("OUTPUT_IS_NOT_FILE")
    return resolved


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(arguments)
    except SystemExit as error:
        return 0 if error.code == 0 else EXIT_CONFIGURATION
    try:
        validate_bounds(
            max_frames=args.max_frames,
            timeout_seconds=args.timeout_seconds,
        )
        output = _validate_output_path(args.output)
    except ProbeConfigurationError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIGURATION
    except ProbeSecurityError as error:
        print(str(error), file=sys.stderr)
        return EXIT_SECURITY
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIGURATION

    try:
        exit_code, report = asyncio.run(
            run_probe(
                max_frames=args.max_frames,
                timeout_seconds=args.timeout_seconds,
            )
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical_report_bytes(report))
        print(report["conclusion"]["status"])
        print(f"REPORT={output}")
        return exit_code
    except ProbeSecurityError as error:
        print(f"SECURITY_VIOLATION: {error}", file=sys.stderr)
        return EXIT_SECURITY
    except BaseException as error:
        print(
            f"POLYMARKET_WS_WIRE_SHAPE_INTERNAL_FAILURE: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
