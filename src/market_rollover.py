from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from src.market_discovery import (
    MarketIdentity,
    discover_btc_daily_range_candidates,
)


ROLLOVER_SEQUENCE = (
    "CURRENT_LIVE",
    "NEXT_DISCOVERED",
    "NEXT_BUFFERING",
    "NEXT_RECONCILED",
    "CUTOVER_COMMITTED",
    "CURRENT_LIVE",
)


@dataclass(frozen=True, slots=True)
class MarketPair:
    current: MarketIdentity
    next: MarketIdentity

    def __post_init__(self) -> None:
        if type(self.current) is not MarketIdentity:
            raise ValueError("INVALID_CURRENT_MARKET_IDENTITY")
        if type(self.next) is not MarketIdentity:
            raise ValueError("INVALID_NEXT_MARKET_IDENTITY")
        if self.current.event_id == self.next.event_id:
            raise ValueError("CURRENT_NEXT_MARKET_IDENTITY_COLLISION")
        if self.current.resolution_utc >= self.next.resolution_utc:
            raise ValueError("INVALID_CURRENT_NEXT_MARKET_ORDER")


@dataclass(frozen=True, slots=True)
class C3RolloverSummary:
    status: str
    old_market_identity_sha256: str
    new_market_identity_sha256: str
    market_count: int
    asset_count: int
    current_book_count: int
    history_completed_asset_count: int
    buffered_event_count: int
    drained_event_count: int
    active_subscription_count: int
    writer_consumer_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "active_subscription_count": self.active_subscription_count,
            "asset_count": self.asset_count,
            "buffered_event_count": self.buffered_event_count,
            "current_book_count": self.current_book_count,
            "drained_event_count": self.drained_event_count,
            "history_completed_asset_count": (
                self.history_completed_asset_count
            ),
            "market_count": self.market_count,
            "new_market_identity_sha256": (
                self.new_market_identity_sha256
            ),
            "old_market_identity_sha256": (
                self.old_market_identity_sha256
            ),
            "status": self.status,
            "trading_approval": False,
            "writer_consumer_count": self.writer_consumer_count,
        }


def build_market_pair(
    payload: list[dict],
    *,
    now_utc: datetime,
) -> MarketPair:
    candidates = discover_btc_daily_range_candidates(
        payload,
        now_utc=now_utc,
    )
    if len(candidates) < 2:
        raise ValueError("NEXT_BTC_DAILY_RANGE_DATA_GAP")
    if candidates[0].resolution_utc == candidates[1].resolution_utc:
        raise ValueError("AMBIGUOUS_CURRENT_MARKET_IDENTITY")
    if (
        len(candidates) > 2
        and candidates[1].resolution_utc == candidates[2].resolution_utc
    ):
        raise ValueError("AMBIGUOUS_NEXT_MARKET_IDENTITY")
    return MarketPair(current=candidates[0], next=candidates[1])


def market_identity_sha256(identity: MarketIdentity) -> str:
    if type(identity) is not MarketIdentity:
        raise ValueError("INVALID_MARKET_IDENTITY")
    payload = {
        "active": identity.active,
        "asset_ids": list(identity.asset_ids),
        "event_id": identity.event_id,
        "event_slug": identity.event_slug,
        "market_ids": list(identity.market_ids),
        "outcomes": list(identity.outcomes),
        "resolution_utc": identity.resolution_utc.isoformat(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MarketRolloverLifecycle:
    __slots__ = ("_blocker", "_index", "_states")

    def __init__(self) -> None:
        self._blocker: str | None = None
        self._index = 0
        self._states = [ROLLOVER_SEQUENCE[0]]

    @property
    def current_state(self) -> str:
        return (
            "ROLLOVER_BLOCKED"
            if self._blocker is not None
            else ROLLOVER_SEQUENCE[self._index]
        )

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(self._states)

    @property
    def blocker(self) -> str | None:
        return self._blocker

    def transition(self, state: str) -> bool:
        if type(state) is not str or not state:
            raise ValueError("INVALID_ROLLOVER_STATE")
        if self._blocker is not None:
            raise ValueError("ROLLOVER_BLOCKED")
        if state == ROLLOVER_SEQUENCE[self._index]:
            return False
        next_index = self._index + 1
        if self._index == len(ROLLOVER_SEQUENCE) - 1:
            next_index = 1
        if state != ROLLOVER_SEQUENCE[next_index]:
            raise ValueError("INVALID_ROLLOVER_TRANSITION")
        self._index = next_index
        self._states.append(state)
        return True

    def block(self, reason: str) -> bool:
        if type(reason) is not str or not reason:
            raise ValueError("INVALID_ROLLOVER_BLOCKER")
        if self._blocker is None:
            self._blocker = reason
            self._states.append("ROLLOVER_BLOCKED")
            return True
        if self._blocker == reason:
            return False
        raise ValueError("ROLLOVER_BLOCKER_CONFLICT")
