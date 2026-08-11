from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from src.fixed_point import ProbabilityMicros
from src.mvp_current_runtime import CurrentMarketSnapshotV1, StrictACurrentInputSource
from src.polymarket_provider import MarketBook
from tests.test_current_input import OBSERVED_AT_MS, _model_candles


class MvpCurrentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_strict_capture_builds_executable_input_and_evidence(self):
        books = []
        for index in range(11):
            book = MarketBook(f"yes-{index}")
            book.asks = {200_000 + index * 10_000: 6_000_000}
            book.book_hash = f"book-{index}"
            book.source_timestamp_ms = OBSERVED_AT_MS - 1_000
            books.append(book)
        snapshot = CurrentMarketSnapshotV1(
            market_id="market-1",
            market_date="2026-08-11",
            bucket_bounds=tuple(
                [(None, 99_500.0)]
                + [
                    (99_500.0 + index * 100.0, 99_600.0 + index * 100.0)
                    for index in range(9)
                ]
                + [(100_400.0, None)]
            ),
            yes_token_ids=tuple(f"yes-{index}" for index in range(11)),
            no_token_ids=tuple(f"no-{index}" for index in range(11)),
            yes_books=tuple(books),
            market_q_yes=tuple(
                ProbabilityMicros(500_000 if index == 5 else 50_000)
                for index in range(11)
            ),
            fee_schedules=tuple(
                {"rate": 0.07, "exponent": 1, "takerOnly": True}
                for _ in range(11)
            ),
            price_history_source_sha256="a" * 64,
            price_history_observed_at_ms=OBSERVED_AT_MS - 1_000,
        )

        async def candle_loader(_observed_at_ms):
            return _model_candles()

        async def market_loader(_observed_at_ms):
            return snapshot

        source = StrictACurrentInputSource(
            candle_loader=candle_loader,
            market_loader=market_loader,
            prior_position_loader=lambda _market_date: False,
            clock_ms=lambda: OBSERVED_AT_MS,
            contract_path=Path(
                "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
            ),
            paper_authorized=True,
        )
        due = SimpleNamespace(
            origin="CURRENT_LIVE_REEVALUATION",
            schedule=SimpleNamespace(
                strategy_id="YES_STRICT_A_OPERATIONAL",
                market_id="market-1",
                checkpoint_minutes=60,
                schedule_key="schedule-1",
            ),
        )

        result = await source.capture(due=due, current=True)

        self.assertIsNotNone(result.executable_input)
        self.assertTrue(result.historical_depth_available)
        self.assertEqual(len(result.executable_input.buckets), 11)
        evidence = source.evidence_for("schedule-1")
        self.assertEqual(evidence.bucket_index, 5)
        self.assertEqual(evidence.token_id, "yes-5")
        self.assertGreater(evidence.fee_per_share_usd_micros, 0)

    async def test_pf1_and_recovered_inputs_remain_fail_closed(self):
        async def unavailable(_observed_at_ms):
            raise AssertionError("provider must not be called")

        source = StrictACurrentInputSource(
            candle_loader=unavailable,
            market_loader=unavailable,
            prior_position_loader=lambda _market_date: False,
            clock_ms=lambda: OBSERVED_AT_MS,
            contract_path=Path(
                "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
            ),
            paper_authorized=True,
        )
        pf1 = SimpleNamespace(
            origin="CURRENT_LIVE_REEVALUATION",
            schedule=SimpleNamespace(strategy_id="YES_PF1_OPERATIONAL"),
        )
        recovered = SimpleNamespace(
            origin="RECOVERED_AFTER_DOWNTIME",
            schedule=SimpleNamespace(strategy_id="YES_STRICT_A_T60"),
        )
        self.assertEqual(
            (await source.capture(due=pf1, current=True)).reason_code,
            "BLOCKED_MISSING_PRODUCTION_MODEL_BUNDLE",
        )
        self.assertEqual(
            (await source.capture(due=recovered, current=False)).reason_code,
            "RECOVERED_EXECUTION_FORBIDDEN",
        )

    async def test_provider_and_malformed_bucket_metadata_fail_closed(self):
        async def candles(_observed_at_ms):
            return _model_candles()

        async def unavailable_market(_observed_at_ms):
            raise RuntimeError("public provider unavailable")

        source = StrictACurrentInputSource(
            candle_loader=candles,
            market_loader=unavailable_market,
            prior_position_loader=lambda _market_date: False,
            clock_ms=lambda: OBSERVED_AT_MS,
            contract_path=Path(
                "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
            ),
            paper_authorized=True,
        )
        due = SimpleNamespace(
            origin="CURRENT_LIVE_REEVALUATION",
            schedule=SimpleNamespace(
                strategy_id="YES_STRICT_A_OPERATIONAL",
                market_id="market-1",
                checkpoint_minutes=60,
                schedule_key="schedule-1",
            ),
        )
        self.assertEqual(
            (await source.capture(due=due, current=True)).reason_code,
            "CURRENT_PROVIDER_UNAVAILABLE",
        )

        books = []
        for index in range(11):
            book = MarketBook(f"yes-{index}")
            book.asks = {200_000: 6_000_000}
            book.book_hash = f"book-{index}"
            book.source_timestamp_ms = OBSERVED_AT_MS - 1_000
            books.append(book)
        malformed = CurrentMarketSnapshotV1(
            market_id="market-1",
            market_date="2026-08-11",
            bucket_bounds=tuple((None, None) for _ in range(11)),
            yes_token_ids=tuple(f"yes-{index}" for index in range(11)),
            no_token_ids=tuple(f"no-{index}" for index in range(11)),
            yes_books=tuple(books),
            market_q_yes=tuple(ProbabilityMicros(90_000) for _ in range(11)),
            fee_schedules=tuple(
                {"rate": 0.07, "exponent": 1, "takerOnly": True}
                for _ in range(11)
            ),
            price_history_source_sha256="a" * 64,
            price_history_observed_at_ms=OBSERVED_AT_MS - 1_000,
        )

        async def malformed_market(_observed_at_ms):
            return malformed

        source = StrictACurrentInputSource(
            candle_loader=candles,
            market_loader=malformed_market,
            prior_position_loader=lambda _market_date: False,
            clock_ms=lambda: OBSERVED_AT_MS,
            contract_path=Path(
                "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
            ),
            paper_authorized=True,
        )
        self.assertEqual(
            (await source.capture(due=due, current=True)).reason_code,
            "INVALID_BUCKET_BOUNDS",
        )


if __name__ == "__main__":
    unittest.main()
