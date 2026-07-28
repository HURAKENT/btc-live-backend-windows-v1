from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.canary import (
    CANARY_ID,
    commit_canary_if_new,
    evaluate_canary,
)
from src.models import (
    CanonicalSnapshot,
    canonical_payload_json,
    payload_sha256,
)
from src.outbox import OutboxBroker
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
        changed = evaluate_canary(
            self._snapshot(backend_session_id="session-002")
        )
        self.assertEqual(first.evaluation_key, second.evaluation_key)
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
    ):
        payload = {
            "backend_session_id": backend_session_id,
            "binance_ready": binance_ready,
            "canonical": canonical,
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


if __name__ == "__main__":
    unittest.main()
