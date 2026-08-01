from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from aiohttp import WSMsgType, web


MINUTE_MS = 60_000


class FakeProviderServer:
    def __init__(
        self,
        *,
        startup_barrier: asyncio.Event | None = None,
        initial_book_batch: bool = False,
    ) -> None:
        self._runner: web.AppRunner | None = None
        self.port: int | None = None
        self.startup_barrier = startup_barrier
        self.initial_book_batch = initial_book_batch
        self.startup_barrier_reached = asyncio.Event()
        self.polymarket_connections = 0
        self.binance_connections = 0
        self.observed_subscriptions: list[dict] = []
        self.history_requests: list[dict[str, str]] = []
        self.external_requests = 0
        fixture = json.loads(
            Path("tests/fixtures/gamma_btc_daily_range.json").read_text(
                encoding="utf-8"
            )
        )[0]
        self.event = copy.deepcopy(fixture)
        self.asset_ids = tuple(
            asset_id
            for market in self.event["markets"]
            for asset_id in json.loads(market["clobTokenIds"])
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/gamma/events/keyset", self._gamma)
        app.router.add_get("/api/v3/klines", self._binance_rest)
        app.router.add_get("/binance/ws", self._binance_ws)
        app.router.add_get("/polymarket/book", self._book)
        app.router.add_get("/polymarket/prices-history", self._history)
        app.router.add_get("/polymarket/ws", self._polymarket_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def endpoint_payload(self, api_port: int) -> dict:
        if self.port is None:
            raise RuntimeError("FAKE_SERVER_NOT_STARTED")
        base = f"http://127.0.0.1:{self.port}"
        websocket = f"ws://127.0.0.1:{self.port}"
        return {
            "schema_version": "C1_LOOPBACK_INTEGRATION_ENDPOINTS_V1",
            "api_bind_host": "127.0.0.1",
            "api_bind_port": api_port,
            "binance_rest_bases": [base],
            "binance_websocket_url": f"{websocket}/binance/ws",
            "gamma_events_url": f"{base}/gamma/events/keyset",
            "polymarket_clob_base_url": f"{base}/polymarket",
            "polymarket_websocket_url": f"{websocket}/polymarket/ws",
        }

    async def _gamma(self, request: web.Request) -> web.Response:
        if (
            self.startup_barrier is not None
            and not self.startup_barrier.is_set()
        ):
            self.startup_barrier_reached.set()
            await self.startup_barrier.wait()
        return web.json_response({"events": [self.event], "next_cursor": ""})

    async def _binance_rest(self, request: web.Request) -> web.Response:
        start = int(request.query["startTime"])
        end = int(request.query["endTime"])
        rows = [
            _rest_kline(open_time)
            for open_time in range(start, end + 1, MINUTE_MS)
        ]
        return web.json_response(rows)

    async def _binance_ws(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self.binance_connections += 1
        now_ms = int(asyncio.get_running_loop().time() * 1000)
        # Runtime boundaries use wall clock; compute a wall-clock aligned minute.
        import time

        open_time = int(time.time() * 1000) // MINUTE_MS * MINUTE_MS - MINUTE_MS
        await websocket.send_json(_ws_kline(open_time))
        async for message in websocket:
            if message.type == WSMsgType.ERROR:
                break
        return websocket

    async def _book(self, request: web.Request) -> web.Response:
        asset_id = request.query["token_id"]
        if asset_id not in self.asset_ids:
            return web.json_response({"error": "unknown"}, status=404)
        return web.json_response(_book_payload(asset_id, "rest-book"))

    async def _history(self, request: web.Request) -> web.Response:
        self.history_requests.append(dict(request.query))
        end_ts = int(request.query["endTs"])
        return web.Response(
            text=json.dumps(
                {"history": [{"t": end_ts, "p": 0.45}]},
                separators=(",", ":"),
            ),
            content_type="application/json",
        )

    async def _polymarket_ws(
        self,
        request: web.Request,
    ) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self.polymarket_connections += 1
        subscription = await websocket.receive_json(timeout=5)
        self.observed_subscriptions.append(subscription)
        if tuple(subscription.get("assets_ids", ())) != self.asset_ids:
            await websocket.close(code=1008, message=b"invalid assets")
            return websocket
        suffix = str(self.polymarket_connections)
        if self.initial_book_batch:
            initial_payload = [
                _book_payload(asset_id, f"ws-book-{suffix}-{index:02d}")
                for index, asset_id in enumerate(self.asset_ids)
            ]
        else:
            initial_payload = _book_payload(
                self.asset_ids[0],
                f"ws-book-{suffix}",
            )
        await websocket.send_json(initial_payload)
        if self.polymarket_connections == 1:
            await asyncio.sleep(0.15)
            await websocket.close()
            return websocket
        async for message in websocket:
            if message.type == WSMsgType.TEXT and message.data == "PING":
                await websocket.send_str("PONG")
            elif message.type == WSMsgType.ERROR:
                break
        return websocket


def _rest_kline(open_time: int) -> list:
    return [
        open_time,
        "60000",
        "60100",
        "59900",
        "60010",
        "12.5",
        open_time + MINUTE_MS - 1,
        "750125",
        42,
        "6.25",
        "375062.5",
        "0",
    ]


def _ws_kline(open_time: int) -> dict:
    return {
        "e": "kline",
        "E": open_time + MINUTE_MS + 1,
        "s": "BTCUSDT",
        "k": {
            "t": open_time,
            "T": open_time + MINUTE_MS - 1,
            "s": "BTCUSDT",
            "i": "1m",
            "f": 1,
            "L": 42,
            "o": "60000",
            "c": "60010",
            "h": "60100",
            "l": "59900",
            "v": "12.5",
            "n": 42,
            "x": True,
            "q": "750125",
            "V": "6.25",
            "Q": "375062.5",
        },
    }


def _book_payload(asset_id: str, book_hash: str) -> dict:
    import time

    return {
        "event_type": "book",
        "asset_id": asset_id,
        "market": "bucket",
        "timestamp": str(int(time.time() * 1000)),
        "hash": book_hash,
        "bids": [{"price": "0.45", "size": "10"}],
        "asks": [{"price": "0.55", "size": "10"}],
    }
