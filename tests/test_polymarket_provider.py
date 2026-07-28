from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

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


FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def load_fixture(name):
    return json.loads(Path(FIXTURES, name).read_text(encoding="utf-8"))


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


class PolymarketProviderTests(unittest.IsolatedAsyncioTestCase):
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

        async def handler(request):
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            subscription = await websocket.receive_json(timeout=1)
            observed["subscription"] = subscription
            ping = await websocket.receive(timeout=1)
            observed["ping"] = ping.data
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
                await asyncio.wait_for(
                    stream.run(
                        ["token-00-yes", "token-00-no"],
                        asyncio.Queue(),
                    ),
                    timeout=2,
                )
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
