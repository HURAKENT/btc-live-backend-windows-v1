from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


_CURRENT_IDENTIFIER = re.compile(
    r"^bitcoin-price-on-(?:"
    r"\d{4}-\d{2}-\d{2}|"
    r"(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)-\d{1,2}-\d{4}"
    r")$"
)
_LEGACY_IDENTIFIER = re.compile(r"^BTC-DAILY-RANGE-\d{4}-\d{2}-\d{2}$")
_ISO_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_MONTH_NAME_DATE = re.compile(
    r"bitcoin-price-on-"
    r"(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)-(\d{1,2})-(\d{4})",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    name: index
    for index, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
_LESS_THAN_BUCKET = re.compile(r"^<\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
_GREATER_THAN_BUCKET = re.compile(r"^>\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
_RANGE_BUCKET = re.compile(
    r"^([0-9][0-9,]*(?:\.[0-9]+)?)\s*-\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)$"
)


@dataclass(frozen=True, slots=True)
class MarketIdentity:
    event_id: str
    event_slug: str
    active: bool
    resolution_utc: datetime
    market_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    asset_ids: tuple[str, ...]
    market_date: str | None = None
    condition_ids: tuple[str, ...] = ()
    bucket_bounds: tuple[tuple[float | None, float | None], ...] = ()
    fee_schedules: tuple[dict[str, Any], ...] = ()


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


def _market_date(ticker: str, slug: str) -> str | None:
    for candidate in (ticker, slug):
        match = _ISO_DATE.search(candidate)
        if match is not None:
            try:
                return date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                return None
        month_match = _MONTH_NAME_DATE.fullmatch(candidate)
        if month_match is not None:
            try:
                return date(
                    int(month_match.group(3)),
                    _MONTH_NUMBERS[month_match.group(1).lower()],
                    int(month_match.group(2)),
                ).isoformat()
            except ValueError:
                return None
    return None


def _optional_bound(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if type(value) not in (str, int, float) or type(value) is bool:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_TYPE: {field}")
    try:
        parsed = float(value)
    except ValueError:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_SHAPE: {field}") from None
    if not parsed > 0:
        raise ValueError(f"INVALID_MARKET_DISCOVERY_SHAPE: {field}")
    return parsed


def _label_number(value: str) -> float:
    parsed = float(value.replace(",", ""))
    if not parsed > 0:
        raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
    return parsed


def _bounds_from_canonical_labels(
    labels: list[str],
) -> list[tuple[float | None, float | None]]:
    if len(labels) != 11:
        raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
    first = _LESS_THAN_BUCKET.fullmatch(labels[0].strip())
    last = _GREATER_THAN_BUCKET.fullmatch(labels[-1].strip())
    if first is None or last is None:
        raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
    bounds: list[tuple[float | None, float | None]] = [
        (None, _label_number(first.group(1)))
    ]
    for label in labels[1:-1]:
        match = _RANGE_BUCKET.fullmatch(label.strip())
        if match is None:
            raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
        lower = _label_number(match.group(1))
        upper = _label_number(match.group(2))
        if lower >= upper:
            raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
        bounds.append((lower, upper))
    bounds.append((_label_number(last.group(1)), None))
    if any(bounds[index - 1][1] != bounds[index][0] for index in range(1, 11)):
        raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
    return bounds


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
    condition_ids: list[str] = []
    bucket_bounds: list[tuple[float | None, float | None]] = []
    fee_schedules: list[dict[str, Any]] = []
    metadata_count = 0
    bounds_count = 0
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
        metadata_fields = ("conditionId" in market, "feeSchedule" in market)
        bound_fields = ("lowerBound" in market, "upperBound" in market)
        if any(metadata_fields) or any(bound_fields):
            if not all(metadata_fields) or any(bound_fields) != all(bound_fields):
                raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
            condition_id = _require_type(
                market.get("conditionId"), str, f"event.markets[{index}].conditionId"
            )
            fee_schedule = _require_type(
                market.get("feeSchedule"), dict, f"event.markets[{index}].feeSchedule"
            )
            if not condition_id:
                raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
            condition_ids.append(condition_id)
            fee_schedules.append(dict(fee_schedule))
            metadata_count += 1
            if all(bound_fields):
                bucket_bounds.append(
                    (
                        _optional_bound(market.get("lowerBound"), f"event.markets[{index}].lowerBound"),
                        _optional_bound(market.get("upperBound"), f"event.markets[{index}].upperBound"),
                    )
                )
                bounds_count += 1

    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("DUPLICATE_MARKET_ASSET_ID")
    if len(set(market_ids)) != len(market_ids):
        raise ValueError("DUPLICATE_MARKET_ID")
    if metadata_count not in (0, 11) or bounds_count not in (0, 11):
        raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
    if metadata_count == 11 and bounds_count == 0:
        bucket_bounds = _bounds_from_canonical_labels(bucket_outcomes)

    return MarketIdentity(
        event_id=event_id,
        event_slug=slug,
        active=True,
        resolution_utc=resolution,
        market_ids=tuple(market_ids),
        outcomes=tuple(bucket_outcomes),
        asset_ids=tuple(asset_ids),
        market_date=_market_date(ticker, slug),
        condition_ids=tuple(condition_ids),
        bucket_bounds=tuple(bucket_bounds),
        fee_schedules=tuple(fee_schedules),
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


def discover_btc_daily_range_candidates(
    payload: list[dict[str, Any]],
    *,
    now_utc: datetime,
) -> tuple[MarketIdentity, ...]:
    _require_type(payload, list, "payload")
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None:
        raise ValueError("INVALID_DISCOVERY_CLOCK")
    now_utc = now_utc.astimezone(timezone.utc)

    by_event_id: dict[str, MarketIdentity | None] = {}
    for index, event in enumerate(payload):
        _require_type(event, dict, f"payload[{index}]")
        event_id = _require_type(event.get("id"), str, f"payload[{index}].id")
        candidate = _candidate_identity(event, now_utc=now_utc)
        if event_id in by_event_id:
            if by_event_id[event_id] != candidate:
                raise ValueError("MARKET_ROLLOVER_IDENTITY_CONFLICT")
            continue
        by_event_id[event_id] = candidate

    candidates = sorted(
        (
            candidate
            for candidate in by_event_id.values()
            if candidate is not None
        ),
        key=lambda candidate: (
            candidate.resolution_utc,
            candidate.event_id,
        ),
    )
    if not candidates:
        raise ValueError("BTC_DAILY_RANGE_DATA_GAP")
    return tuple(candidates)
