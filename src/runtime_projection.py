from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.fixed_point import SharesMicros, UsdMicros
from src.models import CanonicalSnapshot, SourceEvent, payload_sha256
from src.polymarket_provider import MarketBook
from src.runtime_adapters import MarketReconciliation


RECOVERED_EVALUATION_ORIGIN = "RECOVERED_AFTER_DOWNTIME"
CURRENT_EVALUATION_ORIGIN = "CURRENT_LIVE_REEVALUATION"


@dataclass(frozen=True, slots=True)
class CommittedSourceEvent:
    source_event_id: int
    event: SourceEvent
    inserted: bool
    authoritative_replay: bool = False

    def __post_init__(self) -> None:
        if type(self.source_event_id) is not int or self.source_event_id <= 0:
            raise ValueError("INVALID_COMMITTED_SOURCE_EVENT_ID")
        if type(self.event) is not SourceEvent:
            raise ValueError("INVALID_COMMITTED_SOURCE_EVENT")
        if type(self.inserted) is not bool:
            raise ValueError("INVALID_COMMITTED_SOURCE_INSERTED")
        if type(self.authoritative_replay) is not bool:
            raise ValueError("INVALID_COMMITTED_SOURCE_REPLAY")


class CanonicalProjector:
    def __init__(self, *, backend_session_id: str) -> None:
        if type(backend_session_id) is not str or not backend_session_id:
            raise ValueError("INVALID_BACKEND_SESSION_ID")
        self._backend_session_id = backend_session_id
        self._market: MarketReconciliation | None = None
        self._expected_assets: tuple[str, ...] = ()
        self._books: dict[str, MarketBook] = {}
        self._book_events: dict[str, CommittedSourceEvent] = {}
        self._binance: CommittedSourceEvent | None = None
        self._seen_event_ids: set[int] = set()
        self._live_ready = False
        self.latest_snapshot: CanonicalSnapshot | None = None

    @property
    def current_source_event_ids(self) -> tuple[int, ...]:
        if self._binance is None:
            return ()
        book_ids = tuple(
            self._book_events[asset_id].source_event_id
            for asset_id in self._expected_assets
            if asset_id in self._book_events
        )
        return (*book_ids, self._binance.source_event_id)

    def set_market_identity(
        self,
        reconciliation: MarketReconciliation,
    ) -> None:
        if type(reconciliation) is not MarketReconciliation:
            raise ValueError("INVALID_MARKET_RECONCILIATION")
        if (
            len(reconciliation.market_ids) != 11
            or len(set(reconciliation.market_ids)) != 11
            or len(reconciliation.asset_ids) != 22
            or len(set(reconciliation.asset_ids)) != 22
        ):
            raise ValueError("INCOMPLETE_MARKET_RECONCILIATION")
        self._market = reconciliation
        self._expected_assets = reconciliation.asset_ids
        self._books = {
            asset_id: MarketBook(asset_id)
            for asset_id in reconciliation.asset_ids
        }
        self._book_events.clear()

    def set_live_ready(self) -> None:
        if self._market is None:
            raise ValueError("MARKET_RECONCILIATION_REQUIRED")
        self._live_ready = True

    def apply(
        self,
        committed: CommittedSourceEvent,
    ) -> CanonicalSnapshot | None:
        if type(committed) is not CommittedSourceEvent:
            raise ValueError("INVALID_COMMITTED_SOURCE_EVENT")
        if not committed.inserted and not committed.authoritative_replay:
            return None
        if committed.source_event_id in self._seen_event_ids:
            return None

        event = committed.event
        if event.source == "binance":
            if event.event_type != "BINANCE_KLINE_CLOSED":
                raise ValueError("RUNTIME_BINANCE_NOT_CLOSED")
            if (
                self._binance is not None
                and event.source_timestamp_ms
                <= self._binance.event.source_timestamp_ms
            ):
                return None
            self._accept(committed)
            self._binance = committed
            if self._live_ready and self._has_complete_state():
                origin = (
                    RECOVERED_EVALUATION_ORIGIN
                    if event.recovery_origin == RECOVERED_EVALUATION_ORIGIN
                    else CURRENT_EVALUATION_ORIGIN
                )
                return self.build_snapshot(
                    evaluation_origin=origin,
                    trigger_committed_after_live_ready=True,
                )
            return None

        if event.source != "polymarket":
            return None
        if event.event_type == "UNHANDLED_PROVIDER_EVENT":
            return None
        if event.event_type not in {
            "POLYMARKET_BOOK",
            "POLYMARKET_PRICE_CHANGE",
        }:
            return None

        payload = self._decode_object(event.payload_json)
        asset_id = payload.get("asset_id")
        if type(asset_id) is not str or asset_id not in self._books:
            return None
        previous = self._book_events.get(asset_id)
        if (
            previous is not None
            and event.source_timestamp_ms
            < previous.event.source_timestamp_ms
        ):
            return None
        if (
            event.event_type == "POLYMARKET_PRICE_CHANGE"
            and asset_id not in self._book_events
        ):
            return None

        book = self._books[asset_id]
        book.apply(event, source_event_id=committed.source_event_id)
        self._accept(committed)
        self._book_events[asset_id] = committed
        return None

    def _has_complete_state(self) -> bool:
        return (
            self._market is not None
            and self._binance is not None
            and set(self._book_events) == set(self._expected_assets)
        )

    def build_snapshot(
        self,
        *,
        evaluation_origin: str,
        trigger_committed_after_live_ready: bool,
    ) -> CanonicalSnapshot:
        if evaluation_origin not in {
            RECOVERED_EVALUATION_ORIGIN,
            CURRENT_EVALUATION_ORIGIN,
        }:
            raise ValueError("INVALID_RUNTIME_EVALUATION_ORIGIN")
        if type(trigger_committed_after_live_ready) is not bool:
            raise ValueError("INVALID_RUNTIME_TRIGGER_PHASE")
        if not self._has_complete_state():
            raise ValueError("INCOMPLETE_CANONICAL_RUNTIME_STATE")

        binance_state = self._binance_state(self._binance)
        books = [
            self._book_state(index, asset_id)
            for index, asset_id in enumerate(self._expected_assets)
        ]
        state_value = {
            "binance": binance_state,
            "market_identity": self._market.market_id,
            "polymarket": {
                "asset_count": len(self._expected_assets),
                "books": books,
                "market_count": len(self._market.market_ids),
            },
        }
        state_json = json.dumps(
            state_value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical_state_hash = payload_sha256(state_json)
        source_event_ids = self.current_source_event_ids
        payload_value = {
            "backend_session_id": self._backend_session_id,
            "binance": binance_state,
            "binance_ready": True,
            "canonical": True,
            "canonical_state_hash": canonical_state_hash,
            "evaluation_origin": evaluation_origin,
            "market_identity": self._market.market_id,
            "polymarket": state_value["polymarket"],
            "polymarket_ready": True,
            "source_event_ids": list(source_event_ids),
            "trigger_committed_after_live_ready": (
                trigger_committed_after_live_ready
            ),
            "triggering_event_natural_key": (
                self._binance.event.natural_key
            ),
        }
        payload_json = json.dumps(
            payload_value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = payload_sha256(payload_json)
        snapshot = CanonicalSnapshot(
            snapshot_key=f"canonical:{digest}",
            source_event_ids=source_event_ids,
            payload_json=payload_json,
            payload_sha256=digest,
            created_at_ms=self._binance.event.received_timestamp_ms,
            recovery_origin=evaluation_origin,
        )
        self.latest_snapshot = snapshot
        return snapshot

    def _accept(self, committed: CommittedSourceEvent) -> None:
        self._seen_event_ids.add(committed.source_event_id)

    def _book_state(self, index: int, asset_id: str) -> dict[str, Any]:
        market = self._market
        if market is None:
            raise ValueError("MARKET_RECONCILIATION_REQUIRED")
        book = self._books[asset_id]
        committed = self._book_events[asset_id]
        return {
            "asset_id": asset_id,
            "best_ask_micros": book.best_ask_micros,
            "best_bid_micros": book.best_bid_micros,
            "book_hash": book.book_hash,
            "market_id": market.market_ids[index // 2],
            "origin": committed.event.recovery_origin,
            "outcome": "YES" if index % 2 == 0 else "NO",
            "source_event_id": committed.source_event_id,
            "source_timestamp_ms": committed.event.source_timestamp_ms,
            "spread_micros": book.spread_micros,
        }

    @classmethod
    def _binance_state(
        cls,
        committed: CommittedSourceEvent,
    ) -> dict[str, Any]:
        payload = cls._decode_object(committed.event.payload_json)
        if type(payload.get("k")) is dict:
            kline = payload["k"]
            values = {
                "close": kline.get("c"),
                "high": kline.get("h"),
                "low": kline.get("l"),
                "open": kline.get("o"),
                "volume": kline.get("v"),
            }
            open_time_ms = kline.get("t")
        else:
            values = {
                "close": payload.get("close"),
                "high": payload.get("high"),
                "low": payload.get("low"),
                "open": payload.get("open"),
                "volume": payload.get("volume"),
            }
            open_time_ms = payload.get(
                "open_time_ms",
                committed.event.source_timestamp_ms,
            )
        if type(open_time_ms) is not int or open_time_ms < 0:
            raise ValueError("INVALID_RUNTIME_BINANCE_OPEN_TIME")
        for field, value in values.items():
            if type(value) is not str:
                raise ValueError(
                    f"INVALID_RUNTIME_BINANCE_DECIMAL: {field}"
                )
        return {
            "close_usd_micros": UsdMicros.from_decimal(
                values["close"]
            ).value,
            "high_usd_micros": UsdMicros.from_decimal(
                values["high"]
            ).value,
            "low_usd_micros": UsdMicros.from_decimal(
                values["low"]
            ).value,
            "open_time_ms": open_time_ms,
            "open_usd_micros": UsdMicros.from_decimal(
                values["open"]
            ).value,
            "origin": committed.event.recovery_origin,
            "source_event_id": committed.source_event_id,
            "volume_micros": SharesMicros.from_decimal(
                values["volume"]
            ).value,
        }

    @staticmethod
    def _decode_object(payload_json: str) -> dict[str, Any]:
        try:
            value = json.loads(payload_json)
        except json.JSONDecodeError:
            raise ValueError("INVALID_RUNTIME_EVENT_JSON") from None
        if type(value) is not dict:
            raise ValueError("INVALID_RUNTIME_EVENT_SHAPE")
        return value
