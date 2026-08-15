from __future__ import annotations

import unittest
from datetime import date

from src.performance_metrics import build_metrics
from src.performance_models import (
    PerformanceObservation,
    PerformanceResolution,
    five_share_economics,
    v1_performance_price,
    v2_performance_price,
)


class PerformanceMetricsTests(unittest.TestCase):
    @staticmethod
    def observation(
        key: str,
        logical: str,
        market_date: str,
        checkpoint: int,
        *,
        accepted: bool,
        price: int | None,
        source_layer: str = "HISTORICAL",
        emitted: bool = False,
        semantic_variant: str = "SAME",
        scoring_status: str | None = None,
    ) -> PerformanceObservation:
        if scoring_status is None:
            scoring_status = (
                "REJECTED"
                if not accepted
                else "RESOLUTION_PENDING"
                if price is not None and price > 0
                else "UNSCORABLE"
            )
        return PerformanceObservation.create(
            observation_key=key,
            logical_decision_key=logical,
            source_layer=source_layer,
            strategy_id="NO_A0",
            strategy_version="V1",
            family="CONTROL",
            registry_index=1,
            activation_status="ENABLED_RESEARCH",
            activation_reason_code="CANONICAL_V1",
            market_id=f"market:{market_date}",
            market_date=market_date,
            evaluation_key=f"evaluation:{key}",
            signal_identity_key=f"signal:{key}" if emitted else None,
            parent_strategy_id=None,
            source_decision_identity=semantic_variant,
            checkpoint_minutes=checkpoint,
            horizon=f"T{checkpoint}",
            side="NO",
            selected_buckets=("bucket-3",),
            accepted=accepted,
            emitted=emitted,
            reason_code="ACCEPTED" if accepted else "FILTER_REJECTED",
            reference_price_micros=price if accepted else None,
            performance_price_micros=price if accepted else None,
            performance_price_basis=(
                "CONTRACT_STRESSED_REFERENCE_EXPLICIT"
                if accepted and price is not None
                else "NOT_APPLICABLE"
            ),
            scoring_status=scoring_status,
            scoring_reason_code=(
                "SETTLEMENT_PENDING"
                if scoring_status == "RESOLUTION_PENDING"
                else "EVALUATOR_REJECTED"
                if scoring_status == "REJECTED"
                else "UNSCORABLE_MISSING_PRICE"
            ),
            observed_at_ms=1,
            source_created_at_ms=None,
            provenance_run_id="run",
            source_result_sha256="a" * 64,
            input_sha256="b" * 64,
        )

    @staticmethod
    def resolution(
        observation: PerformanceObservation,
        *,
        won: bool,
        resolution_date: str | None = None,
    ) -> PerformanceResolution:
        return PerformanceResolution.create_for_observation(
            observation,
            resolution_key=f"resolution:{observation.observation_key}",
            revision=1,
            supersedes_resolution_key=None,
            settlement_identity=f"settlement:{observation.market_date}",
            settlement_source_event_id=None,
            winning_bucket_identity="bucket-7",
            won=won,
            resolution_date=resolution_date or observation.market_date,
            resolved_at_ms=None,
            provenance_json={"source": "canonical-settlement"},
        )

    def test_price_basis_rules_and_exact_five_share_economics(self) -> None:
        self.assertEqual(
            v1_performance_price(
                stressed_reference_cost_micros=610_000,
                market_probability_micros=665_000,
                selected_leg_count=2,
            ),
            (610_000, "CONTRACT_STRESSED_REFERENCE_EXPLICIT"),
        )
        self.assertEqual(
            v1_performance_price(
                stressed_reference_cost_micros=None,
                market_probability_micros=665_000,
                selected_leg_count=1,
            ),
            (695_000, "CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED"),
        )
        self.assertEqual(
            v1_performance_price(
                stressed_reference_cost_micros=None,
                market_probability_micros=970_000,
                selected_leg_count=2,
            ),
            (1_000_000, "CONTRACT_STRESSED_REFERENCE_RECONSTRUCTED"),
        )
        self.assertEqual(
            v2_performance_price(
                stressed_q_3c_micros=720_000,
                actual_price_micros=100_000,
            ),
            (720_000, "CONTRACT_V2_STRESSED_Q_3C"),
        )
        self.assertEqual(
            five_share_economics(695_000, won=True),
            {
                "shares_micros": 5_000_000,
                "cost_usd_micros": 3_475_000,
                "gross_payout_usd_micros": 5_000_000,
                "pnl_usd_micros": 1_525_000,
                "turnover_usd_micros": 3_475_000,
            },
        )
        self.assertEqual(five_share_economics(695_000, won=False)["pnl_usd_micros"], -3_475_000)

    def test_build_metrics_exact_counts_ratios_economics_drawdown_and_streaks(self) -> None:
        rejected = self.observation("o0", "d0", "2026-01-01", 60, accepted=False, price=None)
        winner = self.observation("o1", "d1", "2026-01-02", 30, accepted=True, price=695_000)
        loser = self.observation("o2", "d2", "2026-01-03", 60, accepted=True, price=400_000)
        pending = self.observation("o3", "d3", "2026-01-04", 60, accepted=True, price=300_000)
        unscorable = self.observation("o4", "d4", "2026-01-05", 60, accepted=True, price=None)
        metrics = build_metrics(
            [unscorable, pending, loser, winner, rejected],
            [self.resolution(loser, won=False), self.resolution(winner, won=True)],
            source_view="HISTORICAL",
            as_of_date=date(2026, 1, 31),
        )

        self.assertEqual(metrics["opportunity_count"], 5)
        self.assertEqual(metrics["accepted_signal_count"], 4)
        self.assertEqual(metrics["emitted_signal_count"], 0)
        self.assertEqual(metrics["priced_signal_count"], 3)
        self.assertEqual(metrics["resolved_signal_count"], 2)
        self.assertEqual(metrics["unresolved_signal_count"], 2)
        self.assertEqual(metrics["unscorable_signal_count"], 1)
        self.assertEqual((metrics["wins"], metrics["losses"]), (1, 1))
        self.assertEqual(metrics["win_rate_ratio"], "0.500000000000")
        self.assertEqual(metrics["turnover_usd_micros"], 5_475_000)
        self.assertEqual(metrics["gross_payout_usd_micros"], 5_000_000)
        self.assertEqual(metrics["pnl_usd_micros"], -475_000)
        self.assertEqual(metrics["roi_ratio"], "-0.086757990868")
        self.assertEqual(metrics["average_price_micros"], "465000.000000000000")
        self.assertEqual(metrics["average_winner_return_ratio"], "0.438848920863")
        self.assertEqual(metrics["average_loser_return_ratio"], "-1.000000000000")
        self.assertEqual(metrics["max_drawdown_usd_micros"], 2_000_000)
        self.assertEqual(metrics["current_drawdown_usd_micros"], 2_000_000)
        self.assertEqual(metrics["longest_win_streak"], 1)
        self.assertEqual(metrics["longest_loss_streak"], 1)
        self.assertEqual(metrics["first_signal_date"], "2026-01-02")
        self.assertEqual(metrics["last_signal_date"], "2026-01-05")
        self.assertEqual(metrics["last_resolved_date"], "2026-01-03")
        self.assertEqual(metrics["decision_coverage_ratio"], "0.800000000000")
        self.assertEqual(metrics["resolution_coverage_ratio"], "0.500000000000")
        self.assertEqual(metrics["price_coverage_ratio"], "0.750000000000")
        self.assertIsNone(metrics["annualized_return_ratio"])
        self.assertEqual(
            metrics["annualized_return_reason_code"],
            "INSUFFICIENT_365_DAY_RESOLVED_HISTORY",
        )

        self.assertEqual(
            [point["observation_key"] for point in metrics["cumulative_series"]],
            ["o1", "o2"],
        )
        self.assertEqual(
            [point["cumulative_pnl_usd_micros"] for point in metrics["cumulative_series"]],
            [1_525_000, -475_000],
        )
        self.assertEqual(metrics["monthly_series"][0]["month"], "2026-01")
        self.assertEqual(metrics["monthly_series"][0]["pnl_usd_micros"], -475_000)
        for window in ("30D", "90D", "365D"):
            self.assertEqual(metrics["rolling_windows"][window]["resolved_signal_count"], 2)

    def test_financial_order_uses_checkpoint_desc_then_key_not_resolution_timestamp(self) -> None:
        low_checkpoint = self.observation("z-key", "d1", "2026-02-01", 30, accepted=True, price=200_000)
        high_checkpoint = self.observation("b-key", "d2", "2026-02-01", 60, accepted=True, price=800_000)
        same_checkpoint_a = self.observation("a-key", "d3", "2026-02-01", 30, accepted=True, price=500_000)
        resolutions = [
            self.resolution(low_checkpoint, won=True),
            self.resolution(high_checkpoint, won=False),
            self.resolution(same_checkpoint_a, won=True),
        ]

        metrics = build_metrics(
            [low_checkpoint, same_checkpoint_a, high_checkpoint],
            list(reversed(resolutions)),
            source_view="HISTORICAL",
            as_of_date="2026-02-01",
        )
        self.assertEqual(
            [point["observation_key"] for point in metrics["cumulative_series"]],
            ["b-key", "a-key", "z-key"],
        )

    def test_monthly_and_rolling_windows_use_utc_calendar_days_inclusively(self) -> None:
        dates = ("2025-01-30", "2025-05-01", "2025-05-02", "2026-01-01", "2026-01-30")
        observations = [
            self.observation(f"o{i}", f"d{i}", value, 60, accepted=True, price=500_000)
            for i, value in enumerate(dates)
        ]
        resolutions = [self.resolution(row, won=True) for row in observations]
        metrics = build_metrics(
            observations,
            resolutions,
            source_view="HISTORICAL",
            as_of_date="2026-01-30",
        )

        self.assertEqual(metrics["rolling_windows"]["30D"]["resolved_signal_count"], 2)
        self.assertEqual(metrics["rolling_windows"]["90D"]["resolved_signal_count"], 2)
        self.assertEqual(metrics["rolling_windows"]["365D"]["resolved_signal_count"], 4)
        self.assertEqual(
            [row["month"] for row in metrics["monthly_series"]],
            ["2025-01", "2025-05", "2026-01"],
        )
        self.assertEqual(set(metrics["rolling_series"]), {"30D", "90D", "365D"})
        self.assertEqual(len(metrics["rolling_series"]["30D"]), len(observations))

    def test_combined_deduplicates_identical_semantics_prefers_forward_and_fails_on_conflict(self) -> None:
        historical = self.observation("h", "same", "2026-03-01", 60, accepted=True, price=500_000)
        forward = self.observation(
            "f", "same", "2026-03-01", 60,
            accepted=True, price=500_000, source_layer="FORWARD", emitted=True,
        )
        metrics = build_metrics(
            [historical, forward],
            [self.resolution(historical, won=True), self.resolution(forward, won=True)],
            source_view="COMBINED",
            as_of_date="2026-03-01",
        )
        self.assertEqual(metrics["historical_raw_observation_count"], 1)
        self.assertEqual(metrics["forward_raw_observation_count"], 1)
        self.assertEqual(metrics["identical_overlap_count"], 1)
        self.assertEqual(metrics["effective_observation_count"], 1)
        self.assertEqual(metrics["accepted_signal_count"], 1)
        self.assertEqual(metrics["emitted_signal_count"], 1)
        self.assertEqual(metrics["cumulative_series"][0]["observation_key"], "f")

        conflict = self.observation(
            "f-conflict", "same", "2026-03-01", 60,
            accepted=True, price=600_000, source_layer="FORWARD", emitted=True,
        )
        with self.assertRaisesRegex(ValueError, "COMBINED_CROSS_LAYER_CONFLICT"):
            build_metrics(
                [historical, conflict],
                [],
                source_view="COMBINED",
                as_of_date="2026-03-01",
            )

    def test_zero_denominators_return_null_ratios_and_explicit_reason(self) -> None:
        metrics = build_metrics([], [], source_view="FORWARD", as_of_date="2026-04-01")
        for key in (
            "win_rate_ratio",
            "roi_ratio",
            "average_price_micros",
            "average_winner_return_ratio",
            "average_loser_return_ratio",
            "decision_coverage_ratio",
            "resolution_coverage_ratio",
            "price_coverage_ratio",
            "annualized_return_ratio",
        ):
            self.assertIsNone(metrics[key])
        self.assertEqual(metrics["annualized_return_reason_code"], "NO_RESOLVED_SIGNALS")

        orphan = self.observation(
            "orphan", "orphan-decision", "2026-04-01", 60,
            accepted=True, price=500_000,
        )
        with self.assertRaisesRegex(
            ValueError,
            "PERFORMANCE_RESOLUTION_OBSERVATION_MISSING",
        ):
            build_metrics(
                [],
                [self.resolution(orphan, won=True)],
                source_view="HISTORICAL",
                as_of_date="2026-04-01",
            )


if __name__ == "__main__":
    unittest.main()
