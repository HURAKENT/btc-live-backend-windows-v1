from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.performance_historical import (
    HistoricalPerformanceObservationBuilder,
    HistoricalSettlementReconciler,
    PinnedAhrArtifactLoader,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = PROJECT_ROOT.parent.parent
PINNED_RUN = "20260814T205244105513Z"


class PinnedAhrArtifactLoaderTests(unittest.TestCase):
    def test_rejects_acceptance_whose_bytes_do_not_match_the_pinned_hash(self) -> None:
        source_run = CANONICAL_ROOT / "reports" / "historical_revalidation" / PINNED_RUN
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / PINNED_RUN
            run_dir.mkdir()
            (run_dir / "ACCEPTANCE.json").write_bytes(
                source_run.joinpath("ACCEPTANCE.json").read_bytes() + b"\n"
            )
            with self.assertRaisesRegex(ValueError, "ACCEPTANCE_HASH_MISMATCH"):
                PinnedAhrArtifactLoader(project_root=PROJECT_ROOT, run_dir=run_dir).load()

    def test_loads_pinned_bundle_and_builds_all_observations_without_network(self) -> None:
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            bundle = PinnedAhrArtifactLoader(project_root=PROJECT_ROOT).load()
            observations = HistoricalPerformanceObservationBuilder(
                project_root=PROJECT_ROOT, bundle=bundle
            ).build()

        self.assertEqual(4_994, len(observations))
        self.assertEqual(1_850, sum(row.accepted for row in observations))
        self.assertEqual(3_144, sum(not row.accepted for row in observations))
        self.assertEqual(47, len({row.strategy_id for row in observations}))
        self.assertEqual({"HISTORICAL"}, {row.source_layer for row in observations})
        self.assertEqual(
            {"HISTORICAL_V1", "STRICT_A", "PF1", "VOLATILITY_V2"},
            {row.family for row in observations},
        )
        self.assertFalse(any(row.emitted for row in observations))
        self.assertTrue(all(
            row.shares_micros == (5_000_000 if row.accepted else 0)
            for row in observations
        ))
        self.assertTrue(all(
            row.performance_price_micros is None
            for row in observations if not row.accepted
        ))

        v2 = [row for row in observations if row.strategy_version == "V2"]
        self.assertEqual(694, len(v2))
        self.assertTrue(all(row.parent_strategy_id for row in v2))
        self.assertTrue(all(row.source_decision_identity for row in v2))
        self.assertEqual({"DISABLED_RESEARCH_ONLY"}, {row.activation_status for row in v2})
        self.assertEqual(
            {"V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY"},
            {row.activation_reason_code for row in v2},
        )

    def test_v1_accepted_price_uses_canonical_stress_contract(self) -> None:
        bundle = PinnedAhrArtifactLoader(project_root=PROJECT_ROOT).load()
        rows = HistoricalPerformanceObservationBuilder(
            project_root=PROJECT_ROOT, bundle=bundle
        ).build()
        target = next(
            row for row in rows
            if row.strategy_id == "NO_A0"
            and row.market_date == "2026-01-03"
            and row.accepted
        )
        self.assertEqual(695_000, target.performance_price_micros)
        self.assertEqual(
            "CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED",
            target.performance_price_basis,
        )
        self.assertEqual("127251", target.market_id)

    def test_reconciles_every_accepted_observation_at_five_shares(self) -> None:
        bundle = PinnedAhrArtifactLoader(project_root=PROJECT_ROOT).load()
        observations = HistoricalPerformanceObservationBuilder(
            project_root=PROJECT_ROOT, bundle=bundle
        ).build()
        resolutions = HistoricalSettlementReconciler(
            project_root=PROJECT_ROOT, bundle=bundle
        ).reconcile(observations)
        self.assertEqual(1_850, len(resolutions))
        self.assertEqual({row.observation_key for row in observations if row.accepted},
                         {row.observation_key for row in resolutions})
        self.assertTrue(all(row.shares_micros == 5_000_000 for row in resolutions))
        self.assertTrue(all(row.turnover_usd_micros == row.cost_usd_micros for row in resolutions))
        self.assertTrue(all(
            row.pnl_usd_micros == row.gross_payout_usd_micros - row.cost_usd_micros
            for row in resolutions
        ))
        target_observation = next(
            row for row in observations
            if row.strategy_id == "NO_A0" and row.market_date == "2026-01-03"
            and row.accepted
        )
        target_resolution = next(
            row for row in resolutions
            if row.observation_key == target_observation.observation_key
        )
        self.assertTrue(target_resolution.won)
        self.assertEqual(1_525_000, target_resolution.pnl_usd_micros)


if __name__ == "__main__":
    unittest.main()
