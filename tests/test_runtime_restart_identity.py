from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.integration_endpoints import IntegrationEndpoints
from src.outbox import OutboxBroker
from src.runtime_orchestrator import build_default_runtime_orchestrator
from src.storage import SqliteStore
from tests.support.fake_provider_servers import FakeProviderServer


class _StableRestartFakeProviderServer(FakeProviderServer):
    """Arm one future market pair once, then keep it stable across restarts."""

    def _rollover_now(self) -> datetime:
        return (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(hours=12)
        )


class SameDatabaseRestartIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "runtime.sqlite3"
        self.server = _StableRestartFakeProviderServer(enable_rollover=True)
        await self.server.start()
        payload = self.server.endpoint_payload(api_port=49123)
        self.endpoints = IntegrationEndpoints(
            api_bind_host=payload["api_bind_host"],
            api_bind_port=payload["api_bind_port"],
            binance_rest_bases=tuple(payload["binance_rest_bases"]),
            binance_websocket_url=payload["binance_websocket_url"],
            gamma_events_url=payload["gamma_events_url"],
            polymarket_clob_base_url=payload["polymarket_clob_base_url"],
            polymarket_websocket_url=payload["polymarket_websocket_url"],
        )

    async def asyncTearDown(self) -> None:
        await self.server.close()
        self.temp.cleanup()

    async def test_same_book_hash_with_later_observation_survives_restart(self) -> None:
        first_status = await self._run_once()
        await asyncio.sleep(0.01)
        second_status = await self._run_once()

        self.assertTrue(first_status.live_ready)
        self.assertTrue(second_status.live_ready)
        self.assertIsNone(first_status.failure)
        self.assertIsNone(second_status.failure)

        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM market_catalog"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM source_events
                    WHERE natural_key LIKE 'polymarket:%:book:rest-book'
                    """
                ).fetchone()[0],
                22,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE status = 'RECOVERY_BLOCKED'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("PRAGMA quick_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            connection.close()

    async def _run_once(self):
        store = SqliteStore.open(self.database_path)
        store.migrate()
        runtime = await build_default_runtime_orchestrator(
            store=store,
            broker=OutboxBroker(),
            integration_endpoints=self.endpoints,
        )
        try:
            await asyncio.wait_for(runtime.start(), timeout=15)
            return runtime.status()
        finally:
            await runtime.stop()
            store.close()


if __name__ == "__main__":
    unittest.main()
