from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import run_backend
from aiohttp import ClientSession
from tests.support.fake_provider_servers import FakeProviderServer

try:
    from src.integration_endpoints import (
        IntegrationEndpoints,
        load_integration_endpoints,
    )
except ModuleNotFoundError:
    IntegrationEndpoints = None
    load_integration_endpoints = None


class IntegrationEndpointContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_loopback_contract_is_immutable(self):
        payload_path = self._write()
        endpoints = load_integration_endpoints(payload_path)
        self.assertIs(type(endpoints), IntegrationEndpoints)
        with self.assertRaises(AttributeError):
            endpoints.api_bind_port = 1

    def test_non_loopback_url_is_rejected(self):
        payload = self._payload()
        payload["binance_rest_bases"] = ["https://api.binance.com"]
        with self.assertRaisesRegex(ValueError, "INTEGRATION_ENDPOINT_NOT_LOOPBACK"):
            load_integration_endpoints(self._write(payload))

    def test_url_userinfo_and_fragment_are_rejected(self):
        for value in (
            "http://user@127.0.0.1:1",
            "http://127.0.0.1:1/path#fragment",
        ):
            payload = self._payload()
            payload["gamma_events_url"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_integration_endpoints(self._write(payload))

    def test_unknown_or_missing_keys_are_rejected(self):
        payload = self._payload()
        payload["unknown"] = True
        with self.assertRaisesRegex(ValueError, "INVALID_INTEGRATION_ENDPOINTS_SCHEMA"):
            load_integration_endpoints(self._write(payload))
        payload = self._payload()
        del payload["gamma_events_url"]
        with self.assertRaisesRegex(ValueError, "INVALID_INTEGRATION_ENDPOINTS_SCHEMA"):
            load_integration_endpoints(self._write(payload))

    def test_endpoints_without_mode_returns_exit_30(self):
        path = self._write()
        self.assertEqual(self._main(["--integration-endpoints", str(path)]), 30)

    def test_mode_without_endpoints_returns_exit_30(self):
        self.assertEqual(self._main(["--integration-test-mode"]), 30)

    def test_relative_endpoints_path_returns_exit_30(self):
        self.assertEqual(
            self._main(
                [
                    "--integration-test-mode",
                    "--integration-endpoints",
                    "relative.json",
                ]
            ),
            30,
        )

    def test_malformed_endpoints_json_returns_exit_30(self):
        path = self.root / "bad.json"
        path.write_text("{", encoding="utf-8")
        self.assertEqual(
            self._main(
                [
                    "--integration-test-mode",
                    "--integration-endpoints",
                    str(path),
                ]
            ),
            30,
        )

    def test_environment_cannot_enable_integration_mode(self):
        with patch.dict(
            os.environ,
            {"INTEGRATION_TEST_MODE": "1"},
            clear=False,
        ):
            backend = patch.object(run_backend, "LiveBackend").start()
            runner = patch.object(
                run_backend,
                "run_backend",
                new=AsyncMock(return_value=0),
            ).start()
            try:
                self.assertEqual(run_backend.main([]), 0)
                runner.assert_awaited_once()
                self.assertNotIn(
                    "integration_endpoints",
                    backend.call_args.kwargs,
                )
            finally:
                patch.stopall()

    def _main(self, args):
        with patch.object(run_backend, "LiveBackend"), patch.object(
            run_backend,
            "run_backend",
            new=AsyncMock(return_value=0),
        ):
            return run_backend.main(args)

    def _write(self, payload=None):
        path = self.root / "endpoints.json"
        path.write_text(
            json.dumps(self._payload() if payload is None else payload),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _payload():
        return {
            "schema_version": "C1_LOOPBACK_INTEGRATION_ENDPOINTS_V1",
            "api_bind_host": "127.0.0.1",
            "api_bind_port": 49123,
            "binance_rest_bases": ["http://127.0.0.1:49124"],
            "binance_websocket_url": "ws://127.0.0.1:49124/binance/ws",
            "gamma_events_url": "http://127.0.0.1:49124/gamma/events/keyset",
            "polymarket_clob_base_url": "http://127.0.0.1:49124/polymarket",
            "polymarket_websocket_url": "ws://127.0.0.1:49124/polymarket/ws",
        }


class FakeProviderRolloverScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollover_schedule_arms_once_at_first_gamma_response(self):
        clock_calls = 0

        class ClockedFakeProviderServer(FakeProviderServer):
            def _rollover_now(self):
                nonlocal clock_calls
                clock_calls += 1
                return datetime(2035, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        server = ClockedFakeProviderServer(enable_rollover=True)
        initial_event_ids = (
            server.event["id"],
            server.next_event["id"],
            server.later_event["id"],
        )
        initial_asset_ids = (
            server.asset_ids,
            server.next_asset_ids,
            server.later_asset_ids,
        )
        self.assertEqual(clock_calls, 0)
        self.assertEqual(server.event["endDate"], "2026-08-02T04:00:00Z")

        await server.start()
        try:
            url = f"http://127.0.0.1:{server.port}/gamma/events/keyset"
            async with ClientSession() as session:
                async with session.get(url) as response:
                    first_body = await response.read()
                async with session.get(url) as response:
                    second_body = await response.read()

            self.assertEqual(clock_calls, 1)
            self.assertEqual(first_body, second_body)
            payload = json.loads(first_body)
            events = payload["events"]
            self.assertEqual(
                tuple(event["endDate"] for event in events),
                (
                    "2035-01-02T03:04:07Z",
                    "2035-01-03T03:04:07Z",
                    "2035-01-04T03:04:07Z",
                ),
            )
            self.assertEqual(
                tuple(event["id"] for event in events),
                initial_event_ids,
            )
            self.assertEqual(
                (
                    server.asset_ids,
                    server.next_asset_ids,
                    server.later_asset_ids,
                ),
                initial_asset_ids,
            )
        finally:
            await server.close()


class ProcessRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.startup_barrier = asyncio.Event()
        self.server = FakeProviderServer(
            startup_barrier=self.startup_barrier,
            initial_book_batch=True,
            enable_rollover=True,
        )
        await self.server.start()
        self.api_port = self._free_port()
        self.database_path = self.root / "runtime.sqlite3"
        self.endpoints_path = self.root / "endpoints.json"
        self.endpoints_path.write_text(
            json.dumps(self.server.endpoint_payload(self.api_port)),
            encoding="utf-8",
        )
        self.processes = []
        self.status_timeline = []
        self.timeline_started_at = time.monotonic()

    async def asyncTearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
        await self.server.close()
        self.temp.cleanup()

    async def test_real_process_starting_pass_reconnect_mutex_and_restart(self):
        started = time.monotonic()
        first = self._spawn()
        await asyncio.wait_for(
            self.server.startup_barrier_reached.wait(),
            timeout=10,
        )
        starting_health = await self._wait_for_health(first)
        self.assertEqual(starting_health["status"], "STARTING")
        self.assertIsNone(first.poll())
        self.assertFalse(self.startup_barrier.is_set())
        self.assertNotEqual(starting_health["status"], "PASS")

        self.startup_barrier.set()
        observed_starting, health = await self._wait_for_pass(
            first,
            observed_starting=True,
        )
        self.assertTrue(observed_starting)
        self.assertEqual(
            [entry["status"] for entry in self.status_timeline[:2]],
            ["STARTING", "PASS"],
            self.status_timeline,
        )
        readiness = health["runtime_readiness"]
        self.assertTrue(readiness["live_ready"])
        self.assertEqual(readiness["market_count"], 11)
        self.assertEqual(readiness["asset_count"], 22)
        self.assertEqual(health["source_health"], {
            "binance": "LIVE",
            "polymarket": "LIVE",
        })
        self.assertIsNone(first.poll())
        await self._wait_for_polymarket_reconnect()
        await self._wait_for_rollover()

        counts_before = self._database_counts()
        self.assertEqual(counts_before["market_catalog"], 2)
        self.assertGreater(counts_before["source_events"], 0)
        self.assertGreater(counts_before["canonical_state"], 0)
        self.assertGreater(counts_before["strategy_evaluations"], 0)
        self.assertGreater(counts_before["signals"], 0)
        self.assertGreater(counts_before["outbox_events"], 0)
        self.assertEqual(counts_before["duplicate_natural_keys"], 0)
        self.assertEqual(len(self.server.history_requests), 44)
        self.assertEqual(counts_before["c3_rollover_pass"], 1)

        second = self._spawn()
        second_exit = await asyncio.to_thread(second.wait, 10)
        self.assertEqual(second_exit, 20)
        self.assertIsNone(first.poll())

        replay_ids = await self._read_outbox_replay()
        self.assertEqual(replay_ids, sorted(set(replay_ids)))
        self.assertGreater(len(replay_ids), 0)

        await self._ctrl_break(first)
        self.assertEqual(first.returncode, 0)
        self.assertTrue(self._port_is_free(self.api_port))
        self.assertEqual(self._integrity(), ("ok", "ok", "wal", 2, 1))

        first_counts = self._database_counts()
        self.startup_barrier.clear()
        self.server.startup_barrier_reached.clear()
        restarted = self._spawn()
        await asyncio.wait_for(
            self.server.startup_barrier_reached.wait(),
            timeout=10,
        )
        restart_starting_health = await self._wait_for_health(
            restarted,
            phase="restart",
        )
        self.assertEqual(restart_starting_health["status"], "STARTING")
        self.assertIsNone(restarted.poll())
        self.startup_barrier.set()
        restart_observed_starting, restarted_health = await self._wait_for_pass(
            restarted,
            observed_starting=True,
            phase="restart",
        )
        self.assertTrue(restart_observed_starting)
        self.assertEqual(restarted_health["status"], "PASS")
        self.assertEqual(
            len(self.server.history_requests),
            44,
            "same-DB restart must use persisted per-asset history cursors",
        )
        self.assertEqual(
            [
                entry["status"]
                for entry in self.status_timeline
                if entry["phase"] == "restart"
            ][:2],
            ["STARTING", "PASS"],
            self.status_timeline,
        )
        self.assertEqual(
            self._blocking_incidents(),
            (),
            self._incident_dump(),
        )
        await self._ctrl_break(restarted)
        self.assertEqual(restarted.returncode, 0)
        self.assertTrue(self._port_is_free(self.api_port))
        self.assertTrue(
            all(process.poll() is not None for process in self.processes)
        )
        restarted_counts = self._database_counts()
        self.assertEqual(restarted_counts["market_catalog"], 2)
        self.assertEqual(restarted_counts["duplicate_natural_keys"], 0)
        self.assertEqual(restarted_counts["c2_recovery_pass"], 2)
        self.assertEqual(restarted_counts["c3_rollover_pass"], 1)
        self.assertGreaterEqual(
            restarted_counts["strategy_evaluations"],
            first_counts["strategy_evaluations"],
        )
        self.assertLess(time.monotonic() - started, 120)

    async def _wait_for_health(self, process, *, phase="initial"):
        deadline = time.monotonic() + 10
        url = f"http://127.0.0.1:{self.api_port}/api/v1/health"
        async with ClientSession() as session:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f"backend exited early: {process.returncode}")
                try:
                    async with session.get(url) as response:
                        payload = await response.json()
                    self._record_status(phase, payload)
                    return payload
                except OSError:
                    await asyncio.sleep(0)
        self.fail("backend API did not become available")

    async def _wait_for_pass(
        self,
        process,
        *,
        observed_starting=False,
        phase="initial",
    ):
        deadline = time.monotonic() + 45
        url = f"http://127.0.0.1:{self.api_port}/api/v1/health"
        async with ClientSession() as session:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f"backend exited early: {process.returncode}")
                try:
                    async with session.get(url) as response:
                        payload = await response.json()
                    self._record_status(phase, payload)
                    observed_starting |= payload["status"] in {
                        "STARTING",
                        "DEGRADED",
                    }
                    if payload["status"] == "PASS":
                        return observed_starting, payload
                except OSError:
                    pass
                await asyncio.sleep(0.05)
        self.fail("backend did not reach PASS")

    def _record_status(self, phase, payload):
        status = payload["status"]
        if (
            self.status_timeline
            and self.status_timeline[-1]["phase"] == phase
            and self.status_timeline[-1]["status"] == status
        ):
            return
        self.status_timeline.append(
            {
                "elapsed_ms": int(
                    (time.monotonic() - self.timeline_started_at) * 1000
                ),
                "phase": phase,
                "status": status,
            }
        )

    def test_fake_provider_default_has_no_startup_barrier(self):
        server = FakeProviderServer()
        self.assertIsNone(server.startup_barrier)
        self.assertFalse(server.initial_book_batch)
        self.assertFalse(server.enable_rollover)

    async def _wait_for_polymarket_reconnect(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.polymarket_connections >= 2:
                return
            await asyncio.sleep(0.05)
        self.fail("Polymarket stream did not reconnect")

    async def _wait_for_rollover(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._database_counts()["c3_rollover_pass"] == 1:
                return
            await asyncio.sleep(0.05)
        self.fail("C3 market rollover did not complete")

    async def _read_outbox_replay(self):
        url = f"http://127.0.0.1:{self.api_port}/ws/v1/events?after_event_id=0"
        ids = []
        async with ClientSession() as session:
            async with session.ws_connect(url) as websocket:
                for _ in range(2):
                    message = await websocket.receive_json(timeout=5)
                    ids.append(message["event_id"])
        return ids

    async def _ctrl_break(self, process):
        process.send_signal(signal.CTRL_BREAK_EVENT)
        await asyncio.to_thread(process.wait, 20)

    def _spawn(self):
        command = [
            sys.executable,
            str(Path("run_backend.py").resolve()),
            "--database-path",
            str(self.database_path.resolve()),
            "--integration-test-mode",
            "--integration-endpoints",
            str(self.endpoints_path.resolve()),
        ]
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.processes.append(process)
        return process

    def _database_counts(self):
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            result = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "market_catalog",
                    "source_events",
                    "canonical_state",
                    "strategy_evaluations",
                    "signals",
                    "outbox_events",
                )
            }
            result["duplicate_natural_keys"] = connection.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT natural_key FROM source_events "
                "GROUP BY natural_key HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            result["c2_recovery_pass"] = connection.execute(
                """
                SELECT COUNT(*) FROM incidents
                WHERE incident_key LIKE 'c2-recovery:%'
                  AND status = 'C2_RECOVERY_HARDENING_PASS'
                """
            ).fetchone()[0]
            result["c3_rollover_pass"] = connection.execute(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status = 'C3_MARKET_ROLLOVER_PASS'"
            ).fetchone()[0]
            return result
        finally:
            connection.close()

    def _integrity(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            return (
                connection.execute("PRAGMA quick_check").fetchone()[0],
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                connection.execute("PRAGMA synchronous").fetchone()[0],
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
            )
        finally:
            connection.close()

    def _blocking_incidents(self):
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            text = "\n".join(
                f"{row[0]}\n{row[1]}"
                for row in connection.execute(
                    "SELECT status, payload_json FROM incidents"
                )
            )
        finally:
            connection.close()
        return tuple(
            code
            for code in (
                "SOURCE_EVENT_CONFLICT",
                "RECOVERY_BLOCKED",
                "WRITER_FAILED",
                "UNEXPECTED_PROVIDER_STREAM_EXIT",
            )
            if code in text
        )

    def _incident_dump(self):
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            return connection.execute(
                "SELECT incident_key, status, payload_json FROM incidents "
                "WHERE status IN ('RECOVERY_BLOCKED', 'WRITER_FAILED') "
                "ORDER BY incident_id"
            ).fetchall()
        finally:
            connection.close()

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
