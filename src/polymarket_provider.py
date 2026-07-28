from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import WSMsgType

from src.fixed_point import ProbabilityMicros, SharesMicros
from src.models import SourceEvent, payload_sha256


CLOB_BASE_URL = "https://clob.polymarket.com"
MARKET_WEBSOCKET_URL = (
    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)
_SUPPORTED_PASSTHROUGH_EVENTS = frozenset(
    {
        "last_trade_price",
        "best_bid_ask",
        "tick_size_change",
        "market_resolved",
    }
)


def _require_type(
    value: Any,
    expected_type: type[Any],
    field: str,
) -> Any:
    if type(value) is not expected_type:
        raise ValueError(f"POLYMARKET_INVALID_TYPE: {field}")
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
    except (TypeError, ValueError):
        raise ValueError("POLYMARKET_INVALID_PAYLOAD") from None


def _normalize_timestamp_ms(value: Any, field: str) -> int:
    if type(value) is int:
        timestamp_ms = value
    elif (
        type(value) is str
        and value
        and value.isascii()
        and value.isdigit()
    ):
        timestamp_ms = int(value)
    else:
        raise ValueError(f"POLYMARKET_INVALID_TYPE: {field}")
    if timestamp_ms <= 0:
        raise ValueError(f"POLYMARKET_INVALID_TYPE: {field}")
    return timestamp_ms


def _reject_json_constant(_value: str) -> None:
    raise ValueError("POLYMARKET_INVALID_PAYLOAD")


def _decode_history_json(raw_body: bytes) -> Any:
    if type(raw_body) is not bytes:
        raise ValueError("POLYMARKET_INVALID_PAYLOAD")
    try:
        return json.loads(
            raw_body.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("POLYMARKET_INVALID_PAYLOAD") from None


async def _read_history_payload(response: Any) -> Any:
    read = getattr(response, "read", None)
    if callable(read):
        return _decode_history_json(await read())
    return await response.json()


def _normalize_history_price(value: Any, field: str) -> tuple[int, str]:
    if type(value) not in (Decimal, str, int):
        raise ValueError(f"POLYMARKET_INVALID_TYPE: {field}")
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"POLYMARKET_INVALID_TYPE: {field}") from None
    if not decimal_value.is_finite():
        raise ValueError(f"POLYMARKET_INVALID_TYPE: {field}")
    price_micros = ProbabilityMicros.from_decimal(decimal_value).value
    return price_micros, format(decimal_value, "f")


def _source_event(
    *,
    event_type: str,
    natural_key: str,
    timestamp_ms: int,
    payload: dict[str, Any],
    recovery_origin: str = "LIVE",
) -> SourceEvent:
    payload_json = _canonical_json(payload)
    return SourceEvent(
        source="polymarket",
        natural_key=natural_key,
        source_timestamp_ms=timestamp_ms,
        received_timestamp_ms=timestamp_ms,
        event_type=event_type,
        payload_json=payload_json,
        payload_sha256=payload_sha256(payload_json),
        recovery_origin=recovery_origin,
    )


def _parse_levels(levels: Any, field: str) -> list[dict[str, int]]:
    _require_type(levels, list, field)
    normalized: dict[int, int] = {}
    for index, level in enumerate(levels):
        _require_type(level, dict, f"{field}[{index}]")
        price = ProbabilityMicros.from_decimal(
            _require_type(
                level.get("price"),
                str,
                f"{field}[{index}].price",
            )
        ).value
        size = SharesMicros.from_decimal(
            _require_type(
                level.get("size"),
                str,
                f"{field}[{index}].size",
            )
        ).value
        if price in normalized:
            raise ValueError("POLYMARKET_DUPLICATE_BOOK_LEVEL")
        normalized[price] = size
    return [
        {"price_micros": price, "size_micros": normalized[price]}
        for price in sorted(normalized)
    ]


def _parse_book(payload: dict[str, Any]) -> SourceEvent:
    asset_id = _require_type(payload.get("asset_id"), str, "asset_id")
    book_hash = _require_type(payload.get("hash"), str, "hash")
    timestamp_ms = _normalize_timestamp_ms(payload.get("timestamp"), "timestamp")
    bids = _parse_levels(payload.get("bids"), "bids")
    asks = _parse_levels(payload.get("asks"), "asks")

    best_bid = max(
        (level["price_micros"] for level in bids if level["size_micros"] > 0),
        default=None,
    )
    best_ask = min(
        (level["price_micros"] for level in asks if level["size_micros"] > 0),
        default=None,
    )
    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        raise ValueError("POLYMARKET_CROSSED_BOOK")

    normalized = {
        "asset_id": asset_id,
        "asks": asks,
        "best_ask_micros": best_ask,
        "best_bid_micros": best_bid,
        "bids": bids,
        "book_hash": book_hash,
        "event_type": "book",
        "provider_payload": payload,
        "spread_micros": (
            None
            if best_bid is None or best_ask is None
            else best_ask - best_bid
        ),
    }
    return _source_event(
        event_type="POLYMARKET_BOOK",
        natural_key=f"polymarket:{asset_id}:book:{book_hash}",
        timestamp_ms=timestamp_ms,
        payload=normalized,
    )


def _parse_price_changes(payload: dict[str, Any]) -> list[SourceEvent]:
    timestamp_ms = _require_type(payload.get("timestamp"), int, "timestamp")
    changes = _require_type(
        payload.get("price_changes"),
        list,
        "price_changes",
    )
    normalized_changes: list[dict[str, Any]] = []
    for index, change in enumerate(changes):
        _require_type(change, dict, f"price_changes[{index}]")
        asset_id = _require_type(
            change.get("asset_id"),
            str,
            f"price_changes[{index}].asset_id",
        )
        side = _require_type(
            change.get("side"),
            str,
            f"price_changes[{index}].side",
        )
        if side not in {"BUY", "SELL"}:
            raise ValueError("POLYMARKET_INVALID_BOOK_SIDE")
        price_micros = ProbabilityMicros.from_decimal(
            _require_type(
                change.get("price"),
                str,
                f"price_changes[{index}].price",
            )
        ).value
        size_micros = SharesMicros.from_decimal(
            _require_type(
                change.get("size"),
                str,
                f"price_changes[{index}].size",
            )
        ).value
        normalized_changes.append(
            {
                "asset_id": asset_id,
                "hash": _require_type(
                    change.get("hash"),
                    str,
                    f"price_changes[{index}].hash",
                ),
                "price_micros": price_micros,
                "provider_change": change,
                "side": side,
                "size_micros": size_micros,
                "timestamp_ms": timestamp_ms,
            }
        )

    events: list[SourceEvent] = []
    for normalized in sorted(
        normalized_changes,
        key=lambda item: (
            item["asset_id"],
            item["side"],
            item["price_micros"],
            item["hash"],
        ),
    ):
        canonical = _canonical_json(normalized)
        identity_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        events.append(
            _source_event(
                event_type="POLYMARKET_PRICE_CHANGE",
                natural_key=(
                    f"polymarket:{normalized['asset_id']}:"
                    f"price_change:{timestamp_ms}:{identity_hash}"
                ),
                timestamp_ms=timestamp_ms,
                payload=normalized,
            )
        )
    return events


def parse_market_ws_message(
    payload: dict[str, Any],
    *,
    incident_sink: Callable[[dict[str, str]], None] | None = None,
) -> list[SourceEvent]:
    _require_type(payload, dict, "payload")
    event_type = _require_type(
        payload.get("event_type"),
        str,
        "event_type",
    )
    if event_type == "book":
        return [_parse_book(payload)]
    if event_type == "price_change":
        return _parse_price_changes(payload)

    asset_id = _require_type(payload.get("asset_id"), str, "asset_id")
    timestamp_ms = _require_type(payload.get("timestamp"), int, "timestamp")
    canonical = _canonical_json(payload)
    identity_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if event_type in _SUPPORTED_PASSTHROUGH_EVENTS:
        canonical_event_type = f"POLYMARKET_{event_type.upper()}"
    else:
        canonical_event_type = "UNHANDLED_PROVIDER_EVENT"
        if incident_sink is not None:
            incident_sink(
                {
                    "severity": "WARNING",
                    "source": "polymarket",
                    "code": "UNHANDLED_PROVIDER_EVENT",
                    "detail": event_type,
                }
            )
    return [
        _source_event(
            event_type=canonical_event_type,
            natural_key=(
                f"polymarket:{asset_id}:{event_type}:"
                f"{timestamp_ms}:{identity_hash}"
            ),
            timestamp_ms=timestamp_ms,
            payload=payload,
        )
    ]


class MarketBook:
    def __init__(self, asset_id: str) -> None:
        _require_type(asset_id, str, "asset_id")
        self.asset_id = asset_id
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.book_hash: str | None = None
        self.source_timestamp_ms: int | None = None
        self.last_reconciled_event_id: int | None = None

    @property
    def best_bid_micros(self) -> int | None:
        return max(self.bids, default=None)

    @property
    def best_ask_micros(self) -> int | None:
        return min(self.asks, default=None)

    @property
    def spread_micros(self) -> int | None:
        if self.best_bid_micros is None or self.best_ask_micros is None:
            return None
        return self.best_ask_micros - self.best_bid_micros

    def apply(self, event: SourceEvent, *, source_event_id: int) -> None:
        _require_type(source_event_id, int, "source_event_id")
        payload = json.loads(event.payload_json)
        if payload.get("asset_id") != self.asset_id:
            raise ValueError("POLYMARKET_BOOK_ASSET_MISMATCH")

        bids = dict(self.bids)
        asks = dict(self.asks)
        book_hash = self.book_hash
        if event.event_type == "POLYMARKET_BOOK":
            bids = {
                level["price_micros"]: level["size_micros"]
                for level in payload["bids"]
                if level["size_micros"] > 0
            }
            asks = {
                level["price_micros"]: level["size_micros"]
                for level in payload["asks"]
                if level["size_micros"] > 0
            }
            book_hash = payload["book_hash"]
        elif event.event_type == "POLYMARKET_PRICE_CHANGE":
            side = bids if payload["side"] == "BUY" else asks
            if payload["size_micros"] == 0:
                side.pop(payload["price_micros"], None)
            else:
                side[payload["price_micros"]] = payload["size_micros"]
        else:
            raise ValueError("POLYMARKET_EVENT_NOT_BOOK_MUTATION")

        best_bid = max(bids, default=None)
        best_ask = min(asks, default=None)
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            raise ValueError("POLYMARKET_CROSSED_BOOK")
        self.bids = bids
        self.asks = asks
        self.book_hash = book_hash
        self.source_timestamp_ms = event.source_timestamp_ms
        self.last_reconciled_event_id = source_event_id


async def fetch_current_books(
    session: Any,
    asset_ids: Sequence[str],
) -> list[SourceEvent]:
    events: list[SourceEvent] = []
    for asset_id in asset_ids:
        _require_type(asset_id, str, "asset_id")
        async with session.get(
            f"{CLOB_BASE_URL}/book",
            params={"token_id": asset_id},
        ) as response:
            if response.status != 200:
                raise ValueError(f"POLYMARKET_BOOK_HTTP_ERROR: {response.status}")
            payload = await response.json()
        event = _parse_book(_require_type(payload, dict, "book.response"))
        if json.loads(event.payload_json)["asset_id"] != asset_id:
            raise ValueError("POLYMARKET_BOOK_ASSET_MISMATCH")
        events.append(event)
    return events


async def iter_price_history(
    session: Any,
    *,
    asset_id: str,
    start_ts: int,
    end_ts: int,
) -> AsyncIterator[SourceEvent]:
    _require_type(asset_id, str, "asset_id")
    _require_type(start_ts, int, "start_ts")
    _require_type(end_ts, int, "end_ts")
    if start_ts < 0 or end_ts < start_ts:
        raise ValueError("POLYMARKET_INVALID_HISTORY_RANGE")

    next_start = start_ts
    seen: set[int] = set()
    while next_start <= end_ts:
        async with session.get(
            f"{CLOB_BASE_URL}/prices-history",
            params={
                "market": asset_id,
                "startTs": next_start,
                "endTs": end_ts,
                "fidelity": 1,
            },
        ) as response:
            if response.status != 200:
                raise ValueError(
                    f"POLYMARKET_HISTORY_HTTP_ERROR: {response.status}"
                )
            payload = await _read_history_payload(response)
        _require_type(payload, dict, "history.response")
        history = _require_type(payload.get("history"), list, "history")
        if not history:
            return

        last_timestamp: int | None = None
        for index, point in enumerate(history):
            _require_type(point, dict, f"history[{index}]")
            timestamp = _require_type(
                point.get("t"),
                int,
                f"history[{index}].t",
            )
            price, provider_price = _normalize_history_price(
                point.get("p"),
                f"history[{index}].p",
            )
            if timestamp > end_ts:
                continue
            if timestamp in seen or (
                last_timestamp is not None and timestamp <= last_timestamp
            ):
                raise ValueError("POLYMARKET_HISTORY_SEQUENCE_ERROR")
            seen.add(timestamp)
            last_timestamp = timestamp
            normalized = {
                "asset_id": asset_id,
                "event_type": "price_history",
                "price_micros": price,
                "provider_point": {**point, "p": provider_price},
                "timestamp_seconds": timestamp,
            }
            canonical = _canonical_json(normalized)
            identity_hash = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()
            yield _source_event(
                event_type="POLYMARKET_PRICE_HISTORY",
                natural_key=(
                    f"polymarket:{asset_id}:price_history:"
                    f"{timestamp}:{identity_hash}"
                ),
                timestamp_ms=timestamp * 1000,
                payload=normalized,
                recovery_origin="REST_BACKFILL",
            )

        if last_timestamp is None:
            return
        next_start = last_timestamp + 60


class PolymarketStream:
    def __init__(
        self,
        session: Any,
        *,
        websocket_url: str = MARKET_WEBSOCKET_URL,
        heartbeat_interval: float = 10,
        incident_sink: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self._session = session
        self._websocket_url = websocket_url
        self._heartbeat_interval = heartbeat_interval
        self._incident_sink = incident_sink

    async def _heartbeat(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            await websocket.send_str("PING")

    async def run(
        self,
        asset_ids: Sequence[str],
        buffer: asyncio.Queue[SourceEvent],
    ) -> None:
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("DUPLICATE_MARKET_ASSET_ID")
        subscription = {
            "assets_ids": list(asset_ids),
            "type": "market",
            "custom_feature_enabled": True,
        }
        async with self._session.ws_connect(
            self._websocket_url
        ) as websocket:
            await websocket.send_json(subscription)
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for message in websocket:
                    if message.type != WSMsgType.TEXT:
                        continue
                    if message.data == "PONG":
                        continue
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        raise ValueError("POLYMARKET_INVALID_WS_JSON") from None
                    for event in parse_market_ws_message(
                        payload,
                        incident_sink=self._incident_sink,
                    ):
                        await buffer.put(event)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
