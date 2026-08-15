from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Callable

from aiohttp import WSMsgType, web

from src.models import OutboxEvent
from src.outbox import OutboxBroker
from src.performance_query import PerformanceQueryService
from src.storage import SqliteReadStore
from src.dashboard import attach_dashboard_routes


API_BIND_HOST = "127.0.0.1"
API_BIND_PORT = 8767
REST_RESULT_LIMIT = 100
OUTBOX_READ_LIMIT = 100
SUBSCRIBER_QUEUE_LIMIT = 100


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_response(value: Any, *, status: int = 200) -> web.Response:
    return web.json_response(
        value,
        status=status,
        dumps=_json_dumps,
    )


def _parse_limit(request: web.Request) -> int:
    value = request.query.get("limit")
    if value is None:
        return REST_RESULT_LIMIT
    if not value.isdigit():
        raise web.HTTPBadRequest(text="INVALID_LIMIT")
    limit = int(value)
    if not 1 <= limit <= REST_RESULT_LIMIT:
        raise web.HTTPBadRequest(text="INVALID_LIMIT")
    return limit


def _parse_after_event_id(request: web.Request) -> int:
    value = request.query.get("after_event_id")
    if value is None or not value.isdigit():
        raise web.HTTPBadRequest(text="INVALID_AFTER_EVENT_ID")
    event_id = int(value)
    if event_id < 0:
        raise web.HTTPBadRequest(text="INVALID_AFTER_EVENT_ID")
    return event_id


def _outbox_message(event: OutboxEvent) -> dict[str, Any]:
    try:
        payload = json.loads(event.payload_json)
    except json.JSONDecodeError:
        raise ValueError("INVALID_STORED_OUTBOX_JSON") from None
    if type(payload) is not dict:
        raise ValueError("INVALID_STORED_OUTBOX_SHAPE")
    event_type = payload.get("event_type", event.topic.upper())
    if type(event_type) is not str:
        raise ValueError("INVALID_STORED_OUTBOX_EVENT_TYPE")
    return {
        "created_at_ms": event.created_at_ms,
        "event_id": event.event_id,
        "event_type": event_type,
        "payload": payload,
        "topic": event.topic,
    }


def create_api_app(
    read_store: SqliteReadStore,
    outbox_broker: OutboxBroker,
    *,
    runtime_status: Callable[[], Any] | None = None,
) -> web.Application:
    if type(read_store) is not SqliteReadStore:
        raise ValueError("INVALID_API_READ_STORE")
    if not isinstance(outbox_broker, OutboxBroker):
        raise ValueError("INVALID_API_OUTBOX_BROKER")

    app = web.Application()
    app["bind_host"] = API_BIND_HOST
    app["bind_port"] = API_BIND_PORT
    app["read_store"] = read_store
    app["outbox_broker"] = outbox_broker
    app["runtime_status"] = runtime_status or _starting_runtime_status
    app.router.add_get("/api/v1/bootstrap", _bootstrap)
    app.router.add_get("/api/v1/health", _health)
    app.router.add_get("/api/v1/sources", _sources)
    app.router.add_get("/api/v1/signals", _signals)
    app.router.add_get("/api/v1/incidents", _incidents)
    app.router.add_get("/api/v1/performance/status", _performance_status)
    app.router.add_get("/api/v1/performance/strategies", _performance_strategies)
    app.router.add_get(
        "/api/v1/performance/strategies/{strategy_id}/timeseries",
        _performance_timeseries,
    )
    app.router.add_get(
        "/api/v1/performance/strategies/{strategy_id}/observations",
        _performance_observations,
    )
    app.router.add_get(
        "/api/v1/performance/strategies/{strategy_id}",
        _performance_strategy_detail,
    )
    app.router.add_get("/ws/v1/events", _websocket_events)
    attach_dashboard_routes(app)
    return app


def _store(request: web.Request) -> SqliteReadStore:
    return request.app["read_store"]


async def _health(request: web.Request) -> web.Response:
    return _json_response(
        build_health_payload(
            _store(request),
            request.app["runtime_status"](),
        )
    )


async def _sources(request: web.Request) -> web.Response:
    return _json_response({"sources": _store(request).sources()})


async def _signals(request: web.Request) -> web.Response:
    limit = _parse_limit(request)
    return _json_response(
        {"signals": _store(request).signals(limit=limit)}
    )


async def _incidents(request: web.Request) -> web.Response:
    limit = _parse_limit(request)
    return _json_response(
        {"incidents": _store(request).incidents(limit=limit)}
    )


async def _performance_status(request: web.Request) -> web.Response:
    return _json_response(_performance_service(request).status())


async def _performance_strategies(request: web.Request) -> web.Response:
    return _json_response(_performance_service(request).strategies())


async def _performance_strategy_detail(request: web.Request) -> web.Response:
    service = _performance_service(request)
    try:
        return _json_response(
            service.strategy_detail(request.match_info["strategy_id"])
        )
    except KeyError:
        return _json_response(
            {"error": "UNKNOWN_PERFORMANCE_STRATEGY_ID"},
            status=404,
        )
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)


async def _performance_timeseries(request: web.Request) -> web.Response:
    service = _performance_service(request)
    try:
        return _json_response(
            service.timeseries(
                request.match_info["strategy_id"],
                source_view=request.query.get("view", "HISTORICAL"),
            )
        )
    except KeyError:
        return _json_response(
            {"error": "UNKNOWN_PERFORMANCE_STRATEGY_ID"},
            status=404,
        )
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)


async def _performance_observations(request: web.Request) -> web.Response:
    service = _performance_service(request)
    try:
        return _json_response(
            service.observations(
                request.match_info["strategy_id"],
                limit=_parse_performance_limit(request),
                after_observation_key=request.query.get("after_observation_key"),
            )
        )
    except KeyError:
        return _json_response(
            {"error": "UNKNOWN_PERFORMANCE_STRATEGY_ID"},
            status=404,
        )
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)


async def _bootstrap(request: web.Request) -> web.Response:
    store = _store(request)
    health = build_health_payload(
        store,
        request.app["runtime_status"](),
    )
    return _json_response(
        {
            "interface_version": "BTC_DAILY_RANGE_MVP_V1",
            "current_market_identity": store.current_market_identity(),
            "execution_readiness": store.paper_readiness(),
            "health": health,
            "incidents": store.incidents(limit=REST_RESULT_LIMIT),
            "last_event_id": store.last_event_id(),
            "paper_account": store.paper_account(),
            "paper_fills": store.paper_fills(),
            "paper_positions": store.paper_positions(),
            "signals": store.signals(limit=REST_RESULT_LIMIT),
            "sources": store.sources(),
            "strict_a_signal": store.latest_strict_a_signal(),
        }
    )


def _performance_service(request: web.Request) -> PerformanceQueryService:
    return PerformanceQueryService(_store(request))


def _parse_performance_limit(request: web.Request) -> int:
    value = request.query.get("limit")
    if value is None:
        return 100
    if not value.isdigit():
        raise ValueError("INVALID_PERFORMANCE_LIMIT")
    return int(value)


def _starting_runtime_status() -> dict[str, Any]:
    return {
        "state": "BOOTING",
        "live_ready": False,
        "source_health": (
            ("binance", "STARTING"),
            ("polymarket", "STARTING"),
        ),
        "market_id": None,
        "market_count": 0,
        "asset_count": 0,
        "last_event_id": 0,
        "failure": None,
    }


def build_health_payload(
    read_store: SqliteReadStore,
    runtime_status: Any,
) -> dict[str, Any]:
    database_health = read_store.health()
    performance_status = PerformanceQueryService(read_store).status()
    if type(runtime_status) is dict:
        runtime = dict(runtime_status)
    else:
        runtime = {
            "state": runtime_status.state,
            "live_ready": runtime_status.live_ready,
            "source_health": runtime_status.source_health,
            "market_id": runtime_status.market_id,
            "market_count": runtime_status.market_count,
            "asset_count": runtime_status.asset_count,
            "last_event_id": runtime_status.last_event_id,
            "failure": runtime_status.failure,
        }
    source_health = dict(runtime["source_health"])
    required_sources_ready = (
        source_health.get("binance") == "LIVE"
        and source_health.get("polymarket") == "LIVE"
    )
    live_ready = runtime["live_ready"] is True
    database_pass = database_health.get("status") == "PASS"
    if database_pass and live_ready and required_sources_ready:
        status = "PASS"
    elif runtime.get("failure") is not None or not database_pass:
        status = "DEGRADED"
    else:
        status = "STARTING"
    return {
        "database_health": database_health,
        "performance": {
            "blocking_reason": performance_status["blocking_reason"],
            "catchup": performance_status["catchup"],
            "current_revision_count": performance_status["performance"][
                "current_revision_count"
            ],
            "latest_generated_at_ms": performance_status["performance"][
                "latest_generated_at_ms"
            ],
            "schema_version": performance_status["schema_version"],
            "source_freshness": performance_status["source_freshness"],
        },
        "runtime_readiness": {
            "asset_count": runtime["asset_count"],
            "failure": runtime["failure"],
            "last_event_id": runtime["last_event_id"],
            "live_ready": live_ready,
            "market_count": runtime["market_count"],
            "market_id": runtime["market_id"],
            "state": runtime["state"],
        },
        "source_health": source_health,
        "status": status,
    }


async def _send_available(
    websocket: web.WebSocketResponse,
    store: SqliteReadStore,
    after_event_id: int,
) -> int:
    last_sent = after_event_id
    while not websocket.closed:
        rows = store.read_outbox_after(last_sent, OUTBOX_READ_LIMIT)
        if not rows:
            break
        for event in rows:
            if event.event_id <= last_sent:
                continue
            await websocket.send_json(
                _outbox_message(event),
                dumps=_json_dumps,
            )
            last_sent = event.event_id
        if len(rows) < OUTBOX_READ_LIMIT:
            break
    return last_sent


async def _websocket_events(request: web.Request) -> web.StreamResponse:
    after_event_id = _parse_after_event_id(request)
    store = _store(request)
    broker: OutboxBroker = request.app["outbox_broker"]
    websocket = web.WebSocketResponse()
    await websocket.prepare(request)

    subscription = None
    try:
        last_sent = await _send_available(
            websocket,
            store,
            after_event_id,
        )
        subscription = broker.subscribe(
            max_queue=SUBSCRIBER_QUEUE_LIMIT
        )
        last_sent = await _send_available(websocket, store, last_sent)

        while not websocket.closed:
            notification = asyncio.create_task(subscription.get())
            client_message = asyncio.create_task(websocket.receive())
            done, pending = await asyncio.wait(
                {notification, client_message},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            if client_message in done:
                message = client_message.result()
                if message.type in {
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.CLOSING,
                    WSMsgType.ERROR,
                }:
                    break
            if notification in done:
                notification.result()
                last_sent = await _send_available(
                    websocket,
                    store,
                    last_sent,
                )
    except (ConnectionError, RuntimeError):
        pass
    finally:
        if subscription is not None:
            subscription.close()
        await websocket.close()
    return websocket
