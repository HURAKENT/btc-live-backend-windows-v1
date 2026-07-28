from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class MarketIdentity:
    event_id: str
    event_slug: str
    active: bool
    resolution_utc: datetime
    market_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    asset_ids: tuple[str, ...]


def _require_type(
    value: Any,
    expected_type: type[Any],
    field: str,
) -> Any:
    if type(value) is not expected_type:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_TYPE: {field}")
    return value


def _decode_string_list(value: Any, field: str) -> list[str]:
    _require_type(value, str, field)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_SHAPE: {field}") from None
    _require_type(decoded, list, field)
    if any(type(item) is not str or not item for item in decoded):
        raise ValueError(f"INVALID_MARKET_DISCOVERY_TYPE: {field}")
    return decoded


def _parse_resolution(value: Any) -> datetime:
    _require_type(value, str, "event.endDate")
    try:
        resolution = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("INVALID_MARKET_DISCOVERY_SHAPE: event.endDate") from None
    if resolution.tzinfo is None:
        raise ValueError("INVALID_MARKET_DISCOVERY_SHAPE: event.endDate")
    return resolution


def _candidate_identity(
    event: dict[str, Any],
    *,
    now_utc: datetime,
) -> MarketIdentity | None:
    active = _require_type(event.get("active"), bool, "event.active")
    closed = _require_type(event.get("closed"), bool, "event.closed")
    ticker = _require_type(event.get("ticker"), str, "event.ticker")
    slug = _require_type(event.get("slug"), str, "event.slug")
    if (
        not active
        or closed
        or not ticker.startswith("BTC-DAILY-RANGE-")
        or not slug.startswith("bitcoin-price-on-")
    ):
        return None

    resolution = _parse_resolution(event.get("endDate"))
    if resolution <= now_utc:
        return None
    event_id = _require_type(event.get("id"), str, "event.id")
    markets = _require_type(event.get("markets"), list, "event.markets")
    if len(markets) != 11:
        raise ValueError("INVALID_BTC_DAILY_RANGE_STRUCTURE")

    market_ids: list[str] = []
    bucket_outcomes: list[str] = []
    asset_ids: list[str] = []
    for index, market in enumerate(markets):
        _require_type(market, dict, f"event.markets[{index}]")
        if (
            _require_type(
                market.get("active"),
                bool,
                f"event.markets[{index}].active",
            )
            is not True
            or _require_type(
                market.get("closed"),
                bool,
                f"event.markets[{index}].closed",
            )
            is not False
        ):
            raise ValueError("INVALID_BTC_DAILY_RANGE_STRUCTURE")
        market_ids.append(
            _require_type(
                market.get("id"),
                str,
                f"event.markets[{index}].id",
            )
        )
        bucket_outcomes.append(
            _require_type(
                market.get("groupItemTitle"),
                str,
                f"event.markets[{index}].groupItemTitle",
            )
        )
        yes_no = _decode_string_list(
            market.get("outcomes"),
            f"event.markets[{index}].outcomes",
        )
        tokens = _decode_string_list(
            market.get("clobTokenIds"),
            f"event.markets[{index}].clobTokenIds",
        )
        if yes_no != ["Yes", "No"] or len(tokens) != 2:
            raise ValueError("INVALID_BTC_DAILY_RANGE_STRUCTURE")
        asset_ids.extend(tokens)

    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("DUPLICATE_MARKET_ASSET_ID")
    if len(set(market_ids)) != len(market_ids):
        raise ValueError("DUPLICATE_MARKET_ID")

    return MarketIdentity(
        event_id=event_id,
        event_slug=slug,
        active=True,
        resolution_utc=resolution,
        market_ids=tuple(market_ids),
        outcomes=tuple(bucket_outcomes),
        asset_ids=tuple(asset_ids),
    )


def discover_active_btc_daily_range(
    payload: list[dict[str, Any]],
    *,
    now_utc: datetime,
) -> MarketIdentity:
    _require_type(payload, list, "payload")
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None:
        raise ValueError("INVALID_DISCOVERY_CLOCK")

    candidates: list[MarketIdentity] = []
    for index, event in enumerate(payload):
        _require_type(event, dict, f"payload[{index}]")
        candidate = _candidate_identity(event, now_utc=now_utc)
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        raise ValueError("BTC_DAILY_RANGE_DATA_GAP")
    if len(candidates) != 1:
        raise ValueError("AMBIGUOUS_BTC_DAILY_RANGE")
    return candidates[0]
