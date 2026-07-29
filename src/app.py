from __future__ import annotations

import asyncio
import inspect
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from src.api import create_api_app
from src.config import RuntimeConfig, load_runtime_config
from src.outbox import OutboxBroker
from src.single_instance import AlreadyRunningError, WindowsMutex
from src.storage import SqliteReadStore, SqliteStore


CLEAN_STOP_EXIT = 0
ALREADY_RUNNING_EXIT = 20
CONFIG_FAILURE_EXIT = 30
DATABASE_INTEGRITY_EXIT = 40
UNEXPECTED_FAILURE_EXIT = 1
DEFAULT_MUTEX_NAME = "BTC_LIVE_BACKEND_WINDOWS_V1"

SHUTDOWN_ORDER = (
    "stop accepting API connections",
    "stop provider reconnect loops",
    "drain source queue",
    "commit pending writer commands",
    "flush outbox state",
    "close read connections",
    "close writer",
    "release mutex",
)


class BackendConfigError(RuntimeError):
    pass


class DatabaseIntegrityError(RuntimeError):
    pass


class RuntimeLifecycle(Protocol):
    def initialize(self, config: RuntimeConfig, store: SqliteStore) -> None: ...

    async def start_runtime_tasks(self) -> None: ...

    async def start_api(self, host: str, port: int) -> None: ...

    async def stop_api(self) -> None: ...

    async def stop_providers(self) -> None: ...

    async def drain_source_queue(self) -> None: ...

    async def commit_pending_writer_commands(self) -> None: ...

    async def flush_outbox(self) -> None: ...

    async def close_read_connections(self) -> None: ...

    async def close_writer(self) -> None: ...


class BackendRuntime:
    def __init__(self, *, orchestrator_factory=None) -> None:
        self._store: SqliteStore | None = None
        self._read_store: SqliteReadStore | None = None
        self._broker: OutboxBroker | None = None
        self._api_runner: web.AppRunner | None = None
        self._provider_tasks: list[asyncio.Task[Any]] = []
        self._source_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._config: RuntimeConfig | None = None
        self._orchestrator_factory = orchestrator_factory
        self._orchestrator = None

    def initialize(
        self,
        config: RuntimeConfig,
        store: SqliteStore,
    ) -> None:
        self._config = config
        self._store = store
        self._read_store = store.open_read_store()
        self._broker = OutboxBroker()

    async def start_runtime_tasks(self) -> None:
        if self._store is None or self._broker is None:
            raise RuntimeError("BACKEND_RUNTIME_NOT_INITIALIZED")
        factory = self._orchestrator_factory
        if factory is None:
            from src.runtime_orchestrator import (
                build_default_runtime_orchestrator,
            )

            factory = build_default_runtime_orchestrator
        orchestrator = factory(store=self._store, broker=self._broker)
        if inspect.isawaitable(orchestrator):
            orchestrator = await orchestrator
        self._orchestrator = orchestrator
        await self._orchestrator.start()

    async def start_api(self, host: str, port: int) -> None:
        if self._read_store is None or self._broker is None:
            raise RuntimeError("BACKEND_RUNTIME_NOT_INITIALIZED")
        runtime_status = (
            None
            if self._orchestrator is None
            else self._orchestrator.status
        )
        app = create_api_app(
            self._read_store,
            self._broker,
            runtime_status=runtime_status,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._api_runner = runner

    async def stop_api(self) -> None:
        if self._api_runner is not None:
            await self._api_runner.cleanup()
            self._api_runner = None

    async def stop_providers(self) -> None:
        if self._orchestrator is not None:
            await self._orchestrator.stop()
            return
        for task in self._provider_tasks:
            task.cancel()
        if self._provider_tasks:
            await asyncio.gather(
                *self._provider_tasks,
                return_exceptions=True,
            )
        self._provider_tasks.clear()

    async def drain_source_queue(self) -> None:
        if self._orchestrator is not None:
            await self._orchestrator.drain_source_queue()
            return
        await self._source_queue.join()

    async def commit_pending_writer_commands(self) -> None:
        if self._store is not None:
            self._store.commit_pending()

    async def flush_outbox(self) -> None:
        return None

    async def close_read_connections(self) -> None:
        if self._read_store is not None:
            self._read_store.close()
            self._read_store = None

    async def close_writer(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


class LiveBackend:
    def __init__(
        self,
        *,
        config_path: Path,
        database_path: Path,
        runtime: RuntimeLifecycle | None = None,
        mutex_name: str = DEFAULT_MUTEX_NAME,
        config_loader: Callable[[Path], RuntimeConfig] = load_runtime_config,
        mutex_factory: Callable[[str], WindowsMutex] = WindowsMutex.acquire,
        store_factory: Callable[[Path], SqliteStore] = SqliteStore.open,
    ) -> None:
        self._config_path = config_path
        self._database_path = database_path
        self._runtime = runtime or BackendRuntime()
        self._mutex_name = mutex_name
        self._config_loader = config_loader
        self._mutex_factory = mutex_factory
        self._store_factory = store_factory
        self._mutex: WindowsMutex | None = None
        self._store: SqliteStore | None = None
        self._runtime_initialized = False
        self._runtime_tasks_started = False
        self._api_started = False
        self._started = False
        self._stopped = False
        self._shutdown_trace: list[str] = []

    @property
    def shutdown_trace(self) -> tuple[str, ...]:
        return tuple(self._shutdown_trace)

    async def start(self) -> None:
        if self._started:
            return
        try:
            config = self._config_loader(self._config_path)
        except BaseException as error:
            raise BackendConfigError("BACKEND_CONFIG_FAILURE") from error

        try:
            self._mutex = self._mutex_factory(self._mutex_name)
            try:
                self._store = self._store_factory(self._database_path)
                self._store.migrate()
                report = self._store.integrity_report()
            except BaseException as error:
                raise DatabaseIntegrityError(
                    "BACKEND_DATABASE_INTEGRITY_FAILURE"
                ) from error
            if report.get("status") != "PASS":
                raise DatabaseIntegrityError(
                    "BACKEND_DATABASE_INTEGRITY_FAILURE"
                )

            self._runtime_initialized = True
            self._runtime.initialize(config, self._store)
            await self._runtime.start_runtime_tasks()
            self._runtime_tasks_started = True
            await self._runtime.start_api(
                config.bind_host,
                config.bind_port,
            )
            self._api_started = True
            self._started = True
        except BaseException as error:
            try:
                await self.stop()
            except BaseException as cleanup_error:
                error.add_note(f"cleanup failed: {cleanup_error}")
            raise

    async def stop(self) -> None:
        if self._stopped:
            return
        errors: list[BaseException] = []

        if self._api_started:
            await self._shutdown_step(
                "stop accepting API connections",
                self._runtime.stop_api,
                errors,
            )
        if self._runtime_tasks_started:
            await self._shutdown_step(
                "stop provider reconnect loops",
                self._runtime.stop_providers,
                errors,
            )
        if self._runtime_initialized:
            for label, operation in (
                ("drain source queue", self._runtime.drain_source_queue),
                (
                    "commit pending writer commands",
                    self._runtime.commit_pending_writer_commands,
                ),
                ("flush outbox state", self._runtime.flush_outbox),
                (
                    "close read connections",
                    self._runtime.close_read_connections,
                ),
                ("close writer", self._runtime.close_writer),
            ):
                await self._shutdown_step(label, operation, errors)
        elif self._store is not None:
            try:
                self._store.close()
            except BaseException as error:
                errors.append(error)

        if self._mutex is not None:
            self._shutdown_trace.append("release mutex")
            try:
                self._mutex.close()
            except BaseException as error:
                errors.append(error)
            self._mutex = None

        self._stopped = True
        self._started = False
        if errors:
            raise RuntimeError("BACKEND_SHUTDOWN_FAILED") from errors[0]

    async def _shutdown_step(
        self,
        label: str,
        operation,
        errors: list[BaseException],
    ) -> None:
        self._shutdown_trace.append(label)
        try:
            await operation()
        except BaseException as error:
            errors.append(error)


async def run_backend(
    backend,
    *,
    wait_for_stop: bool = True,
) -> int:
    try:
        await backend.start()
        if wait_for_stop:
            await _wait_for_sigint()
        await backend.stop()
        return CLEAN_STOP_EXIT
    except AlreadyRunningError:
        await _stop_after_failure(backend)
        return ALREADY_RUNNING_EXIT
    except BackendConfigError:
        await _stop_after_failure(backend)
        return CONFIG_FAILURE_EXIT
    except DatabaseIntegrityError:
        await _stop_after_failure(backend)
        return DATABASE_INTEGRITY_EXIT
    except BaseException:
        await _stop_after_failure(backend)
        return UNEXPECTED_FAILURE_EXIT


async def _stop_after_failure(backend) -> None:
    try:
        await backend.stop()
    except BaseException:
        pass


async def _wait_for_sigint() -> None:
    loop = asyncio.get_running_loop()
    requested = asyncio.Event()
    previous = signal.getsignal(signal.SIGINT)

    def request_stop(signum, frame) -> None:
        loop.call_soon_threadsafe(requested.set)

    signal.signal(signal.SIGINT, request_stop)
    try:
        await requested.wait()
    finally:
        signal.signal(signal.SIGINT, previous)
