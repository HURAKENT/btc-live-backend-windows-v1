from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from src.binance_provider import BinanceStream, MINUTE_MS
from src.canary import commit_canary_if_new
from src.lifecycle import STARTUP_SEQUENCE, LifecycleStateMachine
from src.models import CanonicalSnapshot, SourceEvent
from src.outbox import OutboxBroker
from src.polymarket_provider import PolymarketStream
from src.recovery import reconcile_binance_minutes, reconcile_polymarket_state
from src.runtime_adapters import (
    BinanceRuntimeAdapter,
    MarketReconciliation,
    PolymarketRuntimeAdapter,
)
from src.runtime_projection import (
    CURRENT_EVALUATION_ORIGIN,
    RECOVERED_EVALUATION_ORIGIN,
    CanonicalProjector,
    CommittedSourceEvent,
)
from src.storage import PersistResult, SqliteStore


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


@dataclass(frozen=True, slots=True)
class _RuntimeWriteCommand:
    operation: str
    payload: Any
    future: asyncio.Future[Any] | None


@dataclass(frozen=True, slots=True)
class _SourceWrite:
    event: SourceEvent
    authoritative_replay: bool


@dataclass(frozen=True, slots=True)
class _SourceWriteResult:
    event_id: int
    inserted: bool
    snapshot: CanonicalSnapshot | None


class _LiveIngress:
    __slots__ = ("_owner", "_source")

    def __init__(self, owner: C1RuntimeOrchestrator, source: str) -> None:
        self._owner = owner
        self._source = source

    async def put(self, event: SourceEvent) -> None:
        await self._owner._ingest_live(self._source, event)


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
        if (
            binance_start_ms < MINUTE_MS
            or binance_end_ms < binance_start_ms
            or binance_start_ms % MINUTE_MS != 0
            or binance_end_ms % MINUTE_MS != 0
        ):
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
        self._write_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._live_buffer: list[tuple[str, SourceEvent]] = []
        self._buffering_live = True
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._lifecycle = LifecycleStateMachine()
        self._projector = CanonicalProjector(backend_session_id=run_id)
        self._ready = asyncio.Event()
        self._progress = asyncio.Event()
        self._failure_lock = asyncio.Lock()
        self._started = False
        self._stopping = False
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
        return len(self._live_buffer) + self._write_queue.qsize()

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
        if self._stopped:
            raise RuntimeError("RUNTIME_STOPPED")
        self._tasks["writer"] = asyncio.create_task(
            self._writer_consumer(),
            name=f"{self._run_id}:writer",
        )
        try:
            for state in STARTUP_SEQUENCE[:4]:
                await self._transition(state)
                self._ensure_running()

            self._market = await self._polymarket.discover_and_reconcile()
            self._validate_market(self._market)
            await self._submit_write("SET_MARKET", self._market)
            await self._submit_write("MARKET_IDENTITY", self._market)
            self._ensure_running()

            await self._transition("LIVE_BUFFERING")
            self._tasks["binance_stream"] = asyncio.create_task(
                self._provider_stream(
                    "binance",
                    self._binance.stream(_LiveIngress(self, "binance")),
                ),
                name=f"{self._run_id}:binance",
            )
            self._tasks["polymarket_stream"] = asyncio.create_task(
                self._provider_stream(
                    "polymarket",
                    self._polymarket.stream(
                        asset_ids=self._market.asset_ids,
                        queue=_LiveIngress(self, "polymarket"),
                    ),
                ),
                name=f"{self._run_id}:polymarket",
            )
            await asyncio.sleep(0)
            self._ensure_running()

            await self._transition("BINANCE_BACKFILL")
            recovered = tuple(
                await self._binance.recover(
                    start_ms=self._binance_start_ms,
                    end_ms=self._binance_end_ms,
                )
            )
            for event in recovered:
                await self._submit_source(
                    event,
                    authoritative_replay=True,
                )
                self._ensure_running()
            continuity = reconcile_binance_minutes(
                last_committed_open_ms=(
                    self._binance_start_ms - MINUTE_MS
                ),
                current_open_ms=self._binance_end_ms + MINUTE_MS,
                recovered_events=recovered,
            )
            if continuity.blocker:
                raise RuntimeError(
                    "BINANCE_CONTINUITY_BLOCKED: "
                    f"missing={continuity.missing_open_times}"
                )
            self._ensure_running()

            await self._transition("POLYMARKET_BACKFILL")
            history = tuple(
                await self._polymarket.recover(
                    self._market,
                    start_ts=self._history_start_ts,
                    end_ts=self._history_end_ts,
                )
            )
            for event in history:
                await self._submit_source(
                    event,
                    authoritative_replay=True,
                )
                self._ensure_running()
            for event in self._market.current_events:
                await self._submit_source(
                    event,
                    authoritative_replay=True,
                )
                self._ensure_running()

            reconciliation = reconcile_polymarket_state(
                price_history=True,
                current_book=True,
                historical_depth=False,
                requires_depth=False,
            )
            if not reconciliation.live_ready_allowed:
                raise RuntimeError(reconciliation.status)
            await self._transition("RECONCILIATION")
            await self._submit_write(
                "BUILD_SNAPSHOT",
                {
                    "evaluation_origin": RECOVERED_EVALUATION_ORIGIN,
                    "trigger_committed_after_live_ready": False,
                },
            )
            self._ensure_running()

            await self._transition("STRATEGY_REPLAY")
            await self._drain_live_buffer()
            await self._wait_for_live_evidence()
            await self._submit_write("SET_LIVE_READY", None)
            await self._submit_write(
                "BUILD_SNAPSHOT",
                {
                    "evaluation_origin": CURRENT_EVALUATION_ORIGIN,
                    "trigger_committed_after_live_ready": True,
                },
            )
            self._ensure_running()
            self._ensure_streams_alive()

            await self._transition("BUFFER_DRAIN")
            self._ensure_running()
            self._ensure_streams_alive()
            await self._transition("LIVE_READY")
            self._started = True
            self._ready.set()
        except asyncio.CancelledError:
            await self.stop()
            raise
        except BaseException as error:
            if self._failure is None:
                await self._fail(str(error))
            await self.stop()
            raise RuntimeError(self._failure or str(error)) from error

    async def enqueue(self, event: Any) -> None:
        if self._stopping or self._stopped:
            raise RuntimeError("RUNTIME_STOPPED")
        if type(event) is not SourceEvent:
            await self._fail("INVALID_RUNTIME_SOURCE_EVENT")
            return
        await self._submit_source(event)

    async def wait_ready(self) -> None:
        await self._ready.wait()
        if not self.status().live_ready:
            raise RuntimeError(self._failure or "RUNTIME_NOT_READY")

    async def wait_for_source_idle(self) -> None:
        await asyncio.sleep(0)
        await self._write_queue.join()
        await asyncio.sleep(0)
        await self._write_queue.join()

    async def drain_source_queue(self) -> None:
        await self._write_queue.join()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopping = True
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
        self._live_buffer.clear()
        await self._write_queue.join()
        writer = self._tasks.get("writer")
        if writer is not None and not writer.done():
            await self._write_queue.put(_STOP)
            await self._write_queue.join()
            await writer
        await self._binance.close()
        await self._polymarket.close()
        self._stopped = True

    async def _provider_stream(self, source: str, operation: Any) -> None:
        try:
            await operation
            if not self._stopping:
                await self._fail(
                    f"UNEXPECTED_PROVIDER_STREAM_EXIT: {source}",
                    source=source,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._stopping:
                await self._fail(
                    f"{source.upper()}_STREAM_FAILED: {error}",
                    source=source,
                )

    async def _ingest_live(
        self,
        source: str,
        event: SourceEvent,
    ) -> None:
        if type(event) is not SourceEvent or event.source != source:
            await self._fail(
                f"INVALID_{source.upper()}_LIVE_EVENT",
                source=source,
            )
            return
        if self._buffering_live:
            self._live_buffer.append((source, event))
            return
        result = await self._submit_source(event)
        self._mark_live_evidence(source, result)

    async def _drain_live_buffer(self) -> None:
        while self._live_buffer:
            buffered = sorted(
                self._live_buffer,
                key=lambda item: (
                    item[1].source_timestamp_ms,
                    item[0],
                    item[1].natural_key,
                ),
            )
            self._live_buffer.clear()
            for source, event in buffered:
                result = await self._submit_source(
                    event,
                )
                self._mark_live_evidence(source, result)
                self._ensure_running()
        self._buffering_live = False

    def _mark_live_evidence(
        self,
        source: str,
        result: _SourceWriteResult,
    ) -> None:
        if type(result) is not _SourceWriteResult:
            raise ValueError("INVALID_RUNTIME_SOURCE_WRITE_RESULT")
        self._source_health[source] = "LIVE"
        self._progress.set()

    async def _wait_for_live_evidence(self) -> None:
        while not all(
            self._source_health[source] == "LIVE"
            for source in ("binance", "polymarket")
        ):
            self._ensure_running()
            self._progress.clear()
            if all(
                self._source_health[source] == "LIVE"
                for source in ("binance", "polymarket")
            ):
                break
            await self._progress.wait()
        self._ensure_running()

    def _ensure_streams_alive(self) -> None:
        for name in ("binance_stream", "polymarket_stream"):
            task = self._tasks.get(name)
            if task is None or task.done():
                raise RuntimeError(
                    f"UNEXPECTED_PROVIDER_STREAM_EXIT: {name}"
                )

    async def _submit_source(
        self,
        event: SourceEvent,
        *,
        authoritative_replay: bool = False,
    ) -> _SourceWriteResult:
        return await self._submit_write(
            "SOURCE_EVENT",
            _SourceWrite(
                event=event,
                authoritative_replay=authoritative_replay,
            ),
        )

    async def _submit_write(
        self,
        operation: str,
        payload: Any,
        *,
        allow_failed: bool = False,
    ) -> Any:
        if self._failure is not None and not allow_failed:
            raise RuntimeError(self._failure)
        writer = self._tasks.get("writer")
        if writer is None or writer.done():
            raise RuntimeError(self._failure or "RUNTIME_WRITER_NOT_RUNNING")
        future = asyncio.get_running_loop().create_future()
        await self._write_queue.put(
            _RuntimeWriteCommand(operation, payload, future)
        )
        return await future

    async def _writer_consumer(self) -> None:
        while True:
            command = await self._write_queue.get()
            try:
                if command is _STOP:
                    return
                if type(command) is not _RuntimeWriteCommand:
                    raise ValueError("INVALID_RUNTIME_WRITE_COMMAND")
                try:
                    result = self._execute_write(command)
                except BaseException as error:
                    if command.future is not None and not command.future.done():
                        command.future.set_exception(error)
                    self._writer_failed(error)
                    self._reject_pending_writes(error)
                    return
                else:
                    if command.future is not None and not command.future.done():
                        command.future.set_result(result)
            finally:
                self._write_queue.task_done()

    def _execute_write(self, command: _RuntimeWriteCommand) -> Any:
        operation = command.operation
        payload = command.payload
        if operation == "LIFECYCLE":
            return self._store.append_lifecycle_state(**payload)
        if operation == "INCIDENT":
            return self._store.append_incident(**payload)
        if operation == "MARKET_IDENTITY":
            return self._persist_market_identity(payload)
        if operation == "SET_MARKET":
            self._projector.set_market_identity(payload)
            return None
        if operation == "SET_LIVE_READY":
            self._projector.set_live_ready()
            return None
        if operation == "SOURCE_EVENT":
            return self._write_source_event(payload)
        if operation == "BUILD_SNAPSHOT":
            snapshot = self._projector.build_snapshot(**payload)
            self._persist_snapshot_and_canary(snapshot)
            return snapshot
        raise ValueError("UNKNOWN_RUNTIME_WRITE_COMMAND")

    def _write_source_event(
        self,
        request: _SourceWrite,
    ) -> _SourceWriteResult:
        if type(request) is not _SourceWrite:
            raise ValueError("INVALID_RUNTIME_SOURCE_WRITE")
        event = request.event
        result = self._store.append_source_event(event)
        self._last_event_id = max(self._last_event_id, result.event_id)
        if result.inserted:
            self._advance_cursor(event)
        committed = CommittedSourceEvent(
            source_event_id=result.event_id,
            event=event,
            inserted=result.inserted,
            authoritative_replay=request.authoritative_replay,
        )
        snapshot = self._projector.apply(committed)
        if snapshot is not None:
            self._persist_snapshot_and_canary(snapshot)
        return _SourceWriteResult(
            event_id=result.event_id,
            inserted=result.inserted,
            snapshot=snapshot,
        )

    def _advance_cursor(self, event: SourceEvent) -> None:
        source = self._cursor_source(event)
        stored = self._store.read_source_cursor(source)
        if (
            stored is not None
            and event.source_timestamp_ms <= stored["updated_at_ms"]
        ):
            return
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

    @staticmethod
    def _cursor_source(event: SourceEvent) -> str:
        source = event.source
        if source == "polymarket":
            try:
                asset_id = json.loads(event.payload_json).get("asset_id")
            except json.JSONDecodeError:
                asset_id = None
            if type(asset_id) is str and asset_id:
                source = f"polymarket:{asset_id}"
        return source

    def _persist_market_identity(
        self,
        market: MarketReconciliation,
    ) -> PersistResult:
        digest = hashlib.sha256(
            market.market_identity_json.encode("utf-8")
        ).hexdigest()
        stored = self._store.rows(
            """
            SELECT rowid, payload_json, payload_sha256
            FROM market_catalog
            WHERE market_id = ?
            """,
            (market.market_id,),
        )
        if stored:
            row_id, payload_json, payload_hash = stored[0]
            if (
                payload_json != market.market_identity_json
                or payload_hash != digest
            ):
                raise ValueError("MARKET_IDENTITY_CONFLICT")
            return PersistResult(row_id=row_id, inserted=False)
        return self._store.persist_market_identity(
            market_id=market.market_id,
            payload_json=market.market_identity_json,
            payload_sha256=digest,
            updated_at_ms=self._clock_ms(),
        )

    def _persist_snapshot_and_canary(
        self,
        snapshot: CanonicalSnapshot,
    ) -> None:
        self._store.append_canonical_snapshot(snapshot)
        commit_canary_if_new(
            self._store,
            snapshot,
            broker=self._broker,
        )

    async def _transition(self, state: str) -> None:
        sequence_index = len(self._lifecycle.states)
        self._lifecycle.transition(state)
        await self._submit_write(
            "LIFECYCLE",
            {
                "run_id": self._run_id,
                "state": state,
                "sequence_index": sequence_index,
                "created_at_ms": self._clock_ms(),
            },
        )

    async def _fail(
        self,
        detail: str,
        *,
        source: str | None = None,
    ) -> None:
        async with self._failure_lock:
            if self._failure is not None:
                return
            sanitized = str(detail)[:500]
            self._failure = sanitized
            if source is not None:
                self._source_health[source] = "FAILED"
            incident_hash = hashlib.sha256(
                sanitized.encode("utf-8")
            ).hexdigest()[:16]
            try:
                await self._submit_write(
                    "INCIDENT",
                    {
                        "incident_key": (
                            f"runtime:{self._run_id}:failure:{incident_hash}"
                        ),
                        "severity": "CRITICAL",
                        "status": "RECOVERY_BLOCKED",
                        "payload_json": json.dumps(
                            {
                                "code": "RUNTIME_FATAL",
                                "detail": sanitized,
                                "source": source,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "created_at_ms": self._clock_ms(),
                    },
                    allow_failed=True,
                )
                self._lifecycle.fail_closed(sanitized)
                await self._submit_write(
                    "LIFECYCLE",
                    {
                        "run_id": self._run_id,
                        "state": "RECOVERY_BLOCKED",
                        "sequence_index": len(self._lifecycle.states),
                        "created_at_ms": self._clock_ms(),
                        "detail": sanitized,
                    },
                    allow_failed=True,
                )
            except BaseException:
                if self._lifecycle.current_state != "RECOVERY_BLOCKED":
                    self._lifecycle.fail_closed(sanitized)
            self._ready.set()
            self._progress.set()
            self._cancel_provider_tasks()

    def _writer_failed(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = f"WRITER_FAILED: {error}"[:500]
        if self._lifecycle.current_state != "RECOVERY_BLOCKED":
            self._lifecycle.fail_closed(self._failure)
        try:
            self._store.append_lifecycle_state(
                run_id=self._run_id,
                state="RECOVERY_BLOCKED",
                sequence_index=len(self._lifecycle.states),
                created_at_ms=self._clock_ms(),
                detail=self._failure,
            )
        except BaseException:
            pass
        self._ready.set()
        self._progress.set()
        self._cancel_provider_tasks()

    def _reject_pending_writes(self, error: BaseException) -> None:
        while True:
            try:
                pending = self._write_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if (
                    type(pending) is _RuntimeWriteCommand
                    and pending.future is not None
                    and not pending.future.done()
                ):
                    pending.future.set_exception(error)
            finally:
                self._write_queue.task_done()

    def _cancel_provider_tasks(self) -> None:
        current = asyncio.current_task()
        for name in ("binance_stream", "polymarket_stream"):
            task = self._tasks.get(name)
            if task is not None and task is not current and not task.done():
                task.cancel()

    def _ensure_running(self) -> None:
        if self._failure is not None:
            raise RuntimeError(self._failure)
        writer = self._tasks.get("writer")
        if writer is not None and writer.done() and not self._stopping:
            raise RuntimeError("RUNTIME_WRITER_EXITED")

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
