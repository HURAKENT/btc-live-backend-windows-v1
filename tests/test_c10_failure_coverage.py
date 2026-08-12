from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from src.checkpoint_scheduler import CheckpointScheduler
from src.models import SignalRecord, StrategyEvaluation
from src.outbox import OutboxBroker
from src.runtime_orchestrator import C1RuntimeOrchestrator, CheckpointInputResult
from src.storage import SqliteStore
from tests import test_paper as paper_test_helpers
from tests.test_runtime_orchestrator import (
    FakeBinanceRuntimeAdapter,
    FakePolymarketRuntimeAdapter,
    LoopbackCheckpointInputSource,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _OutageThenHealthyCheckpointSource:
    def __init__(self) -> None:
        self.available = False
        self._healthy = LoopbackCheckpointInputSource()

    async def capture(self, *, due, current):
        if not self.available:
            return CheckpointInputResult(
                executable_input=None,
                historical_depth_available=False,
                reason_code="CURRENT_PROVIDER_UNAVAILABLE",
            )
        return await self._healthy.capture(due=due, current=current)


class C10ProviderOutageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 10

    async def test_integrated_current_provider_outage_returns_without_phantom_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory, "provider-outage.sqlite3")
            store = SqliteStore.open(database_path)
            store.migrate()
            now_ms = [1_000]
            source = _OutageThenHealthyCheckpointSource()
            runtime = C1RuntimeOrchestrator(
                run_id="c10-provider-outage",
                store=store,
                broker=OutboxBroker(),
                binance_adapter=FakeBinanceRuntimeAdapter(),
                polymarket_adapter=FakePolymarketRuntimeAdapter(),
                binance_start_ms=60_000,
                binance_end_ms=60_000,
                history_start_ts=1,
                history_end_ts=2,
                clock_ms=lambda: now_ms[0],
                checkpoint_input_source=source,
                checkpoint_recovery_cutoff_ms=0,
                checkpoint_poll_interval_seconds=3_600,
            )
            try:
                await runtime.start()
                self.assertTrue(runtime.status().live_ready)
                self.assertEqual(
                    dict(runtime.status().source_health),
                    {"binance": "LIVE", "polymarket": "LIVE"},
                )
                baseline_signals = store.count("signals")
                baseline_outbox = store.count("outbox_events")

                now_ms[0] = 2_000_000_000_000
                runtime._source_last_accepted_at_ms.update(
                    {"binance": now_ms[0], "polymarket": now_ms[0]}
                )
                self.assertEqual(await runtime.poll_strategy_checkpoints_once(), 0)
                self.assertEqual(store.count("signals"), baseline_signals)
                self.assertEqual(store.count("outbox_events"), baseline_outbox)
                self.assertEqual(store.count("paper_fills"), 0)
                self.assertEqual(
                    store.scalar(
                        "SELECT COUNT(*) FROM strategy_evaluations "
                        "WHERE checkpoint_group_key IS NOT NULL"
                    ),
                    0,
                )
                self.assertEqual(
                    store.scalar(
                        "SELECT COUNT(*) FROM strategy_checkpoint_schedules "
                        "WHERE state <> 'PENDING'"
                    ),
                    0,
                )
                self.assertEqual(
                    store.scalar(
                        "SELECT COUNT(*) FROM incidents "
                        "WHERE status='CURRENT_PROVIDER_UNAVAILABLE'"
                    ),
                    4,
                )

                source.available = True
                self.assertEqual(await runtime.poll_strategy_checkpoints_once(), 6)
                recovered_counts = (
                    store.count("strategy_evaluations"),
                    store.count("signals"),
                    store.count("outbox_events"),
                )
                self.assertGreater(recovered_counts[0], 0)
                self.assertGreater(recovered_counts[1], baseline_signals)
                self.assertEqual(
                    store.scalar(
                        "SELECT COUNT(*) FROM strategy_checkpoint_schedules "
                        "WHERE state='COMPLETED'"
                    ),
                    4,
                )
                self.assertEqual(store.count("paper_fills"), 0)

                self.assertEqual(await runtime.poll_strategy_checkpoints_once(), 0)
                self.assertEqual(
                    (
                        store.count("strategy_evaluations"),
                        store.count("signals"),
                        store.count("outbox_events"),
                    ),
                    recovered_counts,
                )
                self.assertEqual(store.count("paper_fills"), 0)
            finally:
                await runtime.stop()
                store.close()


class C10TransactionalRestartTests(unittest.TestCase):
    def test_paper_transaction_abort_rolls_back_and_restart_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory, "paper-atomicity.sqlite3")
            store = SqliteStore.open(database_path)
            store.migrate()
            ledger = store.paper_ledger()
            ledger.initialize_account(updated_at_ms=1)
            initial_account = ledger.get_account()
            store.rows(
                """
                CREATE TRIGGER c10_abort_paper_position
                BEFORE INSERT ON paper_positions
                BEGIN
                    SELECT RAISE(ABORT, 'c10 forced paper interruption');
                END
                """
            )

            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "c10 forced paper interruption"
            ):
                ledger.execute(
                    paper_test_helpers.PaperLedgerTests.signal(),
                    paper_test_helpers.PaperLedgerTests.evidence(),
                    checked_at_ms=101,
                )

            self.assertEqual(ledger.get_account(), initial_account)
            for table in (
                "paper_execution_readiness",
                "paper_intents",
                "paper_fills",
                "paper_positions",
            ):
                self.assertEqual(ledger.row_count(table), 0, table)
            self.assertEqual(store.count("outbox_events"), 0)
            store.rows("DROP TRIGGER c10_abort_paper_position")
            store.close()

            restarted = SqliteStore.open(database_path)
            restarted.migrate()
            try:
                restarted_ledger = restarted.paper_ledger()
                committed = restarted_ledger.execute(
                    paper_test_helpers.PaperLedgerTests.signal(),
                    paper_test_helpers.PaperLedgerTests.evidence(),
                    checked_at_ms=101,
                )
                replayed = restarted_ledger.execute(
                    paper_test_helpers.PaperLedgerTests.signal(),
                    paper_test_helpers.PaperLedgerTests.evidence(),
                    checked_at_ms=999,
                )

                self.assertFalse(committed.replayed)
                self.assertTrue(replayed.replayed)
                self.assertEqual(replayed.intent, committed.intent)
                self.assertEqual(replayed.fill, committed.fill)
                self.assertEqual(replayed.position, committed.position)
                self.assertEqual(replayed.account, committed.account)
                self.assertEqual(committed.fill.shares_micros, 5_000_000)
                self.assertEqual(
                    initial_account.cash_usd_micros
                    - committed.account.cash_usd_micros,
                    3_050_000,
                )
                for table in ("paper_intents", "paper_fills", "paper_positions"):
                    self.assertEqual(restarted_ledger.row_count(table), 1, table)
                self.assertEqual(restarted.count("outbox_events"), 5)
            finally:
                restarted.close()

    def test_abrupt_checkpoint_process_exit_restarts_to_exactly_once_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "checkpoint-crash.sqlite3"
            payload_path = root / "capture.json"
            store = SqliteStore.open(database_path)
            store.migrate()
            resolution_ms = 2_000_000_000_000
            market_payload = json.dumps(
                {
                    "event_id": "c10-crash-market",
                    "resolution_utc": datetime.fromtimestamp(
                        resolution_ms / 1_000, tz=UTC
                    ).isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            store.persist_market_identity(
                market_id="c10-crash-market",
                payload_json=market_payload,
                payload_sha256=hashlib.sha256(market_payload.encode()).hexdigest(),
                updated_at_ms=1,
            )
            scheduler = CheckpointScheduler(project_root=PROJECT_ROOT, store=store)
            schedules = scheduler.register_market(
                market_id="c10-crash-market", created_at_ms=1
            )
            target = next(
                item for item in schedules if item.strategy_id == "YES_STRICT_A_T60"
            )
            self.assertEqual(target.checkpoint_minutes, 60)
            self.assertEqual(target.state, "PENDING")
            payload = json.dumps(
                {
                    "checkpoint_minutes": 60,
                    "market_id": "c10-crash-market",
                    "schema_version": "C6_MISSING_HISTORICAL_DEPTH_V1",
                    "strategy_id": "YES_STRICT_A_T60",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            payload_path.write_text(payload, encoding="utf-8")
            payload_hash = hashlib.sha256(payload.encode()).hexdigest()
            store.close()

            crashed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tests/helpers/c10_checkpoint_crash_worker.py"),
                    str(database_path),
                    str(PROJECT_ROOT),
                    target.schedule_key,
                    str(payload_path),
                    payload_hash,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                crashed.returncode,
                91,
                {"stdout": crashed.stdout, "stderr": crashed.stderr},
            )

            restarted = SqliteStore.open(database_path)
            restarted.migrate()
            try:
                self.assertEqual(
                    restarted.scalar(
                        "SELECT state FROM strategy_checkpoint_schedules "
                        "WHERE schedule_key=?",
                        (target.schedule_key,),
                    ),
                    "CAPTURED",
                )
                restarted_scheduler = CheckpointScheduler(
                    project_root=PROJECT_ROOT, store=restarted
                )
                resumed = restarted_scheduler.claim_due(
                    now_ms=target.due_at_ms + 30_001,
                    recovery_cutoff_ms=0,
                    active_market_id="c10-crash-market",
                    runtime_state="LIVE_READY",
                    poller_id="c10-restarted-worker",
                    limit=1,
                )
                self.assertEqual(len(resumed), 1)
                self.assertEqual(resumed[0].schedule.schedule_key, target.schedule_key)
                self.assertEqual(resumed[0].schedule.state, "CAPTURED")
                self.assertEqual(resumed[0].origin, "LIVE")

                input_hash = restarted_scheduler.evaluation_input_snapshot_hash(
                    target.evaluation_key
                )
                evaluation_key = restarted_scheduler.scheduled_evaluation_key(
                    target.evaluation_key, input_hash, 1
                )
                evaluation_payload = json.dumps(
                    {
                        "checkpoint_minutes": 60,
                        "market_id": "c10-crash-market",
                        "origin": "LIVE",
                        "status": "SIGNAL",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                evaluation = StrategyEvaluation(
                    evaluation_key=evaluation_key,
                    strategy_id=target.strategy_id,
                    strategy_version=target.strategy_version,
                    status="SIGNAL",
                    input_snapshot_hash=input_hash,
                    evaluation_revision=1,
                    execution_eligible=False,
                    evaluated_at_ms=target.due_at_ms + 30_002,
                    payload_json=evaluation_payload,
                    reason_code="C10_RESTART_RECOVERED",
                    origin="LIVE",
                    historical_signal_is_current_live_signal=False,
                    current_reevaluation_required=False,
                )
                signal_payload = json.dumps(
                    {
                        "evaluation_key": evaluation_key,
                        "origin": "LIVE",
                        "strategy_id": target.strategy_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                signal = SignalRecord(
                    identity_key=f"c10-checkpoint-signal:{evaluation_key}",
                    evaluation_key=evaluation_key,
                    strategy_id=target.strategy_id,
                    signal_type="STRATEGY_EVALUATION_SIGNAL",
                    payload_json=signal_payload,
                    created_at_ms=evaluation.evaluated_at_ms,
                    origin="LIVE",
                    execution_eligible=False,
                    infrastructure_only=False,
                )
                committed = restarted_scheduler.complete(
                    resumed[0], evaluation=evaluation, signal=signal
                )
                replayed = restarted_scheduler.complete(
                    resumed[0], evaluation=evaluation, signal=signal
                )

                self.assertTrue(committed.inserted)
                self.assertFalse(replayed.inserted)
                self.assertEqual(
                    (
                        committed.evaluation_id,
                        committed.signal_id,
                        committed.outbox_id,
                    ),
                    (
                        replayed.evaluation_id,
                        replayed.signal_id,
                        replayed.outbox_id,
                    ),
                )
                self.assertEqual(restarted.count("strategy_evaluations"), 1)
                self.assertEqual(restarted.count("signals"), 1)
                self.assertEqual(restarted.count("outbox_events"), 1)
                self.assertEqual(restarted.count("paper_fills"), 0)
                self.assertEqual(
                    restarted.scalar(
                        "SELECT state FROM strategy_checkpoint_schedules "
                        "WHERE schedule_key=?",
                        (target.schedule_key,),
                    ),
                    "COMPLETED",
                )
            finally:
                restarted.close()


if __name__ == "__main__":
    unittest.main()
