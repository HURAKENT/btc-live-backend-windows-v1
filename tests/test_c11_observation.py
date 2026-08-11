from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import socket
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

import c11_observation
from src.c11_observation import (
    EXPECTED_DURATION_SECONDS,
    build_report_skeleton,
    evaluate_future_acceptance,
    generate_run_id,
    update_heartbeat,
)
from tests.support.fake_provider_servers import FakeProviderServer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class C11ObservationContractTests(unittest.TestCase):
    def test_run_id_is_reproducible_for_inputs_and_changes_with_start_time(self):
        database_path = Path("C:/runtime/accepted.sqlite3")
        first = generate_run_id(
            started_at_ns=1_700_000_000_000_000_000,
            integration_commit="a" * 40,
            database_path=database_path,
        )
        replay = generate_run_id(
            started_at_ns=1_700_000_000_000_000_000,
            integration_commit="a" * 40,
            database_path=database_path,
        )
        later = generate_run_id(
            started_at_ns=1_700_000_000_000_000_001,
            integration_commit="a" * 40,
            database_path=database_path,
        )

        self.assertEqual(first, replay)
        self.assertNotEqual(first, later)
        self.assertRegex(first, r"^c11-\d{8}T\d{6}Z-[0-9a-f]{12}$")

    def test_future_gate_requires_real_time_rollover_restarts_outage_and_integrity(self):
        report = build_report_skeleton(
            run_id="c11-20260812T000000Z-123456789abc",
            started_at="2026-08-12T00:00:00Z",
            integration_commit="b" * 40,
            config_identity={"schema_version": "BTC_DAILY_RANGE_MVP_RUNTIME_V1"},
            database_path=Path("C:/runtime/accepted.sqlite3"),
            log_path=Path("C:/observations/run/backend.log"),
            host_runtime={"os": "Windows", "python": "3.12.10"},
            security_state={
                "trading_approval": False,
                "real_orders": False,
                "wallet": False,
                "signing": False,
                "authenticated_clob_writes": False,
            },
        )
        initial = evaluate_future_acceptance(report)
        self.assertEqual(initial["status"], "BLOCKED")
        self.assertIn("REAL_ELAPSED_48H_NOT_REACHED", initial["blockers"])

        report.update(
            {
                "ended_at": "2026-08-14T00:00:00Z",
                "elapsed_seconds": EXPECTED_DURATION_SECONDS,
                "actual_duration_hours": 48.0,
                "process_restarts": 2,
                "source_outages": 1,
                "recoveries": 3,
                "market_rollovers": 1,
                "duplicate_violations": 0,
                "accounting_violations": 0,
                "fatal_errors": 0,
                "database_quick_check": "ok",
                "heartbeat_max_gap_seconds": 120.0,
                "final_report_from_real_observations": True,
            }
        )
        accepted = evaluate_future_acceptance(report)
        self.assertEqual(accepted, {"status": "PASS", "blockers": []})

        report["process_restarts"] = 3
        unexpected_restart = evaluate_future_acceptance(report)
        self.assertIn(
            "EXACTLY_TWO_PLANNED_RESTARTS_NOT_OBSERVED",
            unexpected_restart["blockers"],
        )

    def test_heartbeat_keeps_cumulative_monotonic_max_gap(self):
        state = {
            "health_snapshots": 0,
            "last_heartbeat_monotonic": None,
            "heartbeat_max_gap_seconds": None,
        }

        update_heartbeat(state, observed_monotonic=100.0)
        update_heartbeat(state, observed_monotonic=160.0)
        update_heartbeat(state, observed_monotonic=520.0)

        self.assertEqual(state["health_snapshots"], 3)
        self.assertEqual(state["last_heartbeat_monotonic"], 520.0)
        self.assertEqual(state["heartbeat_max_gap_seconds"], 360.0)


@unittest.skipUnless(os.name == "nt", "native Windows observation required")
class C11ObservationDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.observation_root = self.root / "observations"
        self.database_path = self.root / "runtime.sqlite3"
        self.endpoints_path = self.root / "endpoints.json"
        self.server = FakeProviderServer(
            initial_book_batch=True,
            enable_rollover=True,
        )
        await self.server.start()
        self.api_port = self._free_port()
        self.endpoints_path.write_text(
            json.dumps(self.server.endpoint_payload(self.api_port)),
            encoding="utf-8",
        )
        self.run_dir: Path | None = None

    async def asyncTearDown(self):
        if self.run_dir is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self._invoke,
                    "stop",
                    "--run-dir",
                    str(self.run_dir),
                    "--timeout-seconds",
                    "30",
                )
        await self.server.close()
        self.temp.cleanup()

    async def test_short_real_backend_dry_run_status_and_finalize(self):
        started = await asyncio.to_thread(
            self._invoke,
            "start",
            "--observation-root",
            str(self.observation_root),
            "--database-path",
            str(self.database_path),
            "--integration-endpoints",
            str(self.endpoints_path),
            "--poll-seconds",
            "1",
        )
        self.assertEqual(started["status"], "STARTED")
        self.run_dir = Path(started["run_dir"])

        deadline = time.monotonic() + 45
        status = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            status = await asyncio.to_thread(
                self._invoke, "status", "--run-dir", str(self.run_dir)
            )
            if status["backend_alive"] and status["health_status"] == "PASS":
                break
        else:
            self.fail(f"C11 dry-run backend did not reach PASS: {status}")

        self.assertEqual(status["run_id"], started["run_id"])
        self.assertEqual(status["source_health"], {
            "binance": "LIVE",
            "polymarket": "LIVE",
        })
        self.assertIsNotNone(status["paper_account"])
        self.assertGreater(status["elapsed_seconds"], 0)

        restarted = await asyncio.to_thread(
            self._invoke,
            "restart",
            "--run-dir",
            str(self.run_dir),
            "--timeout-seconds",
            "45",
        )
        self.assertEqual(restarted["process_restarts"], 1)
        self.assertTrue(restarted["backend_alive"])
        self.assertEqual(restarted["health_status"], "PASS")

        finalized = await asyncio.to_thread(
            self._invoke,
            "stop",
            "--run-dir",
            str(self.run_dir),
            "--timeout-seconds",
            "30",
        )
        report = json.loads(
            (self.run_dir / "observation_report.json").read_text(encoding="utf-8")
        )
        events = [
            json.loads(line)
            for line in (self.run_dir / "observation.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

        self.assertEqual(finalized["controller_status"], "FINALIZED")
        self.assertFalse(finalized["backend_alive"])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("REAL_ELAPSED_48H_NOT_REACHED", report["blockers"])
        self.assertLess(report["actual_duration_hours"], 1.0)
        self.assertTrue(report["final_report_from_real_observations"])
        self.assertEqual(report["security_state"], {
            "authenticated_clob_writes": False,
            "real_orders": False,
            "signing": False,
            "trading_approval": False,
            "wallet": False,
        })
        self.assertGreater(report["health_snapshots"], 0)
        self.assertEqual(report["recoveries"], 1)
        self.assertIn("strict_a_evaluations", report)
        self.assertIn("paper_readiness_checks", report)
        self.assertIn("paper_positions", report)
        self.assertIsNotNone(report["paper_account"])
        self.assertIn("observation.started", {event["event"] for event in events})
        self.assertIn(
            "backend.restart_recovered", {event["event"] for event in events}
        )
        self.assertIn("backend.clean_stop", {event["event"] for event in events})
        self.assertIn("observation.finalized", {event["event"] for event in events})

    @staticmethod
    def _invoke(*arguments: str) -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = c11_observation.main(list(arguments))
        if exit_code != 0:
            raise AssertionError(
                f"C11 command failed: {arguments}; output={stdout.getvalue()}"
            )
        return json.loads(stdout.getvalue().strip().splitlines()[-1])

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
