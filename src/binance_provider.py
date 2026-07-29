from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from src.models import SourceEvent


BINANCE_REST_BASES = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
)
BINANCE_KLINES_PATH = "/api/v3/klines"
BINANCE_WEBSOCKET_URL = (
    "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
)
BINANCE_SYMBOL = "BTCUSDT"
BINANCE_INTERVAL = "1m"
MINUTE_MS = 60_000

_WS_DECIMAL_FIELDS = ("o", "c", "h", "l", "v", "q", "V", "Q")
_WS_INTEGER_FIELDS = ("t", "T", "f", "L", "n")
_REST_DECIMAL_INDEXES = (1, 2, 3, 4, 5, 7, 9, 10, 11)


def _require_type(
    value: Any,
    expected_type: type[Any],
    field: str,
) -> Any:
    if type(value) is not expected_type:
        raise ValueError(f"BINANCE_INVALID_TYPE: {field}")
    return value


def _reject_floats(value: Any, field: str) -> None:
    if type(value) is float:
        raise ValueError(f"BINANCE_FLOAT_FORBIDDEN: {field}")
    if type(value) is dict:
        for key, child in value.items():
            _reject_floats(child, f"{field}.{key}")
    elif type(value) is list:
        for index, child in enumerate(value):
            _reject_floats(child, f"{field}[{index}]")


def _event_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_decimal_text(value: str, field: str) -> str:
    _require_type(value, str, field)
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"BINANCE_INVALID_DECIMAL: {field}") from None
    if not decimal_value.is_finite():
        raise ValueError(f"BINANCE_INVALID_DECIMAL: {field}")
    if decimal_value == 0:
        return "0"
    canonical = format(decimal_value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def _canonical_closed_kline_payload(
    *,
    open_time_ms: int,
    close_time_ms: int,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
    volume: str,
    quote_asset_volume: str,
    number_of_trades: int,
    taker_buy_base_asset_volume: str,
    taker_buy_quote_asset_volume: str,
) -> dict[str, Any]:
    _require_type(open_time_ms, int, "open_time_ms")
    _require_type(close_time_ms, int, "close_time_ms")
    _require_type(number_of_trades, int, "number_of_trades")
    if open_time_ms < 0 or close_time_ms < open_time_ms or number_of_trades < 0:
        raise ValueError("BINANCE_INVALID_CLOSED_KLINE")
    return {
        "close": _canonical_decimal_text(close_price, "close"),
        "close_time_ms": close_time_ms,
        "high": _canonical_decimal_text(high_price, "high"),
        "interval": BINANCE_INTERVAL,
        "low": _canonical_decimal_text(low_price, "low"),
        "number_of_trades": number_of_trades,
        "open": _canonical_decimal_text(open_price, "open"),
        "open_time_ms": open_time_ms,
        "quote_asset_volume": _canonical_decimal_text(
            quote_asset_volume, "quote_asset_volume"
        ),
        "symbol": BINANCE_SYMBOL,
        "taker_buy_base_asset_volume": _canonical_decimal_text(
            taker_buy_base_asset_volume, "taker_buy_base_asset_volume"
        ),
        "taker_buy_quote_asset_volume": _canonical_decimal_text(
            taker_buy_quote_asset_volume, "taker_buy_quote_asset_volume"
        ),
        "volume": _canonical_decimal_text(volume, "volume"),
    }


def parse_binance_kline_message(
    payload: dict[str, Any],
) -> SourceEvent | None:
    _require_type(payload, dict, "payload")
    kline = _require_type(payload.get("k"), dict, "k")

    for field in _WS_INTEGER_FIELDS:
        _require_type(kline.get(field), int, f"k.{field}")
    for field in _WS_DECIMAL_FIELDS:
        _require_type(kline.get(field), str, f"k.{field}")
    _require_type(kline.get("x"), bool, "k.x")
    _require_type(kline.get("s"), str, "k.s")
    _require_type(kline.get("i"), str, "k.i")
    _require_type(payload.get("E"), int, "E")
    _require_type(payload.get("s"), str, "s")
    _reject_floats(payload, "payload")

    if kline["s"] != BINANCE_SYMBOL or payload["s"] != BINANCE_SYMBOL:
        raise ValueError("BINANCE_UNEXPECTED_SYMBOL")
    if kline["i"] != BINANCE_INTERVAL:
        raise ValueError("BINANCE_UNEXPECTED_INTERVAL")
    if not kline["x"]:
        return None

    canonical_payload = _canonical_closed_kline_payload(
        open_time_ms=kline["t"],
        close_time_ms=kline["T"],
        open_price=kline["o"],
        high_price=kline["h"],
        low_price=kline["l"],
        close_price=kline["c"],
        volume=kline["v"],
        quote_asset_volume=kline["q"],
        number_of_trades=kline["n"],
        taker_buy_base_asset_volume=kline["V"],
        taker_buy_quote_asset_volume=kline["Q"],
    )
    return SourceEvent.binance_closed_kline(
        symbol=BINANCE_SYMBOL,
        interval=BINANCE_INTERVAL,
        open_time_ms=kline["t"],
        payload=_event_payload_bytes(canonical_payload),
        received_timestamp_ms=payload["E"],
        recovery_origin="LIVE",
    )


def _parse_rest_kline(row: Any) -> SourceEvent:
    _require_type(row, list, "rest.kline")
    if len(row) != 12:
        raise ValueError("BINANCE_INVALID_REST_KLINE_SHAPE")
    _require_type(row[0], int, "rest.kline[0]")
    _require_type(row[6], int, "rest.kline[6]")
    _require_type(row[8], int, "rest.kline[8]")
    for index in _REST_DECIMAL_INDEXES:
        _require_type(row[index], str, f"rest.kline[{index}]")
    _reject_floats(row, "rest.kline")

    audit_payload = _canonical_closed_kline_payload(
        open_time_ms=row[0],
        close_time_ms=row[6],
        open_price=row[1],
        high_price=row[2],
        low_price=row[3],
        close_price=row[4],
        volume=row[5],
        quote_asset_volume=row[7],
        number_of_trades=row[8],
        taker_buy_base_asset_volume=row[9],
        taker_buy_quote_asset_volume=row[10],
    )
    return SourceEvent.binance_closed_kline(
        symbol=BINANCE_SYMBOL,
        interval=BINANCE_INTERVAL,
        open_time_ms=row[0],
        payload=_event_payload_bytes(audit_payload),
        received_timestamp_ms=row[6],
        recovery_origin="REST_BACKFILL",
    )


async def _fetch_kline_page(
    session: Any,
    *,
    start_ms: int,
    end_ms: int,
) -> list[Any]:
    params = {
        "symbol": BINANCE_SYMBOL,
        "interval": BINANCE_INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }
    statuses: list[int] = []
    for base in BINANCE_REST_BASES:
        try:
            async with session.get(
                f"{base}{BINANCE_KLINES_PATH}",
                params=params,
            ) as response:
                statuses.append(response.status)
                if response.status != 200:
                    continue
                payload = await response.json()
                _require_type(payload, list, "rest.response")
                return payload
        except (OSError, asyncio.TimeoutError):
            continue
    detail = ",".join(str(status) for status in statuses) or "transport"
    raise ValueError(f"BINANCE_HTTP_ERROR: {detail}")


async def iter_binance_backfill(
    session: Any,
    start_ms: int,
    end_ms: int,
) -> AsyncIterator[SourceEvent]:
    _require_type(start_ms, int, "start_ms")
    _require_type(end_ms, int, "end_ms")
    if start_ms < 0 or end_ms < start_ms:
        raise ValueError("BINANCE_INVALID_BACKFILL_RANGE")
    if start_ms % MINUTE_MS != 0 or end_ms % MINUTE_MS != 0:
        raise ValueError("BINANCE_BACKFILL_RANGE_NOT_MINUTE_ALIGNED")

    next_start_ms = start_ms
    expected_open_time_ms = start_ms
    seen_open_times: set[int] = set()
    while next_start_ms <= end_ms:
        page = await _fetch_kline_page(
            session,
            start_ms=next_start_ms,
            end_ms=end_ms,
        )
        if not page:
            return

        last_closed_open_time: int | None = None
        for raw_row in page:
            event = _parse_rest_kline(raw_row)
            open_time_ms = event.source_timestamp_ms
            if open_time_ms > end_ms:
                continue
            if open_time_ms in seen_open_times:
                raise ValueError("BINANCE_BACKFILL_DUPLICATE_OPEN_TIME")
            if open_time_ms != expected_open_time_ms:
                raise ValueError("BINANCE_BACKFILL_CADENCE_ERROR")
            seen_open_times.add(open_time_ms)
            expected_open_time_ms += MINUTE_MS
            last_closed_open_time = open_time_ms
            yield event

        if last_closed_open_time is None:
            return
        next_start_ms = last_closed_open_time + MINUTE_MS


class ReconnectPolicy:
    def __init__(
        self,
        *,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._random_value = random_value
        self._attempt = 0

    def next_delay(self) -> float:
        base_delay = min(1 * (2**self._attempt), 30)
        self._attempt += 1
        random_value = self._random_value()
        if not 0.0 <= random_value <= 1.0:
            raise ValueError("BINANCE_INVALID_JITTER_SOURCE")
        return base_delay * (0.8 + 0.4 * random_value)

    def record_healthy_duration(self, seconds: float) -> None:
        if seconds >= 300:
            self._attempt = 0


class BinanceStream:
    def __init__(
        self,
        *,
        connect: Callable[[str], Any],
        incident_sink: Callable[[dict[str, str]], None] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        policy: ReconnectPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connect
        self._incident_sink = incident_sink
        self._sleep = sleep
        self._policy = policy or ReconnectPolicy()
        self._monotonic = monotonic

    async def run(
        self,
        buffer: asyncio.Queue[SourceEvent],
        *,
        ready_event: asyncio.Event | None = None,
    ) -> None:
        while True:
            connected_at = self._monotonic()
            try:
                async with self._connect(BINANCE_WEBSOCKET_URL) as websocket:
                    if ready_event is not None:
                        ready_event.set()
                    async for message in websocket:
                        payload = (
                            message
                            if type(message) is dict
                            else message.json()
                        )
                        event = parse_binance_kline_message(payload)
                        if event is not None:
                            await buffer.put(event)
                        self._policy.record_healthy_duration(
                            self._monotonic() - connected_at
                        )
                raise ConnectionError("stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._incident_sink is not None:
                    self._incident_sink(
                        {
                            "severity": "WARNING",
                            "source": "binance",
                            "code": "BINANCE_STREAM_DISCONNECTED",
                            "detail": str(error),
                        }
                    )
                await self._sleep(self._policy.next_delay())
