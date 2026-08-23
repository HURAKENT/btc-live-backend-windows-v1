from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from src.api import create_api_app
from src.app import BackendRuntime
from src.config import load_versioned_runtime_config
from src.models import SignalRecord
from src.outbox import OutboxBroker
from src.storage import SqliteStore
from tests.test_paper import PaperLedgerTests


class MvpApiDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_ready_without_active_market_does_not_fall_back_to_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            payload_json = json.dumps(
                {"event_id": "stale", "market_date": "2026-07-08"},
                sort_keys=True,
                separators=(",", ":"),
            )
            store.persist_market_identity(
                market_id="stale",
                payload_json=payload_json,
                payload_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
                updated_at_ms=200,
            )
            read_store = store.open_read_store()
            runtime_status = lambda: {
                "state": "LIVE_READY",
                "live_ready": True,
                "source_health": (("binance", "LIVE"), ("polymarket", "LIVE")),
                "market_id": None,
                "market_count": 11,
                "asset_count": 22,
                "last_event_id": 0,
                "failure": None,
            }
            client = TestClient(
                TestServer(
                    create_api_app(
                        read_store,
                        OutboxBroker(),
                        runtime_status=runtime_status,
                    )
                )
            )
            await client.start_server()
            try:
                bootstrap = await (await client.get("/api/v1/bootstrap")).json()
                self.assertIsNone(bootstrap["current_market_identity"])
                self.assertEqual(bootstrap["health"]["status"], "DEGRADED")
                self.assertEqual(
                    bootstrap["health"]["runtime_readiness"]["blocking_reason"],
                    "ACTIVE_MARKET_IDENTITY_MISSING",
                )
            finally:
                await client.close()
                read_store.close()
                store.close()

    async def test_missing_runtime_active_market_is_explicitly_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            read_store = store.open_read_store()
            runtime_status = lambda: {
                "state": "LIVE_READY",
                "live_ready": True,
                "source_health": (("binance", "LIVE"), ("polymarket", "LIVE")),
                "market_id": "missing-active",
                "market_count": 11,
                "asset_count": 22,
                "last_event_id": 0,
                "failure": None,
            }
            client = TestClient(
                TestServer(
                    create_api_app(
                        read_store,
                        OutboxBroker(),
                        runtime_status=runtime_status,
                    )
                )
            )
            await client.start_server()
            try:
                health = await (await client.get("/api/v1/health")).json()
                bootstrap = await (await client.get("/api/v1/bootstrap")).json()
                self.assertEqual(health["status"], "DEGRADED")
                self.assertEqual(
                    health["runtime_readiness"]["blocking_reason"],
                    "ACTIVE_MARKET_IDENTITY_MISSING",
                )
                self.assertIsNone(bootstrap["current_market_identity"])
                self.assertEqual(bootstrap["health"]["status"], "DEGRADED")
            finally:
                await client.close()
                read_store.close()
                store.close()

    async def test_bootstrap_uses_runtime_active_market_not_newer_inventory_import(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            for market_id, market_date, updated_at_ms in (
                ("active", "2026-08-23", 100),
                ("historical", "2026-07-08", 200),
            ):
                payload_json = json.dumps(
                    {"event_id": market_id, "market_date": market_date},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                store.persist_market_identity(
                    market_id=market_id,
                    payload_json=payload_json,
                    payload_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
                    updated_at_ms=updated_at_ms,
                )
            read_store = store.open_read_store()
            runtime_status = lambda: {
                "state": "LIVE_READY",
                "live_ready": True,
                "source_health": (("binance", "LIVE"), ("polymarket", "LIVE")),
                "market_id": "active",
                "market_count": 11,
                "asset_count": 22,
                "last_event_id": 0,
                "failure": None,
            }
            client = TestClient(
                TestServer(
                    create_api_app(
                        read_store,
                        OutboxBroker(),
                        runtime_status=runtime_status,
                    )
                )
            )
            await client.start_server()
            try:
                response = await client.get("/api/v1/bootstrap")
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    (await response.json())["current_market_identity"]["market_id"],
                    "active",
                )
            finally:
                await client.close()
                read_store.close()
                store.close()

    async def test_latest_strict_a_signal_ignores_newer_generic_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            for created_at_ms, signal_type in (
                (1, "STRICT_A_SIGNAL_V1"),
                (2, "STRATEGY_EVALUATION_SIGNAL"),
            ):
                payload_json = json.dumps(
                    {
                        "created_at_ms": created_at_ms,
                        "signal_type": signal_type,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                store.commit_signal_and_outbox(
                    SignalRecord(
                        identity_key=f"signal-{created_at_ms}",
                        evaluation_key=f"evaluation-{created_at_ms}",
                        strategy_id="YES_STRICT_A_OPERATIONAL",
                        signal_type=signal_type,
                        payload_json=payload_json,
                        created_at_ms=created_at_ms,
                    ),
                    topic="signal.strict_a",
                )
            read_store = store.open_read_store()
            try:
                selected = read_store.latest_strict_a_signal()
                self.assertIsNotNone(selected)
                self.assertEqual(selected["signal_type"], "STRICT_A_SIGNAL_V1")
                self.assertEqual(selected["created_at_ms"], 1)
            finally:
                read_store.close()
                store.close()

    async def test_clean_runtime_initializes_default_paper_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            runtime = BackendRuntime()
            runtime.initialize(
                load_versioned_runtime_config(Path("config/mvp_runtime_v1.json")),
                store,
            )
            read_store = store.open_read_store()
            try:
                account = read_store.paper_account()
                self.assertEqual(account["account_key"], "default")
                self.assertEqual(
                    account["starting_bankroll_usd_micros"], 1_000_000_000
                )
                self.assertEqual(account["cash_usd_micros"], 1_000_000_000)
            finally:
                read_store.close()
                await runtime.close_read_connections()
                store.close()

    async def test_runtime_invokes_configured_performance_bootstrap_before_api(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            calls = []
            runtime = BackendRuntime(
                performance_bootstrap=lambda selected: calls.append(selected)
            )
            runtime.initialize(
                load_versioned_runtime_config(Path("config/mvp_runtime_v1.json")),
                store,
            )
            try:
                self.assertEqual(calls, [store])
            finally:
                await runtime.close_read_connections()
                store.close()

    async def test_real_paper_state_is_exposed_to_attached_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            ledger = store.paper_ledger()
            ledger.initialize_account(updated_at_ms=1)
            signal = PaperLedgerTests.signal()
            evidence = PaperLedgerTests.evidence()
            store.commit_signal_and_outbox(
                SignalRecord(
                    identity_key=signal.signal_key,
                    evaluation_key=signal.evaluation_key,
                    strategy_id=signal.strategy_id,
                    signal_type="STRICT_A_SIGNAL_V1",
                    payload_json=json.dumps(
                        asdict(signal), sort_keys=True, separators=(",", ":")
                    ),
                    created_at_ms=signal.evaluated_at_ms,
                    origin=signal.origin,
                    execution_eligible=True,
                    infrastructure_only=False,
                ),
                topic="signal.strict_a",
            )
            ledger.execute(signal, evidence, checked_at_ms=101)
            read_store = store.open_read_store()
            client = TestClient(TestServer(create_api_app(read_store, OutboxBroker())))
            await client.start_server()
            try:
                response = await client.get("/api/v1/bootstrap")
                self.assertEqual(response.status, 200)
                payload = await response.json()
                self.assertEqual(payload["interface_version"], "BTC_DAILY_RANGE_MVP_V1")
                self.assertEqual(payload["strict_a_signal"]["signal_key"], signal.signal_key)
                self.assertTrue(payload["execution_readiness"]["ready"])
                self.assertEqual(payload["paper_account"]["account_key"], "default")
                self.assertEqual(len(payload["paper_positions"]), 1)
                self.assertEqual(len(payload["paper_fills"]), 1)
                health = await client.get("/api/v1/health")
                self.assertEqual(health.status, 200)
                self.assertIn("performance", await health.json())
                performance_status = await client.get("/api/v1/performance/status")
                self.assertEqual(performance_status.status, 200)
                self.assertEqual(
                    (await performance_status.json())["schema_version"],
                    "PERFORMANCE_QUERY_V1",
                )
                dashboard = await client.get("/dashboard")
                self.assertEqual(dashboard.status, 200)
                self.assertIn("LOCAL PAPER", await dashboard.text())
                websocket = await client.ws_connect(
                    "/ws/v1/events?after_event_id=0"
                )
                topics = []
                for _ in range(payload["last_event_id"]):
                    message = await websocket.receive_json(timeout=2)
                    topics.append(message["topic"])
                await websocket.close()
                self.assertIn("paper.fill", topics)
            finally:
                await client.close()
                read_store.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
