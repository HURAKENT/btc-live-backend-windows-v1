from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from src.models import SourceEvent, payload_sha256
from src.market_discovery import _bounds_from_canonical_labels


@dataclass(frozen=True, slots=True)
class CatchupMarketEvidence:
    market_id: str
    payload_json: str
    payload_sha256: str
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class GammaCatchupProjection:
    markets: tuple[CatchupMarketEvidence, ...]
    events: tuple[SourceEvent, ...]


@dataclass(frozen=True, slots=True)
class GammaDailyRangeInventory:
    events: tuple[Mapping[str, Any], ...]
    complete: bool


async def fetch_gamma_daily_range_inventory(
    *,
    session: Any,
    gamma_events_url: str,
    start_date: str,
    end_date: str,
    max_pages_per_state: int = 8,
) -> GammaDailyRangeInventory:
    _dates(start_date, end_date)
    if not isinstance(gamma_events_url, str) or not gamma_events_url:
        raise ValueError("INVALID_GAMMA_CATCHUP_URL")
    if type(max_pages_per_state) is not int or max_pages_per_state <= 0:
        raise ValueError("INVALID_GAMMA_CATCHUP_PAGE_BOUND")
    collected: list[Mapping[str, Any]] = []
    for closed in (True, False):
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(max_pages_per_state):
            params: dict[str, object] = {
                "ascending": "true",
                "closed": str(closed).lower(),
                "end_date_max": f"{end_date}T23:59:59Z",
                "end_date_min": f"{start_date}T00:00:00Z",
                "limit": 500,
                "order": "endDate",
                "title_search": "Bitcoin price on",
            }
            if cursor is not None:
                params["after_cursor"] = cursor
            async with session.get(
                gamma_events_url,
                params=params,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"GAMMA_CATCHUP_HTTP_ERROR:{response.status}")
                payload = await response.json()
            if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
                raise ValueError("INVALID_GAMMA_CATCHUP_RESPONSE")
            if any(not isinstance(item, Mapping) for item in payload["events"]):
                raise ValueError("INVALID_GAMMA_CATCHUP_RESPONSE")
            collected.extend(payload["events"])
            next_cursor = payload.get("next_cursor")
            if next_cursor in (None, ""):
                break
            if not isinstance(next_cursor, str):
                raise ValueError("INVALID_GAMMA_CATCHUP_CURSOR")
            if next_cursor in seen:
                raise ValueError("GAMMA_CATCHUP_CURSOR_LOOP")
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            raise ValueError("GAMMA_CATCHUP_PAGE_BOUND_EXCEEDED")
    unique: dict[str, Mapping[str, Any]] = {}
    for event in collected:
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("INVALID_GAMMA_CATCHUP_EVENT_ID")
        previous = unique.get(event_id)
        if previous is not None and _canonical(previous) != _canonical(event):
            raise ValueError("GAMMA_CATCHUP_EVENT_CONFLICT")
        unique[event_id] = event
    return GammaDailyRangeInventory(tuple(unique.values()), True)


def project_gamma_daily_range_inventory(
    *,
    events: Sequence[Mapping[str, Any]],
    start_date: str,
    end_date: str,
    observed_at_ms: int,
    inventory_complete: bool,
) -> GammaCatchupProjection:
    dates = _dates(start_date, end_date)
    if type(observed_at_ms) is not int or observed_at_ms <= 0:
        raise ValueError("INVALID_GAMMA_CATCHUP_TIMESTAMP")
    if type(inventory_complete) is not bool:
        raise ValueError("INVALID_GAMMA_CATCHUP_COMPLETENESS")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise ValueError("INVALID_GAMMA_CATCHUP_EVENTS")

    by_date: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("INVALID_GAMMA_CATCHUP_EVENT")
        market_date = _event_market_date(event)
        returned_end_date = datetime.fromisoformat(
            _iso_utc(event.get("endDate"))
        ).date().isoformat()
        if market_date is None:
            if returned_end_date in dates:
                raise ValueError("GAMMA_DAILY_RANGE_MARKET_DATE_UNPARSEABLE")
            continue
        if market_date != returned_end_date:
            raise ValueError("GAMMA_DAILY_RANGE_MARKET_DATE_CONFLICT")
        if market_date not in dates:
            continue
        if market_date in by_date:
            raise ValueError("GAMMA_DAILY_RANGE_DATE_AMBIGUOUS")
        by_date[market_date] = event

    markets: list[CatchupMarketEvidence] = []
    source_events: list[SourceEvent] = []
    for market_date in dates:
        event = by_date.get(market_date)
        if event is None:
            if inventory_complete:
                source_events.append(_absence_event(market_date, observed_at_ms))
            continue
        market, resolution = _project_event(event, market_date, observed_at_ms)
        markets.append(market)
        if resolution is not None:
            source_events.append(resolution)
    return GammaCatchupProjection(tuple(markets), tuple(source_events))


def _project_event(
    event: Mapping[str, Any], market_date: str, observed_at_ms: int
) -> tuple[CatchupMarketEvidence, SourceEvent | None]:
    event_id = _required_string(event.get("id"), "GAMMA_DAILY_RANGE_EVENT_ID")
    closed = event.get("closed")
    active = event.get("active")
    if type(closed) is not bool or type(active) is not bool:
        raise ValueError("GAMMA_DAILY_RANGE_STATE_INVALID")
    raw_markets = event.get("markets")
    if not isinstance(raw_markets, Sequence) or isinstance(raw_markets, (str, bytes)) or len(raw_markets) != 11:
        raise ValueError("GAMMA_DAILY_RANGE_MARKETS_INCOMPLETE")

    market_ids: list[str] = []
    condition_ids: list[str] = []
    asset_ids: list[str] = []
    outcomes: list[str] = []
    winners: list[str] = []
    fee_schedules: list[Mapping[str, Any]] = []
    fee_schedule_count = 0
    for raw in raw_markets:
        if not isinstance(raw, Mapping):
            raise ValueError("GAMMA_DAILY_RANGE_MARKET_INVALID")
        if raw.get("closed") is not closed or type(raw.get("active")) is not bool:
            raise ValueError("GAMMA_DAILY_RANGE_MARKET_STATE_INVALID")
        market_ids.append(_required_string(raw.get("id"), "GAMMA_DAILY_RANGE_MARKET_ID"))
        condition_ids.append(_required_string(raw.get("conditionId"), "GAMMA_DAILY_RANGE_CONDITION_ID"))
        title = _required_string(raw.get("groupItemTitle"), "GAMMA_DAILY_RANGE_BUCKET_TITLE")
        outcomes.append(title)
        if _string_array(raw.get("outcomes"), "GAMMA_DAILY_RANGE_OUTCOMES") != ["Yes", "No"]:
            raise ValueError("GAMMA_DAILY_RANGE_OUTCOMES_INVALID")
        tokens = _string_array(raw.get("clobTokenIds"), "GAMMA_DAILY_RANGE_TOKENS")
        if len(tokens) != 2:
            raise ValueError("GAMMA_DAILY_RANGE_TOKENS_INVALID")
        asset_ids.extend(tokens)
        fee_schedule = raw.get("feeSchedule")
        if fee_schedule is not None:
            if not isinstance(fee_schedule, Mapping):
                raise ValueError("GAMMA_DAILY_RANGE_FEE_SCHEDULE_INVALID")
            fee_schedules.append(dict(fee_schedule))
            fee_schedule_count += 1
        if closed:
            prices = _decimal_array(raw.get("outcomePrices"))
            if prices == [Decimal(1), Decimal(0)]:
                winners.append(title)
            elif prices != [Decimal(0), Decimal(1)]:
                raise ValueError("GAMMA_DAILY_RANGE_SETTLEMENT_NOT_FINAL")
    for values, error in (
        (market_ids, "GAMMA_DAILY_RANGE_MARKET_IDS_AMBIGUOUS"),
        (condition_ids, "GAMMA_DAILY_RANGE_CONDITIONS_AMBIGUOUS"),
        (asset_ids, "GAMMA_DAILY_RANGE_TOKENS_AMBIGUOUS"),
        (outcomes, "GAMMA_DAILY_RANGE_BUCKETS_AMBIGUOUS"),
    ):
        if len(values) != len(set(values)):
            raise ValueError(error)

    identity = {
        "asset_ids": asset_ids,
        "bucket_bounds": [list(item) for item in _bounds_from_canonical_labels(outcomes)],
        "condition_ids": condition_ids,
        "event_id": event_id,
        "event_slug": _required_string(event.get("slug"), "GAMMA_DAILY_RANGE_EVENT_SLUG"),
        "fee_schedules": fee_schedules if fee_schedule_count == 11 else [],
        "market_date": market_date,
        "market_ids": market_ids,
        "outcomes": outcomes,
        "resolution_utc": _iso_utc(event.get("endDate")),
    }
    if fee_schedule_count not in (0, 11):
        raise ValueError("GAMMA_DAILY_RANGE_FEE_SCHEDULE_INCOMPLETE")
    payload_json = _canonical(identity)
    market = CatchupMarketEvidence(
        market_id=event_id,
        payload_json=payload_json,
        payload_sha256=payload_sha256(payload_json),
        observed_at_ms=observed_at_ms,
    )
    if not closed:
        return market, None
    if len(winners) != 1:
        raise ValueError("GAMMA_DAILY_RANGE_WINNER_AMBIGUOUS")
    resolution_payload = {
        "market_date": market_date,
        "market_id": event_id,
        "resolved": True,
        "source": "GAMMA_PUBLIC_EVENT_INVENTORY",
        "winning_bucket_count": 1,
        "winning_bucket_identity": winners[0],
    }
    resolution_json = _canonical(resolution_payload)
    updated = event.get("updatedAt")
    revision_identity = updated if isinstance(updated, str) and updated else payload_sha256(resolution_json)
    source_timestamp_ms = _timestamp_ms(updated) or _timestamp_ms(event.get("endDate"))
    if source_timestamp_ms is None:
        raise ValueError("GAMMA_DAILY_RANGE_SOURCE_TIMESTAMP_MISSING")
    resolution = SourceEvent(
        source="polymarket",
        natural_key=f"polymarket:daily-range-resolution:{event_id}:{revision_identity}",
        source_timestamp_ms=source_timestamp_ms,
        received_timestamp_ms=observed_at_ms,
        event_type="POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
        payload_json=resolution_json,
        payload_sha256=payload_sha256(resolution_json),
        recovery_origin="RECOVERED",
    )
    return market, resolution


def _absence_event(market_date: str, observed_at_ms: int) -> SourceEvent:
    payload = {
        "market_date": market_date,
        "reason_code": "COMPLETE_GAMMA_DAILY_RANGE_INVENTORY_NO_MATCH",
        "source": "GAMMA_PUBLIC_EVENT_INVENTORY",
    }
    payload_json = _canonical(payload)
    return SourceEvent(
        source="polymarket",
        natural_key=f"polymarket:daily-range-absence:{market_date}",
        source_timestamp_ms=int(
            datetime.fromisoformat(f"{market_date}T23:59:59+00:00").timestamp()
            * 1000
        ),
        received_timestamp_ms=observed_at_ms,
        event_type="POLYMARKET_DAILY_RANGE_MARKET_ABSENT",
        payload_json=payload_json,
        payload_sha256=payload_sha256(payload_json),
        recovery_origin="RECOVERED",
    )


def _event_market_date(event: Mapping[str, Any]) -> str | None:
    ticker = event.get("ticker")
    slug = event.get("slug")
    for value in (ticker, slug):
        if not isinstance(value, str):
            continue
        prefix = "bitcoin-price-on-"
        if value.lower().startswith(prefix):
            suffix = value[len(prefix):]
            try:
                return date.fromisoformat(suffix).isoformat()
            except ValueError:
                match = re.fullmatch(
                    r"(january|february|march|april|may|june|july|august|"
                    r"september|october|november|december)-(\d{1,2})-(\d{4})",
                    suffix.lower(),
                )
                if match is not None:
                    months = (
                        "january", "february", "march", "april", "may", "june",
                        "july", "august", "september", "october", "november", "december",
                    )
                    try:
                        return date(
                            int(match.group(3)),
                            months.index(match.group(1)) + 1,
                            int(match.group(2)),
                        ).isoformat()
                    except ValueError:
                        pass
    return None


def _dates(start: str, end: str) -> tuple[str, ...]:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError("INVALID_GAMMA_CATCHUP_DATE_RANGE") from None
    if first.isoformat() != start or last.isoformat() != end or last < first:
        raise ValueError("INVALID_GAMMA_CATCHUP_DATE_RANGE")
    result = []
    while first <= last:
        result.append(first.isoformat())
        first += timedelta(days=1)
    return tuple(result)


def _required_string(value: object, error: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(error)
    return value


def _string_array(value: object, error: str) -> list[str]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        raise ValueError(error) from None
    if not isinstance(decoded, list) or any(not isinstance(item, str) or not item for item in decoded):
        raise ValueError(error)
    return decoded


def _decimal_array(value: object) -> list[Decimal]:
    raw = _string_array(value, "GAMMA_DAILY_RANGE_PRICES_INVALID")
    try:
        values = [Decimal(item) for item in raw]
    except (InvalidOperation, ValueError):
        raise ValueError("GAMMA_DAILY_RANGE_PRICES_INVALID") from None
    if len(values) != 2 or any(not item.is_finite() for item in values):
        raise ValueError("GAMMA_DAILY_RANGE_PRICES_INVALID")
    return values


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _iso_utc(value: object) -> str:
    raw = _required_string(value, "GAMMA_DAILY_RANGE_END_DATE")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("GAMMA_DAILY_RANGE_END_DATE") from None
    if parsed.tzinfo is None:
        raise ValueError("GAMMA_DAILY_RANGE_END_DATE")
    return parsed.astimezone(timezone.utc).isoformat()


def _timestamp_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)
