from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from src.market_discovery import MarketIdentity
from src.models import SourceEvent, payload_sha256

try:
    from src.runtime_adapters import (
        BinanceRuntimeAdapter,
        MarketReconciliation,
        PolymarketRuntimeAdapter,
    )
except ModuleNotFoundError:
    BinanceRuntimeAdapter = None
    MarketReconciliation = None
    PolymarketRuntimeAdapter = None


def event(
    source: str,
    key: str,
    *,
    event_type: str,
    timestamp: int = 60_000,
    asset_id: str | None = None,
) -> SourceEvent:
    payload_value = {"event_type": event_type, "source": source}
    if asset_id is not None:
        payload_value["asset_id"] = asset_id
    payload = json.dumps(
        payload_value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return SourceEvent(
        source=source,
        natural_key=key,
        source_timestamp_ms=timestamp,
        received_timestamp_ms=timestamp,
        event_type=event_type,
        payload_json=payload,
        payload_sha256=payload_sha256(payload),
        recovery_origin="REST_BACKFILL",
    )


class FakeStream:
    def __init__(self, emitted: SourceEvent | None = None) -> None:
        self.emitted = emitted
        self.calls = []

    async def run(self, *args) -> None:
        self.calls.append(args)
        if self.emitted is not None:
            queue = args[-1]
            await queue.put(self.emitted)


class RuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_recover_uses_existing_backfill_primitive(self):
        calls = []
        closed = event(
            "binance",
            "binance:BTCUSDT:1m:60000",
            event_type="BINANCE_KLINE_CLOSED",
        )

        async def backfill(session, start_ms, end_ms):
            calls.append((session, start_ms, end_ms))
            yield closed

        adapter = BinanceRuntimeAdapter(
            session="session",
            stream=FakeStream(),
            backfill=backfill,
        )
        self.assertEqual(
            await adapter.recover(start_ms=60_000, end_ms=60_000),
            [closed],
        )
        self.assertEqual(calls, [("session", 60_000, 60_000)])

    async def test_binance_recover_rejects_non_closed_event(self):
        open_event = event(
            "binance",
            "open",
            event_type="BINANCE_KLINE_OPEN",
        )

        async def backfill(*_args):
            yield open_event

        adapter = BinanceRuntimeAdapter(
            session=object(),
            stream=FakeStream(),
            backfill=backfill,
        )
        with self.assertRaisesRegex(ValueError, "RUNTIME_BINANCE_NOT_CLOSED"):
            await adapter.recover(start_ms=0, end_ms=60_000)

    async def test_binance_stream_uses_provided_queue(self):
        closed = event(
            "binance",
            "binance:BTCUSDT:1m:60000",
            event_type="BINANCE_KLINE_CLOSED",
        )
        stream = FakeStream(closed)
        adapter = BinanceRuntimeAdapter(
            session=object(),
            stream=stream,
            backfill=None,
        )
        queue = asyncio.Queue()
        await adapter.stream(queue)
        self.assertIs(await queue.get(), closed)
        self.assertIs(stream.calls[0][0], queue)

    async def test_binance_cancellation_is_not_wrapped(self):
        class CancelledStream:
            async def run(self, _queue):
                raise asyncio.CancelledError

        adapter = BinanceRuntimeAdapter(
            session=object(),
            stream=CancelledStream(),
            backfill=None,
        )
        with self.assertRaises(asyncio.CancelledError):
            await adapter.stream(asyncio.Queue())

    async def test_polymarket_discovery_returns_ordered_identity_and_books(self):
        identity = self._identity()
        books = self._books(identity.asset_ids)
        adapter = self._polymarket(identity=identity, books=books)
        reconciliation = await adapter.discover_and_reconcile()
        self.assertEqual(reconciliation.market_id, identity.event_id)
        self.assertEqual(reconciliation.asset_ids, identity.asset_ids)
        self.assertEqual(reconciliation.current_events, tuple(books))

    async def test_polymarket_requires_all_current_books(self):
        identity = self._identity()
        adapter = self._polymarket(
            identity=identity,
            books=self._books(identity.asset_ids[:-1]),
        )
        with self.assertRaisesRegex(
            ValueError, "RUNTIME_POLYMARKET_INCOMPLETE_BOOK_SET"
        ):
            await adapter.discover_and_reconcile()

    async def test_polymarket_historical_depth_is_explicit(self):
        reconciliation = await self._polymarket().discover_and_reconcile()
        self.assertEqual(
            reconciliation.historical_depth,
            "NOT_AVAILABLE_NOT_REQUIRED",
        )

    async def test_polymarket_stream_receives_exact_discovered_assets(self):
        identity = self._identity()
        stream = FakeStream()
        adapter = self._polymarket(identity=identity, stream=stream)
        queue = asyncio.Queue()
        await adapter.stream(asset_ids=identity.asset_ids, queue=queue)
        self.assertEqual(stream.calls, [(identity.asset_ids, queue)])

    async def test_adapters_do_not_require_sqlite_store(self):
        adapter = self._polymarket()
        self.assertNotIn("store", adapter.__slots__)
        self.assertNotIn("store", BinanceRuntimeAdapter.__slots__)

    async def test_adapters_create_no_unmanaged_tasks(self):
        before = asyncio.all_tasks()
        await self._polymarket().discover_and_reconcile()
        after = asyncio.all_tasks()
        self.assertEqual(after, before)

    async def test_close_is_idempotent(self):
        closes = []

        async def close():
            closes.append("closed")

        adapter = self._polymarket(close=close)
        await adapter.close()
        await adapter.close()
        self.assertEqual(closes, ["closed"])

    async def test_provider_exception_preserves_causal_chain(self):
        cause = RuntimeError("provider broke")

        async def fetch(_session, _asset_ids):
            raise cause

        adapter = self._polymarket(fetch=fetch)
        with self.assertRaisesRegex(
            RuntimeError, "RUNTIME_POLYMARKET_DISCOVERY_FAILED"
        ) as caught:
            await adapter.discover_and_reconcile()
        self.assertIs(caught.exception.__cause__, cause)

    async def test_recover_uses_available_history_without_inventing_depth(self):
        reconciliation = await self._polymarket().discover_and_reconcile()
        recovered = event(
            "polymarket",
            "history:1",
            event_type="POLYMARKET_PRICE_HISTORY",
            asset_id=reconciliation.asset_ids[0],
        )

        async def history(_session, *, asset_id, start_ts, end_ts):
            self.assertEqual((start_ts, end_ts), (1, 2))
            if asset_id == reconciliation.asset_ids[0]:
                yield recovered

        adapter = self._polymarket(history=history)
        self.assertEqual(
            await adapter.recover(
                reconciliation,
                start_ts=1,
                end_ts=2,
            ),
            [recovered],
        )

    def test_runtime_adapter_module_has_no_auth_order_or_wallet_surface(self):
        import inspect
        import src.runtime_adapters as module

        source = inspect.getsource(module).lower()
        for token in ("authorization", "private_key", "place_order", "wallet"):
            self.assertNotIn(token, source)

    def _identity(self):
        return MarketIdentity(
            event_id="event-1",
            event_slug="bitcoin-price-on-2026-07-30",
            active=True,
            resolution_utc=datetime(2026, 7, 30, tzinfo=timezone.utc),
            market_ids=tuple(f"market-{index}" for index in range(11)),
            outcomes=tuple(f"range-{index}" for index in range(11)),
            asset_ids=tuple(f"asset-{index}" for index in range(22)),
        )

    def _books(self, asset_ids):
        books = []
        for asset_id in asset_ids:
            payload = json.dumps(
                {"asset_id": asset_id, "event_type": "book"},
                sort_keys=True,
                separators=(",", ":"),
            )
            books.append(
                SourceEvent(
                    source="polymarket",
                    natural_key=f"polymarket:{asset_id}:book:hash-{asset_id}",
                    source_timestamp_ms=60_000,
                    received_timestamp_ms=60_000,
                    event_type="POLYMARKET_BOOK",
                    payload_json=payload,
                    payload_sha256=payload_sha256(payload),
                    recovery_origin="REST_BACKFILL",
                )
            )
        return books

    def _polymarket(
        self,
        *,
        identity=None,
        books=None,
        stream=None,
        fetch=None,
        history=None,
        close=None,
    ):
        identity = identity or self._identity()
        books = self._books(identity.asset_ids) if books is None else books

        def discover(_payload, *, now_utc):
            return identity

        async def load_events():
            return [{"id": identity.event_id}]

        async def fetch_default(_session, _asset_ids):
            return books

        async def history_default(*_args, **_kwargs):
            if False:
                yield None

        return PolymarketRuntimeAdapter(
            session=object(),
            stream=stream or FakeStream(),
            event_loader=load_events,
            now_utc=lambda: datetime(2026, 7, 29, tzinfo=timezone.utc),
            discover=discover,
            fetch_books=fetch or fetch_default,
            history=history or history_default,
            close_session=close,
        )


if __name__ == "__main__":
    unittest.main()
