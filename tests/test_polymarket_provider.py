from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession, WSMsgType, web

import src.polymarket_provider as polymarket_provider
from src.market_discovery import (
    MarketIdentity,
    discover_active_btc_daily_range,
)
from src.polymarket_provider import (
    MarketBook,
    PolymarketStream,
    fetch_current_books,
    iter_price_history,
    parse_market_ws_message,
)
try:
    from src.polymarket_provider import ReconnectPolicy
except ImportError:
    ReconnectPolicy = None


FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def load_fixture(name):
    return json.loads(Path(FIXTURES, name).read_text(encoding="utf-8"))


def synthetic_book(asset_id, book_hash):
    return {
        "asks": [{"price": "0.55", "size": "10"}],
        "asset_id": asset_id,
        "bids": [{"price": "0.45", "size": "10"}],
        "event_type": "book",
        "hash": book_hash,
        "last_trade_price": "0.50",
        "market": "synthetic-market",
        "tick_size": "0.01",
        "timestamp": "1785556800000",
    }


def parse_frame(
    payload,
    subscribed_asset_ids,
    *,
    allow_initial_book_batch=True,
    incident_sink=None,
):
    parser = getattr(
        polymarket_provider,
        "parse_market_ws_frame",
        parse_market_ws_message,
    )
    if parser is parse_market_ws_message:
        return tuple(parser(payload, incident_sink=incident_sink))
    return parser(
        payload,
        subscribed_asset_ids=subscribed_asset_ids,
        allow_initial_book_batch=allow_initial_book_batch,
        incident_sink=incident_sink,
    )


def discovery_event(
    *,
    event_id: str,
    end_date: str,
    current_naming: bool,
) -> dict:
    event = copy.deepcopy(load_fixture("gamma_btc_daily_range.json")[0])
    date = end_date[:10]
    event["id"] = event_id
    event["endDate"] = end_date
    event["slug"] = f"bitcoin-price-on-{date}"
    event["title"] = f"Bitcoin price on {date}?"
    event["ticker"] = (
        f"bitcoin-price-on-{date}"
        if current_naming
        else f"BTC-DAILY-RANGE-{date}"
    )
    for index, market in enumerate(event["markets"]):
        market["id"] = f"{event_id}-bucket-{index:02d}"
        market["clobTokenIds"] = json.dumps(
            [
                f"{event_id}-token-{index:02d}-yes",
                f"{event_id}-token-{index:02d}-no",
            ]
        )
    return event


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params):
        self.calls.append((url, dict(params)))
        if not self.responses:
            raise AssertionError("unexpected request")
        return FakeResponse(self.responses.pop(0))


class RawHistoryResponse(FakeResponse):
    def __init__(self, body, status=200):
        super().__init__(None, status=status)
        self.body = body
        self.json_calls = 0
        self.read_calls = 0

    async def json(self):
        self.json_calls += 1
        raise AssertionError("price history must use Decimal-aware raw JSON")

    async def read(self):
        self.read_calls += 1
        return self.body


class RawHistorySession:
    def __init__(self, body):
        self.response = RawHistoryResponse(body)
        self.calls = []

    def get(self, url, *, params):
        self.calls.append((url, dict(params)))
        return self.response


class PolymarketWireBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _book_with_timestamp(timestamp):
        payload = load_fixture("polymarket_book.json")
        payload["timestamp"] = timestamp
        return payload

    @staticmethod
    async def _history_events(session):
        return [
            event
            async for event in iter_price_history(
                session,
                asset_id="token-00-yes",
                start_ts=100,
                end_ts=100,
            )
        ]

    async def test_rest_book_accepts_digit_only_string_timestamp(self):
        payload = self._book_with_timestamp("1785556800000")

        events = await fetch_current_books(
            FakeSession([payload]),
            ["token-00-yes"],
        )

        self.assertEqual(events[0].source_timestamp_ms, 1785556800000)

    async def test_rest_book_int_and_string_timestamp_have_same_identity(self):
        int_event = (
            await fetch_current_books(
                FakeSession([self._book_with_timestamp(1785556800000)]),
                ["token-00-yes"],
            )
        )[0]
        string_event = (
            await fetch_current_books(
                FakeSession([self._book_with_timestamp("1785556800000")]),
                ["token-00-yes"],
            )
        )[0]

        self.assertEqual(
            (int_event.source_timestamp_ms, int_event.natural_key),
            (string_event.source_timestamp_ms, string_event.natural_key),
        )

    def test_ws_book_accepts_digit_only_string_timestamp(self):
        event = parse_market_ws_message(
            self._book_with_timestamp("1785556800000")
        )[0]

        self.assertEqual(event.source_timestamp_ms, 1785556800000)

    async def test_ws_and_rest_book_share_timestamp_policy(self):
        payload = self._book_with_timestamp("1785556800000")
        rest_event = (
            await fetch_current_books(FakeSession([payload]), ["token-00-yes"])
        )[0]
        ws_event = parse_market_ws_message(copy.deepcopy(payload))[0]

        self.assertEqual(
            (rest_event.source_timestamp_ms, rest_event.natural_key),
            (ws_event.source_timestamp_ms, ws_event.natural_key),
        )

    def test_bool_book_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(self._book_with_timestamp(True))

    def test_negative_string_book_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(self._book_with_timestamp("-1"))

    def test_whitespace_book_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(self._book_with_timestamp(" 1785556800000"))

    def test_fractional_string_book_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(self._book_with_timestamp("123.0"))

    def test_exponent_string_book_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(self._book_with_timestamp("1e3"))

    def test_non_numeric_book_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(self._book_with_timestamp("abc"))

    async def test_history_json_fraction_decodes_as_decimal_not_float(self):
        session = RawHistorySession(
            b'{"history":[{"t":100,"p":0.456}]}'
        )

        events = await self._history_events(session)

        payload = json.loads(events[0].payload_json)
        self.assertEqual(payload["price_micros"], 456000)
        self.assertEqual(payload["provider_point"]["p"], "0.456")
        self.assertEqual(session.response.read_calls, 1)
        self.assertEqual(session.response.json_calls, 0)


class PolymarketInitialBookBatchTests(unittest.IsolatedAsyncioTestCase):
    def test_probe_like_two_book_batch_returns_two_events(self):
        assets = ("synthetic-yes", "synthetic-no")
        payload = [
            synthetic_book(assets[0], "batch-hash-yes"),
            synthetic_book(assets[1], "batch-hash-no"),
        ]

        events = parse_frame(payload, assets)

        self.assertEqual(len(events), 2)
        self.assertEqual(
            [json.loads(event.payload_json)["asset_id"] for event in events],
            list(assets),
        )

    def test_batch_preserves_wire_order(self):
        assets = ("synthetic-yes", "synthetic-no")
        payload = [
            synthetic_book(assets[1], "batch-hash-no"),
            synthetic_book(assets[0], "batch-hash-yes"),
        ]

        events = parse_frame(payload, assets)

        self.assertEqual(
            [json.loads(event.payload_json)["asset_id"] for event in events],
            [assets[1], assets[0]],
        )

    def test_single_book_object_remains_accepted(self):
        payload = synthetic_book("synthetic-yes", "single-book")

        events = parse_frame(payload, ("synthetic-yes", "synthetic-no"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "POLYMARKET_BOOK")

    def test_single_price_change_object_remains_accepted(self):
        payload = load_fixture("polymarket_price_change.json")

        events = parse_frame(payload, ("token-00-yes", "token-00-no"))

        self.assertTrue(events)
        self.assertTrue(
            all(
                event.event_type == "POLYMARKET_PRICE_CHANGE"
                for event in events
            )
        )

    def test_twenty_two_unique_subscribed_books_are_accepted(self):
        assets = tuple(f"synthetic-asset-{index:02d}" for index in range(22))
        payload = [
            synthetic_book(asset_id, f"batch-hash-{index:02d}")
            for index, asset_id in enumerate(assets)
        ]

        events = parse_frame(payload, assets)

        self.assertEqual(len(events), 22)
        self.assertEqual(
            [json.loads(event.payload_json)["asset_id"] for event in events],
            list(assets),
        )

    def test_book_array_after_initial_frame_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_BOOK_BATCH_NOT_INITIAL",
        ):
            parse_frame(
                [synthetic_book("synthetic-yes", "batch-hash-yes")],
                ("synthetic-yes",),
                allow_initial_book_batch=False,
            )

    def test_empty_array_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_EMPTY_INITIAL_BOOK_BATCH",
        ):
            parse_frame([], ("synthetic-yes", "synthetic-no"))

    def test_mixed_object_and_primitive_array_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_INVALID_BOOK_BATCH_ELEMENT",
        ):
            parse_frame(
                [synthetic_book("synthetic-yes", "hash-yes"), 1],
                ("synthetic-yes", "synthetic-no"),
            )

    def test_nested_array_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_INVALID_BOOK_BATCH_ELEMENT",
        ):
            parse_frame(
                [[synthetic_book("synthetic-yes", "hash-yes")]],
                ("synthetic-yes",),
            )

    def test_price_change_array_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_UNSUPPORTED_BATCH_EVENT_TYPE",
        ):
            parse_frame(
                [load_fixture("polymarket_price_change.json")],
                ("token-00-yes", "token-00-no"),
            )

    def test_service_object_array_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_UNSUPPORTED_BATCH_EVENT_TYPE",
        ):
            parse_frame(
                [{"type": "subscribed"}, {"type": "connected"}],
                ("synthetic-yes", "synthetic-no"),
            )

    def test_duplicate_batch_asset_id_is_rejected(self):
        payload = [
            synthetic_book("synthetic-yes", "hash-one"),
            synthetic_book("synthetic-yes", "hash-two"),
        ]
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_DUPLICATE_BATCH_ASSET_ID",
        ):
            parse_frame(payload, ("synthetic-yes", "synthetic-no"))

    def test_unsubscribed_batch_asset_id_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_UNSUBSCRIBED_BATCH_ASSET_ID",
        ):
            parse_frame(
                [synthetic_book("synthetic-unknown", "hash-unknown")],
                ("synthetic-yes", "synthetic-no"),
            )

    def test_batch_longer_than_subscription_is_rejected(self):
        payload = [
            synthetic_book(f"synthetic-{index}", f"hash-{index}")
            for index in range(3)
        ]
        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_INITIAL_BOOK_BATCH_TOO_LARGE",
        ):
            parse_frame(payload, ("synthetic-0", "synthetic-1"))

    def test_invalid_second_book_rejects_whole_batch(self):
        invalid = synthetic_book("synthetic-no", "hash-no")
        del invalid["hash"]
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_frame(
                [
                    synthetic_book("synthetic-yes", "hash-yes"),
                    invalid,
                ],
                ("synthetic-yes", "synthetic-no"),
            )

    def test_invalid_first_book_is_rejected(self):
        invalid = synthetic_book("synthetic-yes", "hash-yes")
        del invalid["timestamp"]
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_frame(
                [invalid],
                ("synthetic-yes", "synthetic-no"),
            )

    def test_malformed_levels_block_whole_batch(self):
        invalid = synthetic_book("synthetic-no", "hash-no")
        invalid["asks"] = "not-a-list"
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_frame(
                [
                    synthetic_book("synthetic-yes", "hash-yes"),
                    invalid,
                ],
                ("synthetic-yes", "synthetic-no"),
            )

    def test_primitive_top_level_remains_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_frame("not-an-object", ("synthetic-yes",))

    def test_unknown_single_object_behavior_does_not_regress(self):
        incidents = []
        payload = {
            "asset_id": "synthetic-yes",
            "event_type": "future_provider_event",
            "timestamp": 1785556800000,
        }

        events = parse_frame(
            payload,
            ("synthetic-yes",),
            incident_sink=incidents.append,
        )

        self.assertEqual(events[0].event_type, "UNHANDLED_PROVIDER_EVENT")
        self.assertEqual(incidents[0]["code"], "UNHANDLED_PROVIDER_EVENT")

    async def test_local_ws_batch_enqueues_two_events_in_wire_order(self):
        assets = ("synthetic-yes", "synthetic-no")
        connections = 0
        incidents = []

        async def handler(request):
            nonlocal connections
            connections += 1
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            subscription = await websocket.receive_json(timeout=1)
            self.assertEqual(tuple(subscription["assets_ids"]), assets)
            await websocket.send_json(
                [
                    synthetic_book(assets[1], "hash-no"),
                    synthetic_book(assets[0], "hash-yes"),
                ]
            )
            async for _message in websocket:
                pass
            return websocket

        runner, websocket_url = await self._start_ws_server(handler)
        try:
            async with ClientSession() as session:
                queue = asyncio.Queue()
                stream = PolymarketStream(
                    session,
                    websocket_url=websocket_url,
                    incident_sink=incidents.append,
                )
                task = asyncio.create_task(stream.run(assets, queue))
                try:
                    events = [
                        await asyncio.wait_for(queue.get(), timeout=1)
                        for _ in range(2)
                    ]
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        finally:
            await runner.cleanup()

        self.assertEqual(
            [json.loads(event.payload_json)["asset_id"] for event in events],
            [assets[1], assets[0]],
        )
        self.assertEqual(connections, 1)
        self.assertEqual(incidents, [])

    async def test_local_ws_invalid_second_element_enqueues_nothing(self):
        assets = ("synthetic-yes", "synthetic-no")
        invalid = synthetic_book(assets[1], "hash-no")
        invalid["asks"] = "not-a-list"

        async def handler(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive_json(timeout=1)
            await websocket.send_json(
                [synthetic_book(assets[0], "hash-yes"), invalid]
            )
            await websocket.close()
            return websocket

        runner, websocket_url = await self._start_ws_server(handler)
        try:
            async with ClientSession() as session:
                queue = asyncio.Queue()
                stream = PolymarketStream(
                    session,
                    websocket_url=websocket_url,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "POLYMARKET_INVALID_TYPE",
                ):
                    await stream.run(assets, queue)
                self.assertTrue(queue.empty())
        finally:
            await runner.cleanup()

    @staticmethod
    async def _start_ws_server(handler):
        app = web.Application()
        app.router.add_get("/ws/market", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}/ws/market"


class _ReconnectWebSocket:
    def __init__(self, messages=(), *, error=None):
        self._messages = list(messages)
        self._error = error
        self.sent_json = []
        self.sent_text = []

    async def send_json(self, value):
        self.sent_json.append(value)

    async def send_str(self, value):
        self.sent_text.append(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self._messages:
            return self._messages.pop(0)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        raise StopAsyncIteration


class _ReconnectContext:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ReconnectSession:
    def __init__(self, websockets):
        self.websockets = list(websockets)
        self.connect_calls = 0

    def ws_connect(self, url):
        self.connect_calls += 1
        if not self.websockets:
            raise asyncio.CancelledError
        return _ReconnectContext(self.websockets.pop(0))


class PolymarketReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_close_reconnects_and_reports_incident(self):
        first = _ReconnectWebSocket()
        second = _ReconnectWebSocket()
        session = _ReconnectSession([first, second])
        incidents = []
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 2:
                raise asyncio.CancelledError

        stream = PolymarketStream(
            session,
            incident_sink=incidents.append,
            sleep=sleep,
            jitter=lambda: 1.0,
        )
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(["yes", "no"], asyncio.Queue())

        self.assertEqual(session.connect_calls, 2)
        self.assertEqual(delays, [1.0, 2.0])
        self.assertEqual(
            [item["code"] for item in incidents],
            ["POLYMARKET_STREAM_DISCONNECTED"] * 2,
        )

    async def test_transient_connection_errors_reconnect(self):
        session = _ReconnectSession(
            [_ReconnectWebSocket(error=OSError("offline"))]
        )
        delays = []

        async def sleep(delay):
            delays.append(delay)
            raise asyncio.CancelledError

        stream = PolymarketStream(
            session,
            sleep=sleep,
            jitter=lambda: 1.0,
        )
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(["yes", "no"], asyncio.Queue())
        self.assertEqual(delays, [1.0])

    def test_backoff_is_exponential_and_capped_with_jitter(self):
        policy = ReconnectPolicy(jitter=lambda: 1.0)
        self.assertEqual(
            [policy.next_delay() for _ in range(7)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    def test_healthy_connection_resets_backoff(self):
        policy = ReconnectPolicy(jitter=lambda: 1.0)
        self.assertEqual(policy.next_delay(), 1.0)
        self.assertEqual(policy.next_delay(), 2.0)
        policy.record_healthy_duration(300.0)
        self.assertEqual(policy.next_delay(), 1.0)

    async def test_ready_is_set_only_after_subscription(self):
        websocket = _ReconnectWebSocket()
        session = _ReconnectSession([websocket])
        ready = asyncio.Event()

        async def sleep(_delay):
            raise asyncio.CancelledError

        stream = PolymarketStream(session, sleep=sleep)
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(["yes", "no"], asyncio.Queue(), ready_event=ready)
        self.assertTrue(ready.is_set())
        self.assertEqual(len(websocket.sent_json), 1)

    async def test_heartbeat_is_cancelled_before_reconnect(self):
        websocket = _ReconnectWebSocket()
        session = _ReconnectSession([websocket])
        cancelled = asyncio.Event()

        async def heartbeat(_websocket):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def sleep(_delay):
            raise asyncio.CancelledError

        stream = PolymarketStream(session, sleep=sleep)
        stream._heartbeat = heartbeat
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(["yes", "no"], asyncio.Queue())
        self.assertTrue(cancelled.is_set())

    async def test_cancellation_is_immediate_and_has_no_incident(self):
        incidents = []
        session = _ReconnectSession([])
        stream = PolymarketStream(session, incident_sink=incidents.append)
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(["yes", "no"], asyncio.Queue())
        self.assertEqual(incidents, [])

    async def test_malformed_json_propagates_without_reconnect(self):
        message = SimpleNamespace(type=WSMsgType.TEXT, data="{")
        session = _ReconnectSession([_ReconnectWebSocket([message])])
        stream = PolymarketStream(session)
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_WS_JSON"):
            await stream.run(["yes", "no"], asyncio.Queue())
        self.assertEqual(session.connect_calls, 1)

    async def test_reconnected_event_reaches_runtime_queue(self):
        payload = load_fixture("polymarket_book.json")
        message = SimpleNamespace(
            type=WSMsgType.TEXT,
            data=json.dumps(payload),
        )
        session = _ReconnectSession(
            [_ReconnectWebSocket(), _ReconnectWebSocket([message])]
        )
        queue = asyncio.Queue()

        async def sleep(_delay):
            return None

        stream = PolymarketStream(
            session,
            sleep=sleep,
            jitter=lambda: 1.0,
        )
        task = asyncio.create_task(stream.run(["yes", "no"], queue))
        event = await asyncio.wait_for(queue.get(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(event.event_type, "POLYMARKET_BOOK")

    async def test_reconnect_does_not_leave_duplicate_heartbeat_tasks(self):
        session = _ReconnectSession(
            [_ReconnectWebSocket(), _ReconnectWebSocket()]
        )
        active = 0
        maximum = 0

        async def heartbeat(_websocket):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        calls = 0

        async def sleep(_delay):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError

        stream = PolymarketStream(session, sleep=sleep)
        stream._heartbeat = heartbeat
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(["yes", "no"], asyncio.Queue())
        self.assertEqual(maximum, 1)
        self.assertEqual(active, 0)


class PolymarketHistoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _history_events(session):
        return [
            event
            async for event in iter_price_history(
                session,
                asset_id="token-00-yes",
                start_ts=100,
                end_ts=100,
            )
        ]

    async def test_history_decimal_price_is_accepted(self):
        events = await self._history_events(
            FakeSession([{"history": [{"t": 100, "p": Decimal("0.456")}]}])
        )

        self.assertEqual(
            json.loads(events[0].payload_json)["price_micros"],
            456000,
        )

    async def test_history_decimal_string_price_remains_accepted(self):
        events = await self._history_events(
            FakeSession([{"history": [{"t": 100, "p": "0.456"}]}])
        )

        self.assertEqual(
            json.loads(events[0].payload_json)["price_micros"],
            456000,
        )

    async def test_history_exact_integer_price_is_accepted(self):
        events = await self._history_events(
            FakeSession([{"history": [{"t": 100, "p": 1}]}])
        )

        self.assertEqual(
            json.loads(events[0].payload_json)["price_micros"],
            1000000,
        )

    async def test_history_direct_float_price_is_rejected(self):
        session = FakeSession([{"history": [{"t": 100, "p": 0.456}]}])

        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            await self._history_events(session)

    async def test_history_malformed_and_non_finite_prices_are_rejected(self):
        invalid_prices = (
            "not-a-decimal",
            "NaN",
            "Infinity",
            "-Infinity",
            Decimal("NaN"),
            Decimal("Infinity"),
        )
        for price in invalid_prices:
            with self.subTest(price=price):
                session = FakeSession([{"history": [{"t": 100, "p": price}]}])
                with self.assertRaises(ValueError):
                    await self._history_events(session)


class PolymarketHistorySequenceDiagnosticTests(
    unittest.IsolatedAsyncioTestCase
):
    @staticmethod
    async def _failure_text(responses, *, start_ts=0, end_ts=300):
        session = FakeSession(responses)
        with unittest.TestCase().assertRaisesRegex(
            ValueError,
            "POLYMARKET_HISTORY_SEQUENCE_ERROR",
        ) as raised:
            _ = [
                event
                async for event in iter_price_history(
                    session,
                    asset_id="synthetic-history-asset",
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
            ]
        return str(raised.exception)

    @staticmethod
    def _diagnostic(error_text):
        prefix = "POLYMARKET_HISTORY_SEQUENCE_ERROR:"
        if not error_text.startswith(prefix):
            return {}
        return json.loads(error_text.removeprefix(prefix))

    async def test_duplicate_within_page_has_sanitized_diagnostic(self):
        error_text = await self._failure_text(
            [
                {
                    "history": [
                        {"t": 100, "p": "0.45"},
                        {"t": 100, "p": "0.45"},
                    ]
                }
            ]
        )

        self.assertEqual(
            self._diagnostic(error_text),
            {
                "adjacent_relations": {"EQ": 1, "GT": 0, "LT": 0},
                "cross_page_overlap_count": 0,
                "direction": "ALL_EQUAL",
                "duplicate_position_count": 1,
                "duplicate_positions": [1],
                "duplicate_positions_truncated": False,
                "first_rejected_position": 1,
                "item_count": 2,
                "page_index": 0,
                "rejection_category": "DUPLICATE_WITHIN_PAGE",
                "request_range_sha256": unittest.mock.ANY,
                "timestamp_type_histogram": {"int": 2},
            },
        )
        self.assertEqual(
            len(self._diagnostic(error_text)["request_range_sha256"]),
            64,
        )

    async def test_cross_page_overlap_is_distinguished(self):
        error_text = await self._failure_text(
            [
                {
                    "history": [
                        {"t": 100, "p": "0.45"},
                        {"t": 160, "p": "0.46"},
                    ]
                },
                {
                    "history": [
                        {"t": 160, "p": "0.46"},
                        {"t": 220, "p": "0.47"},
                    ]
                },
            ],
            start_ts=100,
            end_ts=220,
        )

        diagnostic = self._diagnostic(error_text)
        self.assertEqual(diagnostic["page_index"], 1)
        self.assertEqual(
            diagnostic["rejection_category"],
            "CROSS_PAGE_OVERLAP",
        )
        self.assertEqual(diagnostic["cross_page_overlap_count"], 1)
        self.assertEqual(diagnostic["first_rejected_position"], 0)

    async def test_non_monotonic_page_is_distinguished(self):
        error_text = await self._failure_text(
            [
                {
                    "history": [
                        {"t": 100, "p": "0.45"},
                        {"t": 90, "p": "0.46"},
                    ]
                }
            ]
        )

        diagnostic = self._diagnostic(error_text)
        self.assertEqual(diagnostic["direction"], "STRICT_DESCENDING")
        self.assertEqual(
            diagnostic["rejection_category"],
            "NON_INCREASING_WITHIN_PAGE",
        )
        self.assertEqual(
            diagnostic["adjacent_relations"],
            {"EQ": 0, "GT": 1, "LT": 0},
        )

    async def test_diagnostic_contains_no_raw_values_or_identifier(self):
        raw_timestamp = 1785456789
        raw_asset = "synthetic-history-asset"
        raw_price = "0.123456"
        error_text = await self._failure_text(
            [
                {
                    "history": [
                        {"t": raw_timestamp, "p": raw_price},
                        {"t": raw_timestamp, "p": raw_price},
                    ]
                }
            ],
            start_ts=raw_timestamp,
            end_ts=raw_timestamp,
        )

        self.assertNotIn(str(raw_timestamp), error_text)
        self.assertNotIn(raw_asset, error_text)
        self.assertNotIn(raw_price, error_text)
        self.assertLessEqual(len(error_text), 500)

    def test_diagnostic_remains_bounded_for_adversarial_page_shape(self):
        timestamp_values = [1] * 64
        history = [
            {"t": value, "p": "0.1"} for value in timestamp_values
        ]

        error = polymarket_provider._history_sequence_error(
            history=history,
            page_index=999,
            request_start=1,
            request_end=9_999_999_999,
            prior_seen={1},
            first_rejected_position=len(history) - 1,
            rejection_category="DUPLICATE_WITHIN_PAGE",
        )

        self.assertIs(type(error), ValueError)
        self.assertLessEqual(len(str(error)), 500)


class PolymarketProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_current_gamma_naming_is_accepted(self):
        event = discovery_event(
            event_id="current-2026-07-29",
            end_date="2026-07-29T16:00:00Z",
            current_naming=True,
        )

        identity = discover_active_btc_daily_range(
            [event],
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "current-2026-07-29")

    def test_legacy_gamma_naming_remains_accepted(self):
        event = discovery_event(
            event_id="legacy-2026-07-29",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )

        identity = discover_active_btc_daily_range(
            [event],
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "legacy-2026-07-29")

    def test_multiple_future_events_select_nearest_resolution(self):
        events = [
            discovery_event(
                event_id="later",
                end_date="2026-07-31T16:00:00Z",
                current_naming=False,
            ),
            discovery_event(
                event_id="nearest",
                end_date="2026-07-29T16:00:00Z",
                current_naming=False,
            ),
            discovery_event(
                event_id="middle",
                end_date="2026-07-30T16:00:00Z",
                current_naming=False,
            ),
        ]

        identity = discover_active_btc_daily_range(
            events,
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "nearest")

    def test_nearest_selection_is_independent_of_input_order(self):
        nearest = discovery_event(
            event_id="nearest",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )
        later = discovery_event(
            event_id="later",
            end_date="2026-07-30T16:00:00Z",
            current_naming=False,
        )
        now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)

        forward = discover_active_btc_daily_range(
            [nearest, later],
            now_utc=now,
        )
        reverse = discover_active_btc_daily_range(
            [later, nearest],
            now_utc=now,
        )

        self.assertEqual(forward, reverse)

    def test_equal_nearest_resolution_is_ambiguous(self):
        first = discovery_event(
            event_id="first",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )
        second = discovery_event(
            event_id="second",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )

        with self.assertRaisesRegex(
            ValueError,
            "AMBIGUOUS_BTC_DAILY_RANGE",
        ):
            discover_active_btc_daily_range(
                [first, second],
                now_utc=datetime(
                    2026,
                    7,
                    29,
                    12,
                    tzinfo=timezone.utc,
                ),
            )

    def test_later_valid_event_does_not_create_ambiguity(self):
        nearest = discovery_event(
            event_id="nearest",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )
        later = discovery_event(
            event_id="rollover",
            end_date="2026-08-04T16:00:00Z",
            current_naming=False,
        )

        identity = discover_active_btc_daily_range(
            [nearest, later],
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "nearest")

    def test_past_event_is_ignored(self):
        past = discovery_event(
            event_id="past",
            end_date="2026-07-28T16:00:00Z",
            current_naming=False,
        )
        future = discovery_event(
            event_id="future",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )

        identity = discover_active_btc_daily_range(
            [past, future],
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "future")

    def test_structurally_invalid_current_event_is_ignored(self):
        invalid = discovery_event(
            event_id="invalid-current",
            end_date="2026-07-29T14:00:00Z",
            current_naming=True,
        )
        invalid["markets"].pop()
        valid = discovery_event(
            event_id="valid-current",
            end_date="2026-07-29T16:00:00Z",
            current_naming=True,
        )

        identity = discover_active_btc_daily_range(
            [invalid, valid],
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "valid-current")

    def test_no_valid_event_preserves_data_gap(self):
        invalid = discovery_event(
            event_id="invalid-current",
            end_date="2026-07-29T16:00:00Z",
            current_naming=True,
        )
        invalid["markets"] = []

        with self.assertRaisesRegex(
            ValueError,
            "BTC_DAILY_RANGE_DATA_GAP",
        ):
            discover_active_btc_daily_range(
                [invalid],
                now_utc=datetime(
                    2026,
                    7,
                    29,
                    12,
                    tzinfo=timezone.utc,
                ),
            )

    def test_duplicate_event_id_does_not_create_false_ambiguity(self):
        event = discovery_event(
            event_id="same-id",
            end_date="2026-07-29T16:00:00Z",
            current_naming=False,
        )

        identity = discover_active_btc_daily_range(
            [event, copy.deepcopy(event)],
            now_utc=datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(identity.event_id, "same-id")

    def test_discovery_selects_one_active_future_resolution_event(self):
        identity = discover_active_btc_daily_range(
            load_fixture("gamma_btc_daily_range.json"),
            now_utc=NOW,
        )
        self.assertIsInstance(identity, MarketIdentity)
        self.assertTrue(identity.active)
        self.assertEqual(len(identity.outcomes), 11)
        self.assertEqual(len(identity.asset_ids), 22)
        self.assertEqual(len(set(identity.asset_ids)), 22)

    def test_market_identity_is_immutable(self):
        identity = discover_active_btc_daily_range(
            load_fixture("gamma_btc_daily_range.json"),
            now_utc=NOW,
        )
        with self.assertRaisesRegex(
            (AttributeError, TypeError), "cannot assign|read-only|super"
        ):
            identity.event_id = "changed"

    def test_ambiguous_discovery_is_rejected(self):
        payload = load_fixture("gamma_btc_daily_range.json")
        duplicate = copy.deepcopy(payload[0])
        duplicate["id"] = "second-eligible-event"
        duplicate["slug"] = "bitcoin-price-on-august-2-2026"
        duplicate["ticker"] = "BTC-DAILY-RANGE-2026-08-02"
        for index, market in enumerate(duplicate["markets"]):
            market["id"] = f"second-bucket-{index:02d}"
            market["clobTokenIds"] = json.dumps(
                [f"second-{index:02d}-yes", f"second-{index:02d}-no"]
            )
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_BTC_DAILY_RANGE"):
            discover_active_btc_daily_range(
                payload + [duplicate],
                now_utc=NOW,
            )

    def test_no_matching_discovery_is_explicit_data_gap(self):
        payload = load_fixture("gamma_btc_daily_range.json")
        payload[0]["active"] = False
        with self.assertRaisesRegex(ValueError, "BTC_DAILY_RANGE_DATA_GAP"):
            discover_active_btc_daily_range(payload, now_utc=NOW)

    def test_duplicate_asset_ids_are_rejected(self):
        payload = load_fixture("gamma_btc_daily_range.json")
        first_ids = json.loads(payload[0]["markets"][0]["clobTokenIds"])
        payload[0]["markets"][1]["clobTokenIds"] = json.dumps(first_ids)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_MARKET_ASSET_ID"):
            discover_active_btc_daily_range(payload, now_utc=NOW)

    def test_book_key_contains_provider_hash_and_fixed_point_levels(self):
        event = parse_market_ws_message(
            load_fixture("polymarket_book.json")
        )[0]
        payload = json.loads(event.payload_json)
        bids_by_price = {
            level["price_micros"]: level["size_micros"]
            for level in payload["bids"]
        }
        self.assertIn("bookhash-001", event.natural_key)
        self.assertEqual(bids_by_price[450000], 10250000)
        self.assertIs(type(payload["bids"][0]["price_micros"]), int)
        self.assertIs(type(payload["bids"][0]["size_micros"]), int)

    def test_book_snapshot_replaces_previous_levels(self):
        book = MarketBook("token-00-yes")
        first = parse_market_ws_message(
            load_fixture("polymarket_book.json")
        )[0]
        book.apply(first, source_event_id=1)

        replacement_payload = load_fixture("polymarket_book.json")
        replacement_payload["hash"] = "bookhash-002"
        replacement_payload["timestamp"] += 1000
        replacement_payload["bids"] = [{"price": "0.44", "size": "2"}]
        replacement = parse_market_ws_message(replacement_payload)[0]
        book.apply(replacement, source_event_id=2)
        self.assertEqual(book.bids, {440000: 2000000})
        self.assertEqual(book.best_bid_micros, 440000)
        self.assertEqual(book.best_ask_micros, 550000)
        self.assertEqual(book.spread_micros, 110000)

    def test_zero_size_price_change_removes_level(self):
        book = MarketBook("token-00-yes")
        book.apply(
            parse_market_ws_message(
                load_fixture("polymarket_book.json")
            )[0],
            source_event_id=1,
        )
        changes = parse_market_ws_message(
            load_fixture("polymarket_price_change.json")
        )
        for index, event in enumerate(reversed(changes), start=2):
            book.apply(event, source_event_id=index)
        self.assertNotIn(450000, book.bids)
        self.assertEqual(book.bids[460000], 3500000)

    def test_crossed_book_fails_closed(self):
        payload = load_fixture("polymarket_book.json")
        payload["bids"][0]["price"] = "0.56"
        with self.assertRaisesRegex(ValueError, "POLYMARKET_CROSSED_BOOK"):
            parse_market_ws_message(payload)

    def test_float_at_fixed_point_boundary_is_rejected(self):
        payload = load_fixture("polymarket_book.json")
        payload["bids"][0]["price"] = 0.45
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(payload)

    def test_unknown_event_is_persisted_and_warned(self):
        incidents = []
        payload = {
            "event_type": "future_provider_event",
            "asset_id": "token-00-yes",
            "timestamp": 1785556802000,
            "provider_field": {"kept": "exactly"},
        }
        event = parse_market_ws_message(
            payload,
            incident_sink=incidents.append,
        )[0]
        self.assertEqual(event.event_type, "UNHANDLED_PROVIDER_EVENT")
        self.assertEqual(
            json.loads(event.payload_json)["provider_field"],
            {"kept": "exactly"},
        )
        self.assertEqual(incidents[0]["severity"], "WARNING")

    def test_malformed_schema_is_rejected(self):
        payload = load_fixture("polymarket_book.json")
        del payload["asset_id"]
        with self.assertRaisesRegex(ValueError, "POLYMARKET_INVALID_TYPE"):
            parse_market_ws_message(payload)

    def test_duplicate_provider_event_natural_key_is_deterministic(self):
        payload = load_fixture("polymarket_price_change.json")
        first = parse_market_ws_message(payload)
        second = parse_market_ws_message(copy.deepcopy(payload))
        self.assertEqual(
            [event.natural_key for event in first],
            [event.natural_key for event in second],
        )

    async def test_current_books_fetches_public_book_per_asset(self):
        first = load_fixture("polymarket_book.json")
        second = copy.deepcopy(first)
        second["asset_id"] = "token-00-no"
        second["hash"] = "bookhash-no-001"
        session = FakeSession([first, second])
        events = await fetch_current_books(
            session,
            ["token-00-yes", "token-00-no"],
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [call[1]["token_id"] for call in session.calls],
            ["token-00-yes", "token-00-no"],
        )

    async def test_price_history_pagination_is_bounded(self):
        session = FakeSession(
            [
                {"history": [{"t": 100, "p": "0.45"}, {"t": 160, "p": "0.46"}]},
                {"history": [{"t": 220, "p": "0.47"}, {"t": 280, "p": "0.48"}]},
            ]
        )
        events = [
            event
            async for event in iter_price_history(
                session,
                asset_id="token-00-yes",
                start_ts=100,
                end_ts=220,
            )
        ]
        self.assertEqual(
            [event.source_timestamp_ms for event in events],
            [100000, 160000, 220000],
        )
        self.assertEqual(
            [call[1]["startTs"] for call in session.calls],
            [100, 220],
        )

    async def test_local_websocket_subscription_and_ping_pong(self):
        observed = {}
        ping_received = asyncio.Event()

        async def handler(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            subscription = await websocket.receive_json(timeout=1)
            observed["subscription"] = subscription
            ping = await websocket.receive(timeout=1)
            observed["ping"] = ping.data
            ping_received.set()
            await websocket.send_str("PONG")
            await websocket.close()
            return websocket

        app = web.Application()
        app.router.add_get("/ws/market", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        try:
            async with ClientSession() as session:
                stream = PolymarketStream(
                    session,
                    websocket_url=f"http://127.0.0.1:{port}/ws/market",
                    heartbeat_interval=0.05,
                )
                ready = asyncio.Event()
                task = asyncio.create_task(
                    stream.run(
                        ["token-00-yes", "token-00-no"],
                        asyncio.Queue(),
                        ready_event=ready,
                    )
                )
                try:
                    await asyncio.wait_for(ready.wait(), timeout=1)
                    await asyncio.wait_for(ping_received.wait(), timeout=1)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertTrue(ready.is_set())
        finally:
            await runner.cleanup()

        self.assertEqual(
            observed["subscription"],
            {
                "assets_ids": ["token-00-yes", "token-00-no"],
                "type": "market",
                "custom_feature_enabled": True,
            },
        )
        self.assertEqual(observed["ping"], "PING")


if __name__ == "__main__":
    unittest.main()
