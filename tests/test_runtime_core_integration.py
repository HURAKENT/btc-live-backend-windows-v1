from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from src.api import build_health_payload, create_api_app
from src.outbox import OutboxBroker
from src.storage import SqliteStore
from tests.test_runtime_orchestrator import (
    FakeBinanceRuntimeAdapter,
    FakePolymarketRuntimeAdapter,
)

try:
    from src.runtime_orchestrator import C1RuntimeOrchestrator, RuntimeStatus
except ModuleNotFoundError:
    C1RuntimeOrchestrator = None
    RuntimeStatus = None


class RuntimeCoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "core.sqlite3"))
        self.store.migrate()
        self.read_store = self.store.open_read_store()
        self.broker = OutboxBroker()
        self.binance = FakeBinanceRuntimeAdapter()
        self.polymarket = FakePolymarketRuntimeAdapter()
        self.runtime = C1RuntimeOrchestrator(
            run_id="integration-run",
            store=self.store,
            broker=self.broker,
            binance_adapter=self.binance,
            polymarket_adapter=self.polymarket,
            binance_start_ms=60_000,
            binance_end_ms=60_000,
            history_start_ts=1,
            history_end_ts=2,
            clock_ms=lambda: 2_000,
        )

    async def asyncTearDown(self):
        if self.runtime is not None:
            await self.runtime.stop()
        self.read_store.close()
        self.store.close()
        self.temp.cleanup()

    async def test_real_storage_projection_canary_and_outbox_flow(self):
        await self.runtime.start()
        self.binance.release.set()
        await self.runtime.wait_for_source_idle()
        self.assertEqual(self.store.count("market_catalog"), 1)
        self.assertEqual(self.store.count("canonical_state"), 1)
        self.assertEqual(self.store.count("strategy_evaluations"), 1)
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)

    async def test_duplicate_redelivery_is_ignored_and_database_is_valid(self):
        await self.runtime.start()
        self.binance.release.set()
        await self.runtime.wait_for_source_idle()
        await self.runtime.enqueue(self.binance.live_event)
        await self.runtime.wait_for_source_idle()
        self.assertEqual(self.store.count("canonical_state"), 1)
        self.assertEqual(self.store.integrity_report()["status"], "PASS")

    async def test_api_is_not_pass_while_runtime_is_starting(self):
        status = RuntimeStatus.starting()
        health = build_health_payload(self.read_store, status)
        self.assertNotEqual(health["status"], "PASS")
        self.assertEqual(health["runtime_readiness"]["state"], "BOOTING")

    async def test_api_is_not_pass_with_one_required_source_missing(self):
        status = RuntimeStatus(
            state="LIVE_READY",
            live_ready=True,
            source_health=(("binance", "LIVE"), ("polymarket", "MISSING")),
            market_id="event-1",
            market_count=11,
            asset_count=22,
            last_event_id=1,
            failure=None,
        )
        self.assertNotEqual(
            build_health_payload(self.read_store, status)["status"],
            "PASS",
        )

    async def test_live_ready_both_sources_is_api_pass_without_socket(self):
        await self.runtime.start()
        app = create_api_app(
            self.read_store,
            self.broker,
            runtime_status=self.runtime.status,
        )
        self.assertEqual(
            build_health_payload(
                self.read_store,
                app["runtime_status"](),
            )["status"],
            "PASS",
        )
        await self.runtime.stop()
        self.assertEqual(self.runtime.pending_source_events, 0)
        self.assertEqual(self.runtime.pending_owned_tasks, ())


if __name__ == "__main__":
    unittest.main()
