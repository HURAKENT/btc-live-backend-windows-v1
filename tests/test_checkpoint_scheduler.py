from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from src.models import SignalRecord, StrategyEvaluation
from src.storage import SqliteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CheckpointSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "scheduler.sqlite3"
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _persist_market(self, market_id: str, resolution_ms: int) -> None:
        resolution_utc = datetime.fromtimestamp(
            resolution_ms / 1_000,
            tz=UTC,
        ).isoformat()
        payload = json.dumps(
            {
                "event_id": market_id,
                "resolution_utc": resolution_utc,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.store.persist_market_identity(
            market_id=market_id,
            payload_json=payload,
            payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            updated_at_ms=1,
        )

    def _scheduler(self):
        from src.checkpoint_scheduler import CheckpointScheduler

        return CheckpointScheduler(project_root=PROJECT_ROOT, store=self.store)

    @staticmethod
    def _capture_payload(due, *, historical_depth_available: bool = True):
        if not historical_depth_available:
            payload = json.dumps(
                {
                    "checkpoint_minutes": due.schedule.checkpoint_minutes,
                    "market_id": due.schedule.market_id,
                    "schema_version": "C6_MISSING_HISTORICAL_DEPTH_V1",
                    "strategy_id": due.schedule.strategy_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            return payload, hashlib.sha256(payload.encode()).hexdigest()
        from src.checkpoint_scheduler import encode_executable_checkpoint_input
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
                no_token_id=None,
            )
            for index in range(11)
        )
        evidence_sha = hashlib.sha256(
            due.schedule.schedule_key.encode()
        ).hexdigest()
        checkpoint_timestamp = due.schedule.due_at_ms
        return encode_executable_checkpoint_input(
            V1ExecutableCheckpointInput(
                checkpoint_minutes=due.schedule.checkpoint_minutes,
                buckets=buckets,
                prior_position=False,
                strict_price_history_evidence=StrictPriceHistoryEvidence(
                    provenance="CLOB_PRICE_HISTORY",
                    source_sha256=evidence_sha,
                    checkpoint_timestamp_ms=checkpoint_timestamp,
                    observation_timestamp_ms=checkpoint_timestamp - 1,
                ),
                pf1_snapshot_evidence=Pf1SnapshotEvidence(
                    bucket_count=11,
                    snapshot_complete=True,
                    synchronized=True,
                    fresh=True,
                    crossed_book_count=0,
                    fee_provenance="PUBLIC_CLOB_FEE_SCHEDULE",
                    snapshot_sha256=evidence_sha,
                    fee_schedule_sha256=evidence_sha,
                ),
            )
        )

    def _evaluation(self, due, *, status: str = "SIGNAL", payload_extra=None):
        payload = {
            "checkpoint_minutes": due.schedule.checkpoint_minutes,
            "market_id": due.schedule.market_id,
            "origin": due.origin,
            "status": status,
        }
        if payload_extra:
            payload.update(payload_extra)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        input_snapshot_hash = self._scheduler().evaluation_input_snapshot_hash(
            due.schedule.evaluation_key
        )
        return StrategyEvaluation(
            evaluation_key=self._scheduler().scheduled_evaluation_key(
                due.schedule.evaluation_key,
                input_snapshot_hash,
                1,
            ),
            strategy_id=due.schedule.strategy_id,
            strategy_version=due.schedule.strategy_version,
            status=status,
            input_snapshot_hash=input_snapshot_hash,
            evaluation_revision=1,
            execution_eligible=False,
            evaluated_at_ms=due.schedule.due_at_ms + 1,
            payload_json=payload_json,
            reason_code="SYNTHETIC_C6_ACCEPTANCE",
            origin=due.origin,
            historical_signal_is_current_live_signal=False,
            current_reevaluation_required=(
                due.origin == "RECOVERED_AFTER_DOWNTIME"
            ),
        )

    @staticmethod
    def _signal(due, evaluation):
        payload = json.dumps(
            {
                "evaluation_key": evaluation.evaluation_key,
                "origin": due.origin,
                "strategy_id": evaluation.strategy_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return SignalRecord(
            identity_key=f"checkpoint-signal:{evaluation.evaluation_key}",
            evaluation_key=evaluation.evaluation_key,
            strategy_id=evaluation.strategy_id,
            signal_type="STRATEGY_EVALUATION_SIGNAL",
            payload_json=payload,
            created_at_ms=evaluation.evaluated_at_ms,
            origin=due.origin,
            execution_eligible=False,
            infrastructure_only=False,
        )

    def test_scheduler_module_exists(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("src.checkpoint_scheduler"))

    def test_schema_v3_contains_checkpoint_schedule(self) -> None:
        self.assertEqual(
            self.store.scalar("SELECT 1 FROM schema_migrations WHERE version=3"),
            1,
        )
        names = {
            row[0]
            for row in self.store.rows(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("strategy_checkpoint_schedules", names)

    def test_schema_v3_rejects_unknown_evaluation_and_signal_origins(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.rows(
                """
                INSERT INTO strategy_evaluations(
                    evaluation_key,strategy_id,strategy_version,status,
                    input_snapshot_hash,evaluation_revision,execution_eligible,
                    evaluated_at_ms,payload_json,origin
                ) VALUES ('bad-origin-eval','S','V1','NO_SIGNAL',?,1,0,1,'{}','OTHER')
                """,
                ("1" * 64,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.rows(
                """
                INSERT INTO signals(
                    identity_key,evaluation_key,strategy_id,signal_type,
                    payload_json,created_at_ms,origin
                ) VALUES ('bad-origin-signal','missing','S','X','{}',1,'OTHER')
                """
            )

    def test_one_market_registers_exact_four_operational_checkpoints(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        schedules = self._scheduler().register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        self.assertEqual(len(schedules), 4)
        self.assertEqual(self.store.count("strategy_checkpoint_schedules"), 4)
        self.assertEqual(
            {schedule.strategy_id for schedule in schedules},
            {
                "YES_STRICT_A_OPERATIONAL",
                "YES_STRICT_A_T30",
                "YES_STRICT_A_T60",
            },
        )
        self.assertEqual(
            sorted(schedule.checkpoint_minutes for schedule in schedules),
            [30, 30, 60, 60],
        )

    def test_registration_is_idempotent_and_recurs_for_future_markets(self) -> None:
        scheduler = self._scheduler()
        for index, market_id in enumerate(("market-a", "market-b", "market-c")):
            resolution = 2_000_000_000_000 + index * 86_400_000
            self._persist_market(market_id, resolution)
            first = scheduler.register_market(
                market_id=market_id,
                created_at_ms=10 + index,
            )
            second = scheduler.register_market(
                market_id=market_id,
                created_at_ms=9_000 + index,
            )
            self.assertEqual(first, second)
        self.assertEqual(self.store.count("strategy_checkpoint_schedules"), 12)

    def test_market_identity_and_schedule_registration_are_atomic(self) -> None:
        resolution_ms = 2_000_000_000_000
        payload = json.dumps(
            {
                "event_id": "market-atomic",
                "resolution_utc": datetime.fromtimestamp(
                    resolution_ms / 1_000, tz=UTC
                ).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        self.store.rows(
            """
            CREATE TRIGGER fail_atomic_schedule
            BEFORE INSERT ON strategy_checkpoint_schedules
            BEGIN SELECT RAISE(ABORT, 'forced schedule failure'); END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced schedule failure"):
            self._scheduler().persist_market_and_register(
                market_id="market-atomic",
                market_identity_json=payload,
                market_identity_sha256=digest,
                created_at_ms=10,
            )
        self.assertEqual(self.store.count("market_catalog"), 0)
        self.assertEqual(self.store.count("strategy_checkpoint_schedules"), 0)

    def test_same_identity_with_changed_schedule_fails_closed(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        scheduler = self._scheduler()
        scheduler.register_market(
            market_id="market-a", created_at_ms=10
        )
        changed_payload = json.dumps(
            {
                "event_id": "market-a",
                "resolution_utc": datetime.fromtimestamp(
                    (resolution + 1) / 1_000,
                    tz=UTC,
                ).isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.store.rows(
            "UPDATE market_catalog SET payload_json=?, payload_sha256=? "
            "WHERE market_id='market-a'",
            (
                changed_payload,
                hashlib.sha256(changed_payload.encode()).hexdigest(),
            ),
        )
        with self.assertRaisesRegex(ValueError, "CHECKPOINT_SCHEDULE_CONFLICT"):
            scheduler.register_market(
                market_id="market-a",
                created_at_ms=10,
            )

    def test_due_jobs_are_ordered_and_classified_live_or_recovered(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(
            market_id="market-a", created_at_ms=10
        )
        earliest = min(item.due_at_ms for item in schedules)
        latest = max(item.due_at_ms for item in schedules)
        due = scheduler.claim_due(
            now_ms=latest + 1,
            recovery_cutoff_ms=earliest + 1,
            active_market_id="market-a",
            runtime_state="LIVE_READY",
            poller_id="ordered-poll",
            limit=100,
        )
        self.assertEqual(len(due), 4)
        self.assertEqual(
            [(item.schedule.due_at_ms, item.schedule.registry_index) for item in due],
            sorted(
                (item.schedule.due_at_ms, item.schedule.registry_index)
                for item in due
            ),
        )
        self.assertEqual(due[0].origin, "RECOVERED_AFTER_DOWNTIME")
        self.assertTrue(any(item.origin == "LIVE" for item in due))

    def test_only_live_ready_runtime_state_can_claim(self) -> None:
        self._persist_market("market-a", 2_000_000_000_000)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(market_id="market-a", created_at_ms=10)
        now_ms = max(item.due_at_ms for item in schedules) + 1
        with self.assertRaisesRegex(ValueError, "C6_RUNTIME_NOT_STRATEGY_READY"):
            scheduler.claim_due(
                now_ms=now_ms,
                recovery_cutoff_ms=now_ms,
                active_market_id="market-a",
                runtime_state="PASS",
                poller_id="not-ready",
                limit=1,
            )
        self.assertEqual(
            len(
                scheduler.claim_due(
                    now_ms=now_ms,
                    recovery_cutoff_ms=now_ms,
                    active_market_id="market-a",
                    runtime_state="LIVE_READY",
                    poller_id="ready",
                    limit=1,
                )
            ),
            1,
        )

    def test_capture_requires_executable_input_bound_to_schedule(self) -> None:
        due = self._single_due(recovered=False, capture=False)
        payload = json.dumps(
            {"checkpoint_minutes": due.schedule.checkpoint_minutes},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.assertRaisesRegex(
            ValueError, "C6_INVALID_EXECUTABLE_CHECKPOINT_JSON"
        ):
            self._scheduler().capture(
                due,
                input_payload_json=payload,
                input_snapshot_hash=hashlib.sha256(payload.encode()).hexdigest(),
                historical_depth_available=True,
            )

    def test_unavailable_live_input_releases_claim_without_output(self) -> None:
        due = self._single_due(recovered=False, capture=False)
        self._scheduler().release_unavailable(
            due,
            reason_code="C6_EXECUTABLE_INPUT_UNAVAILABLE",
            updated_at_ms=due.schedule.due_at_ms + 1,
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT state FROM strategy_checkpoint_schedules WHERE schedule_key=?",
                (due.schedule.schedule_key,),
            ),
            "PENDING",
        )
        self.assertEqual(self.store.count("strategy_evaluations"), 0)
        self.assertEqual(self.store.count("signals"), 0)
        self.assertEqual(self.store.count("outbox_events"), 0)

    def test_captured_checkpoint_is_reclaimed_after_restart(self) -> None:
        due = self._single_due(recovered=True)
        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        resumed = self._scheduler().claim_due(
            now_ms=due.schedule.due_at_ms + 30_001,
            recovery_cutoff_ms=due.schedule.due_at_ms + 1,
            active_market_id="market-a",
            runtime_state="LIVE_READY",
            poller_id="resume-captured",
            limit=1,
        )
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0].schedule.state, "CAPTURED")
        evaluation = self._evaluation(resumed[0], status="NO_SIGNAL")
        result = self._scheduler().complete(
            resumed[0], evaluation=evaluation, signal=None
        )
        self.assertTrue(result.inserted)

    def test_captured_live_origin_survives_restart_and_new_cutoff(self) -> None:
        due = self._single_due(recovered=False)
        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        resumed = self._scheduler().claim_due(
            now_ms=due.schedule.due_at_ms + 30_001,
            recovery_cutoff_ms=due.schedule.due_at_ms + 1,
            active_market_id="market-a",
            runtime_state="LIVE_READY",
            poller_id="resume-live-origin",
            limit=1,
        )
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0].origin, "LIVE")

    def test_atomic_completion_creates_one_evaluation_signal_and_outbox(self) -> None:
        due = self._single_due(recovered=False)
        evaluation = self._evaluation(due)
        signal = self._signal(due, evaluation)
        result = self._scheduler().complete(
            due,
            evaluation=evaluation,
            signal=signal,
        )
        self.assertTrue(result.inserted)
        self.assertNotEqual(evaluation.evaluation_key, due.schedule.evaluation_key)
        self.assertEqual(
            evaluation.evaluation_key,
            self._scheduler().scheduled_evaluation_key(
                due.schedule.evaluation_key,
                evaluation.input_snapshot_hash,
                evaluation.evaluation_revision,
            ),
        )
        self.assertEqual(self.store.count("strategy_evaluations"), 1)
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)
        self.assertEqual(
            self.store.scalar(
                "SELECT state FROM strategy_checkpoint_schedules WHERE schedule_key=?",
                (due.schedule.schedule_key,),
            ),
            "COMPLETED",
        )

    def test_exact_completion_replay_is_noop_and_signal_not_duplicated(self) -> None:
        due = self._single_due(recovered=False)
        evaluation = self._evaluation(due)
        signal = self._signal(due, evaluation)
        first = self._scheduler().complete(due, evaluation=evaluation, signal=signal)
        second = self._scheduler().complete(due, evaluation=evaluation, signal=signal)
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual((first.evaluation_id, first.signal_id, first.outbox_id), (second.evaluation_id, second.signal_id, second.outbox_id))
        self.assertEqual(self.store.count("signals"), 1)
        self.assertEqual(self.store.count("outbox_events"), 1)

    def test_completion_replay_cannot_omit_existing_signal(self) -> None:
        due = self._single_due(recovered=False)
        evaluation = self._evaluation(due)
        self._scheduler().complete(
            due,
            evaluation=evaluation,
            signal=self._signal(due, evaluation),
        )
        with self.assertRaisesRegex(
            ValueError,
            "C6_CHECKPOINT_SIGNAL_PRESENCE_CONFLICT",
        ):
            self._scheduler().complete(
                due,
                evaluation=evaluation,
                signal=None,
            )

    def test_completion_conflict_fails_closed_without_partial_writes(self) -> None:
        due = self._single_due(recovered=False)
        evaluation = self._evaluation(due)
        signal = self._signal(due, evaluation)
        self._scheduler().complete(due, evaluation=evaluation, signal=signal)
        changed = self._evaluation(due, payload_extra={"changed": True})
        with self.assertRaisesRegex(ValueError, "STRATEGY_EVALUATION_CONFLICT"):
            self._scheduler().complete(
                due,
                evaluation=changed,
                signal=self._signal(due, changed),
            )
        self.assertEqual(self.store.count("strategy_evaluations"), 1)
        self.assertEqual(self.store.count("signals"), 1)

    def test_completion_rejects_unbound_input_snapshot_hash(self) -> None:
        import dataclasses

        due = self._single_due(recovered=False)
        invalid = dataclasses.replace(
            self._evaluation(due),
            input_snapshot_hash="f" * 64,
        )
        with self.assertRaisesRegex(
            ValueError,
            "C6_EVALUATION_INPUT_HASH_MISMATCH",
        ):
            self._scheduler().complete(
                due,
                evaluation=invalid,
                signal=None,
            )
        self.assertEqual(self.store.count("strategy_evaluations"), 0)

    def test_outbox_failure_rolls_back_evaluation_signal_and_schedule(self) -> None:
        due = self._single_due(recovered=False)
        self.store.rows(
            """
            CREATE TRIGGER fail_scheduled_outbox
            BEFORE INSERT ON outbox_events
            BEGIN SELECT RAISE(ABORT, 'forced scheduled outbox failure'); END
            """
        )
        evaluation = self._evaluation(due)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced scheduled outbox failure"):
            self._scheduler().complete(
                due,
                evaluation=evaluation,
                signal=self._signal(due, evaluation),
            )
        self.assertEqual(self.store.count("strategy_evaluations"), 0)
        self.assertEqual(self.store.count("signals"), 0)
        self.assertEqual(self.store.count("outbox_events"), 0)
        self.assertEqual(
            self.store.scalar(
                "SELECT state FROM strategy_checkpoint_schedules WHERE schedule_key=?",
                (due.schedule.schedule_key,),
            ),
            "CAPTURED",
        )

    def test_recovered_jobs_cannot_be_execution_eligible(self) -> None:
        import dataclasses

        due = self._single_due(recovered=True)
        evaluation = dataclasses.replace(
            self._evaluation(due), execution_eligible=True
        )
        with self.assertRaisesRegex(ValueError, "RECOVERED_EVALUATION_EXECUTION_FORBIDDEN"):
            self._scheduler().complete(due, evaluation=evaluation, signal=None)

    def test_origin_mismatch_fails_closed(self) -> None:
        import dataclasses

        due = self._single_due(recovered=True)
        evaluation = dataclasses.replace(self._evaluation(due), origin="LIVE")
        with self.assertRaisesRegex(ValueError, "CHECKPOINT_EVALUATION_ORIGIN_MISMATCH"):
            self._scheduler().complete(due, evaluation=evaluation, signal=None)

    def test_completion_without_signal_is_atomic_and_restart_safe(self) -> None:
        due = self._single_due(recovered=True, capture=False)
        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        scheduler = self._scheduler()
        recovered = scheduler.claim_due(
            now_ms=due.schedule.due_at_ms + 30_002,
            recovery_cutoff_ms=due.schedule.due_at_ms + 1,
            active_market_id="market-a",
            runtime_state="LIVE_READY",
            poller_id="restart-recovery",
            limit=1,
        )
        self.assertEqual(len(recovered), 1)
        payload, digest = self._capture_payload(recovered[0])
        scheduler.capture(
            recovered[0],
            input_payload_json=payload,
            input_snapshot_hash=digest,
            historical_depth_available=True,
        )
        evaluation = self._evaluation(recovered[0], status="NO_SIGNAL")
        result = scheduler.complete(
            recovered[0], evaluation=evaluation, signal=None
        )
        self.assertTrue(result.inserted)
        self.assertIsNone(result.signal_id)
        self.assertIsNone(result.outbox_id)
        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        remaining = self._scheduler().claim_due(
                now_ms=due.schedule.due_at_ms + 30_003,
                recovery_cutoff_ms=due.schedule.due_at_ms + 1,
                active_market_id="market-a",
                runtime_state="LIVE_READY",
                poller_id="post-completion",
                limit=100,
            )
        self.assertNotIn(
            due.schedule.schedule_key,
            {item.schedule.schedule_key for item in remaining},
        )

    def test_schedules_bind_exact_c5_activation_provenance(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        schedules = self._scheduler().register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        c5_sha = hashlib.sha256(
            (PROJECT_ROOT / "reports/C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json")
            .read_bytes()
        ).hexdigest()
        matrix = json.loads(
            (PROJECT_ROOT / "reports/STRATEGY_47_STATUS_MATRIX.json").read_text(
                encoding="utf-8"
            )
        )
        matrix_sha = hashlib.sha256(
            (PROJECT_ROOT / "reports/STRATEGY_47_STATUS_MATRIX.json").read_bytes()
        ).hexdigest()
        enabled = {
            row["strategy_id"]: row
            for row in matrix["strategies"]
            if row["activation_status"] == "PAPER_EVALUATION_ENABLED"
        }
        self.assertEqual(len(enabled), 8)
        for schedule in schedules:
            row = enabled[schedule.strategy_id]
            self.assertEqual(schedule.c5_acceptance_sha256, c5_sha)
            self.assertEqual(schedule.c5_status_matrix_sha256, matrix_sha)
            self.assertEqual(schedule.activation_source_commit, matrix["source_commit"])
            self.assertEqual(schedule.activation_status, "PAPER_EVALUATION_ENABLED")
            self.assertEqual(schedule.rule_spec_sha256, row["rule_spec_sha256"])

    def test_c5_activation_provenance_mutation_fails_closed(self) -> None:
        from src.checkpoint_scheduler import load_c5_scheduler_contract

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reports = root / "reports"
            reports.mkdir()
            for name in (
                "C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json",
                "STRATEGY_47_STATUS_MATRIX.json",
            ):
                (reports / name).write_bytes(
                    (PROJECT_ROOT / "reports" / name).read_bytes()
                )
            contract = load_c5_scheduler_contract(root)
            self.assertEqual(len(contract.enabled), 8)
            matrix_path = reports / "STRATEGY_47_STATUS_MATRIX.json"
            matrix_path.write_bytes(matrix_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "C6_C5_PROVENANCE_MISMATCH"):
                load_c5_scheduler_contract(root)

    def test_resolution_is_bound_to_immutable_market_identity(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        stored_hash = self.store.scalar(
            "SELECT payload_sha256 FROM market_catalog WHERE market_id='market-a'"
        )
        schedules = self._scheduler().register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        self.assertTrue(
            all(schedule.market_identity_sha256 == stored_hash for schedule in schedules)
        )
        self.assertTrue(
            all(schedule.resolution_ms == resolution for schedule in schedules)
        )

        self.store.rows(
            "UPDATE market_catalog SET payload_json='{}' WHERE market_id='market-a'"
        )
        with self.assertRaisesRegex(ValueError, "MARKET_IDENTITY_HASH_MISMATCH"):
            self._scheduler().register_market(
                market_id="market-a",
                created_at_ms=11,
            )

    def test_restart_registration_ignores_different_created_at_metadata(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        first = self._scheduler().register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        second = self._scheduler().register_market(
            market_id="market-a",
            created_at_ms=99_999,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            {
                row[0]
                for row in self.store.rows(
                    "SELECT created_at_ms FROM strategy_checkpoint_schedules"
                )
            },
            {10},
        )

    def test_incremental_t60_t30_inputs_compose_only_when_complete(self) -> None:
        from src.checkpoint_scheduler import encode_executable_checkpoint_input
        from src.strategy_dispatch import (
            V1ExecutableCheckpointInput,
            load_strategy_dispatcher,
        )
        from src.strategy_v1 import BucketInput, StrictPriceHistoryEvidence

        buckets = tuple(
            BucketInput(
                bucket_index=index,
                model_p=0.50 if index == 0 else 0.05,
                market_q_yes=0.30 if index == 0 else 0.10 + index / 100,
                market_q_no=None,
                vwap5=0.35 if index == 0 else 0.15,
                confirmed_fee=0.0,
                no_token_id=None,
            )
            for index in range(11)
        )
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        operational = {
            item.checkpoint_minutes: item
            for item in schedules
            if item.strategy_id == "YES_STRICT_A_OPERATIONAL"
        }
        t60 = scheduler.claim_schedule(
            operational[60].schedule_key,
            origin="LIVE",
            poller_id="t60",
        )
        payload, digest = encode_executable_checkpoint_input(
            V1ExecutableCheckpointInput(
                checkpoint_minutes=60,
                buckets=buckets,
                prior_position=False,
                strict_price_history_evidence=StrictPriceHistoryEvidence(
                    provenance="CLOB_PRICE_HISTORY",
                    source_sha256="1" * 64,
                    checkpoint_timestamp_ms=1_000,
                    observation_timestamp_ms=999,
                ),
            )
        )
        scheduler.capture(
            t60,
            input_payload_json=payload,
            input_snapshot_hash=digest,
            historical_depth_available=True,
        )
        self.assertIsNone(
            scheduler.compose_dispatch(
                market_id="market-a",
                strategy_id="YES_STRICT_A_OPERATIONAL",
            )
        )

        self.store.close()
        self.store = SqliteStore.open(self.db_path)
        self.store.migrate()
        scheduler = self._scheduler()
        t30 = scheduler.claim_schedule(
            operational[30].schedule_key,
            origin="LIVE",
            poller_id="t30",
        )
        payload, digest = encode_executable_checkpoint_input(
            V1ExecutableCheckpointInput(
                checkpoint_minutes=30,
                buckets=buckets,
                prior_position=True,
                strict_price_history_evidence=StrictPriceHistoryEvidence(
                    provenance="CLOB_PRICE_HISTORY",
                    source_sha256="2" * 64,
                    checkpoint_timestamp_ms=2_000,
                    observation_timestamp_ms=1_999,
                ),
            )
        )
        scheduler.capture(
            t30,
            input_payload_json=payload,
            input_snapshot_hash=digest,
            historical_depth_available=True,
        )
        composition = scheduler.compose_dispatch(
            market_id="market-a",
            strategy_id="YES_STRICT_A_OPERATIONAL",
        )
        self.assertIsNotNone(composition)
        self.assertEqual(
            tuple(item.checkpoint_minutes for item in composition.checkpoints),
            (60, 30),
        )
        self.assertEqual(composition.input_schema_version, "BTC_STRATEGY_EXECUTABLE_CHECKPOINT_INPUT_V1")
        dispatched = scheduler.dispatch_composition(
            composition,
            dispatcher=load_strategy_dispatcher(PROJECT_ROOT),
        )
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0].checkpoint_minutes, 60)
        self.assertTrue(dispatched[0].accepted)

    def test_operational_evaluation_atomically_completes_both_checkpoints(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        operational = {
            item.checkpoint_minutes: item
            for item in schedules
            if item.strategy_id == "YES_STRICT_A_OPERATIONAL"
        }
        self.assertEqual(
            operational[60].evaluation_key,
            operational[30].evaluation_key,
        )
        claimed = {}
        for checkpoint in (60, 30):
            due = scheduler.claim_schedule(
                operational[checkpoint].schedule_key,
                origin="LIVE",
                poller_id=f"operational-{checkpoint}",
            )
            payload, digest = self._capture_payload(due)
            scheduler.capture(
                due,
                input_payload_json=payload,
                input_snapshot_hash=digest,
                historical_depth_available=True,
            )
            claimed[checkpoint] = due

        evaluation = self._evaluation(claimed[30], status="NO_SIGNAL")
        scheduler.complete(
            claimed[30],
            evaluation=evaluation,
            signal=None,
        )

        self.assertEqual(
            self.store.rows(
                "SELECT checkpoint_minutes,state FROM strategy_checkpoint_schedules "
                "WHERE market_id='market-a' AND strategy_id='YES_STRICT_A_OPERATIONAL' "
                "ORDER BY checkpoint_minutes DESC"
            ),
            [(60, "COMPLETED"), (30, "COMPLETED")],
        )
        self.assertEqual(self.store.count("strategy_evaluations"), 1)

    def test_operational_mixed_recovered_and_live_origin_fails_closed(self) -> None:
        self._persist_market("market-a", 2_000_000_000_000)
        scheduler = self._scheduler()
        operational = {
            item.checkpoint_minutes: item
            for item in scheduler.register_market(
                market_id="market-a",
                created_at_ms=10,
            )
            if item.strategy_id == "YES_STRICT_A_OPERATIONAL"
        }
        claimed = {}
        for checkpoint, origin in ((60, "RECOVERED_AFTER_DOWNTIME"), (30, "LIVE")):
            due = scheduler.claim_schedule(
                operational[checkpoint].schedule_key,
                origin=origin,
                poller_id=f"mixed-{checkpoint}",
            )
            payload, digest = self._capture_payload(due)
            scheduler.capture(
                due,
                input_payload_json=payload,
                input_snapshot_hash=digest,
                historical_depth_available=True,
            )
            claimed[checkpoint] = due
        with self.assertRaisesRegex(ValueError, "C6_MIXED_CHECKPOINT_ORIGIN"):
            scheduler.complete(
                claimed[30],
                evaluation=self._evaluation(claimed[30], status="NO_SIGNAL"),
                signal=None,
            )
        self.assertEqual(self.store.count("strategy_evaluations"), 0)

    def test_recovered_missing_historical_depth_blocks_without_evaluation(self) -> None:
        due = self._single_due(recovered=True, capture=False)
        payload, digest = self._capture_payload(
            due,
            historical_depth_available=False,
        )
        result = self._scheduler().capture(
            due,
            input_payload_json=payload,
            input_snapshot_hash=digest,
            historical_depth_available=False,
        )
        self.assertEqual(result.state, "BLOCKED")
        self.assertEqual(result.reason_code, "RECOVERED_HISTORICAL_DEPTH_MISSING")
        self.assertEqual(self.store.count("strategy_evaluations"), 0)
        self.assertEqual(
            self.store.scalar(
                "SELECT state FROM strategy_checkpoint_schedules WHERE schedule_key=?",
                (due.schedule.schedule_key,),
            ),
            "BLOCKED",
        )

    def test_recovered_completion_requires_and_tracks_current_reevaluation(self) -> None:
        import dataclasses

        due = self._single_due(recovered=True)
        invalid = dataclasses.replace(
            self._evaluation(due),
            current_reevaluation_required=False,
        )
        with self.assertRaisesRegex(
            ValueError,
            "RECOVERED_CURRENT_REEVALUATION_REQUIRED",
        ):
            self._scheduler().complete(due, evaluation=invalid, signal=None)

        recovered = self._evaluation(due, status="NO_SIGNAL")
        result = self._scheduler().complete(due, evaluation=recovered, signal=None)
        self.assertTrue(result.current_reevaluation_required)
        pending_status = self._scheduler().pending_current_reevaluations("market-a")
        self.assertEqual(len(pending_status), 1)
        self.assertEqual(
            pending_status[0].evaluation_group_key,
            due.schedule.evaluation_key,
        )
        pending = self._scheduler().claim_current_reevaluations(
            market_id="market-a",
            poller_id="current-test",
            now_ms=due.schedule.due_at_ms + 2,
            limit=10,
        )
        self.assertEqual(len(pending), 1)
        current_payload, current_hash = self._capture_payload(
            due,
        )
        self._scheduler().capture_current_reevaluation(
            pending[0],
            input_payload_json=current_payload,
            input_snapshot_hash=current_hash,
        )
        current_key = self._scheduler().current_reevaluation_key(
            due.schedule.evaluation_key, current_hash, 1
        )
        current = dataclasses.replace(
            recovered,
            evaluation_key=current_key,
            input_snapshot_hash=current_hash,
            origin="LIVE",
            current_reevaluation_required=False,
            evaluated_at_ms=recovered.evaluated_at_ms + 1,
        )
        first_current_id = self._scheduler().complete_current_reevaluation(
            pending[0],
            evaluation=current,
        )
        second_current_id = self._scheduler().complete_current_reevaluation(
            pending[0],
            evaluation=current,
        )
        self.assertEqual(first_current_id, second_current_id)
        self.assertEqual(
            self._scheduler().pending_current_reevaluations("market-a"),
            (),
        )
        self.assertEqual(self.store.count("strategy_evaluations"), 2)

    def test_current_reevaluation_rejects_unpersisted_input_hash(self) -> None:
        import dataclasses

        due = self._single_due(recovered=True)
        recovered = self._evaluation(due, status="NO_SIGNAL")
        self._scheduler().complete(due, evaluation=recovered, signal=None)
        pending = self._scheduler().claim_current_reevaluations(
            market_id="market-a",
            poller_id="current-input-test",
            now_ms=due.schedule.due_at_ms + 2,
            limit=10,
        )[0]
        payload, digest = self._capture_payload(due)
        self._scheduler().capture_current_reevaluation(
            pending,
            input_payload_json=payload,
            input_snapshot_hash=digest,
        )
        arbitrary = "b" * 64
        invalid = dataclasses.replace(
            recovered,
            evaluation_key=self._scheduler().current_reevaluation_key(
                pending.evaluation_group_key, arbitrary, 1
            ),
            input_snapshot_hash=arbitrary,
            origin="LIVE",
            current_reevaluation_required=False,
        )
        with self.assertRaisesRegex(
            ValueError, "C6_CURRENT_REEVALUATION_INPUT_MISMATCH"
        ):
            self._scheduler().complete_current_reevaluation(
                pending, evaluation=invalid
            )

    def test_stale_or_rollover_blocked_runtime_cannot_claim_strategy_work(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        now_ms = max(item.due_at_ms for item in schedules) + 1
        with self.assertRaisesRegex(ValueError, "ROLLOVER_BLOCKED_STRATEGY_GUARD"):
            scheduler.claim_due(
                now_ms=now_ms,
                recovery_cutoff_ms=now_ms,
                active_market_id="market-a",
                runtime_state="ROLLOVER_BLOCKED",
                poller_id="blocked",
                limit=10,
            )
        with self.assertRaisesRegex(ValueError, "STALE_MARKET_STRATEGY_GUARD"):
            scheduler.claim_due(
                now_ms=now_ms,
                recovery_cutoff_ms=now_ms,
                active_market_id="market-b",
                runtime_state="LIVE_READY",
                poller_id="stale",
                limit=10,
            )
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM strategy_checkpoint_schedules "
                "WHERE state='CLAIMED'"
            ),
            0,
        )

    def test_concurrent_double_poll_claims_each_schedule_once(self) -> None:
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        schedules = self._scheduler().register_market(
            market_id="market-a",
            created_at_ms=10,
        )
        target = min(schedules, key=lambda item: (item.due_at_ms, item.registry_index))
        self.store.rows(
            "UPDATE strategy_checkpoint_schedules SET state='BLOCKED' "
            "WHERE schedule_key<>?",
            (target.schedule_key,),
        )
        barrier = threading.Barrier(2)

        def poll(poller_id: str):
            store = SqliteStore.open(self.db_path)
            try:
                store.migrate()
                scheduler = self._scheduler_for_store(store)
                barrier.wait(timeout=5)
                return scheduler.claim_due(
                    now_ms=target.due_at_ms + 1,
                    recovery_cutoff_ms=target.due_at_ms,
                    active_market_id="market-a",
                    runtime_state="LIVE_READY",
                    poller_id=poller_id,
                    limit=1,
                )
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(poll, ("poll-a", "poll-b")))
        self.assertEqual(sum(len(items) for items in results), 1)
        self.assertEqual(
            {item.schedule.schedule_key for items in results for item in items},
            {target.schedule_key},
        )

    def test_concurrent_current_reevaluation_claims_group_once(self) -> None:
        due = self._single_due(recovered=True)
        self._scheduler().complete(
            due,
            evaluation=self._evaluation(due, status="NO_SIGNAL"),
            signal=None,
        )
        barrier = threading.Barrier(2)

        def poll(poller_id: str):
            store = SqliteStore.open(self.db_path)
            try:
                store.migrate()
                scheduler = self._scheduler_for_store(store)
                barrier.wait(timeout=5)
                return scheduler.claim_current_reevaluations(
                    market_id="market-a",
                    poller_id=poller_id,
                    now_ms=due.schedule.due_at_ms + 2,
                    limit=10,
                )
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(poll, ("current-a", "current-b")))
        self.assertEqual(sum(len(items) for items in results), 1)

    @staticmethod
    def _scheduler_for_store(store):
        from src.checkpoint_scheduler import CheckpointScheduler

        return CheckpointScheduler(project_root=PROJECT_ROOT, store=store)

    def _single_due(self, *, recovered: bool, capture: bool = True):
        resolution = 2_000_000_000_000
        self._persist_market("market-a", resolution)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(
            market_id="market-a", created_at_ms=10
        )
        target = min(schedules, key=lambda item: (item.due_at_ms, item.registry_index))
        cutoff = target.due_at_ms + 1 if recovered else target.due_at_ms
        due = scheduler.claim_due(
            now_ms=target.due_at_ms + 1,
            recovery_cutoff_ms=cutoff,
            active_market_id="market-a",
            runtime_state="LIVE_READY",
            poller_id="single-due",
            limit=1,
        )
        self.assertEqual(len(due), 1)
        self.assertEqual(
            due[0].origin,
            "RECOVERED_AFTER_DOWNTIME" if recovered else "LIVE",
        )
        if capture:
            payload, digest = self._capture_payload(due[0])
            scheduler.capture(
                due[0],
                input_payload_json=payload,
                input_snapshot_hash=digest,
                historical_depth_available=True,
            )
        return due[0]


if __name__ == "__main__":
    unittest.main()
