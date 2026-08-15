from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.lifecycle import STARTUP_SEQUENCE
from src.models import SourceEvent, payload_sha256
from src.outbox import OutboxBroker
from src.runtime_adapters import MarketReconciliation
from src.storage import SqliteStore


class LoopbackCheckpointInputSource:
    async def capture(self, *, due, current):
        from src.runtime_orchestrator import CheckpointInputResult
        from src.strategy_dispatch import V1ExecutableCheckpointInput
        from src.strategy_v1 import (
            BucketInput,
            Pf1SnapshotEvidence,
            StrictPriceHistoryEvidence,
        )

        buckets = tuple(
            BucketInput(
                bucket_index=index,
                model_p=0.50 if index == 0 else 0.05,
                market_q_yes=0.30 if index == 0 else 0.10 + index / 100,
                market_q_no=None,
                vwap5=0.35 if index == 0 else 0.15,
                confirmed_fee=0.0,
                no_token_id=f"no-token-{index}",
            )
            for index in range(11)
        )
        evidence_sha = "1" * 64
        strict = None
        pf1 = None
        if "STRICT" in due.schedule.strategy_id:
            strict = StrictPriceHistoryEvidence(
                provenance="CLOB_PRICE_HISTORY",
                source_sha256=evidence_sha,
                checkpoint_timestamp_ms=due.schedule.due_at_ms,
                observation_timestamp_ms=due.schedule.due_at_ms - 1,
            )
        else:
            pf1 = Pf1SnapshotEvidence(
                bucket_count=11,
                snapshot_complete=True,
                synchronized=True,
                fresh=True,
                crossed_book_count=0,
                fee_provenance="PUBLIC_CLOB_FEE_SCHEDULE",
                snapshot_sha256=evidence_sha,
                fee_schedule_sha256=evidence_sha,
            )
        return CheckpointInputResult(
            executable_input=V1ExecutableCheckpointInput(
                checkpoint_minutes=due.schedule.checkpoint_minutes,
                buckets=buckets,
                prior_position=False,
                strict_price_history_evidence=strict,
                pf1_snapshot_evidence=pf1,
            ),
            historical_depth_available=True,
            reason_code=None,
        )

try:
    from src.runtime_orchestrator import C1RuntimeOrchestrator
except ModuleNotFoundError:
    C1RuntimeOrchestrator = None


def runtime_event(
    source: str,
    key: str,
    event_type: str,
    timestamp: int,
    *,
    asset_id: str | None = None,
) -> SourceEvent:
    value = {"event_type": event_type}
    if event_type == "BINANCE_KLINE_CLOSED":
        value.update(
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
        value.update(
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
        value["asset_id"] = asset_id
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return SourceEvent(
        source=source,
        natural_key=key,
        source_timestamp_ms=timestamp,
        received_timestamp_ms=timestamp,
        event_type=event_type,
        payload_json=payload,
        payload_sha256=payload_sha256(payload),
        recovery_origin="REST_BACKFILL" if timestamp < 120_000 else "LIVE",
    )


class FakeBinanceRuntimeAdapter:
    def __init__(self, *, fail_recover=False, fail_stream=False):
        self.release = asyncio.Event()
        self.closed = 0
        self.fail_recover = fail_recover
        self.fail_stream = fail_stream
        self.live_event = runtime_event(
            "binance",
            "binance:BTCUSDT:1m:120000",
            "BINANCE_KLINE_CLOSED",
            120_000,
        )

    async def recover(self, *, start_ms, end_ms):
        if self.fail_recover:
            raise RuntimeError("binance recovery failed")
        return [
            runtime_event(
                "binance",
                "binance:BTCUSDT:1m:60000",
                "BINANCE_KLINE_CLOSED",
                60_000,
            )
        ]

    async def stream(self, queue):
        if self.fail_stream:
            raise RuntimeError("binance stream failed")
        await queue.put(self.live_event)
        await asyncio.Event().wait()

    async def close(self):
        self.closed += 1


class FakePolymarketRuntimeAdapter:
    def __init__(self, *, incomplete=False, fail_stream=False):
        self.assets = tuple(f"asset-{index}" for index in range(22))
        self.closed = 0
        self.fail_stream = fail_stream
        count = 21 if incomplete else 22
        books = tuple(
            runtime_event(
                "polymarket",
                f"book:{asset_id}",
                "POLYMARKET_BOOK",
                index + 1,
                asset_id=asset_id,
            )
            for index, asset_id in enumerate(self.assets[:count])
        )
        identity = json.dumps(
            {
                "asset_ids": list(self.assets),
                "event_id": "event-1",
                "market_date": "2033-05-18",
                "outcomes": [f"bucket-{index}" for index in range(11)],
                "resolution_utc": "2033-05-18T03:33:20+00:00",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.reconciliation = MarketReconciliation(
            market_identity_json=identity,
            market_id="event-1",
            market_ids=tuple(f"market-{index}" for index in range(11)),
            asset_ids=self.assets,
            current_events=books,
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )
        self.stream_assets = None
        self.live_event = runtime_event(
            "polymarket",
            "book:asset-0:live",
            "POLYMARKET_BOOK",
            120_001,
            asset_id=self.assets[0],
        )

    async def discover_and_reconcile(self):
        return self.reconciliation

    async def recover(self, reconciliation, *, start_ts, end_ts):
        return []

    async def stream(self, *, asset_ids, queue):
        self.stream_assets = asset_ids
        if self.fail_stream:
            raise RuntimeError("polymarket stream failed")
        await queue.put(self.live_event)
        await asyncio.Event().wait()

    async def close(self):
        self.closed += 1


class RuntimeOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "runtime.sqlite3"))
        self.store.migrate()
        self.binance = FakeBinanceRuntimeAdapter()
        self.polymarket = FakePolymarketRuntimeAdapter()
        self.broker = OutboxBroker()
        self.runtime = self._runtime()

    async def asyncTearDown(self):
        if self.runtime is not None:
            await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    def _runtime(self, **overrides):
        values = {
            "run_id": "run-1",
            "store": self.store,
            "broker": self.broker,
            "binance_adapter": self.binance,
            "polymarket_adapter": self.polymarket,
            "binance_start_ms": 60_000,
            "binance_end_ms": 60_000,
            "history_start_ts": 1,
            "history_end_ts": 2,
            "clock_ms": lambda: 1_000,
        }
        values.update(overrides)
        return C1RuntimeOrchestrator(**values)

    async def test_orchestrator_owns_runtime_and_checkpoint_tasks(self):
        await self.runtime.start()
        self.assertEqual(set(self.runtime.owned_task_names), {
            "writer",
            "binance_stream",
            "polymarket_stream",
            "checkpoint_scheduler",
        })

    async def test_startup_transitions_follow_frozen_sequence(self):
        await self.runtime.start()
        self.assertEqual(self.runtime.lifecycle_states, STARTUP_SEQUENCE)

    async def test_each_transition_is_persisted(self):
        await self.runtime.start()
        self.assertEqual(
            self.store.scalar(
                """
                SELECT COUNT(*) FROM incidents
                WHERE incident_key LIKE 'lifecycle:%'
                """
            ),
            len(STARTUP_SEQUENCE),
        )

    async def test_discovery_identity_is_persisted(self):
        await self.runtime.start()
        reader = self.store.open_read_store()
        try:
            self.assertEqual(
                reader.current_market_identity()["market_id"],
                "event-1",
            )
        finally:
            reader.close()

    async def test_provider_events_use_single_writer_consumer(self):
        await self.runtime.start()
        self.assertEqual(self.runtime.writer_consumer_count, 1)
        self.assertGreater(self.store.count("source_events"), 0)

    async def test_scheduler_poll_uses_real_dispatcher_and_single_writer(self):
        self.runtime = self._runtime(
            clock_ms=lambda: 2_000_000_000_000,
            checkpoint_input_source=LoopbackCheckpointInputSource(),
            checkpoint_recovery_cutoff_ms=0,
        )
        await self.runtime.start()
        await asyncio.wait_for(self.runtime.wait_checkpoint_progress(), timeout=5)
        self.assertIn("checkpoint_scheduler", self.runtime.owned_task_names)
        self.assertEqual(self.runtime.writer_consumer_count, 1)
        self.assertGreater(
            self.store.scalar(
                "SELECT COUNT(*) FROM strategy_evaluations "
                "WHERE checkpoint_group_key IS NOT NULL"
            ),
            0,
        )
        self.assertGreater(self.store.count("strategy_performance_observations"), 0)
        self.assertGreater(
            self.store.scalar(
                """
                SELECT COUNT(*) FROM strategy_performance_materialization_revisions
                WHERE source_view = 'FORWARD' AND is_current = 1
                """
            ),
            0,
        )
        self.assertGreater(self.runtime.scheduler_writer_operation_count, 1)

    async def test_missing_production_input_releases_without_strategy_output(self):
        self.runtime = self._runtime(
            clock_ms=lambda: 2_000_000_000_000,
            checkpoint_recovery_cutoff_ms=0,
        )
        await self.runtime.start()
        await self.runtime.poll_strategy_checkpoints_once()
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM strategy_evaluations "
                "WHERE checkpoint_group_key IS NOT NULL"
            ),
            0,
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM strategy_checkpoint_schedules "
                "WHERE state<>'PENDING'"
            ),
            0,
        )
        self.assertGreater(
            self.store.scalar(
                "SELECT COUNT(*) FROM incidents "
                "WHERE incident_key LIKE 'c6-input-source-unavailable:%'"
            ),
            0,
        )

    async def test_recovered_scheduler_persists_current_reevaluation(self):
        self.runtime = self._runtime(
            clock_ms=lambda: 2_000_000_000_000,
            checkpoint_input_source=LoopbackCheckpointInputSource(),
            checkpoint_recovery_cutoff_ms=2_000_000_000_001,
        )
        await self.runtime.start()
        await self.runtime.poll_strategy_checkpoints_once()
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(DISTINCT evaluation_key) "
                "FROM strategy_checkpoint_schedules "
                "WHERE current_reevaluation_required=1 "
                "AND current_reevaluation_evaluation_id IS NOT NULL"
            ),
            3,
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM strategy_evaluations "
                "WHERE checkpoint_group_key IS NULL "
                "AND rule_spec_sha256 IS NOT NULL AND origin='LIVE'"
            ),
            3,
        )

    async def test_duplicate_source_event_is_not_projected_twice(self):
        await self.runtime.start()
        await self.runtime.enqueue(self.binance.live_event)
        await self.runtime.enqueue(self.binance.live_event)
        await self.runtime.drain_source_queue()
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM source_events WHERE natural_key = ?",
                (self.binance.live_event.natural_key,),
            ),
            1,
        )

    async def test_full_state_then_live_binance_creates_snapshot_and_canary(self):
        await self.runtime.start()
        self.binance.release.set()
        await self.runtime.wait_for_source_idle()
        self.assertEqual(self.store.count("canonical_state"), 2)
        self.assertEqual(self.store.count("strategy_evaluations"), 2)
        self.assertEqual(self.store.count("signals"), 2)
        self.assertEqual(self.store.count("outbox_events"), 2)

    async def test_wait_ready_returns_only_after_live_ready(self):
        waiter = asyncio.create_task(self.runtime.wait_ready())
        self.assertFalse(waiter.done())
        await self.runtime.start()
        await waiter
        self.assertTrue(self.runtime.status().live_ready)

    async def test_missing_source_prevents_live_ready(self):
        self.polymarket = FakePolymarketRuntimeAdapter(incomplete=True)
        self.runtime = self._runtime(polymarket_adapter=self.polymarket)
        with self.assertRaisesRegex(Exception, "INCOMPLETE"):
            await self.runtime.start()
        self.assertFalse(self.runtime.status().live_ready)

    async def test_provider_exception_never_reports_pass(self):
        self.binance = FakeBinanceRuntimeAdapter(fail_recover=True)
        self.runtime = self._runtime(binance_adapter=self.binance)
        with self.assertRaisesRegex(RuntimeError, "binance recovery failed"):
            await self.runtime.start()
        self.assertFalse(self.runtime.status().live_ready)

    async def test_writer_exception_moves_runtime_to_blocked(self):
        await self.runtime.start()
        bad = object()
        await self.runtime.enqueue(bad)
        await self.runtime.wait_for_source_idle()
        self.assertEqual(self.runtime.status().state, "RECOVERY_BLOCKED")
        self.assertFalse(self.runtime.status().live_ready)

    async def test_stop_cancels_provider_tasks(self):
        await self.runtime.start()
        await self.runtime.stop()
        self.assertTrue(self.runtime.providers_stopped)

    async def test_stop_drains_accepted_queue(self):
        await self.runtime.start()
        await self.runtime.enqueue(self.binance.live_event)
        await self.runtime.stop()
        self.assertEqual(self.runtime.pending_source_events, 0)

    async def test_stop_leaves_no_pending_owned_tasks(self):
        await self.runtime.start()
        await self.runtime.stop()
        self.assertEqual(self.runtime.pending_owned_tasks, ())

    async def test_stop_is_idempotent(self):
        await self.runtime.start()
        await self.runtime.stop()
        await self.runtime.stop()
        self.assertEqual(self.binance.closed, 1)
        self.assertEqual(self.polymarket.closed, 1)

    async def test_cancelled_provider_is_not_incident(self):
        await self.runtime.start()
        before = self.store.count("incidents")
        await self.runtime.stop()
        self.assertEqual(self.store.count("incidents"), before)

    async def test_orchestrator_does_not_close_external_store(self):
        await self.runtime.start()
        await self.runtime.stop()
        self.assertEqual(self.store.integrity_report()["status"], "PASS")

    async def test_market_assets_are_passed_exactly_to_stream(self):
        await self.runtime.start()
        self.assertEqual(
            self.polymarket.stream_assets,
            self.polymarket.assets,
        )

    async def test_registry_strategies_are_not_executed(self):
        await self.runtime.start()
        self.assertEqual(self.runtime.registry_execution_count, 0)

    def test_runtime_has_no_order_paper_wallet_or_signing_surface(self):
        import inspect
        import src.runtime_orchestrator as module

        source = inspect.getsource(module).lower()
        for token in ("place_order", "paper_fill", "private_key", "sign_order"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
