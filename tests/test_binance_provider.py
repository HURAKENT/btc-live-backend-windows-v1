from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from src.binance_provider import (
    BinanceStream,
    ReconnectPolicy,
    iter_binance_backfill,
    parse_binance_kline_message,
)


FIXTURES = Path("tests/fixtures")
MINUTE_MS = 60_000


def load_fixture(name):
    return json.loads(Path(FIXTURES, name).read_text(encoding="utf-8"))


def rest_kline(open_time_ms):
    return [
        open_time_ms,
        "60000.00000000",
        "60020.00000000",
        "59990.00000000",
        "60010.00000000",
        "12.34560000",
        open_time_ms + MINUTE_MS - 1,
        "740000.00000000",
        21,
        "6.00000000",
        "360000.00000000",
        "0",
    ]


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params):
        self.calls.append((url, dict(params)))
        if not self.responses:
            raise AssertionError("unexpected REST request")
        response = self.responses.pop(0)
        if isinstance(response, FakeResponse):
            return response
        return FakeResponse(response)


class FakeWebSocket:
    def __init__(self, payloads):
        self._payloads = iter(payloads)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._payloads)
        except StopIteration:
            raise asyncio.CancelledError from None


class FakeWebSocketContext:
    def __init__(self, payloads):
        self.websocket = FakeWebSocket(payloads)

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class BinanceProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_closed_fixture_becomes_canonical_event(self):
        event = parse_binance_kline_message(
            load_fixture("binance_kline_closed.json")
        )
        self.assertEqual(event.event_type, "BINANCE_KLINE_CLOSED")
        self.assertEqual(
            event.natural_key,
            "binance:BTCUSDT:1m:1720000000000",
        )
        payload = json.loads(event.payload_json)
        self.assertEqual(payload["close"], "60010")
        self.assertEqual(payload["open_time_ms"], 1720000000000)

    def test_open_fixture_is_not_canonical_history(self):
        event = parse_binance_kline_message(
            load_fixture("binance_kline_open.json")
        )
        self.assertIsNone(event)

    def test_malformed_exact_type_is_rejected(self):
        payload = load_fixture("binance_kline_closed.json")
        payload["k"]["t"] = "1720000000000"
        with self.assertRaisesRegex(ValueError, "BINANCE_INVALID_TYPE: k.t"):
            parse_binance_kline_message(payload)

    def test_float_at_provider_boundary_is_rejected(self):
        payload = load_fixture("binance_kline_closed.json")
        payload["k"]["o"] = 60000.0
        with self.assertRaisesRegex(ValueError, "BINANCE_INVALID_TYPE: k.o"):
            parse_binance_kline_message(payload)

    async def test_two_rest_pages_are_contiguous_and_include_final_closed_minute(self):
        session = FakeSession(
            [
                [rest_kline(0), rest_kline(MINUTE_MS)],
                [
                    rest_kline(2 * MINUTE_MS),
                    rest_kline(3 * MINUTE_MS),
                    rest_kline(4 * MINUTE_MS),
                ],
            ]
        )
        events = [
            event
            async for event in iter_binance_backfill(
                session,
                start_ms=0,
                end_ms=3 * MINUTE_MS,
            )
        ]
        self.assertEqual(
            [event.source_timestamp_ms for event in events],
            [0, MINUTE_MS, 2 * MINUTE_MS, 3 * MINUTE_MS],
        )
        self.assertEqual(len({event.natural_key for event in events}), 4)
        self.assertEqual(
            [call[1]["startTime"] for call in session.calls],
            [0, 2 * MINUTE_MS],
        )
        self.assertNotIn(4 * MINUTE_MS, [event.source_timestamp_ms for event in events])

    async def test_empty_rest_page_ends_iterator(self):
        session = FakeSession([[]])
        events = [
            event
            async for event in iter_binance_backfill(
                session,
                start_ms=0,
                end_ms=MINUTE_MS,
            )
        ]
        self.assertEqual(events, [])

    async def test_inconsistent_rest_page_fails_closed(self):
        session = FakeSession([[rest_kline(0), rest_kline(2 * MINUTE_MS)]])
        with self.assertRaisesRegex(ValueError, "BINANCE_BACKFILL_CADENCE_ERROR"):
            [
                event
                async for event in iter_binance_backfill(
                    session,
                    start_ms=0,
                    end_ms=2 * MINUTE_MS,
                )
            ]

    async def test_http_error_creates_no_synthetic_event(self):
        session = FakeSession(
            [
                FakeResponse({"error": "unavailable"}, status=503),
                FakeResponse({"error": "unavailable"}, status=503),
                FakeResponse({"error": "unavailable"}, status=503),
            ]
        )
        with self.assertRaisesRegex(ValueError, "BINANCE_HTTP_ERROR"):
            [
                event
                async for event in iter_binance_backfill(
                    session,
                    start_ms=0,
                    end_ms=MINUTE_MS,
                )
            ]

    def test_reconnect_delay_is_capped_and_jitter_bounded(self):
        policy = ReconnectPolicy(random_value=lambda: 0.5)
        delays = [policy.next_delay() for _ in range(8)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30, 30])

        low = ReconnectPolicy(random_value=lambda: 0.0).next_delay()
        high = ReconnectPolicy(random_value=lambda: 1.0).next_delay()
        self.assertAlmostEqual(low, 0.8)
        self.assertAlmostEqual(high, 1.2)

    def test_reconnect_delay_resets_after_five_healthy_minutes(self):
        policy = ReconnectPolicy(random_value=lambda: 0.5)
        self.assertEqual([policy.next_delay() for _ in range(3)], [1, 2, 4])
        policy.record_healthy_duration(300)
        self.assertEqual(policy.next_delay(), 1)

    async def test_stream_queues_only_closed_canonical_events(self):
        closed = load_fixture("binance_kline_closed.json")
        open_event = load_fixture("binance_kline_open.json")
        queue = asyncio.Queue()

        def connect(url):
            self.assertEqual(
                url,
                "wss://stream.binance.com:9443/ws/btcusdt@kline_1m",
            )
            return FakeWebSocketContext([closed, open_event])

        stream = BinanceStream(connect=connect)
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(queue)
        self.assertEqual(queue.qsize(), 1)
        event = queue.get_nowait()
        self.assertEqual(event.event_type, "BINANCE_KLINE_CLOSED")

    async def test_stream_ready_event_is_set_after_websocket_connects(self):
        ready = asyncio.Event()

        def connect(_url):
            return FakeWebSocketContext([])

        stream = BinanceStream(connect=connect)
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(asyncio.Queue(), ready_event=ready)

        self.assertTrue(ready.is_set())

    async def test_disconnect_reports_incident_without_sqlite_write(self):
        incidents = []

        def connect(_url):
            raise OSError("offline")

        async def stop_after_incident(_delay):
            raise asyncio.CancelledError

        stream = BinanceStream(
            connect=connect,
            incident_sink=incidents.append,
            sleep=stop_after_incident,
            policy=ReconnectPolicy(random_value=lambda: 0.5),
        )
        with self.assertRaises(asyncio.CancelledError):
            await stream.run(asyncio.Queue())
        self.assertEqual(
            incidents,
            [
                {
                    "severity": "WARNING",
                    "source": "binance",
                    "code": "BINANCE_STREAM_DISCONNECTED",
                    "detail": "offline",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
