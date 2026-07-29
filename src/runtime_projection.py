from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.models import CanonicalSnapshot, SourceEvent, payload_sha256
from src.runtime_adapters import MarketReconciliation


@dataclass(frozen=True, slots=True)
class CommittedSourceEvent:
    source_event_id: int
    event: SourceEvent
    inserted: bool

    def __post_init__(self) -> None:
        if type(self.source_event_id) is not int or self.source_event_id <= 0:
            raise ValueError("INVALID_COMMITTED_SOURCE_EVENT_ID")
        if type(self.event) is not SourceEvent:
            raise ValueError("INVALID_COMMITTED_SOURCE_EVENT")
        if type(self.inserted) is not bool:
            raise ValueError("INVALID_COMMITTED_SOURCE_INSERTED")


class CanonicalProjector:
    def __init__(self, *, backend_session_id: str) -> None:
        if type(backend_session_id) is not str or not backend_session_id:
            raise ValueError("INVALID_BACKEND_SESSION_ID")
        self._backend_session_id = backend_session_id
        self._market: MarketReconciliation | None = None
        self._expected_assets: frozenset[str] = frozenset()
        self._books: dict[str, CommittedSourceEvent] = {}
        self._binance: CommittedSourceEvent | None = None
        self._seen_event_ids: set[int] = set()
        self._causal_event_ids: list[int] = []
        self._live_ready = False
        self.latest_snapshot: CanonicalSnapshot | None = None

    def set_market_identity(
        self,
        reconciliation: MarketReconciliation,
    ) -> None:
        if type(reconciliation) is not MarketReconciliation:
            raise ValueError("INVALID_MARKET_RECONCILIATION")
        if (
            len(reconciliation.market_ids) != 11
            or len(reconciliation.asset_ids) != 22
            or len(set(reconciliation.asset_ids)) != 22
        ):
            raise ValueError("INCOMPLETE_MARKET_RECONCILIATION")
        self._market = reconciliation
        self._expected_assets = frozenset(reconciliation.asset_ids)

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
        if not committed.inserted:
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
            return self._snapshot_if_ready(event)

        if event.source == "polymarket":
            if event.event_type == "UNHANDLED_PROVIDER_EVENT":
                return None
            if event.event_type not in {
                "POLYMARKET_BOOK",
                "POLYMARKET_PRICE_CHANGE",
            }:
                return None
            payload = self._decode_object(event.payload_json)
            asset_id = payload.get("asset_id")
            if type(asset_id) is not str or asset_id not in self._expected_assets:
                return None
            previous = self._books.get(asset_id)
            if (
                previous is not None
                and event.source_timestamp_ms
                <= previous.event.source_timestamp_ms
            ):
                return None
            self._accept(committed)
            self._books[asset_id] = committed
            return None
        return None

    def _accept(self, committed: CommittedSourceEvent) -> None:
        self._seen_event_ids.add(committed.source_event_id)
        self._causal_event_ids.append(committed.source_event_id)

    def _snapshot_if_ready(
        self,
        trigger: SourceEvent,
    ) -> CanonicalSnapshot | None:
        if (
            not self._live_ready
            or self._market is None
            or set(self._books) != set(self._expected_assets)
        ):
            return None
        origins = [
            item.event.recovery_origin for item in self._books.values()
        ] + [trigger.recovery_origin]
        recovery_origin = self._combined_origin(origins)
        payload_value = {
            "backend_session_id": self._backend_session_id,
            "binance_ready": True,
            "canonical": True,
            "market_identity": self._market.market_id,
            "polymarket_ready": True,
            "source_event_ids": list(self._causal_event_ids),
            "trigger_committed_after_live_ready": True,
            "triggering_event_natural_key": trigger.natural_key,
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
            source_event_ids=tuple(self._causal_event_ids),
            payload_json=payload_json,
            payload_sha256=digest,
            created_at_ms=trigger.received_timestamp_ms,
            recovery_origin=recovery_origin,
        )
        self.latest_snapshot = snapshot
        return snapshot

    @staticmethod
    def _combined_origin(origins: list[str]) -> str:
        for value in (
            "RECOVERED_AFTER_DOWNTIME",
            "BUFFERED_DURING_RECOVERY",
            "REST_BACKFILL",
            "LIVE",
        ):
            if value in origins:
                return value
        raise ValueError("INVALID_RUNTIME_RECOVERY_ORIGIN")

    @staticmethod
    def _decode_object(payload_json: str) -> dict:
        try:
            value = json.loads(payload_json)
        except json.JSONDecodeError:
            raise ValueError("INVALID_RUNTIME_EVENT_JSON") from None
        if type(value) is not dict:
            raise ValueError("INVALID_RUNTIME_EVENT_SHAPE")
        return value
