from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path

from aiohttp import ClientSession
from src.app import BackendRuntime, LiveBackend
from src.integration_endpoints import load_integration_endpoints
from src.runtime_orchestrator import build_default_runtime_orchestrator
from src.windows_operations import StartupValidatedSqliteStore
from tests.support.fake_provider_servers import FakeProviderServer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "native Windows process acceptance required")
class C9WindowsProcessAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.barrier = asyncio.Event()
        self.server = FakeProviderServer(
            startup_barrier=self.barrier,
            initial_book_batch=True,
            enable_rollover=True,
        )
        await self.server.start()
        self.api_port = self._free_port()
        self.database_path = self.root / "runtime.sqlite3"
        self.log_path = self.root / "backend.log"
        self.endpoints_path = self.root / "endpoints.json"
        self.endpoints_path.write_text(
            json.dumps(self.server.endpoint_payload(self.api_port)),
            encoding="utf-8",
        )
        self.processes = []

    async def asyncTearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 10)
        await self.server.close()
        self.temp.cleanup()

    async def test_c9_launcher_mutex_clean_shutdown_and_restart(self):
        first = self._spawn()
        await self._wait_for_provider_barrier(first)
        self.assertEqual((await self._health(first))["status"], "STARTING")
        self.barrier.set()
        self.assertEqual((await self._wait_for_pass(first))["status"], "PASS")

        second = self._spawn()
        self.assertEqual(await asyncio.to_thread(second.wait, 10), 20)
        self.assertIsNone(first.poll())

        await self._clean_stop(first)
        self.assertEqual(first.returncode, 0)
        self.assertTrue(self._port_is_free(self.api_port))

        restarted = self._spawn()
        self.assertEqual((await self._wait_for_pass(restarted))["status"], "PASS")
        await self._clean_stop(restarted)
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(self._port_is_free(self.api_port))

        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("backend startup requested", log_text)
        self.assertIn("backend stopped with exit_code=20", log_text)
        self.assertGreaterEqual(log_text.count("backend clean shutdown"), 2)

    async def test_c9_backend_components_start_and_stop_in_process(self):
        self.barrier.set()
        endpoints = load_integration_endpoints(self.endpoints_path)
        runtime = BackendRuntime(
            orchestrator_factory=partial(
                build_default_runtime_orchestrator,
                integration_endpoints=endpoints,
            ),
            api_bind_override=(endpoints.api_bind_host, endpoints.api_bind_port),
        )
        backend = LiveBackend(
            config_path=PROJECT_ROOT / "config" / "mvp_runtime_v1.json",
            database_path=self.database_path,
            runtime=runtime,
            store_factory=StartupValidatedSqliteStore.open,
        )

        await backend.start()
        await backend.stop()

    def _spawn(self):
        process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "run_windows_backend.py"),
                "--database-path",
                str(self.database_path),
                "--log-path",
                str(self.log_path),
                "--integration-test-mode",
                "--integration-endpoints",
                str(self.endpoints_path),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.processes.append(process)
        return process

    async def _health(self, process):
        deadline = time.monotonic() + 10
        url = f"http://127.0.0.1:{self.api_port}/api/v1/health"
        async with ClientSession() as session:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f"backend exited early: {process.returncode}")
                try:
                    async with session.get(url) as response:
                        return await response.json()
                except OSError:
                    await asyncio.sleep(0.05)
        self.fail("backend API did not become available")

    async def _wait_for_provider_barrier(self, process):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.startup_barrier_reached.is_set():
                return
            if process.poll() is not None:
                log_text = (
                    self.log_path.read_text(encoding="utf-8")
                    if self.log_path.exists()
                    else "<log missing>"
                )
                self.fail(
                    f"backend exited before provider barrier: "
                    f"{process.returncode}\n{log_text}"
                )
            await asyncio.sleep(0.05)
        log_text = (
            self.log_path.read_text(encoding="utf-8")
            if self.log_path.exists()
            else "<log missing>"
        )
        self.fail(
            f"provider barrier timeout; process={process.poll()}\n{log_text}"
        )

    async def _wait_for_pass(self, process):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            payload = await self._health(process)
            if payload["status"] == "PASS":
                return payload
            await asyncio.sleep(0.05)
        self.fail("backend did not reach PASS")

    async def _clean_stop(self, process):
        process.send_signal(signal.CTRL_BREAK_EVENT)
        await asyncio.to_thread(process.wait, 20)

    @staticmethod
    def _free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    @staticmethod
    def _port_is_free(port):
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                return False
            return True


if __name__ == "__main__":
    unittest.main()
