from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from src.api import create_api_app
from src.canary import (
    CANARY_ID,
    commit_canary_if_new,
    evaluate_canary,
)
from src.models import (
    CanonicalSnapshot,
    SignalRecord,
    SourceEvent,
    canonical_payload_json,
    payload_sha256,
)
from src.outbox import (
    OutboxBroker,
    commit_signal_and_outbox,
)
from src.storage import SqliteStore


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name, "canary.sqlite3")
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.temp_directory.cleanup()

    def test_canary_requires_both_sources_ready(self):
        evaluation = evaluate_canary(self._snapshot())
        self.assertEqual(evaluation.status, "SIGNAL")
        self.assertEqual(evaluation.reason_code, "CANARY_READY")
        self.assertFalse(evaluation.execution_eligible)

    def test_binance_not_ready_is_blocked(self):
        evaluation = evaluate_canary(self._snapshot(binance_ready=False))
        self.assertEqual(evaluation.status, "BLOCKED")
        self.assertEqual(evaluation.reason_code, "BINANCE_NOT_LIVE_READY")

    def test_polymarket_not_reconciled_is_blocked(self):
        evaluation = evaluate_canary(self._snapshot(polymarket_ready=False))
        self.assertEqual(evaluation.status, "BLOCKED")
        self.assertEqual(
            evaluation.reason_code,
            "POLYMARKET_NOT_RECONCILED",
        )

    def test_canonical_snapshot_is_required(self):
        evaluation = evaluate_canary(self._snapshot(canonical=False))
        self.assertEqual(evaluation.status, "BLOCKED")
        self.assertEqual(evaluation.reason_code, "SNAPSHOT_NOT_CANONICAL")

    def test_one_canary_per_session_and_market_identity(self):
        first = commit_canary_if_new(self.store, self._snapshot())
        second = commit_canary_if_new(self.store, self._snapshot())
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)

    def test_duplicate_closed_kline_does_not_duplicate_canary(self):
        snapshot = self._snapshot(
            triggering_event_natural_key="binance:BTCUSDT:1m:1720000000000"
        )
        first = commit_canary_if_new(self.store, snapshot)
        second = commit_canary_if_new(self.store, snapshot)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.store.count("signals"), 1)

    def test_recovered_canary_is_never_current_or_execution_eligible(self):
        signal = commit_canary_if_new(
            self.store,
            self._snapshot(recovery_origin="RECOVERED_AFTER_DOWNTIME"),
        )
        self.assertEqual(signal.origin, "RECOVERED_AFTER_DOWNTIME")
        self.assertFalse(signal.execution_eligible)
        payload = json.loads(signal.payload_json)
        self.assertFalse(payload["historical_signal_is_current_live_signal"])

    def test_recovered_and_current_reevaluation_rows_are_distinct(self):
        recovered = commit_canary_if_new(
            self.store,
            self._snapshot(recovery_origin="RECOVERED_AFTER_DOWNTIME"),
        )
        current = commit_canary_if_new(
            self.store,
            self._snapshot(
                recovery_origin="LIVE",
                snapshot_key="snapshot:current",
            ),
        )
        self.assertNotEqual(recovered.identity_key, current.identity_key)
        self.assertEqual(self.store.count("signals"), 2)
        self.assertEqual(self.store.count("outbox_events"), 2)

    def test_signal_and_outbox_use_existing_atomic_path(self):
        broker = OutboxBroker()
        signal = commit_canary_if_new(
            self.store,
            self._snapshot(),
            broker=broker,
        )
        self.assertEqual(signal.strategy_id, CANARY_ID)
        self.assertEqual(signal.signal_type, "INFRASTRUCTURE_CANARY")
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)
        self.assertEqual(len(broker.committed_event_ids), 1)

    def test_failed_commit_produces_no_publish(self):
        self.store._connection.execute(
            """
            CREATE TRIGGER fail_canary_outbox
            BEFORE INSERT ON outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'forced canary outbox failure');
            END
            """
        )
        broker = OutboxBroker()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "forced canary outbox failure",
        ):
            commit_canary_if_new(
                self.store,
                self._snapshot(),
                broker=broker,
            )
        self.assertEqual(self.store.count("signals"), 0)
        self.assertEqual(self.store.count("outbox_events"), 0)
        self.assertEqual(broker.committed_event_ids, ())

    def test_evaluation_identity_is_deterministic(self):
        first = evaluate_canary(self._snapshot())
        second = evaluate_canary(self._snapshot())
        restarted = evaluate_canary(
            self._snapshot(backend_session_id="session-002")
        )
        changed = evaluate_canary(
            self._snapshot(canonical_state_hash="b" * 64)
        )
        self.assertEqual(first.evaluation_key, second.evaluation_key)
        self.assertEqual(first.evaluation_key, restarted.evaluation_key)
        self.assertNotEqual(first.evaluation_key, changed.evaluation_key)

    @staticmethod
    def _snapshot(
        *,
        binance_ready=True,
        polymarket_ready=True,
        canonical=True,
        backend_session_id="session-001",
        market_identity="btc-daily-range-2026-08-01",
        triggering_event_natural_key="binance:BTCUSDT:1m:1720000000000",
        recovery_origin="LIVE",
        snapshot_key="snapshot:001",
        canonical_state_hash="a" * 64,
    ):
        payload = {
            "backend_session_id": backend_session_id,
            "binance_ready": binance_ready,
            "canonical": canonical,
            "canonical_state_hash": canonical_state_hash,
            "evaluation_origin": (
                "RECOVERED_AFTER_DOWNTIME"
                if recovery_origin == "RECOVERED_AFTER_DOWNTIME"
                else "CURRENT_LIVE_REEVALUATION"
            ),
            "market_identity": market_identity,
            "polymarket_ready": polymarket_ready,
            "trigger_committed_after_live_ready": True,
            "triggering_event_natural_key": triggering_event_natural_key,
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        payload_json = canonical_payload_json(payload_bytes)
        return CanonicalSnapshot(
            snapshot_key=snapshot_key,
            source_event_ids=(1, 2),
            payload_json=payload_json,
            payload_sha256=payload_sha256(payload_json),
            created_at_ms=1720000060000,
            recovery_origin=recovery_origin,
        )


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_directory.name, "api.sqlite3")
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        self.read_store = self.store.open_read_store()
        self.broker = OutboxBroker()
        self.client = await self._start_client(self.broker)

    async def asyncTearDown(self):
        await self.client.close()
        self.read_store.close()
        self.store.close()
        self.temp_directory.cleanup()

    async def _start_client(self, broker):
        app = create_api_app(self.read_store, broker)
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    async def test_bootstrap_contains_health_sources_and_last_event_id(self):
        self.store.append_source_event(
            SourceEvent.binance_closed_kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=1720000000000,
                payload=b"{}",
            )
        )
        await self._commit_synthetic(1)
        response = await self.client.get("/api/v1/bootstrap")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertIn("health", body)
        self.assertIn("sources", body)
        self.assertIn("last_event_id", body)
        self.assertEqual(body["last_event_id"], 1)
        self.assertEqual(body["sources"][0]["source"], "binance")

    async def test_health_sources_signals_and_incidents_routes(self):
        self.store.append_source_event(
            SourceEvent.binance_closed_kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=1720000000000,
                payload=b"{}",
            )
        )
        await self._commit_synthetic(1)
        self.store._connection.execute(
            """
            INSERT INTO incidents(
                incident_key, severity, status, payload_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "incident:1",
                "WARNING",
                "OPEN",
                '{"code":"TEST_INCIDENT"}',
                1720000001000,
            ),
        )
        expected = {
            "/api/v1/health": "status",
            "/api/v1/sources": "sources",
            "/api/v1/signals": "signals",
            "/api/v1/incidents": "incidents",
        }
        for path, key in expected.items():
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn(key, await response.json())

    async def test_committed_canary_is_pushed_without_http_trigger(self):
        websocket = await self.client.ws_connect(
            "/ws/v1/events?after_event_id=0"
        )
        signal = commit_canary_if_new(
            self.store,
            CanaryTests._snapshot(),
            broker=self.broker,
        )
        self.assertIsNotNone(signal)
        message = await websocket.receive_json(timeout=2)
        self.assertEqual(message["event_type"], "SIGNAL_CREATED")
        self.assertEqual(message["event_id"], 1)
        await websocket.close()

    async def test_reconnect_replays_strictly_after_event_id(self):
        first = await self._commit_synthetic(1)
        second = await self._commit_synthetic(2)
        websocket = await self.client.ws_connect(
            f"/ws/v1/events?after_event_id={first}"
        )
        message = await websocket.receive_json(timeout=2)
        self.assertEqual(message["event_id"], second)
        await websocket.close()

    async def test_backlog_and_race_window_are_delivered_without_duplicate(self):
        first = await self._commit_synthetic(1)

        class RaceBroker(OutboxBroker):
            def __init__(inner_self):
                super().__init__()
                inner_self.hook = None

            def subscribe(inner_self, *, max_queue=100):
                if inner_self.hook is not None:
                    hook = inner_self.hook
                    inner_self.hook = None
                    hook()
                return super().subscribe(max_queue=max_queue)

        race_broker = RaceBroker()

        def commit_race_event():
            self._commit_synthetic_sync(2, race_broker)

        race_broker.hook = commit_race_event
        await self.client.close()
        self.client = await self._start_client(race_broker)
        websocket = await self.client.ws_connect(
            "/ws/v1/events?after_event_id=0"
        )
        messages = [
            await websocket.receive_json(timeout=2),
            await websocket.receive_json(timeout=2),
        ]
        self.assertEqual(
            [message["event_id"] for message in messages],
            [first, first + 1],
        )
        with self.assertRaises(asyncio.TimeoutError):
            await websocket.receive(timeout=0.1)
        await websocket.close()

    async def test_malformed_after_event_id_is_rejected(self):
        for value in ("bad", "-1", "1.0"):
            with self.subTest(value=value):
                with self.assertRaises(WSServerHandshakeError) as caught:
                    await self.client.ws_connect(
                        f"/ws/v1/events?after_event_id={value}"
                    )
                self.assertEqual(caught.exception.status, 400)

    async def test_uncommitted_broker_notification_is_not_delivered(self):
        websocket = await self.client.ws_connect(
            "/ws/v1/events?after_event_id=0"
        )
        self.broker.publish_committed(999)
        with self.assertRaises(asyncio.TimeoutError):
            await websocket.receive(timeout=0.15)
        await websocket.close()

    async def test_disconnected_client_does_not_stop_broker_or_replay(self):
        websocket = await self.client.ws_connect(
            "/ws/v1/events?after_event_id=0"
        )
        await websocket.close()
        outbox_id = await self._commit_synthetic(1)
        self.assertEqual(self.broker.committed_event_ids, (outbox_id,))
        reconnected = await self.client.ws_connect(
            "/ws/v1/events?after_event_id=0"
        )
        message = await reconnected.receive_json(timeout=2)
        self.assertEqual(message["event_id"], outbox_id)
        await reconnected.close()

    async def test_rest_requests_never_trigger_evaluation_or_write(self):
        before = (
            self.store.count("strategy_evaluations"),
            self.store.count("signals"),
            self.store.count("outbox_events"),
        )
        for path in (
            "/api/v1/bootstrap",
            "/api/v1/health",
            "/api/v1/sources",
            "/api/v1/signals",
            "/api/v1/incidents",
        ):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
        after = (
            self.store.count("strategy_evaluations"),
            self.store.count("signals"),
            self.store.count("outbox_events"),
        )
        self.assertEqual(before, after)

    async def test_app_contract_is_loopback_only_without_cors_wildcard(self):
        app = self.client.server.app
        self.assertEqual(app["bind_host"], "127.0.0.1")
        self.assertEqual(app["bind_port"], 8767)
        response = await self.client.get("/api/v1/health")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def _commit_synthetic(self, index):
        return self._commit_synthetic_sync(index, self.broker)

    def _commit_synthetic_sync(self, index, broker):
        signal = SignalRecord(
            identity_key=f"api:signal:{index}",
            evaluation_key=f"api:evaluation:{index}",
            strategy_id="API_SYNTHETIC_TEST",
            signal_type="INFRASTRUCTURE_EVENT",
            payload_json=json.dumps(
                {
                    "event_type": "SIGNAL_CREATED",
                    "sequence": index,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            created_at_ms=1720000000000 + index,
            execution_eligible=False,
            infrastructure_only=True,
        )
        _, outbox_id = commit_signal_and_outbox(
            self.store,
            signal,
            topic="signal.created",
            broker=broker,
        )
        return outbox_id


if __name__ == "__main__":
    unittest.main()
