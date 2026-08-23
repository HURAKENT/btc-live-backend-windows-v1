from __future__ import annotations

import unittest
from pathlib import Path

from src.performance_reconstruction import (
    RecoveredMarketStrategyInput,
    RecoveredStrategyReconstructor,
    build_recovered_market_strategy_input,
    TerminalDistributionRecoveryContract,
    build_terminal_feature,
    sample_point_at_or_before,
    terminal_bucket_probabilities,
)
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore
from src.strategy_v1 import BucketInput
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PerformanceReconstructionTests(unittest.TestCase):
    def test_price_history_sampler_never_selects_future_point(self) -> None:
        points = [
            {"timestamp_ms": 999, "price": 0.40},
            {"timestamp_ms": 1_000, "price": 0.41},
            {"timestamp_ms": 1_001, "price": 0.99},
        ]
        self.assertEqual(
            sample_point_at_or_before(points, target_timestamp_ms=1_000),
            {"timestamp_ms": 1_000, "price": 0.41},
        )
        with self.assertRaisesRegex(ValueError, "CHECKPOINT_PRICE_HISTORY_MISSING"):
            sample_point_at_or_before(points, target_timestamp_ms=998)

    def test_terminal_feature_uses_only_candles_closed_before_checkpoint(self) -> None:
        checkpoint = 20_000_000
        candles = []
        for index in range(181):
            close_time = checkpoint - (181 - index) * 60_000 - 1
            candles.append({"close_time_ms": close_time, "close": 100.0 + index})
        candles.append({"close_time_ms": checkpoint, "close": 9_999.0})

        feature = build_terminal_feature(candles, checkpoint_timestamp_ms=checkpoint)

        self.assertEqual(feature.max_candle_time_ms, checkpoint - 60_000 - 1)
        self.assertEqual(feature.spot, 280.0)
        self.assertLess(feature.max_candle_time_ms, checkpoint)

    def test_frozen_contract_is_deterministic_and_normalized(self) -> None:
        contract = TerminalDistributionRecoveryContract.load(PROJECT_ROOT)
        feature = build_terminal_feature(
            [
                {"close_time_ms": 1_000_000 + index * 60_000, "close": 80_000.0 + index}
                for index in range(181)
            ],
            checkpoint_timestamp_ms=1_000_000 + 181 * 60_000,
        )
        buckets = (
            (None, 79_000.0),
            (79_000.0, 81_000.0),
            (81_000.0, None),
        )
        first = terminal_bucket_probabilities(
            contract, horizon_minutes=60, feature=feature, bucket_bounds=buckets
        )
        second = terminal_bucket_probabilities(
            contract, horizon_minutes=60, feature=feature, bucket_bounds=buckets
        )
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(first), 1.0, places=15)
        self.assertTrue(all(0.0 <= value <= 1.0 for value in first))

    def test_contract_records_full_frozen_baseline_parity(self) -> None:
        contract = TerminalDistributionRecoveryContract.load(PROJECT_ROOT)
        self.assertEqual(contract.baseline_parity["comparisons"], 14_960)
        self.assertEqual(contract.baseline_parity["difference_count"], 0)
        self.assertEqual(contract.baseline_parity["max_absolute_difference"], 0.0)

    def test_reconstructor_processes_47_identities_and_replays_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            repository = PerformanceRepository(store)
            bounds = tuple(
                [(None, 71_000.0)]
                + [(71_000.0 + index * 2_000.0, 73_000.0 + index * 2_000.0) for index in range(9)]
                + [(89_000.0, None)]
            )
            titles = tuple(
                ["<$71,000"]
                + [f"${71_000 + index * 2_000:,}-${73_000 + index * 2_000:,}" for index in range(9)]
                + [">$89,000"]
            )
            buckets = tuple(
                BucketInput(
                    bucket_index=index,
                    model_p=(0.40 if index == 5 else 0.06),
                    market_q_yes=(0.20 if index == 5 else 0.08),
                    market_q_no=(0.78 if index == 5 else 0.90),
                    vwap5=None,
                    confirmed_fee=0.0,
                    no_token_id=f"no-{index}",
                )
                for index in range(11)
            )
            recovered_input = RecoveredMarketStrategyInput.create(
                market_id="event-2026-07-08",
                market_date="2026-07-08",
                resolution_utc="2026-07-08T16:00:00+00:00",
                bucket_titles=titles,
                bucket_bounds=bounds,
                buckets_by_checkpoint={
                    checkpoint: buckets
                    for checkpoint in (30, 60, 120, 240, 360, 480, 720, 1080)
                },
                evidence_sha256="a" * 64,
            )
            reconstructor = RecoveredStrategyReconstructor(
                project_root=PROJECT_ROOT, repository=repository
            )

            first = reconstructor.reconstruct(recovered_input)
            second = reconstructor.reconstruct(recovered_input)

            self.assertEqual(first.identity_count, 47)
            self.assertEqual(first.v1_identity_count, 34)
            self.assertEqual(first.v2_data_gap_count, 13)
            self.assertEqual(len(repository.read_reconstruction_statuses()), 47)
            self.assertGreater(first.observation_inserted_count, 0)
            self.assertEqual(second.observation_inserted_count, 0)
            self.assertEqual(second.observation_replayed_count, first.observation_count)
            self.assertEqual(
                store.count("strategy_performance_observations"), first.observation_count
            )
            store.close()

    def test_market_input_builder_excludes_future_prices_and_candles(self) -> None:
        resolution_ms = 1_800_000_000
        market = {
            "event_id": "event",
            "market_date": "2026-07-08",
            "resolution_utc": "1970-01-21T20:00:00+00:00",
            "asset_ids": [value for index in range(11) for value in (f"yes-{index}", f"no-{index}")],
            "outcomes": [f"bucket-{index}" for index in range(11)],
            "bucket_bounds": [[None, 79_000.0]] + [[79_000.0 + 2_000 * index, 81_000.0 + 2_000 * index] for index in range(9)] + [[97_000.0, None]],
        }
        market["resolution_utc"] = __import__("datetime").datetime.fromtimestamp(
            resolution_ms / 1000, tz=__import__("datetime").timezone.utc
        ).isoformat()
        histories = {}
        for asset_id in market["asset_ids"]:
            histories[asset_id] = (
                {"timestamp_ms": resolution_ms - 1_080 * 60_000, "price": 0.05},
                {"timestamp_ms": resolution_ms + 1, "price": 0.99},
            )
        candles = tuple(
            {
                "close_time_ms": resolution_ms - 1_080 * 60_000 - (181 - index) * 60_000 - 1,
                "close": 80_000.0 + index,
            }
            for index in range(181)
        ) + ({"close_time_ms": resolution_ms + 1, "close": 999_999.0},)

        built = build_recovered_market_strategy_input(
            market_payload=market,
            history_by_asset=histories,
            binance_candles=candles,
            contract=TerminalDistributionRecoveryContract.load(PROJECT_ROOT),
        )

        self.assertEqual(built.buckets_by_checkpoint[1080][0].market_q_yes, 0.05)
        self.assertNotEqual(built.buckets_by_checkpoint[1080][0].market_q_yes, 0.99)


if __name__ == "__main__":
    unittest.main()
