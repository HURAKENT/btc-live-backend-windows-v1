from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.single_instance import AlreadyRunningError, WindowsMutex
from src.storage import SqliteStore
from tools import simulate_downtime


class _Dependencies:
    def __init__(self):
        self.trace = []
        self.now_ns = 1_000_000_000
        self.sleep_advance_ms = 600_000
        self.initial_ready = True
        self.restart_ready = True
        self.initial_stop = self._stop_result()
        self.final_stop = self._stop_result()
        self.second_exit = 20
        self.promotions = 0
        self.initial_path = None
        self.restart_path = None
        self.recovery = self._recovery()
        self.evaluations = self._evaluations()
        self.continuity = self._continuity()
        self.outbox = {
            "proved": True,
            "replayed_event_ids": [2, 3],
            "database_event_ids": [2, 3],
            "missing_event_ids": [],
        }
        self.integrity = self._integrity()

    def start_backend(self, database_path, phase):
        self.trace.append(f"{phase} start")
        if phase == "initial":
            self.initial_path = database_path
        else:
            self.restart_path = database_path
        return phase

    def observe_starting(self, process, phase):
        self.trace.append(f"{phase} STARTING")
        return True

    def wait_live_ready(self, process, phase):
        self.trace.append(f"{phase} LIVE_READY")
        ready = self.initial_ready if phase == "initial" else self.restart_ready
        return {
            "live_ready": ready,
            "startup_state": "LIVE_READY" if ready else "RECOVERY_BLOCKED",
            "binance_status": "LIVE" if ready else "FAILED",
            "polymarket_status": "LIVE" if ready else "FAILED",
            "market_count": 11 if ready else 0,
            "asset_count": 22 if ready else 0,
            "last_event_id": 3 if phase == "restart" else 1,
        }

    def check_second_instance(self, database_path):
        self.trace.append("second-instance check")
        return self.second_exit

    def audit_baseline(self, database_path, ready):
        self.trace.append("initial DB/API baseline")
        return {"last_event_id": 1, "database_integrity": self.integrity}

    def audit_outbox(self, phase, after_event_id):
        self.trace.append(
            "outbox replay" if phase == "initial" else "post-restart outbox audit"
        )
        return dict(self.outbox)

    def stop_backend(self, process, phase):
        self.trace.append(
            "initial graceful stop" if phase == "initial" else "final graceful stop"
        )
        return dict(self.initial_stop if phase == "initial" else self.final_stop)

    def audit_blocked(self, database_path, ready, stop):
        self.trace.append("blocked evidence audit")
        return {
            "database_integrity": self.integrity,
            "history_sequence_diagnostics": [
                {
                    "page_index": 1,
                    "rejection_category": "CROSS_PAGE_OVERLAP",
                }
            ],
            "latest_lifecycle_state": "RECOVERY_BLOCKED",
            "sanitized": True,
        }

    def monotonic_ns(self):
        return self.now_ns

    def record_phase(self, phase):
        self.trace.append(phase)

    def sleep(self, seconds):
        self.trace.append("sleep")
        self.now_ns += self.sleep_advance_ms * 1_000_000

    def audit_recovery(self, database_path, initial, restart):
        self.trace.append("recovery audit")
        return dict(self.recovery)

    def audit_evaluations(self, database_path):
        self.trace.append("recovered/current evaluation audit")
        return dict(self.evaluations)

    def audit_continuity(self, database_path, initial, restart):
        self.trace.append("continuity audit")
        return dict(self.continuity)

    def audit_final_database(self, database_path):
        self.trace.append("final DB integrity")
        return dict(self.integrity)

    def promote_artifacts(self, evidence):
        self.trace.append("pack/report promotion")
        self.promotions += 1
        return {"status": "PASS"}

    @staticmethod
    def _stop_result():
        return {
            "exit_code": 0,
            "forced_termination_used": False,
            "port_free": True,
            "mutex_free": True,
            "child_processes": 0,
        }

    @staticmethod
    def _integrity():
        return {
            "status": "PASS",
            "quick_check": "ok",
            "integrity_check": "ok",
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "migration_version": 2,
            "table_count": 9,
        }

    @staticmethod
    def _recovery():
        return {
            "post_restart_live_ready": True,
            "polymarket_reconciled": True,
            "historical_depth_classification": "NOT_REQUIRED",
        }

    @staticmethod
    def _evaluations():
        return {
            "recovered_count": 1,
            "current_count": 1,
            "distinct_identities": True,
            "duplicate_recovered_identities": 0,
            "recovered_execution_eligible": False,
            "current_execution_eligible": False,
            "recovered_trading_eligible": False,
            "current_trading_eligible": False,
            "recovered_infrastructure_only": True,
            "current_infrastructure_only": True,
        }

    @staticmethod
    def _continuity():
        return {
            "missing_binance_closed_minutes": 0,
            "duplicate_binance_natural_keys": 0,
            "binance_ohlcv_conflicts": 0,
            "source_cursor_regressions": 0,
            "last_event_id_regressions": 0,
            "polymarket_asset_count": 22,
            "source_event_conflicts": 0,
            "snapshot_input_count": 23,
            "snapshot_input_bound": 23,
        }


class _AcceptanceLock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Task14AcceptanceLockTests(unittest.TestCase):
    def test_collision_exits_20_without_creating_run_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_base = root / "data"
            runtime_base = root / "runtime"
            reports = root / "reports"
            artifacts = root / "artifacts"
            reports.mkdir()
            artifacts.mkdir()
            downtime = reports / "C1_DOWNTIME_ACCEPTANCE.json"
            final = reports / "C1_FINAL_ACCEPTANCE.json"
            pack = artifacts / "C1_ACCEPTANCE_PACK.zip"
            downtime.write_bytes(b"historical-downtime")
            final.write_bytes(b"historical-final")
            pack.write_bytes(b"historical-pack")
            canonical_before = {
                path: path.read_bytes()
                for path in (downtime, final, pack)
            }

            def collision(_name):
                raise AlreadyRunningError("BACKEND_ALREADY_RUNNING")

            output = io.StringIO()
            with (
                mock.patch.object(simulate_downtime, "require_windows"),
                mock.patch.object(
                    simulate_downtime,
                    "ACCEPTANCE_DATA_BASE",
                    data_base,
                ),
                mock.patch.object(
                    simulate_downtime,
                    "ACCEPTANCE_RUNTIME_BASE",
                    runtime_base,
                ),
                mock.patch.object(
                    simulate_downtime,
                    "DOWNTIME_REPORT_PATH",
                    downtime,
                ),
                mock.patch.object(
                    simulate_downtime,
                    "FINAL_REPORT_PATH",
                    final,
                ),
                mock.patch.object(
                    simulate_downtime,
                    "PACK_PATH",
                    pack,
                ),
                contextlib.redirect_stdout(output),
            ):
                exit_code = simulate_downtime.main(
                    mutex_factory=collision
                )

            self.assertEqual(exit_code, 20)
            terminal = json.loads(output.getvalue().strip())
            self.assertEqual(
                terminal["status"],
                "BLOCKED_ACCEPTANCE_ALREADY_RUNNING",
            )
            self.assertIsNone(terminal["run_id"])
            self.assertFalse(data_base.exists())
            self.assertFalse(runtime_base.exists())
            self.assertEqual(
                canonical_before,
                {path: path.read_bytes() for path in canonical_before},
            )

    def test_lock_is_held_during_core_and_released_after_return(self):
        lock = _AcceptanceLock()
        observed = []

        def factory(name):
            observed.append(("acquire", name, lock.closed))
            return lock

        def core():
            observed.append(("core", lock.closed))
            return 7

        with (
            mock.patch.object(simulate_downtime, "require_windows"),
            mock.patch.object(
                simulate_downtime, "_run_acceptance_main", core
            ),
        ):
            exit_code = simulate_downtime.main(mutex_factory=factory)

        self.assertEqual(exit_code, 7)
        self.assertEqual(
            observed,
            [
                (
                    "acquire",
                    "BTC_LIVE_BACKEND_WINDOWS_V1_C1_ACCEPTANCE",
                    False,
                ),
                ("core", False),
            ],
        )
        self.assertTrue(lock.closed)

    def test_lock_is_released_after_unexpected_exception(self):
        lock = _AcceptanceLock()

        def core():
            raise RuntimeError("synthetic failure")

        with (
            mock.patch.object(simulate_downtime, "require_windows"),
            mock.patch.object(
                simulate_downtime, "_run_acceptance_main", core
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                simulate_downtime.main(mutex_factory=lambda _name: lock)

        self.assertTrue(lock.closed)

    @unittest.skipUnless(os.name == "nt", "Windows named mutex contract")
    def test_real_windows_acceptance_mutex_collision_and_release(self):
        name = simulate_downtime.ACCEPTANCE_MUTEX_NAME
        first = WindowsMutex.acquire(name)
        try:
            with self.assertRaises(AlreadyRunningError):
                WindowsMutex.acquire(name)
        finally:
            first.close()

        replacement = WindowsMutex.acquire(name)
        replacement.close()


class Task14AcceptanceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name).resolve() / "btc_live_backend.sqlite3"
        self.dependencies = _Dependencies()

    def tearDown(self):
        self.temp.cleanup()

    def run_cycle(self):
        return simulate_downtime.run_acceptance_cycle(
            database_path=self.database,
            dependencies=self.dependencies,
            downtime_target_ms=600_000,
        )

    def test_pass_pack_derives_started_backend_from_live_ready_state(self):
        downtime_report = {
            "status": "PASS",
            "initial_state": {"live_ready": True},
            "database_integrity": {},
            "binance_continuity": {},
            "polymarket_reconciliation": {},
            "canary_semantics": {},
            "outbox_replay": {},
            "blocking_failures": [],
        }

        entries = simulate_downtime._pack_entries(
            downtime_report,
            {"status": "PASS"},
        )
        bounded_log = json.loads(
            entries["evidence/sanitized_bounded_log.json"]
        )

        self.assertIn("BACKEND_STARTED", bounded_log["events"])

    def test_complete_phase_order(self):
        result = self.run_cycle()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            self.dependencies.trace,
            [
                "initial start",
                "initial STARTING",
                "initial LIVE_READY",
                "second-instance check",
                "initial DB/API baseline",
                "outbox replay",
                "initial graceful stop",
                "downtime start",
                "sleep",
                "downtime validation",
                "restart start",
                "restart STARTING",
                "restart LIVE_READY",
                "recovery audit",
                "recovered/current evaluation audit",
                "continuity audit",
                "post-restart outbox audit",
                "final graceful stop",
                "final DB integrity",
                "pack/report promotion",
            ],
        )

    def test_initial_and_restart_reuse_one_database_path(self):
        self.run_cycle()
        self.assertEqual(self.dependencies.initial_path, self.database)
        self.assertEqual(self.dependencies.restart_path, self.database)

    def test_exact_600_second_downtime_passes(self):
        result = self.run_cycle()
        self.assertEqual(result["downtime_duration_ms"], 600_000)
        self.assertEqual(result["status"], "PASS")

    def test_short_downtime_blocks_before_restart(self):
        self.dependencies.sleep_advance_ms = 599_999
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_DOWNTIME_TOO_SHORT")
        self.assertNotIn("restart start", self.dependencies.trace)

    def test_long_downtime_blocks_before_restart(self):
        self.dependencies.sleep_advance_ms = 630_001
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_DOWNTIME_WINDOW")
        self.assertNotIn("restart start", self.dependencies.trace)

    def test_initial_live_ready_is_required(self):
        self.dependencies.initial_ready = False
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_INITIAL_LIVE_READY")
        self.assertNotIn("sleep", self.dependencies.trace)
        self.assertEqual(self.dependencies.promotions, 0)

    def test_initial_blocked_result_includes_sanitized_database_evidence(self):
        self.dependencies.initial_ready = False

        result = self.run_cycle()

        self.assertEqual(
            result["blocked_evidence"],
            {
                "database_integrity": self.dependencies.integrity,
                "history_sequence_diagnostics": [
                    {
                        "page_index": 1,
                        "rejection_category": "CROSS_PAGE_OVERLAP",
                    }
                ],
                "latest_lifecycle_state": "RECOVERY_BLOCKED",
                "sanitized": True,
            },
        )
        self.assertIn("blocked evidence audit", self.dependencies.trace)

    def test_blocked_database_evidence_is_sanitized_and_deduplicated(self):
        diagnostic = {
            "adjacent_relations": {"EQ": 1, "GT": 0, "LT": 0},
            "cross_page_overlap_count": 0,
            "direction": "ALL_EQUAL",
            "duplicate_position_count": 1,
            "duplicate_positions": [1],
            "duplicate_positions_truncated": False,
            "first_rejected_position": 1,
            "item_count": 2,
            "page_index": 0,
            "rejection_category": "DUPLICATE_WITHIN_PAGE",
            "request_range_sha256": "a" * 64,
            "timestamp_type_histogram": {"int": 2},
        }
        detail = (
            "POLYMARKET_HISTORY_SEQUENCE_ERROR:"
            + json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))
        )
        store = SqliteStore.open(self.database)
        try:
            store.migrate()
            store.append_incident(
                incident_key="runtime:synthetic:failure",
                severity="CRITICAL",
                status="RECOVERY_BLOCKED",
                payload_json=json.dumps(
                    {
                        "code": "RUNTIME_FATAL",
                        "detail": detail,
                        "source": None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at_ms=1,
            )
            store.append_lifecycle_state(
                run_id="synthetic-run",
                state="RECOVERY_BLOCKED",
                sequence_index=0,
                created_at_ms=2,
                detail=detail,
            )
        finally:
            store.close()

        evidence = simulate_downtime._blocked_database_evidence(
            self.database
        )

        self.assertEqual(
            evidence["history_sequence_diagnostics"],
            [diagnostic],
        )
        self.assertEqual(
            evidence["latest_lifecycle_state"],
            "RECOVERY_BLOCKED",
        )
        serialized = json.dumps(evidence, sort_keys=True)
        self.assertNotIn("synthetic-run", serialized)
        self.assertNotIn("synthetic:failure", serialized)

    def test_initial_graceful_stop_is_required(self):
        self.dependencies.initial_stop["exit_code"] = 1
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_GRACEFUL_SHUTDOWN")
        self.assertNotIn("restart start", self.dependencies.trace)

    def test_restart_live_ready_is_required(self):
        self.dependencies.restart_ready = False
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_RECOVERY")
        self.assertIn("final graceful stop", self.dependencies.trace)

    def test_recovered_and_current_evaluations_are_required(self):
        self.dependencies.evaluations["current_count"] = 0
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_CANARY_SEMANTICS")

    def test_continuity_load_bearing_fields_are_required(self):
        for field in (
            "missing_binance_closed_minutes",
            "duplicate_binance_natural_keys",
            "binance_ohlcv_conflicts",
            "source_cursor_regressions",
            "last_event_id_regressions",
            "source_event_conflicts",
        ):
            with self.subTest(field=field):
                dependencies = _Dependencies()
                dependencies.continuity[field] = 1
                result = simulate_downtime.run_acceptance_cycle(
                    database_path=self.database,
                    dependencies=dependencies,
                    downtime_target_ms=600_000,
                )
                self.assertNotEqual(result["status"], "PASS")

    def test_outbox_replay_must_be_ordered_complete_and_consistent(self):
        self.dependencies.outbox["replayed_event_ids"] = [3, 2]
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_OUTBOX_REPLAY")

    def test_second_instance_must_exit_20(self):
        self.dependencies.second_exit = 1
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_SINGLE_INSTANCE_GATE")

    def test_final_cleanup_and_integrity_are_required(self):
        self.dependencies.final_stop["mutex_free"] = False
        result = self.run_cycle()
        self.assertEqual(result["status"], "BLOCKED_GRACEFUL_SHUTDOWN")

    def test_incomplete_status_is_not_reachable_after_live_ready(self):
        result = self.run_cycle()
        self.assertNotEqual(
            result["status"],
            "BLOCKED_ACCEPTANCE_RUNNER_INCOMPLETE",
        )

    def test_blocked_run_does_not_promote_canonical_artifacts(self):
        self.dependencies.continuity["source_event_conflicts"] = 1
        self.run_cycle()
        self.assertEqual(self.dependencies.promotions, 0)

    def test_pass_promotes_only_after_all_audits(self):
        self.run_cycle()
        self.assertEqual(self.dependencies.promotions, 1)
        self.assertEqual(self.dependencies.trace[-1], "pack/report promotion")


if __name__ == "__main__":
    unittest.main()
