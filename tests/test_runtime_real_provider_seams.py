from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.binance_provider import (
    MINUTE_MS,
    _parse_rest_kline,
    parse_binance_kline_message,
)
from src.market_discovery import MarketIdentity
from src.models import SourceEvent
from src.outbox import OutboxBroker
from src.polymarket_provider import (
    MarketBook,
    fetch_current_books,
    parse_market_ws_message,
)
from src.runtime_adapters import PolymarketRuntimeAdapter
from src.recovery import assess_c2_recovery
from src.runtime_orchestrator import C1RuntimeOrchestrator, _SourceWriteResult
from src.storage import SqliteStore
from tests.test_runtime_core_adversarial import FakePolymarket, _book


FIXTURES = Path("tests/fixtures")


def _closed_ws_payload(minute: int, *, close: str = "60010.00000000") -> dict:
    payload = json.loads(
        Path(FIXTURES, "binance_kline_closed.json").read_text(encoding="utf-8")
    )
    open_time = minute * MINUTE_MS
    payload["E"] = open_time + MINUTE_MS + 1
    payload["k"]["t"] = open_time
    payload["k"]["T"] = open_time + MINUTE_MS - 1
    payload["k"]["c"] = close
    return payload


def _matching_rest_row(minute: int, *, close: str = "60010.00000000") -> list:
    payload = _closed_ws_payload(minute, close=close)
    kline = payload["k"]
    return [
        kline["t"],
        kline["o"],
        kline["h"],
        kline["l"],
        kline["c"],
        kline["v"],
        kline["T"],
        kline["q"],
        kline["n"],
        kline["V"],
        kline["Q"],
        "0",
    ]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return copy.deepcopy(self._payload)


class _FakeBookSession:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def get(self, url, *, params):
        return _FakeResponse(self._payload)


class _RealParserBinance:
    def __init__(self, *, live_minutes: tuple[int, ...] = (4,)) -> None:
        self.recover_calls: list[tuple[int, int]] = []
        self.closed = False
        self._live_minutes = live_minutes

    async def recover(self, *, start_ms: int, end_ms: int) -> list[SourceEvent]:
        self.recover_calls.append((start_ms, end_ms))
        return [
            _parse_rest_kline(_matching_rest_row(open_time // MINUTE_MS))
            for open_time in range(start_ms, end_ms + 1, MINUTE_MS)
        ]

    async def stream(self, queue) -> None:
        for minute in self._live_minutes:
            event = parse_binance_kline_message(_closed_ws_payload(minute))
            assert event is not None
            await queue.put(event)
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True




class _FlagPolymarketStream:
    def __init__(self, order: list[str], live_event: SourceEvent) -> None:
        self._order = order
        self._live_event = live_event

    async def run(self, asset_ids, queue) -> None:
        self._order.append("stream")
        await queue.put(self._live_event)
        await asyncio.Event().wait()


class _BurstBinance(_RealParserBinance):
    async def stream(self, queue) -> None:
        for minute in (4, 5):
            event = parse_binance_kline_message(_closed_ws_payload(minute))
            assert event is not None
            await queue.put(event)
        await asyncio.Event().wait()


class _SlowPolymarket(FakePolymarket):
    async def discover_and_reconcile(self):
        await asyncio.sleep(0.05)
        return self.reconciliation


class BinanceCanonicalReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "events.sqlite3"))
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_rest_and_ws_same_closed_minute_share_authoritative_payload(self):
        rest = _parse_rest_kline(_matching_rest_row(1))
        live = parse_binance_kline_message(_closed_ws_payload(1))
        self.assertIsNotNone(live)

        self.assertEqual(rest.natural_key, live.natural_key)
        self.assertEqual(rest.source_timestamp_ms, live.source_timestamp_ms)
        self.assertEqual(rest.event_type, live.event_type)
        self.assertEqual(rest.payload_json, live.payload_json)
        self.assertEqual(rest.payload_sha256, live.payload_sha256)
        self.assertNotEqual(rest.recovery_origin, live.recovery_origin)

    def test_storage_accepts_same_authoritative_event_from_rest_then_ws(self):
        rest = _parse_rest_kline(_matching_rest_row(1))
        live = parse_binance_kline_message(_closed_ws_payload(1))
        self.assertIsNotNone(live)

        first = self.store.append_source_event(rest)
        replay = self.store.append_source_event(live)

        self.assertTrue(first.inserted)
        self.assertFalse(replay.inserted)
        self.assertEqual(first.event_id, replay.event_id)
        self.assertEqual(self.store.count("source_events"), 1)

    def test_signed_zero_decimal_formats_share_binance_identity(self):
        rest = _parse_rest_kline(_matching_rest_row(1, close="0.00000000"))
        live = parse_binance_kline_message(
            _closed_ws_payload(1, close="-0.00000000")
        )
        self.assertIsNotNone(live)

        self.assertEqual(rest.payload_json, live.payload_json)
        self.assertEqual(rest.payload_sha256, live.payload_sha256)

    def test_same_minute_with_different_ohlcv_remains_conflict(self):
        rest = _parse_rest_kline(_matching_rest_row(1))
        changed = parse_binance_kline_message(
            _closed_ws_payload(1, close="69999.00000000")
        )
        self.assertIsNotNone(changed)
        self.store.append_source_event(rest)

        with self.assertRaisesRegex(ValueError, "SOURCE_EVENT_CONFLICT"):
            self.store.append_source_event(changed)


class PolymarketCanonicalBookTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_book_is_backfill_but_ws_book_is_live(self):
        payload = json.loads(
            Path(FIXTURES, "polymarket_book.json").read_text(encoding="utf-8")
        )
        rest = (await fetch_current_books(_FakeBookSession(payload), [payload["asset_id"]]))[0]
        live = parse_market_ws_message(copy.deepcopy(payload))[0]

        self.assertEqual(rest.natural_key, live.natural_key)
        self.assertEqual(rest.payload_sha256, live.payload_sha256)
        self.assertEqual(rest.recovery_origin, "REST_BACKFILL")
        self.assertEqual(live.recovery_origin, "LIVE")

    async def test_rest_and_ws_same_book_normalize_transport_representation(self):
        rest_payload = json.loads(
            Path(FIXTURES, "polymarket_book.json").read_text(encoding="utf-8")
        )
        ws_payload = copy.deepcopy(rest_payload)
        rest_payload["timestamp"] = int(rest_payload["timestamp"])
        ws_payload["timestamp"] = str(ws_payload["timestamp"])
        rest_payload["bids"][0]["price"] = "0.4500"
        ws_payload["bids"][0]["price"] = "0.45"

        rest = (
            await fetch_current_books(
                _FakeBookSession(rest_payload),
                [rest_payload["asset_id"]],
            )
        )[0]
        live = parse_market_ws_message(ws_payload)[0]

        self.assertEqual(rest.natural_key, live.natural_key)
        self.assertEqual(rest.payload_json, live.payload_json)
        self.assertEqual(rest.payload_sha256, live.payload_sha256)
        with tempfile.TemporaryDirectory() as temp_directory:
            store = SqliteStore.open(Path(temp_directory, "books.sqlite3"))
            try:
                store.migrate()
                first = store.append_source_event(rest)
                replay = store.append_source_event(live)
            finally:
                store.close()
        self.assertTrue(first.inserted)
        self.assertFalse(replay.inserted)
        self.assertEqual(first.event_id, replay.event_id)

    async def test_price_change_updates_levels_and_current_book_hash(self):
        book_payload = json.loads(
            Path(FIXTURES, "polymarket_book.json").read_text(encoding="utf-8")
        )
        change_payload = json.loads(
            Path(FIXTURES, "polymarket_price_change.json").read_text(encoding="utf-8")
        )
        change_payload["timestamp"] = str(change_payload["timestamp"])
        book_event = parse_market_ws_message(book_payload)[0]
        changes = parse_market_ws_message(change_payload)
        book = MarketBook(book_payload["asset_id"])
        book.apply(book_event, source_event_id=1)

        for index, event in enumerate(changes, start=2):
            book.apply(event, source_event_id=index)
            self.assertEqual(book.book_hash, json.loads(event.payload_json)["hash"])

        self.assertEqual(book.best_bid_micros, 460_000)
        self.assertEqual(book.book_hash, "changehash-002")


class DynamicCutoverBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "runtime.sqlite3"))
        self.store.migrate()
        self.runtime: C1RuntimeOrchestrator | None = None

    async def asyncTearDown(self) -> None:
        if self.runtime is not None:
            await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    async def test_cutover_refreshes_closed_minute_boundary_after_discovery(self):
        binance = _RealParserBinance()
        polymarket = FakePolymarket(
            live=(_book("asset-00", 5 * MINUTE_MS, sequence=1, origin="LIVE"),)
        )
        boundary_values = iter((3 * MINUTE_MS, 3 * MINUTE_MS))
        self.runtime = C1RuntimeOrchestrator(
            run_id="real-seam",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=binance,
            polymarket_adapter=polymarket,
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            binance_end_resolver=lambda: next(boundary_values),
        )

        await asyncio.wait_for(self.runtime.start(), timeout=2)

        self.assertTrue(self.runtime.status().live_ready)
        self.assertEqual(binance.recover_calls, [(MINUTE_MS, 3 * MINUTE_MS)])
        self.assertEqual(
            [
                row[0]
                for row in self.store.rows(
                    """
                    SELECT source_timestamp_ms
                    FROM source_events
                    WHERE source = 'binance'
                    ORDER BY source_timestamp_ms
                    """
                )
            ],
            [MINUTE_MS, 2 * MINUTE_MS, 3 * MINUTE_MS, 4 * MINUTE_MS],
        )
        self.assertIsNone(self.runtime.status().failure)

    async def test_rest_backfill_and_ws_overlap_same_minute_are_idempotent(self):
        binance = _RealParserBinance(live_minutes=(3, 4))
        polymarket = FakePolymarket(
            live=(_book("asset-00", 5 * MINUTE_MS, sequence=1, origin="LIVE"),)
        )
        self.runtime = C1RuntimeOrchestrator(
            run_id="rest-ws-overlap",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=binance,
            polymarket_adapter=polymarket,
            binance_start_ms=MINUTE_MS,
            binance_end_ms=3 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            binance_end_resolver=lambda: 3 * MINUTE_MS,
        )

        await asyncio.wait_for(self.runtime.start(), timeout=2)

        self.assertTrue(self.runtime.status().live_ready)
        self.assertIsNone(self.runtime.status().failure)
        rows = self.store.rows(
            """
            SELECT source_timestamp_ms, COUNT(*)
            FROM source_events
            WHERE source = 'binance'
            GROUP BY source_timestamp_ms
            ORDER BY source_timestamp_ms
            """
        )
        self.assertEqual(
            rows,
            [
                (MINUTE_MS, 1),
                (2 * MINUTE_MS, 1),
                (3 * MINUTE_MS, 1),
                (4 * MINUTE_MS, 1),
            ],
        )

    async def test_cutover_extends_backfill_until_boundary_stabilizes(self):
        binance = _RealParserBinance(live_minutes=(5,))
        polymarket = FakePolymarket(
            live=(_book("asset-00", 6 * MINUTE_MS, sequence=1, origin="LIVE"),)
        )
        boundaries = iter((3 * MINUTE_MS, 4 * MINUTE_MS, 4 * MINUTE_MS))
        self.runtime = C1RuntimeOrchestrator(
            run_id="boundary-refresh",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=binance,
            polymarket_adapter=polymarket,
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            binance_end_resolver=lambda: next(boundaries),
        )

        await asyncio.wait_for(self.runtime.start(), timeout=2)

        self.assertTrue(self.runtime.status().live_ready)
        self.assertEqual(
            binance.recover_calls,
            [
                (MINUTE_MS, 3 * MINUTE_MS),
                (4 * MINUTE_MS, 4 * MINUTE_MS),
            ],
        )
        self.assertEqual(
            [
                row[0]
                for row in self.store.rows(
                    """
                    SELECT source_timestamp_ms
                    FROM source_events
                    WHERE source = 'binance'
                    ORDER BY source_timestamp_ms
                    """
                )
            ],
            [
                MINUTE_MS,
                2 * MINUTE_MS,
                3 * MINUTE_MS,
                4 * MINUTE_MS,
                5 * MINUTE_MS,
            ],
        )

    async def test_live_buffer_has_fail_closed_capacity_limit(self):
        self.runtime = C1RuntimeOrchestrator(
            run_id="buffer-bound",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=_BurstBinance(),
            polymarket_adapter=_SlowPolymarket(),
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            live_buffer_max_events=1,
        )

        with self.assertRaisesRegex(RuntimeError, "LIVE_BUFFER_OVERFLOW"):
            await asyncio.wait_for(self.runtime.start(), timeout=2)
        self.assertEqual(self.runtime.status().state, "RECOVERY_BLOCKED")
        self.assertFalse(self.runtime.status().live_ready)

    async def test_live_buffer_cutover_excludes_late_arrivals_from_drain_count(self):
        self.runtime = C1RuntimeOrchestrator(
            run_id="buffer-cutover",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=_RealParserBinance(live_minutes=()),
            polymarket_adapter=FakePolymarket(live=()),
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
        )
        initial = _book("asset-00", 5 * MINUTE_MS, sequence=1, origin="LIVE")
        late = _book("asset-00", 6 * MINUTE_MS, sequence=2, origin="LIVE")
        self.runtime._live_buffer.append(("polymarket", initial))
        self.runtime._buffering_live = True
        first_write_started = asyncio.Event()
        release_first_write = asyncio.Event()
        writes: list[tuple[str, bool]] = []

        async def controlled_submit_source(
            event: SourceEvent,
            *,
            authoritative_replay: bool = False,
        ) -> _SourceWriteResult:
            self.assertFalse(authoritative_replay)
            writes.append((event.natural_key, self.runtime._buffering_live))
            if event is initial:
                first_write_started.set()
                await asyncio.wait_for(release_first_write.wait(), timeout=1)
            return _SourceWriteResult(
                event_id=len(writes),
                inserted=True,
                snapshot=None,
            )

        self.runtime._submit_source = controlled_submit_source

        buffered_event_count = len(self.runtime._live_buffer)
        drain_task = asyncio.create_task(self.runtime._drain_live_buffer())
        await asyncio.wait_for(first_write_started.wait(), timeout=1)

        await self.runtime._ingest_live("polymarket", late)
        release_first_write.set()
        drained_event_count, _ = await asyncio.wait_for(drain_task, timeout=1)

        self.assertEqual(self.runtime._live_buffer, [])
        summary = assess_c2_recovery(
            binance_expected_closed_minutes=1,
            binance_recovered_closed_minutes=1,
            binance_missing_closed_minutes=0,
            binance_duplicate_count_after_dedup=0,
            market_count=11,
            asset_count=22,
            current_book_count=22,
            history_completed_asset_count=22,
            history_event_count=22,
            buffered_event_count=buffered_event_count,
            drained_event_count=drained_event_count,
            source_duplicate_count_after_dedup=0,
            writer_consumer_count=1,
            recovered_evaluation_committed=True,
            current_evaluation_committed=True,
            recovered_execution_eligible=False,
            current_execution_eligible=False,
        )
        self.assertEqual(summary.blockers, (), summary.as_dict())
        self.assertEqual(writes[-1], (late.natural_key, False))

    async def test_live_evidence_wait_has_fail_closed_timeout(self):
        self.runtime = C1RuntimeOrchestrator(
            run_id="live-evidence-timeout",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=_RealParserBinance(live_minutes=()),
            polymarket_adapter=FakePolymarket(live=()),
            binance_start_ms=MINUTE_MS,
            binance_end_ms=2 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            binance_end_resolver=lambda: 2 * MINUTE_MS,
            live_evidence_timeout_seconds=0.05,
        )

        with self.assertRaisesRegex(RuntimeError, "LIVE_EVIDENCE_TIMEOUT"):
            await asyncio.wait_for(self.runtime.start(), timeout=1)
        self.assertEqual(self.runtime.status().state, "RECOVERY_BLOCKED")
        self.assertFalse(self.runtime.status().live_ready)

    async def test_real_polymarket_adapter_starts_stream_before_book_fetch(self):
        order: list[str] = []
        asset_ids = tuple(f"asset-{index:02d}" for index in range(22))
        market_ids = tuple(f"market-{index:02d}" for index in range(11))
        identity = MarketIdentity(
            event_id="event-real",
            event_slug="bitcoin-price-on-2026-08-01",
            active=True,
            resolution_utc=(
                datetime.now(timezone.utc).replace(microsecond=0)
                + timedelta(days=1)
            ),
            market_ids=market_ids,
            outcomes=tuple(
                outcome
                for _ in range(11)
                for outcome in ("YES", "NO")
            ),
            asset_ids=asset_ids,
        )

        async def event_loader():
            return []

        def discover(_payload, *, now_utc):
            return identity

        async def fetch_books(_session, requested_assets):
            self.assertIn("stream", order)
            order.append("books")
            self.assertEqual(tuple(requested_assets), asset_ids)
            return [
                _book(asset_id, 4 * MINUTE_MS)
                for asset_id in asset_ids
            ]

        async def history(_session, *, asset_id, start_ts, end_ts):
            if False:
                yield asset_id

        live_book = _book(
            asset_ids[0],
            5 * MINUTE_MS,
            sequence=1,
            origin="LIVE",
        )
        polymarket = PolymarketRuntimeAdapter(
            session=object(),
            stream=_FlagPolymarketStream(order, live_book),
            event_loader=event_loader,
            now_utc=lambda: datetime.now(timezone.utc),
            discover=discover,
            fetch_books=fetch_books,
            history=history,
        )
        binance = _RealParserBinance()
        self.runtime = C1RuntimeOrchestrator(
            run_id="poly-seam",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=binance,
            polymarket_adapter=polymarket,
            binance_start_ms=MINUTE_MS,
            binance_end_ms=3 * MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            binance_end_resolver=lambda: 3 * MINUTE_MS,
        )

        await asyncio.wait_for(self.runtime.start(), timeout=2)

        self.assertTrue(self.runtime.status().live_ready)
        self.assertEqual(order[:2], ["stream", "books"])


if __name__ == "__main__":
    unittest.main()
