from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from src.app import (
    ALREADY_RUNNING_EXIT,
    CLEAN_STOP_EXIT,
    CONFIG_FAILURE_EXIT,
    DATABASE_INTEGRITY_EXIT,
    BackendConfigError,
    DatabaseIntegrityError,
    LiveBackend,
    run_backend,
)
from src.single_instance import AlreadyRunningError, WindowsMutex


EXPECTED_SHUTDOWN_ORDER = (
    "stop accepting API connections",
    "stop provider reconnect loops",
    "drain source queue",
    "commit pending writer commands",
    "flush outbox state",
    "close read connections",
    "close writer",
    "release mutex",
)


class _FakeMutex:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.trace.append("release mutex")
            self.closed = True


class _FakeStore:
    def __init__(
        self,
        *,
        integrity_status: str = "PASS",
        fail_migrate: bool = False,
    ) -> None:
        self.integrity_status = integrity_status
        self.fail_migrate = fail_migrate
        self.migrated = False

    def migrate(self) -> None:
        if self.fail_migrate:
            raise RuntimeError("migration failed")
        self.migrated = True

    def integrity_report(self) -> dict[str, str]:
        return {"status": self.integrity_status}


class _FakeRuntime:
    def __init__(
        self,
        trace: list[str],
        *,
        fail_initialize: bool = False,
        fail_start: bool = False,
    ) -> None:
        self.trace = trace
        self.fail_initialize = fail_initialize
        self.fail_start = fail_start
        self.api_started = False

    def initialize(self, config, store) -> None:
        self.trace.append("initialize runtime")
        if self.fail_initialize:
            raise RuntimeError("initialize failed")

    async def start_runtime_tasks(self) -> None:
        self.trace.append("start runtime tasks")
        if self.fail_start:
            raise RuntimeError("runtime start failed")

    async def start_api(self, host: str, port: int) -> None:
        self.trace.append("start API")
        self.api_started = True

    async def stop_api(self) -> None:
        self.trace.append("stop accepting API connections")

    async def stop_providers(self) -> None:
        self.trace.append("stop provider reconnect loops")

    async def drain_source_queue(self) -> None:
        self.trace.append("drain source queue")

    async def commit_pending_writer_commands(self) -> None:
        self.trace.append("commit pending writer commands")

    async def flush_outbox(self) -> None:
        self.trace.append("flush outbox state")

    async def close_read_connections(self) -> None:
        self.trace.append("close read connections")

    async def close_writer(self) -> None:
        self.trace.append("close writer")


class _ExitBackend:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.stop_calls = 0

    async def start(self) -> None:
        if self.error is not None:
            raise self.error

    async def stop(self) -> None:
        self.stop_calls += 1


class WindowsMutexTests(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        name = f"BTC_LIVE_BACKEND_TEST_{uuid.uuid4().hex}"
        first = WindowsMutex.acquire(name)
        try:
            with self.assertRaisesRegex(
                AlreadyRunningError,
                "BACKEND_ALREADY_RUNNING",
            ):
                WindowsMutex.acquire(name)
        finally:
            first.close()

    def test_mutex_is_reusable_after_close(self):
        name = f"BTC_LIVE_BACKEND_TEST_{uuid.uuid4().hex}"
        first = WindowsMutex.acquire(name)
        first.close()
        second = WindowsMutex.acquire(name)
        second.close()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_directory.name, "config.json")
        self.database_path = Path(self.temp_directory.name, "runtime.sqlite3")

    def tearDown(self):
        self.temp_directory.cleanup()

    async def test_api_starts_before_runtime_recovery_after_integrity_gate(self):
        trace: list[str] = []
        backend = self._backend(trace)

        await backend.start()

        self.assertLess(
            trace.index("start API"),
            trace.index("start runtime tasks"),
        )
        await backend.stop()

    async def test_shutdown_order_is_exact_and_mutex_is_last(self):
        trace: list[str] = []
        backend = self._backend(trace)
        await backend.start()
        trace.clear()

        await backend.stop()

        self.assertEqual(tuple(trace), EXPECTED_SHUTDOWN_ORDER)
        self.assertEqual(backend.shutdown_trace, EXPECTED_SHUTDOWN_ORDER)

    async def test_stop_is_idempotent(self):
        trace: list[str] = []
        backend = self._backend(trace)
        await backend.start()
        trace.clear()

        await backend.stop()
        await backend.stop()

        self.assertEqual(tuple(trace), EXPECTED_SHUTDOWN_ORDER)

    async def test_runtime_start_failure_stops_api_and_partial_providers(self):
        trace: list[str] = []
        runtime = _FakeRuntime(trace, fail_start=True)
        backend = self._backend(trace, runtime=runtime)

        with self.assertRaisesRegex(RuntimeError, "runtime start failed"):
            await backend.start()

        self.assertLess(
            trace.index("start API"),
            trace.index("start runtime tasks"),
        )
        self.assertIn("stop accepting API connections", trace)
        self.assertIn("stop provider reconnect loops", trace)
        self.assertLess(
            trace.index("stop accepting API connections"),
            trace.index("stop provider reconnect loops"),
        )

    async def test_partial_start_rolls_back_acquired_resources(self):
        trace: list[str] = []
        runtime = _FakeRuntime(trace, fail_initialize=True)
        backend = self._backend(trace, runtime=runtime)

        with self.assertRaisesRegex(RuntimeError, "initialize failed"):
            await backend.start()

        self.assertEqual(
            trace[-2:],
            ["close writer", "release mutex"],
        )

    async def test_api_does_not_start_before_integrity_gate(self):
        trace: list[str] = []
        runtime = _FakeRuntime(trace)
        backend = self._backend(
            trace,
            runtime=runtime,
            store=_FakeStore(integrity_status="FAIL"),
        )

        with self.assertRaises(DatabaseIntegrityError):
            await backend.start()

        self.assertFalse(runtime.api_started)
        self.assertNotIn("start API", trace)

    async def test_pending_writer_commit_precedes_writer_close(self):
        trace: list[str] = []
        backend = self._backend(trace)
        await backend.start()
        trace.clear()

        await backend.stop()

        self.assertLess(
            trace.index("commit pending writer commands"),
            trace.index("close writer"),
        )

    async def test_config_failure_maps_to_exit_30(self):
        exit_code = await run_backend(
            _ExitBackend(BackendConfigError("bad config")),
            wait_for_stop=False,
        )
        self.assertEqual(exit_code, CONFIG_FAILURE_EXIT)

    async def test_integrity_failure_maps_to_exit_40(self):
        exit_code = await run_backend(
            _ExitBackend(DatabaseIntegrityError("bad database")),
            wait_for_stop=False,
        )
        self.assertEqual(exit_code, DATABASE_INTEGRITY_EXIT)

    async def test_already_running_maps_to_exit_20(self):
        exit_code = await run_backend(
            _ExitBackend(AlreadyRunningError("BACKEND_ALREADY_RUNNING")),
            wait_for_stop=False,
        )
        self.assertEqual(exit_code, ALREADY_RUNNING_EXIT)

    async def test_clean_stop_maps_to_exit_0(self):
        backend = _ExitBackend()
        exit_code = await run_backend(backend, wait_for_stop=False)
        self.assertEqual(exit_code, CLEAN_STOP_EXIT)
        self.assertEqual(backend.stop_calls, 1)

    async def test_unexpected_failure_is_not_clean_exit(self):
        exit_code = await run_backend(
            _ExitBackend(RuntimeError("unexpected")),
            wait_for_stop=False,
        )
        self.assertNotEqual(exit_code, CLEAN_STOP_EXIT)

    def _backend(
        self,
        trace: list[str],
        *,
        runtime: _FakeRuntime | None = None,
        store: _FakeStore | None = None,
    ) -> LiveBackend:
        config = SimpleNamespace(bind_host="127.0.0.1", bind_port=8767)
        fake_mutex = _FakeMutex(trace)
        return LiveBackend(
            config_path=self.config_path,
            database_path=self.database_path,
            runtime=runtime or _FakeRuntime(trace),
            config_loader=lambda path: config,
            mutex_factory=lambda name: fake_mutex,
            store_factory=lambda path: store or _FakeStore(),
        )


if __name__ == "__main__":
    unittest.main()
