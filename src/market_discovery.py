from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_CURRENT_IDENTIFIER = re.compile(
    r"^bitcoin-price-on-(?:"
    r"\d{4}-\d{2}-\d{2}|"
    r"(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)-\d{1,2}-\d{4}"
    r")$"
)
_LEGACY_IDENTIFIER = re.compile(r"^BTC-DAILY-RANGE-\d{4}-\d{2}-\d{2}$")


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


def _parse_resolution(value: Any, field: str) -> datetime:
    _require_type(value, str, field)
    try:
        resolution = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_SHAPE: {field}") from None
    if resolution.tzinfo is None:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_SHAPE: {field}")
    return resolution.astimezone(timezone.utc)


def _has_btc_daily_range_identifier(*, ticker: str, slug: str) -> bool:
    return bool(
        _LEGACY_IDENTIFIER.fullmatch(ticker)
        or _CURRENT_IDENTIFIER.fullmatch(ticker.lower())
        or _CURRENT_IDENTIFIER.fullmatch(slug.lower())
    )


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
        or not _has_btc_daily_range_identifier(ticker=ticker, slug=slug)
    ):
        return None

    resolution_field = (
        "event.endDate" if "endDate" in event else "event.resolution"
    )
    resolution = _parse_resolution(
        event.get(resolution_field.removeprefix("event.")),
        resolution_field,
    )
    if resolution <= now_utc:
        return None
    event_id = _require_type(event.get("id"), str, "event.id")
    if not event_id:
        return None
    markets = _require_type(event.get("markets"), list, "event.markets")
    if len(markets) != 11:
        return None

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
            return None
        market_id = _require_type(
            market.get("id"),
            str,
            f"event.markets[{index}].id",
        )
        bucket_outcome = _require_type(
            market.get("groupItemTitle"),
            str,
            f"event.markets[{index}].groupItemTitle",
        )
        if not market_id or not bucket_outcome:
            return None
        market_ids.append(market_id)
        bucket_outcomes.append(bucket_outcome)
        yes_no = _decode_string_list(
            market.get("outcomes"),
            f"event.markets[{index}].outcomes",
        )
        tokens = _decode_string_list(
            market.get("clobTokenIds"),
            f"event.markets[{index}].clobTokenIds",
        )
        if yes_no != ["Yes", "No"] or len(tokens) != 2:
            return None
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
    now_utc = now_utc.astimezone(timezone.utc)

    candidates: list[MarketIdentity] = []
    seen_event_ids: set[str] = set()
    for index, event in enumerate(payload):
        _require_type(event, dict, f"payload[{index}]")
        event_id = _require_type(event.get("id"), str, f"payload[{index}].id")
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        candidate = _candidate_identity(event, now_utc=now_utc)
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        raise ValueError("BTC_DAILY_RANGE_DATA_GAP")
    candidates.sort(
        key=lambda candidate: (
            candidate.resolution_utc,
            candidate.event_id,
        )
    )
    nearest_resolution = candidates[0].resolution_utc
    if sum(
        candidate.resolution_utc == nearest_resolution
        for candidate in candidates
    ) != 1:
        raise ValueError("AMBIGUOUS_BTC_DAILY_RANGE")
    return candidates[0]
