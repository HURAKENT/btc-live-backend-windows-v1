from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from src.storage import SqliteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRICT_A_IDS = {
    "YES_STRICT_A_OPERATIONAL",
    "YES_STRICT_A_T30",
    "YES_STRICT_A_T60",
}


class OperationalSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(
            Path(self.directory.name) / "operational-scheduler.sqlite3"
        )
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def _scheduler(self):
        from src.checkpoint_scheduler import CheckpointScheduler

        return CheckpointScheduler(project_root=PROJECT_ROOT, store=self.store)

    def _persist_market(self, market_id: str, resolution_ms: int) -> None:
        payload = json.dumps(
            {
                "event_id": market_id,
                "resolution_utc": datetime.fromtimestamp(
                    resolution_ms / 1_000, tz=UTC
                ).isoformat(),
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

    def test_registration_projects_frozen_c5_to_production_evaluator_support(
        self,
    ) -> None:
        c5_paths = (
            PROJECT_ROOT / "reports/C5_STRATEGY_47_ACTIVATION_ACCEPTANCE.json",
            PROJECT_ROOT / "reports/STRATEGY_47_STATUS_MATRIX.json",
        )
        frozen_hashes = tuple(
            hashlib.sha256(path.read_bytes()).hexdigest() for path in c5_paths
        )
        self._persist_market("market-current", 2_000_000_000_000)

        schedules = self._scheduler().register_market(
            market_id="market-current", created_at_ms=10
        )

        self.assertEqual({item.strategy_id for item in schedules}, STRICT_A_IDS)
        self.assertEqual(len(schedules), 4)
        self.assertEqual(
            tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in c5_paths),
            frozen_hashes,
        )

    def test_unavailable_live_checkpoint_is_reclaimed_as_non_live(self) -> None:
        resolution_ms = 2_000_000_000_000
        self._persist_market("market-current", resolution_ms)
        scheduler = self._scheduler()
        schedules = scheduler.register_market(
            market_id="market-current", created_at_ms=10
        )
        target = next(
            item for item in schedules if item.strategy_id == "YES_STRICT_A_T60"
        )
        self.store.rows(
            "UPDATE strategy_checkpoint_schedules SET state='BLOCKED' "
            "WHERE market_id='market-current' AND schedule_key<>?",
            (target.schedule_key,),
        )
        first = scheduler.claim_due(
            now_ms=target.due_at_ms,
            recovery_cutoff_ms=target.due_at_ms,
            active_market_id="market-current",
            runtime_state="LIVE_READY",
            poller_id="on-time",
            limit=1,
        )[0]
        self.assertEqual(first.origin, "LIVE")
        scheduler.release_unavailable(
            first,
            reason_code="PROVIDER_OFFLINE",
            updated_at_ms=target.due_at_ms,
        )

        late = scheduler.claim_due(
            now_ms=target.due_at_ms + 20 * 60_000,
            recovery_cutoff_ms=target.due_at_ms,
            active_market_id="market-current",
            runtime_state="LIVE_READY",
            poller_id="late-retry",
            limit=1,
        )

        self.assertEqual(len(late), 1)
        self.assertEqual(late[0].schedule.schedule_key, target.schedule_key)
        self.assertEqual(late[0].origin, "RECOVERED_AFTER_DOWNTIME")
        missing_payload = json.dumps(
            {
                "checkpoint_minutes": target.checkpoint_minutes,
                "market_id": target.market_id,
                "schema_version": "C6_MISSING_HISTORICAL_DEPTH_V1",
                "strategy_id": target.strategy_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        capture = scheduler.capture(
            late[0],
            input_payload_json=missing_payload,
            input_snapshot_hash=hashlib.sha256(missing_payload.encode()).hexdigest(),
            historical_depth_available=False,
        )
        self.assertEqual(capture.state, "BLOCKED")
        self.assertEqual(capture.reason_code, "RECOVERED_HISTORICAL_DEPTH_MISSING")

    def test_unsupported_and_expired_old_work_does_not_poison_current_market(
        self,
    ) -> None:
        old_resolution_ms = 2_000_000_000_000
        current_resolution_ms = old_resolution_ms + 86_400_000
        self._persist_market("market-old", old_resolution_ms)
        self._persist_market("market-current", current_resolution_ms)
        scheduler = self._scheduler()
        old_schedules = scheduler.register_market(
            market_id="market-old", created_at_ms=10
        )
        current_schedules = scheduler.register_market(
            market_id="market-current", created_at_ms=11
        )
        self.store.rows(
            "UPDATE strategy_checkpoint_schedules SET state='BLOCKED' "
            "WHERE market_id='market-old'"
        )
        unsupported = old_schedules[0]
        expired = old_schedules[1]
        self.store.rows(
            "UPDATE strategy_checkpoint_schedules "
            "SET state='PENDING',strategy_id='YES_PF1_T60',registry_index=11,"
            "blocked_reason=NULL WHERE schedule_key=?",
            (unsupported.schedule_key,),
        )
        self.store.rows(
            "UPDATE strategy_checkpoint_schedules "
            "SET state='PENDING',blocked_reason='PROVIDER_OFFLINE' "
            "WHERE schedule_key=?",
            (expired.schedule_key,),
        )
        now_ms = min(item.due_at_ms for item in current_schedules)

        due = scheduler.claim_due(
            now_ms=now_ms,
            recovery_cutoff_ms=now_ms,
            active_market_id="market-current",
            runtime_state="LIVE_READY",
            poller_id="current-market",
            limit=100,
        )

        self.assertTrue(due)
        self.assertEqual({item.schedule.market_id for item in due}, {"market-current"})
        self.assertTrue(all(item.schedule.strategy_id in STRICT_A_IDS for item in due))
        self.assertTrue(all(item.origin == "LIVE" for item in due))
        old_states = {
            schedule_key: (state, reason)
            for schedule_key, state, reason in self.store.rows(
                "SELECT schedule_key,state,blocked_reason "
                "FROM strategy_checkpoint_schedules "
                "WHERE schedule_key IN (?,?) ORDER BY schedule_key",
                (unsupported.schedule_key, expired.schedule_key),
            )
        }
        self.assertEqual(
            old_states[unsupported.schedule_key],
            ("BLOCKED", "UNSUPPORTED_OPERATIONAL_EVALUATOR"),
        )
        self.assertEqual(
            old_states[expired.schedule_key],
            ("BLOCKED", "MISSED_CHECKPOINT:PROVIDER_OFFLINE"),
        )
        self.assertNotIn(
            "YES_PF1_T60",
            {item.strategy_id for item in scheduler.market_schedules("market-old")},
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM strategy_checkpoint_schedules "
                "WHERE market_id='market-old' AND strategy_id='YES_PF1_T60'"
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
