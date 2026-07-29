from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.binance_provider import BinanceStream, iter_binance_backfill
from src.market_discovery import (
    MarketIdentity,
    discover_active_btc_daily_range,
)
from src.models import SourceEvent
from src.polymarket_provider import (
    PolymarketStream,
    fetch_current_books,
    iter_price_history,
)


@dataclass(frozen=True, slots=True)
class MarketReconciliation:
    market_identity_json: str
    market_id: str
    market_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    current_events: tuple[SourceEvent, ...]
    historical_depth: str


class BinanceRuntimeAdapter:
    __slots__ = (
        "_backfill",
        "_closed",
        "_close_session",
        "_session",
        "_stream",
    )

    def __init__(
        self,
        *,
        session: Any,
        stream: BinanceStream,
        backfill: Callable[
            [Any, int, int], AsyncIterator[SourceEvent]
        ]
        | None = iter_binance_backfill,
        close_session: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._session = session
        self._stream = stream
        self._backfill = backfill
        self._close_session = close_session
        self._closed = False

    async def recover(
        self,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[SourceEvent]:
        if self._backfill is None:
            raise RuntimeError("RUNTIME_BINANCE_BACKFILL_UNAVAILABLE")
        recovered: list[SourceEvent] = []
        try:
            async for item in self._backfill(
                self._session,
                start_ms,
                end_ms,
            ):
                if (
                    type(item) is not SourceEvent
                    or item.source != "binance"
                    or item.event_type != "BINANCE_KLINE_CLOSED"
                ):
                    raise ValueError("RUNTIME_BINANCE_NOT_CLOSED")
                recovered.append(item)
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError("RUNTIME_BINANCE_RECOVERY_FAILED") from error
        return recovered

    async def stream(
        self,
        queue: asyncio.Queue[SourceEvent],
    ) -> None:
        await self._stream.run(queue)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_session is not None:
            result = self._close_session()
            if inspect.isawaitable(result):
                await result


class PolymarketRuntimeAdapter:
    __slots__ = (
        "_close_session",
        "_closed",
        "_discover",
        "_event_loader",
        "_fetch_books",
        "_history",
        "_now_utc",
        "_session",
        "_stream",
    )

    def __init__(
        self,
        *,
        session: Any,
        stream: PolymarketStream,
        event_loader: Callable[[], Awaitable[list[dict[str, Any]]]],
        now_utc: Callable[[], datetime],
        discover: Callable[..., MarketIdentity] = (
            discover_active_btc_daily_range
        ),
        fetch_books: Callable[..., Awaitable[list[SourceEvent]]] = (
            fetch_current_books
        ),
        history: Callable[..., AsyncIterator[SourceEvent]] = (
            iter_price_history
        ),
        close_session: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._session = session
        self._stream = stream
        self._event_loader = event_loader
        self._now_utc = now_utc
        self._discover = discover
        self._fetch_books = fetch_books
        self._history = history
        self._close_session = close_session
        self._closed = False

    async def discover_and_reconcile(self) -> MarketReconciliation:
        try:
            payload = await self._event_loader()
            identity = self._discover(payload, now_utc=self._now_utc())
            books = await self._fetch_books(
                self._session,
                identity.asset_ids,
            )
            observed_assets = tuple(
                json.loads(item.payload_json).get("asset_id")
                for item in books
            )
            if (
                len(identity.market_ids) != 11
                or len(identity.asset_ids) != 22
                or len(set(identity.asset_ids)) != 22
                or len(books) != len(identity.asset_ids)
                or set(observed_assets) != set(identity.asset_ids)
            ):
                raise ValueError(
                    "RUNTIME_POLYMARKET_INCOMPLETE_BOOK_SET"
                )
            identity_json = json.dumps(
                {
                    "asset_ids": list(identity.asset_ids),
                    "event_id": identity.event_id,
                    "event_slug": identity.event_slug,
                    "market_ids": list(identity.market_ids),
                    "outcomes": list(identity.outcomes),
                    "resolution_utc": identity.resolution_utc.isoformat(),
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return MarketReconciliation(
                market_identity_json=identity_json,
                market_id=identity.event_id,
                market_ids=identity.market_ids,
                asset_ids=identity.asset_ids,
                current_events=tuple(books),
                historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
            )
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError(
                "RUNTIME_POLYMARKET_DISCOVERY_FAILED"
            ) from error

    async def recover(
        self,
        reconciliation: MarketReconciliation,
        *,
        start_ts: int,
        end_ts: int,
    ) -> list[SourceEvent]:
        if type(reconciliation) is not MarketReconciliation:
            raise ValueError("INVALID_MARKET_RECONCILIATION")
        recovered: list[SourceEvent] = []
        for asset_id in reconciliation.asset_ids:
            async for item in self._history(
                self._session,
                asset_id=asset_id,
                start_ts=start_ts,
                end_ts=end_ts,
            ):
                if type(item) is not SourceEvent:
                    raise ValueError("INVALID_POLYMARKET_HISTORY_EVENT")
                recovered.append(item)
        return recovered

    async def stream(
        self,
        *,
        asset_ids: tuple[str, ...],
        queue: asyncio.Queue[SourceEvent],
    ) -> None:
        await self._stream.run(asset_ids, queue)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_session is not None:
            result = self._close_session()
            if inspect.isawaitable(result):
                await result
