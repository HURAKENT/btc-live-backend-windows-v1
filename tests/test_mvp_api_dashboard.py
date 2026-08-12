from __future__ import annotations

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
