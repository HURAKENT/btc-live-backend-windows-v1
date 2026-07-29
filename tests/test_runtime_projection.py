from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.canary import commit_canary_if_new, evaluate_canary
from src.models import SourceEvent, payload_sha256
from src.outbox import OutboxBroker
from src.runtime_adapters import MarketReconciliation
from src.storage import SqliteStore

try:
    from src.runtime_projection import (
        CanonicalProjector,
        CommittedSourceEvent,
    )
except ModuleNotFoundError:
    CanonicalProjector = None
    CommittedSourceEvent = None


def source_event(
    *,
    source: str,
    key: str,
    event_type: str,
    timestamp: int,
    asset_id: str | None = None,
    origin: str = "LIVE",
) -> SourceEvent:
    payload_value = {"event_type": event_type}
    if event_type == "BINANCE_KLINE_CLOSED":
        payload_value.update(
            {
                "close": "101.000000",
                "high": "102.000000",
                "interval": "1m",
                "low": "99.000000",
                "open": "100.000000",
                "open_time_ms": timestamp,
                "symbol": "BTCUSDT",
                "volume": "2.500000",
            }
        )
    elif event_type == "POLYMARKET_BOOK":
        if asset_id is None:
            raise ValueError("asset_id required for book fixture")
        payload_value.update(
            {
                "asset_id": asset_id,
                "asks": [
                    {
                        "price_micros": 600_000,
                        "size_micros": 2_000_000,
                    }
                ],
                "best_ask_micros": 600_000,
                "best_bid_micros": 400_000,
                "bids": [
                    {
                        "price_micros": 400_000,
                        "size_micros": 1_000_000,
                    }
                ],
                "book_hash": f"hash-{asset_id}-{timestamp}",
                "spread_micros": 200_000,
            }
        )
    elif asset_id is not None:
        payload_value["asset_id"] = asset_id
    payload = json.dumps(
        payload_value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return SourceEvent(
        source=source,
        natural_key=key,
        source_timestamp_ms=timestamp,
        received_timestamp_ms=timestamp,
        event_type=event_type,
        payload_json=payload,
        payload_sha256=payload_sha256(payload),
        recovery_origin=origin,
    )


class RuntimeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.assets = tuple(f"asset-{index}" for index in range(22))
        self.projector = CanonicalProjector(
            backend_session_id="session-1"
        )
        self.projector.set_market_identity(self._reconciliation())

    def test_binance_only_does_not_create_snapshot(self):
        self.projector.set_live_ready()
        self.assertIsNone(self.projector.apply(self._binance(1)))

    def test_polymarket_only_does_not_create_snapshot(self):
        self.projector.set_live_ready()
        for committed in self._books():
            self.assertIsNone(self.projector.apply(committed))

    def test_full_reconciled_state_creates_one_snapshot(self):
        self._apply_books()
        self.projector.set_live_ready()
        snapshot = self.projector.apply(self._binance(100))
        self.assertIsNotNone(snapshot)
        self.assertTrue(json.loads(snapshot.payload_json)["canonical"])

    def test_source_ids_preserve_causal_order(self):
        self._apply_books(start_id=10)
        self.projector.set_live_ready()
        snapshot = self.projector.apply(self._binance(100))
        self.assertEqual(
            snapshot.source_event_ids,
            tuple(range(10, 32)) + (100,),
        )

    def test_exact_committed_replay_creates_no_snapshot(self):
        self._apply_books()
        self.projector.set_live_ready()
        committed = self._binance(100)
        self.assertIsNotNone(self.projector.apply(committed))
        self.assertIsNone(self.projector.apply(committed))

    def test_duplicate_inserted_false_is_ignored(self):
        self._apply_books()
        self.projector.set_live_ready()
        duplicate = dataclasses.replace(self._binance(100), inserted=False)
        self.assertIsNone(self.projector.apply(duplicate))

    def test_stale_binance_event_does_not_roll_back_state(self):
        self._apply_books()
        self.projector.set_live_ready()
        current = self._binance(100, timestamp=120_000)
        stale = self._binance(101, timestamp=60_000)
        first = self.projector.apply(current)
        self.assertIsNone(self.projector.apply(stale))
        self.assertEqual(self.projector.latest_snapshot, first)

    def test_open_binance_event_is_rejected(self):
        opened = dataclasses.replace(
            self._binance(100).event,
            event_type="BINANCE_KLINE_OPEN",
        )
        with self.assertRaisesRegex(ValueError, "RUNTIME_BINANCE_NOT_CLOSED"):
            self.projector.apply(
                CommittedSourceEvent(100, opened, True)
            )

    def test_incomplete_polymarket_set_does_not_create_snapshot(self):
        for committed in self._books()[:-1]:
            self.projector.apply(committed)
        self.projector.set_live_ready()
        self.assertIsNone(self.projector.apply(self._binance(100)))

    def test_recovery_origin_is_derived_from_events(self):
        self._apply_books(origin="REST_BACKFILL")
        self.projector.set_live_ready()
        snapshot = self.projector.apply(
            self._binance(100, origin="RECOVERED_AFTER_DOWNTIME")
        )
        self.assertEqual(
            snapshot.recovery_origin,
            "RECOVERED_AFTER_DOWNTIME",
        )

    def test_snapshot_payload_contains_no_binary_float(self):
        self._apply_books()
        self.projector.set_live_ready()
        snapshot = self.projector.apply(self._binance(100))
        self.assertNotIn(".", snapshot.payload_json)
        self.assertNotIn("e-", snapshot.payload_json.lower())

    def test_snapshot_hash_is_deterministic(self):
        first = self._build_snapshot()
        second = self._build_snapshot()
        self.assertEqual(first.snapshot_key, second.snapshot_key)
        self.assertEqual(first.payload_sha256, second.payload_sha256)

    def test_unknown_additive_event_does_not_change_state(self):
        self._apply_books()
        unknown = source_event(
            source="polymarket",
            key="unknown:1",
            event_type="UNHANDLED_PROVIDER_EVENT",
            timestamp=2,
            asset_id=self.assets[0],
        )
        self.assertIsNone(
            self.projector.apply(CommittedSourceEvent(99, unknown, True))
        )
        self.projector.set_live_ready()
        snapshot = self.projector.apply(self._binance(100))
        self.assertNotIn(99, snapshot.source_event_ids)

    def test_projector_has_no_storage_dependency(self):
        self.assertFalse(hasattr(self.projector, "_store"))

    def _build_snapshot(self):
        projector = CanonicalProjector(backend_session_id="session-1")
        projector.set_market_identity(self._reconciliation())
        for committed in self._books():
            projector.apply(committed)
        projector.set_live_ready()
        return projector.apply(self._binance(100))

    def _reconciliation(self):
        identity = json.dumps(
            {"asset_ids": list(self.assets), "event_id": "event-1"},
            sort_keys=True,
            separators=(",", ":"),
        )
        return MarketReconciliation(
            market_identity_json=identity,
            market_id="event-1",
            market_ids=tuple(f"market-{index}" for index in range(11)),
            asset_ids=self.assets,
            current_events=(),
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )

    def _books(self, *, start_id=1, origin="LIVE"):
        return tuple(
            CommittedSourceEvent(
                start_id + index,
                source_event(
                    source="polymarket",
                    key=f"book:{asset_id}",
                    event_type="POLYMARKET_BOOK",
                    timestamp=index + 1,
                    asset_id=asset_id,
                    origin=origin,
                ),
                True,
            )
            for index, asset_id in enumerate(self.assets)
        )

    def _apply_books(self, *, start_id=1, origin="LIVE"):
        for committed in self._books(start_id=start_id, origin=origin):
            self.projector.apply(committed)

    def _binance(self, event_id, *, timestamp=60_000, origin="LIVE"):
        return CommittedSourceEvent(
            event_id,
            source_event(
                source="binance",
                key=f"binance:BTCUSDT:1m:{timestamp}",
                event_type="BINANCE_KLINE_CLOSED",
                timestamp=timestamp,
                origin=origin,
            ),
            True,
        )


class AtomicCanaryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "atomic.sqlite3"))
        self.store.migrate()
        helper = RuntimeProjectionTests()
        helper.assets = tuple(f"asset-{index}" for index in range(22))
        self.snapshot = helper._build_snapshot()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_canary_persists_evaluation_signal_and_outbox(self):
        signal = commit_canary_if_new(self.store, self.snapshot)
        self.assertIsNotNone(signal)
        self.assertEqual(self.store.count("strategy_evaluations"), 1)
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)

    def test_atomic_canary_exact_replay_is_idempotent(self):
        commit_canary_if_new(self.store, self.snapshot)
        self.assertIsNone(commit_canary_if_new(self.store, self.snapshot))
        self.assertEqual(self.store.count("strategy_evaluations"), 1)
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)

    def test_outbox_failure_rolls_back_all_three_rows(self):
        self.store._connection.execute(
            """
            CREATE TRIGGER fail_atomic_outbox
            BEFORE INSERT ON outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'forced atomic failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced atomic"):
            commit_canary_if_new(self.store, self.snapshot)
        self.assertEqual(self.store.count("strategy_evaluations"), 0)
        self.assertEqual(self.store.count("signals"), 0)
        self.assertEqual(self.store.count("outbox_events"), 0)

    def test_atomic_path_publishes_only_after_commit(self):
        broker = OutboxBroker()
        commit_canary_if_new(self.store, self.snapshot, broker=broker)
        self.assertEqual(len(broker.committed_event_ids), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)

    def test_canary_remains_infrastructure_only_and_ineligible(self):
        signal = commit_canary_if_new(self.store, self.snapshot)
        evaluation = evaluate_canary(self.snapshot)
        payload = json.loads(signal.payload_json)
        self.assertTrue(signal.infrastructure_only)
        self.assertFalse(signal.execution_eligible)
        self.assertFalse(evaluation.execution_eligible)
        self.assertFalse(payload["paper_or_live_eligible"])

    def test_recovered_and_current_payload_fields_are_preserved(self):
        helper = RuntimeProjectionTests()
        helper.assets = tuple(f"asset-{index}" for index in range(22))
        projector = CanonicalProjector(backend_session_id="session-1")
        projector.set_market_identity(helper._reconciliation())
        for committed in helper._books(origin="REST_BACKFILL"):
            projector.apply(committed)
        projector.apply(
            helper._binance(
                100,
                origin="RECOVERED_AFTER_DOWNTIME",
            )
        )
        recovered = projector.build_snapshot(
            evaluation_origin="RECOVERED_AFTER_DOWNTIME",
            trigger_committed_after_live_ready=False,
        )
        commit_canary_if_new(self.store, recovered)
        row = self.store.rows(
            "SELECT payload_json FROM strategy_evaluations"
        )[0]
        payload = json.loads(row[0])
        self.assertEqual(payload["origin"], "RECOVERED_AFTER_DOWNTIME")
        self.assertTrue(payload["current_reevaluation_required"])
        self.assertFalse(
            payload["historical_signal_is_current_live_signal"]
        )


if __name__ == "__main__":
    unittest.main()
