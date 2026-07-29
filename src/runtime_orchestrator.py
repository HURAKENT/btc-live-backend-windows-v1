from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import aiohttp

from src.binance_provider import BinanceStream, MINUTE_MS
from src.canary import commit_canary_if_new
from src.lifecycle import STARTUP_SEQUENCE, LifecycleStateMachine
from src.models import SourceEvent
from src.outbox import OutboxBroker
from src.polymarket_provider import PolymarketStream
from src.runtime_adapters import (
    BinanceRuntimeAdapter,
    MarketReconciliation,
    PolymarketRuntimeAdapter,
)
from src.runtime_projection import CanonicalProjector, CommittedSourceEvent
from src.storage import SqliteStore


_STOP = object()


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: str
    live_ready: bool
    source_health: tuple[tuple[str, str], ...]
    market_id: str | None
    market_count: int
    asset_count: int
    last_event_id: int
    failure: str | None

    @classmethod
    def starting(cls) -> RuntimeStatus:
        return cls(
            state="BOOTING",
            live_ready=False,
            source_health=(
                ("binance", "STARTING"),
                ("polymarket", "STARTING"),
            ),
            market_id=None,
            market_count=0,
            asset_count=0,
            last_event_id=0,
            failure=None,
        )


class C1RuntimeOrchestrator:
    def __init__(
        self,
        *,
        run_id: str,
        store: SqliteStore,
        broker: OutboxBroker,
        binance_adapter: Any,
        polymarket_adapter: Any,
        binance_start_ms: int,
        binance_end_ms: int,
        history_start_ts: int,
        history_end_ts: int,
        clock_ms: Any = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if type(run_id) is not str or not run_id:
            raise ValueError("INVALID_RUNTIME_RUN_ID")
        if type(store) is not SqliteStore:
            raise ValueError("INVALID_RUNTIME_STORE")
        if not isinstance(broker, OutboxBroker):
            raise ValueError("INVALID_RUNTIME_BROKER")
        for value in (
            binance_start_ms,
            binance_end_ms,
            history_start_ts,
            history_end_ts,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("INVALID_RUNTIME_RECOVERY_BOUNDARY")
        self._run_id = run_id
        self._store = store
        self._broker = broker
        self._binance = binance_adapter
        self._polymarket = polymarket_adapter
        self._binance_start_ms = binance_start_ms
        self._binance_end_ms = binance_end_ms
        self._history_start_ts = history_start_ts
        self._history_end_ts = history_end_ts
        self._clock_ms = clock_ms
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lifecycle = LifecycleStateMachine(state_sink=self._state_sink)
        self._projector = CanonicalProjector(backend_session_id=run_id)
        self._ready = asyncio.Event()
        self._started = False
        self._stopped = False
        self._failure: str | None = None
        self._market: MarketReconciliation | None = None
        self._source_health = {
            "binance": "STARTING",
            "polymarket": "STARTING",
        }
        self._last_event_id = 0
        self._providers_stopped = False

    @property
    def lifecycle_states(self) -> tuple[str, ...]:
        return self._lifecycle.states

    @property
    def owned_task_names(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    @property
    def pending_owned_tasks(self) -> tuple[str, ...]:
        return tuple(
            name for name, task in self._tasks.items() if not task.done()
        )

    @property
    def pending_source_events(self) -> int:
        return self._queue.qsize()

    @property
    def providers_stopped(self) -> bool:
        return self._providers_stopped

    @property
    def writer_consumer_count(self) -> int:
        return int("writer" in self._tasks)

    @property
    def registry_execution_count(self) -> int:
        return 0

    def status(self) -> RuntimeStatus:
        market = self._market
        return RuntimeStatus(
            state=self._lifecycle.current_state or "BOOTING",
            live_ready=(
                self._lifecycle.current_state == "LIVE_READY"
                and self._failure is None
            ),
            source_health=tuple(sorted(self._source_health.items())),
            market_id=None if market is None else market.market_id,
            market_count=0 if market is None else len(market.market_ids),
            asset_count=0 if market is None else len(market.asset_ids),
            last_event_id=self._last_event_id,
            failure=self._failure,
        )

    async def start(self) -> None:
        if self._started:
            return
        try:
            for state in STARTUP_SEQUENCE[:4]:
                self._transition(state)
            self._market = await self._polymarket.discover_and_reconcile()
            self._validate_market(self._market)
            self._projector.set_market_identity(self._market)
            self._store.persist_market_identity(
                market_id=self._market.market_id,
                payload_json=self._market.market_identity_json,
                payload_sha256=hashlib.sha256(
                    self._market.market_identity_json.encode("utf-8")
                ).hexdigest(),
                updated_at_ms=self._clock_ms(),
            )

            self._transition("LIVE_BUFFERING")
            self._tasks["writer"] = asyncio.create_task(
                self._writer_consumer(),
                name=f"{self._run_id}:writer",
            )
            self._tasks["binance_stream"] = asyncio.create_task(
                self._provider_stream(
                    "binance",
                    self._binance.stream(self._queue),
                ),
                name=f"{self._run_id}:binance",
            )
            self._tasks["polymarket_stream"] = asyncio.create_task(
                self._provider_stream(
                    "polymarket",
                    self._polymarket.stream(
                        asset_ids=self._market.asset_ids,
                        queue=self._queue,
                    ),
                ),
                name=f"{self._run_id}:polymarket",
            )
            await asyncio.sleep(0)
            self._ensure_running()

            self._transition("BINANCE_BACKFILL")
            recovered = await self._binance.recover(
                start_ms=self._binance_start_ms,
                end_ms=self._binance_end_ms,
            )
            for event in recovered:
                await self._queue.put(event)
            await self.drain_source_queue()
            if not recovered:
                raise RuntimeError("INCOMPLETE_BINANCE_RECOVERY")
            self._source_health["binance"] = "LIVE"

            self._transition("POLYMARKET_BACKFILL")
            for event in self._market.current_events:
                await self._queue.put(event)
            history = await self._polymarket.recover(
                self._market,
                start_ts=self._history_start_ts,
                end_ts=self._history_end_ts,
            )
            for event in history:
                await self._queue.put(event)
            await self.drain_source_queue()
            self._validate_market(self._market)
            self._source_health["polymarket"] = "LIVE"

            for state in STARTUP_SEQUENCE[7:10]:
                self._transition(state)
            self._projector.set_live_ready()
            self._transition("LIVE_READY")
            self._started = True
            self._ready.set()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            self._block(str(error))
            await self.stop()
            raise

    async def enqueue(self, event: Any) -> None:
        if self._stopped:
            raise RuntimeError("RUNTIME_STOPPED")
        await self._queue.put(event)

    async def wait_ready(self) -> None:
        await self._ready.wait()
        if not self.status().live_ready:
            raise RuntimeError(self._failure or "RUNTIME_NOT_READY")

    async def wait_for_source_idle(self) -> None:
        await asyncio.sleep(0)
        await self._queue.join()
        await asyncio.sleep(0)
        await self._queue.join()

    async def drain_source_queue(self) -> None:
        await self._queue.join()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        provider_tasks = [
            self._tasks.get("binance_stream"),
            self._tasks.get("polymarket_stream"),
        ]
        for task in provider_tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in provider_tasks if task is not None),
            return_exceptions=True,
        )
        self._providers_stopped = True
        await self._queue.join()
        writer = self._tasks.get("writer")
        if writer is not None and not writer.done():
            await self._queue.put(_STOP)
            await self._queue.join()
            await writer
        await self._binance.close()
        await self._polymarket.close()

    async def _provider_stream(self, source: str, operation: Any) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._source_health[source] = "FAILED"
            self._block(f"{source.upper()}_STREAM_FAILED: {error}")

    async def _writer_consumer(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                if type(item) is not SourceEvent:
                    raise ValueError("INVALID_RUNTIME_SOURCE_EVENT")
                result = self._store.append_source_event(item)
                self._last_event_id = result.event_id
                self._persist_cursor(item)
                committed = CommittedSourceEvent(
                    source_event_id=result.event_id,
                    event=item,
                    inserted=result.inserted,
                )
                snapshot = self._projector.apply(committed)
                if snapshot is not None:
                    self._store.append_canonical_snapshot(snapshot)
                    commit_canary_if_new(
                        self._store,
                        snapshot,
                        broker=self._broker,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._block(f"WRITER_FAILED: {error}")
            finally:
                self._queue.task_done()

    def _persist_cursor(self, event: SourceEvent) -> None:
        source = event.source
        if source == "polymarket":
            try:
                asset_id = json.loads(event.payload_json).get("asset_id")
            except json.JSONDecodeError:
                asset_id = None
            if type(asset_id) is str and asset_id:
                source = f"polymarket:{asset_id}"
        cursor_json = json.dumps(
            {
                "natural_key": event.natural_key,
                "source_timestamp_ms": event.source_timestamp_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._store.upsert_source_cursor(
            source=source,
            cursor_json=cursor_json,
            updated_at_ms=event.source_timestamp_ms,
        )

    def _transition(self, state: str) -> None:
        self._lifecycle.transition(state)

    def _state_sink(self, record: dict[str, Any]) -> None:
        self._store.append_lifecycle_state(
            run_id=self._run_id,
            state=record["state"],
            sequence_index=record["sequence_index"],
            created_at_ms=self._clock_ms(),
            detail=record.get("detail"),
        )

    def _block(self, detail: str) -> None:
        if self._failure is not None:
            return
        self._failure = detail[:500]
        self._ready.set()
        self._lifecycle.fail_closed(self._failure)

    def _ensure_running(self) -> None:
        if self._failure is not None:
            raise RuntimeError(self._failure)

    @staticmethod
    def _validate_market(market: MarketReconciliation) -> None:
        if (
            type(market) is not MarketReconciliation
            or len(market.market_ids) != 11
            or len(set(market.market_ids)) != 11
            or len(market.asset_ids) != 22
            or len(set(market.asset_ids)) != 22
            or len(market.current_events) != 22
        ):
            raise ValueError("INCOMPLETE_MARKET_RECONCILIATION")


async def build_default_runtime_orchestrator(
    *,
    store: SqliteStore,
    broker: OutboxBroker,
) -> C1RuntimeOrchestrator:
    session = aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    )
    now = datetime.now(timezone.utc)
    current_open_ms = int(now.timestamp() * 1000) // MINUTE_MS * MINUTE_MS
    end_ms = current_open_ms - MINUTE_MS
    cursor = store.read_source_cursor("binance")
    start_ms = (
        end_ms
        if cursor is None
        else min(
            end_ms,
            cursor["cursor"]["source_timestamp_ms"] + MINUTE_MS,
        )
    )

    async def load_events() -> list[dict[str, Any]]:
        params = {
            "closed": "false",
            "title_search": "Bitcoin price on",
            "end_date_min": (now - timedelta(hours=6)).isoformat(),
            "end_date_max": (now + timedelta(days=14)).isoformat(),
            "order": "endDate",
            "ascending": "true",
            "limit": 500,
        }
        async with session.get(
            "https://gamma-api.polymarket.com/events/keyset",
            params=params,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"GAMMA_DISCOVERY_HTTP_ERROR: {response.status}"
                )
            payload = await response.json()
        events = payload.get("events") if type(payload) is dict else None
        if type(events) is not list:
            raise ValueError("INVALID_GAMMA_DISCOVERY_RESPONSE")
        return events

    binance = BinanceRuntimeAdapter(
        session=session,
        stream=BinanceStream(connect=session.ws_connect),
    )
    polymarket = PolymarketRuntimeAdapter(
        session=session,
        stream=PolymarketStream(session),
        event_loader=load_events,
        now_utc=lambda: datetime.now(timezone.utc),
        close_session=session.close,
    )
    now_seconds = int(now.timestamp())
    return C1RuntimeOrchestrator(
        run_id=f"runtime-{time.time_ns()}",
        store=store,
        broker=broker,
        binance_adapter=binance,
        polymarket_adapter=polymarket,
        binance_start_ms=start_ms,
        binance_end_ms=end_ms,
        history_start_ts=max(0, now_seconds - 3600),
        history_end_ts=now_seconds,
    )
