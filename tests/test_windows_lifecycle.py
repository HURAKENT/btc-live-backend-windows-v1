from __future__ import annotations

import asyncio
import signal
import unittest
from unittest.mock import AsyncMock, patch

from src import app


class _Backend:
    def __init__(self, *, stop_error: BaseException | None = None):
        self.stop_calls = 0
        self.stop_error = stop_error

    async def start(self):
        return None

    async def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error


class _FatalBackend(_Backend):
    async def wait_runtime_termination(self):
        await asyncio.sleep(0)
        raise RuntimeError("RECOVERY_BLOCKED")


class WindowsSignalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waiter_is_armed_before_startup_completes(self):
        events = []

        class SlowStartingBackend(_Backend):
            async def start(self):
                events.append("start-entered")
                await asyncio.sleep(0)
                events.append("start-completed")

        async def signal_during_startup():
            events.append("signal-waiter-armed")

        with patch.object(
            app,
            "_wait_for_shutdown_signal",
            new=signal_during_startup,
        ):
            exit_code = await app.run_backend(SlowStartingBackend())

        self.assertEqual(exit_code, app.CLEAN_STOP_EXIT)
        self.assertLess(
            events.index("signal-waiter-armed"),
            events.index("start-completed"),
            events,
        )

    async def test_fatal_runtime_terminates_backend_without_os_signal(self):
        backend = _FatalBackend()

        async def never_signalled():
            await asyncio.Event().wait()

        with patch.object(
            app,
            "_wait_for_shutdown_signal",
            new=never_signalled,
        ):
            task = asyncio.create_task(app.run_backend(backend))
            done, pending = await asyncio.wait({task}, timeout=0.05)
            try:
                self.assertIn(task, done)
            finally:
                for item in pending:
                    item.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        exit_code = task.result()
        self.assertEqual(exit_code, app.UNEXPECTED_FAILURE_EXIT)
        self.assertEqual(backend.stop_calls, 1)

    async def test_sigint_requests_shutdown_and_restores_handler(self):
        await self._assert_signal_requests_shutdown(signal.SIGINT)

    async def test_sigbreak_requests_shutdown_when_available(self):
        if not hasattr(signal, "SIGBREAK"):
            self.skipTest("SIGBREAK unavailable")
        await self._assert_signal_requests_shutdown(signal.SIGBREAK)

    async def test_repeated_signal_causes_one_backend_stop(self):
        backend = _Backend()

        async def trigger():
            await asyncio.sleep(0)
            for handler in installed.values():
                handler(0, None)
                handler(0, None)

        installed = {}
        with patch.object(signal, "getsignal", return_value="previous"), patch.object(
            signal,
            "signal",
            side_effect=lambda sig, handler: installed.__setitem__(sig, handler),
        ):
            trigger_task = asyncio.create_task(trigger())
            exit_code = await app.run_backend(backend)
            await trigger_task
        self.assertEqual(exit_code, 0)
        self.assertEqual(backend.stop_calls, 1)

    async def test_failed_stop_returns_nonzero(self):
        backend = _Backend(stop_error=RuntimeError("stop failed"))
        with patch.object(
            app,
            "_wait_for_shutdown_signal",
            new=AsyncMock(return_value=None),
        ):
            exit_code = await app.run_backend(backend)
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(backend.stop_calls, 2)

    async def _assert_signal_requests_shutdown(self, target):
        installed = {}
        restored = []

        def set_handler(sig, handler):
            if handler == "previous":
                restored.append(sig)
            else:
                installed[sig] = handler

        with patch.object(signal, "getsignal", return_value="previous"), patch.object(
            signal,
            "signal",
            side_effect=set_handler,
        ):
            task = asyncio.create_task(app._wait_for_shutdown_signal())
            await asyncio.sleep(0)
            installed[target](target, None)
            await asyncio.wait_for(task, timeout=1)

        self.assertIn(target, restored)


if __name__ == "__main__":
    unittest.main()
