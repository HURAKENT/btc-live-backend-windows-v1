from __future__ import annotations

import asyncio
import hashlib
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
from src.market_rollover import MarketPair, build_market_pair
from src.polymarket_provider import (
    PolymarketStream,
    fetch_current_books,
    iter_price_history,
)


@dataclass(frozen=True, slots=True)
class MarketDiscovery:
    market_identity_json: str
    market_id: str
    market_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    historical_depth: str


@dataclass(frozen=True, slots=True)
class MarketReconciliation:
    market_identity_json: str
    market_id: str
    market_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    current_events: tuple[SourceEvent, ...]
    historical_depth: str


@dataclass(frozen=True, slots=True)
class MarketDiscoveryPair:
    current: MarketDiscovery
    next: MarketDiscovery


@dataclass(frozen=True, slots=True)
class PolymarketHistoryRecoverySummary:
    completed_asset_ids: tuple[str, ...]
    empty_asset_ids: tuple[str, ...]
    skipped_asset_ids: tuple[str, ...]
    event_count: int


class BinanceRuntimeAdapter:
    __slots__ = (
        "_backfill",
        "_closed",
        "_close_session",
        "_session",
        "_stream",
        "_stream_ready",
        "_rest_bases",
        "_reconnect_recovery",
        "_state_sink",
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
        rest_bases: tuple[str, ...] | None = None,
    ) -> None:
        self._session = session
        self._stream = stream
        self._backfill = backfill
        self._close_session = close_session
        self._closed = False
        self._stream_ready = asyncio.Event()
        self._rest_bases = rest_bases
        self._reconnect_recovery = None
        self._state_sink = None

    def configure_runtime_callbacks(
        self,
        *,
        state_sink: Callable[[str, str | None], Awaitable[None]],
        reconnect_recovery: Callable[[], Awaitable[None]],
    ) -> None:
        self._state_sink = state_sink
        self._reconnect_recovery = reconnect_recovery

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
            kwargs = {}
            if self._rest_bases is not None:
                kwargs["rest_bases"] = self._rest_bases
            async for item in self._backfill(
                self._session,
                start_ms,
                end_ms,
                **kwargs,
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
        parameters = inspect.signature(self._stream.run).parameters
        kwargs = {}
        if "ready_event" in parameters:
            kwargs["ready_event"] = self._stream_ready
        else:
            self._stream_ready.set()
        if "state_sink" in parameters:
            kwargs["state_sink"] = self._state_sink
        if "reconnect_recovery" in parameters:
            kwargs["reconnect_recovery"] = self._reconnect_recovery
        await self._stream.run(queue, **kwargs)

    async def wait_stream_ready(self) -> None:
        await self._stream_ready.wait()

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
        "_discover_pair",
        "_event_loader",
        "_fetch_books",
        "_history",
        "_last_recovery_summary",
        "_now_utc",
        "_session",
        "_stream",
        "_stream_factory",
        "_stream_ready",
        "_clob_base_url",
        "_reconnect_recovery",
        "_state_sink",
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
        discover_pair: Callable[..., MarketPair] = build_market_pair,
        fetch_books: Callable[..., Awaitable[list[SourceEvent]]] = (
            fetch_current_books
        ),
        history: Callable[..., AsyncIterator[SourceEvent]] = (
            iter_price_history
        ),
        close_session: Callable[[], Awaitable[None] | None] | None = None,
        clob_base_url: str | None = None,
        stream_factory: Callable[[], PolymarketStream] | None = None,
    ) -> None:
        self._session = session
        self._stream = stream
        self._stream_factory = stream_factory
        self._event_loader = event_loader
        self._now_utc = now_utc
        self._discover = discover
        self._discover_pair = discover_pair
        self._fetch_books = fetch_books
        self._history = history
        self._last_recovery_summary: (
            PolymarketHistoryRecoverySummary | None
        ) = None
        self._close_session = close_session
        self._closed = False
        self._stream_ready = asyncio.Event()
        self._clob_base_url = clob_base_url
        self._reconnect_recovery = None
        self._state_sink = None

    def configure_runtime_callbacks(
        self,
        *,
        state_sink: Callable[[str, str | None], Awaitable[None]],
        reconnect_recovery: Callable[[], Awaitable[None]],
    ) -> None:
        self._state_sink = state_sink
        self._reconnect_recovery = reconnect_recovery

    async def discover_market(self) -> MarketDiscovery:
        try:
            payload = await self._event_loader()
            identity = self._discover(payload, now_utc=self._now_utc())
            return self._market_discovery(identity)
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError(
                "RUNTIME_POLYMARKET_DISCOVERY_FAILED"
            ) from error

    async def discover_market_pair(self) -> MarketDiscoveryPair:
        try:
            payload = await self._event_loader()
            pair = self._discover_pair(payload, now_utc=self._now_utc())
            if type(pair) is not MarketPair:
                raise ValueError("INVALID_RUNTIME_MARKET_PAIR")
            return MarketDiscoveryPair(
                current=self._market_discovery(pair.current),
                next=self._market_discovery(pair.next),
            )
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError(
                "RUNTIME_POLYMARKET_PAIR_DISCOVERY_FAILED"
            ) from error

    @staticmethod
    def _market_discovery(identity: MarketIdentity) -> MarketDiscovery:
        if (
            type(identity) is not MarketIdentity
            or len(identity.market_ids) != 11
            or len(set(identity.market_ids)) != 11
            or len(identity.asset_ids) != 22
            or len(set(identity.asset_ids)) != 22
        ):
            raise ValueError(
                "RUNTIME_POLYMARKET_INCOMPLETE_MARKET_IDENTITY"
            )
        identity_json = json.dumps(
            {
                "asset_ids": list(identity.asset_ids),
                "bucket_bounds": [list(item) for item in identity.bucket_bounds],
                "condition_ids": list(identity.condition_ids),
                "event_id": identity.event_id,
                "event_slug": identity.event_slug,
                "fee_schedules": list(identity.fee_schedules),
                "market_date": identity.market_date,
                "market_ids": list(identity.market_ids),
                "outcomes": list(identity.outcomes),
                "resolution_utc": identity.resolution_utc.isoformat(),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return MarketDiscovery(
            market_identity_json=identity_json,
            market_id=identity.event_id,
            market_ids=identity.market_ids,
            asset_ids=identity.asset_ids,
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )

    async def reconcile_current_books(
        self,
        discovery: MarketDiscovery,
    ) -> MarketReconciliation:
        if type(discovery) is not MarketDiscovery:
            raise ValueError("INVALID_MARKET_DISCOVERY")
        try:
            kwargs = {}
            if self._clob_base_url is not None:
                kwargs["clob_base_url"] = self._clob_base_url
            books = await self._fetch_books(
                self._session,
                discovery.asset_ids,
                **kwargs,
            )
            observed_assets = tuple(
                json.loads(item.payload_json).get("asset_id")
                for item in books
            )
            if (
                len(books) != len(discovery.asset_ids)
                or set(observed_assets) != set(discovery.asset_ids)
            ):
                raise ValueError(
                    "RUNTIME_POLYMARKET_INCOMPLETE_BOOK_SET"
                )
            return MarketReconciliation(
                market_identity_json=discovery.market_identity_json,
                market_id=discovery.market_id,
                market_ids=discovery.market_ids,
                asset_ids=discovery.asset_ids,
                current_events=tuple(books),
                historical_depth=discovery.historical_depth,
            )
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError(
                "RUNTIME_POLYMARKET_BOOK_RECONCILIATION_FAILED"
            ) from error

    async def discover_and_reconcile(self) -> MarketReconciliation:
        try:
            discovery = await self.discover_market()
            return await self.reconcile_current_books(discovery)
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except RuntimeError as error:
            if str(error) == "RUNTIME_POLYMARKET_DISCOVERY_FAILED":
                raise
            raise RuntimeError(
                "RUNTIME_POLYMARKET_DISCOVERY_FAILED"
            ) from (error.__cause__ or error)

    async def load_current_strict_snapshot(
        self, discovery: MarketDiscovery, *, observed_at_ms: int
    ):
        from src.fixed_point import ProbabilityMicros
        from src.mvp_current_runtime import CurrentMarketSnapshotV1
        from src.polymarket_provider import MarketBook

        identity = json.loads(discovery.market_identity_json)
        if (
            identity.get("market_date") is None
            or len(identity.get("bucket_bounds", [])) != 11
            or len(identity.get("fee_schedules", [])) != 11
        ):
            raise ValueError("INCOMPLETE_MACHINE_MARKET_METADATA")
        reconciliation = await self.reconcile_current_books(discovery)
        events_by_asset = {
            json.loads(event.payload_json)["asset_id"]: event
            for event in reconciliation.current_events
        }
        yes_token_ids = tuple(discovery.asset_ids[0::2])
        no_token_ids = tuple(discovery.asset_ids[1::2])
        books: list[MarketBook] = []
        q_values: list[ProbabilityMicros] = []
        history_payloads: list[dict[str, Any]] = []
        latest_history_ms = 0
        end_ts = observed_at_ms // 1000
        start_ts = max(0, end_ts - 3600)
        for index, asset_id in enumerate(yes_token_ids, start=1):
            event = events_by_asset.get(asset_id)
            if event is None:
                raise ValueError("RUNTIME_POLYMARKET_INCOMPLETE_BOOK_SET")
            book = MarketBook(asset_id)
            book.apply(event, source_event_id=index)
            books.append(book)
            history_events = []
            kwargs = {}
            if self._clob_base_url is not None:
                kwargs["clob_base_url"] = self._clob_base_url
            async for history_event in self._history(
                self._session,
                asset_id=asset_id,
                start_ts=start_ts,
                end_ts=end_ts,
                **kwargs,
            ):
                history_events.append(history_event)
            if not history_events:
                raise ValueError("MISSING_CURRENT_PRICE_HISTORY")
            latest = history_events[-1]
            payload = json.loads(latest.payload_json)
            q_values.append(ProbabilityMicros(payload["price_micros"]))
            history_payloads.append(payload)
            latest_history_ms = max(latest_history_ms, latest.source_timestamp_ms)
        history_hash = hashlib.sha256(
            json.dumps(
                history_payloads,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return CurrentMarketSnapshotV1(
            market_id=discovery.market_id,
            market_date=identity["market_date"],
            bucket_bounds=tuple(
                (item[0], item[1]) for item in identity["bucket_bounds"]
            ),
            yes_token_ids=yes_token_ids,
            no_token_ids=no_token_ids,
            yes_books=tuple(books),
            market_q_yes=tuple(q_values),
            fee_schedules=tuple(identity["fee_schedules"]),
            price_history_source_sha256=history_hash,
            price_history_observed_at_ms=latest_history_ms,
        )

    async def recover(
        self,
        reconciliation: MarketReconciliation,
        *,
        start_ts: int,
        end_ts: int,
        start_ts_by_asset: dict[str, int | None] | None = None,
    ) -> list[SourceEvent]:
        if type(reconciliation) is not MarketReconciliation:
            raise ValueError("INVALID_MARKET_RECONCILIATION")
        if type(start_ts) is not int or type(end_ts) is not int:
            raise ValueError("INVALID_POLYMARKET_HISTORY_BOUNDARY")
        if start_ts < 0 or end_ts < start_ts:
            raise ValueError("INVALID_POLYMARKET_HISTORY_BOUNDARY")
        if start_ts_by_asset is None:
            starts = {
                asset_id: start_ts for asset_id in reconciliation.asset_ids
            }
        else:
            if (
                type(start_ts_by_asset) is not dict
                or set(start_ts_by_asset) != set(reconciliation.asset_ids)
            ):
                raise ValueError("INVALID_POLYMARKET_HISTORY_STARTS")
            starts = dict(start_ts_by_asset)
        for value in starts.values():
            if value is not None and (
                type(value) is not int
                or value < 0
                or value > end_ts
            ):
                raise ValueError("INVALID_POLYMARKET_HISTORY_STARTS")

        self._last_recovery_summary = None
        recovered: list[SourceEvent] = []
        completed: list[str] = []
        empty: list[str] = []
        skipped: list[str] = []
        for asset_id in reconciliation.asset_ids:
            asset_start_ts = starts[asset_id]
            if asset_start_ts is None:
                completed.append(asset_id)
                skipped.append(asset_id)
                continue
            event_count_before = len(recovered)
            kwargs = {}
            if self._clob_base_url is not None:
                kwargs["clob_base_url"] = self._clob_base_url
            async for item in self._history(
                self._session,
                asset_id=asset_id,
                start_ts=asset_start_ts,
                end_ts=end_ts,
                **kwargs,
            ):
                if (
                    type(item) is not SourceEvent
                    or item.source != "polymarket"
                    or item.event_type != "POLYMARKET_PRICE_HISTORY"
                ):
                    raise ValueError("INVALID_POLYMARKET_HISTORY_EVENT")
                try:
                    observed_asset_id = json.loads(
                        item.payload_json
                    ).get("asset_id")
                except json.JSONDecodeError:
                    raise ValueError(
                        "INVALID_POLYMARKET_HISTORY_EVENT"
                    ) from None
                if observed_asset_id != asset_id:
                    raise ValueError(
                        "RUNTIME_POLYMARKET_HISTORY_ASSET_MISMATCH"
                    )
                recovered.append(item)
            completed.append(asset_id)
            if len(recovered) == event_count_before:
                empty.append(asset_id)
        self._last_recovery_summary = PolymarketHistoryRecoverySummary(
            completed_asset_ids=tuple(completed),
            empty_asset_ids=tuple(empty),
            skipped_asset_ids=tuple(skipped),
            event_count=len(recovered),
        )
        return recovered

    @property
    def last_recovery_summary(
        self,
    ) -> PolymarketHistoryRecoverySummary | None:
        return self._last_recovery_summary

    async def stream(
        self,
        *,
        asset_ids: tuple[str, ...],
        queue: asyncio.Queue[SourceEvent],
    ) -> None:
        parameters = inspect.signature(self._stream.run).parameters
        kwargs = {}
        if "ready_event" in parameters:
            kwargs["ready_event"] = self._stream_ready
        else:
            self._stream_ready.set()
        if "state_sink" in parameters:
            kwargs["state_sink"] = self._state_sink
        if "reconnect_recovery" in parameters:
            kwargs["reconnect_recovery"] = self._reconnect_recovery
        await self._stream.run(asset_ids, queue, **kwargs)

    async def stream_next(
        self,
        *,
        asset_ids: tuple[str, ...],
        queue: asyncio.Queue[SourceEvent],
        ready_event: asyncio.Event,
    ) -> None:
        if self._stream_factory is None:
            raise RuntimeError("RUNTIME_POLYMARKET_NEXT_STREAM_UNAVAILABLE")
        stream = self._stream_factory()
        parameters = inspect.signature(stream.run).parameters
        if "ready_event" in parameters:
            await stream.run(asset_ids, queue, ready_event=ready_event)
        else:
            ready_event.set()
            await stream.run(asset_ids, queue)

    async def wait_stream_ready(self) -> None:
        await self._stream_ready.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_session is not None:
            result = self._close_session()
            if inspect.isawaitable(result):
                await result
