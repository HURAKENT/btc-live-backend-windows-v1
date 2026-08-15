from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.performance_engine import CALCULATION_VERSION, StrategyPerformanceEngine
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StrategyPerformanceEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name) / "runtime.sqlite3")
        self.store.migrate()
        self.repository = PerformanceRepository(self.store)
        self.engine = StrategyPerformanceEngine(
            project_root=PROJECT_ROOT, repository=self.repository
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_historical_bootstrap_and_rebuild_are_idempotent(self) -> None:
        first = self.engine.bootstrap_historical()
        self.assertEqual((4_994, 0), (first.observation_inserted, first.observation_replayed))
        self.assertEqual((1_850, 0), (first.resolution_inserted, first.resolution_replayed))
        self.assertEqual("INSERTED", first.ingest_receipt_outcome)
        self.assertEqual(4_994, self.store.count("strategy_performance_observations"))
        self.assertEqual(1_850, self.store.count("strategy_performance_resolutions"))

        refresh = self.engine.refresh_all(as_of_date="2026-08-15")
        self.assertEqual({"inserted": 141, "replayed": 0}, refresh)
        materialized = self.repository.read_current_materialization(
            strategy_id="NO_A0", source_view="HISTORICAL",
            calculation_version=CALCULATION_VERSION,
        )
        self.assertIsNotNone(materialized)
        metrics = materialized["aggregates"][0].payload
        self.assertGreater(metrics["opportunity_count"], 0)
        self.assertEqual(metrics["resolved_signal_count"], metrics["wins"] + metrics["losses"])
        self.assertEqual(5_000_000, metrics["shares_micros"])

        second = self.engine.bootstrap_historical()
        self.assertEqual((0, 4_994), (second.observation_inserted, second.observation_replayed))
        self.assertEqual((0, 1_850), (second.resolution_inserted, second.resolution_replayed))
        self.assertEqual("REPLAYED", second.ingest_receipt_outcome)
        self.assertEqual({"inserted": 0, "replayed": 141},
                         self.engine.refresh_all(as_of_date="2026-08-15"))
        self.assertEqual(4_994, self.store.count("strategy_performance_observations"))
        self.assertEqual(1_850, self.store.count("strategy_performance_resolutions"))


if __name__ == "__main__":
    unittest.main()
