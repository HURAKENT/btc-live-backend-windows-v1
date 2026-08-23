from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from src.api import build_health_payload, create_api_app
from src.models import SignalRecord
from src.outbox import OutboxBroker
from src.performance_models import (
    AggregateRevision,
    AggregateRow,
    CatchupClassification,
    PerformanceObservation,
    TimeseriesRow,
    canonical_identity,
    canonical_sha256,
    materialization_children_sha256,
)
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CALCULATION_VERSION = "PERFORMANCE_METRICS_V1"


class PerformanceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temporary.name) / "runtime.sqlite3")
        self.store.migrate()
        self.repository = PerformanceRepository(self.store)
        self.read_store = self.store.open_read_store()
        self.client = TestClient(
            TestServer(create_api_app(self.read_store, OutboxBroker()))
        )
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.read_store.close()
        self.store.close()
        self.temporary.cleanup()

    async def test_strategies_list_all_47_and_return_not_ready_without_fabricated_zeroes(self) -> None:
        response = await self.client.get("/api/v1/performance/strategies")

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["schema_version"], "PERFORMANCE_QUERY_V1")
        self.assertEqual(payload["strategy_count"], 47)
        self.assertEqual(len(payload["strategies"]), 47)
        no_a0 = _find_strategy(payload["strategies"], "NO_A0")
        self.assertEqual(no_a0["registry_index"], 4)
        self.assertEqual(no_a0["version"], "V1")
        self.assertEqual(no_a0["views"]["HISTORICAL"]["status"], "NOT_READY")
        self.assertNotIn("opportunity_count", no_a0["views"]["HISTORICAL"])

        v2 = _find_strategy(payload["strategies"], "NO_A0_V2_VOL")
        self.assertEqual(v2["activation_status"], "DISABLED_RESEARCH_ONLY")
        self.assertEqual(
            v2["activation_reason_code"],
            "V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY",
        )
        self.assertEqual(v2["eligibility_labels"], ["RESEARCH_ONLY", "NOT_ROBUST"])

    async def test_strategy_detail_returns_backend_materialization_payloads_verbatim(self) -> None:
        expected_metrics = {
            "accepted_signal_count": 2,
            "calculation_version": CALCULATION_VERSION,
            "decision_coverage_ratio": "0.500000000000",
            "opportunity_count": 4,
            "pnl_usd_micros": 1_525_000,
            "resolved_signal_count": 1,
            "resolved_signal_denominator": 2,
            "roi_ratio": "0.438848920863",
            "shares_micros": 5_000_000,
            "source_view": "HISTORICAL",
            "turnover_usd_micros": 3_475_000,
            "wins": 1,
        }
        expected_cumulative = {
            "cumulative_pnl_usd_micros": 1_525_000,
            "observation_key": "obs:NO_A0:2026-01-03",
            "resolved_signal_count": 1,
        }
        self._publish_materialization(
            strategy_id="NO_A0",
            source_view="HISTORICAL",
            metrics=expected_metrics,
            timeseries_payload=expected_cumulative,
        )

        response = await self.client.get("/api/v1/performance/strategies/NO_A0")

        self.assertEqual(response.status, 200)
        payload = await response.json()
        historical = payload["views"]["HISTORICAL"]
        self.assertEqual(historical["status"], "READY")
        self.assertEqual(historical["metrics"], expected_metrics)
        self.assertEqual(historical["provenance"]["calculation_contract"], CALCULATION_VERSION)
        self.assertEqual(historical["timeseries"]["CUMULATIVE"][0], expected_cumulative)
        self.assertEqual(payload["views"]["FORWARD"]["status"], "NOT_READY")

    async def test_timeseries_observations_status_and_invalid_queries_are_read_only(self) -> None:
        self._publish_materialization(
            strategy_id="NO_A0",
            source_view="FORWARD",
            metrics={
                "accepted_signal_count": 1,
                "opportunity_count": 1,
                "pnl_usd_micros": -2_500_000,
                "resolved_signal_count": 1,
                "shares_micros": 5_000_000,
                "source_view": "FORWARD",
            },
            timeseries_payload={
                "cumulative_pnl_usd_micros": -2_500_000,
                "observation_key": "obs:NO_A0:fwd",
                "resolved_signal_count": 1,
            },
        )
        observation = _observation("obs:NO_A0:fwd", source_layer="FORWARD")
        self.repository.append_observation(observation)
        self.repository.append_catchup_classification(
            CatchupClassification.create(
                catchup_key="catchup:2026-07-08",
                market_date="2026-07-08",
                classification="DATA_GAP",
                reason_code="MISSING_CANONICAL_MARKET",
                market_id=None,
                source_event_identity=None,
                classified_at_ms=1,
                provenance={"source": "unit-test"},
            )
        )
        before_counts = _read_only_counts(self.store)

        timeseries = await self.client.get(
            "/api/v1/performance/strategies/NO_A0/timeseries?view=FORWARD"
        )
        observations = await self.client.get(
            "/api/v1/performance/strategies/NO_A0/observations?limit=1"
        )
        status = await self.client.get("/api/v1/performance/status")
        invalid = await self.client.get(
            "/api/v1/performance/strategies/NO_A0/timeseries?view=PORTFOLIO"
        )
        portfolio = await self.client.get("/api/v1/performance/strategies/ALL")

        self.assertEqual(timeseries.status, 200)
        self.assertEqual((await timeseries.json())["source_view"], "FORWARD")
        self.assertEqual(observations.status, 200)
        self.assertEqual((await observations.json())["observations"][0]["observation_key"], observation.observation_key)
        self.assertEqual(status.status, 200)
        status_payload = await status.json()
        self.assertEqual(status_payload["catchup"]["counts"], {"DATA_GAP": 1})
        self.assertEqual(status_payload["database_health"]["status"], "PASS")
        health = build_health_payload(
            self.read_store,
            {
                "state": "LIVE_READY",
                "live_ready": True,
                "source_health": (("binance", "LIVE"), ("polymarket", "LIVE")),
                "market_id": "market-current",
                "market_count": 11,
                "asset_count": 22,
                "last_event_id": 1,
                "failure": None,
            },
        )
        self.assertEqual(health["status"], "DEGRADED")
        self.assertEqual(health["performance"]["blocking_reason"], "DATA_GAP")
        self.assertEqual(invalid.status, 400)
        self.assertEqual(portfolio.status, 400)
        self.assertEqual(before_counts, _read_only_counts(self.store))

    def _publish_materialization(
        self,
        *,
        strategy_id: str,
        source_view: str,
        metrics: dict[str, object],
        timeseries_payload: dict[str, object],
    ) -> None:
        revision_key = canonical_identity(
            "test-performance-revision",
            [strategy_id, source_view, metrics],
        )
        aggregate = AggregateRow.create(
            aggregate_key=canonical_identity("test-performance-aggregate", [revision_key]),
            revision_key=revision_key,
            strategy_id=strategy_id,
            source_view=source_view,
            window_kind="ALL",
            window_key="ALL",
            calculation_version=CALCULATION_VERSION,
            payload=metrics,
        )
        timeseries = TimeseriesRow.create(
            timeseries_key=canonical_identity("test-performance-timeseries", [revision_key]),
            revision_key=revision_key,
            strategy_id=strategy_id,
            source_view=source_view,
            series_kind="CUMULATIVE",
            period_key=str(timeseries_payload["observation_key"]),
            calculation_version=CALCULATION_VERSION,
            observation_count=1,
            observation_sha256=canonical_sha256(timeseries_payload),
            payload=timeseries_payload,
        )
        revision = AggregateRevision.create(
            revision_key=revision_key,
            strategy_id=strategy_id,
            source_view=source_view,
            calculation_version=CALCULATION_VERSION,
            source_ledger_revision=1,
            source_ledger_sha256=canonical_sha256([strategy_id, source_view]),
            generated_at_ms=1,
            provenance={"calculation_contract": CALCULATION_VERSION},
            aggregate_count=1,
            timeseries_count=1,
            children_sha256=materialization_children_sha256([aggregate], [timeseries]),
        )
        self.repository.publish_materialization(revision, [aggregate], [timeseries])


def _observation(observation_key: str, *, source_layer: str) -> PerformanceObservation:
    return PerformanceObservation.create(
        observation_key=observation_key,
        logical_decision_key=f"decision:{observation_key}",
        source_layer=source_layer,
        strategy_id="NO_A0",
        strategy_version="V1",
        family="CONTROL",
        registry_index=4,
        activation_status="DISABLED_MISSING_EXECUTION_DATA",
        activation_reason_code="HISTORICAL_INPUT_HAS_NO_LIVE_DEPTH_FEE_CONTRACT",
        market_id="btc-range-2026-08-15",
        market_date="2026-08-15",
        evaluation_key=f"evaluation:{observation_key}",
        signal_identity_key=None,
        parent_strategy_id=None,
        source_decision_identity=None,
        checkpoint_minutes=60,
        horizon="T60",
        side="NO",
        selected_buckets=("bucket-3",),
        accepted=True,
        emitted=False,
        reason_code="SIGNAL_ACCEPTED",
        reference_price_micros=665_000,
        performance_price_micros=695_000,
        performance_price_basis="CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED",
        scoring_status="RESOLUTION_PENDING",
        scoring_reason_code="SETTLEMENT_PENDING",
        observed_at_ms=1,
        source_created_at_ms=None,
        provenance_run_id="unit-test",
        source_result_sha256="a" * 64,
        input_sha256="b" * 64,
    )


def _find_strategy(strategies: list[dict[str, object]], strategy_id: str) -> dict[str, object]:
    matches = [row for row in strategies if row["strategy_id"] == strategy_id]
    if len(matches) != 1:
        raise AssertionError(strategy_id)
    return matches[0]


def _read_only_counts(store: SqliteStore) -> dict[str, int]:
    return {
        table: store.count(table)
        for table in (
            "strategy_performance_observations",
            "strategy_performance_resolutions",
            "strategy_performance_ingest_runs",
            "strategy_performance_cursors",
            "strategy_performance_catchup",
            "strategy_performance_materialization_revisions",
            "strategy_performance_aggregates",
            "strategy_performance_timeseries",
            "outbox_events",
        )
    }


if __name__ == "__main__":
    unittest.main()
