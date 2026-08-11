from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from src.current_input import (
    CurrentInputCaptureV1,
    CurrentExecutionEvidenceV1,
    CurrentInputBlocked,
    FeeEvidenceV1,
    assert_current_model_enabled,
    build_executable_checkpoint_input,
    exact_vwap5,
    fee_evidence_from_public_schedule,
    frozen_model_probabilities,
    authorize_current_model_for_paper,
    validate_closed_candles,
)
from src.fixed_point import ProbabilityMicros
from src.models import SourceEvent
from src.polymarket_provider import MarketBook
from src.strategy_dispatch import V1ExecutableCheckpointInput
from src.strategy_v1 import StrictPriceHistoryEvidence


MINUTE_MS = 60_000
OBSERVED_AT_MS = 2_000_000_040_000
LAST_OPEN_MS = 1_999_999_980_000
FIRST_OPEN_MS = LAST_OPEN_MS - 180 * MINUTE_MS
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _candle(open_time_ms: int, close: int = 100_001) -> SourceEvent:
    payload = {
        "close": str(close),
        "close_time_ms": open_time_ms + MINUTE_MS - 1,
        "high": "100002",
        "interval": "1m",
        "low": "99999",
        "number_of_trades": 1,
        "open": "100000",
        "open_time_ms": open_time_ms,
        "quote_asset_volume": "100001",
        "symbol": "BTCUSDT",
        "taker_buy_base_asset_volume": "0.5",
        "taker_buy_quote_asset_volume": "50000.5",
        "volume": "1",
    }
    return SourceEvent.binance_closed_kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=open_time_ms,
        payload=json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode(),
        received_timestamp_ms=open_time_ms + MINUTE_MS - 1,
        recovery_origin="REST_BACKFILL",
    )


def _candles(count: int = 181) -> tuple[SourceEvent, ...]:
    return tuple(
        _candle(FIRST_OPEN_MS + index * MINUTE_MS)
        for index in range(count)
    )


def _model_candles() -> tuple[SourceEvent, ...]:
    return tuple(
        _candle(FIRST_OPEN_MS + index * MINUTE_MS, 99_900 + (index % 17) * 13)
        for index in range(181)
    )


class ClosedCandleWindowTests(unittest.TestCase):
    def test_accepts_exactly_181_contiguous_latest_closed_candles(self):
        window = validate_closed_candles(
            _candles(), observed_at_ms=OBSERVED_AT_MS
        )

        self.assertEqual(window.closed_candle_count, 181)
        self.assertEqual(window.candle_first_open_time_ms, FIRST_OPEN_MS)
        self.assertEqual(window.candle_last_open_time_ms, LAST_OPEN_MS)
        expected = hashlib.sha256(
            json.dumps(
                [json.loads(item.payload_json) for item in _candles()],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.assertEqual(window.candles_sha256, expected)

    def test_missing_candle_fails_closed(self):
        with self.assertRaisesRegex(
            CurrentInputBlocked, "MISSING_181_CLOSED_CANDLES"
        ):
            validate_closed_candles(
                _candles(180), observed_at_ms=OBSERVED_AT_MS
            )

    def test_gap_fails_closed(self):
        candles = list(_candles())
        candles[90] = _candle(candles[90].source_timestamp_ms + MINUTE_MS)
        with self.assertRaisesRegex(
            CurrentInputBlocked, "MISSING_181_CLOSED_CANDLES"
        ):
            validate_closed_candles(
                tuple(candles), observed_at_ms=OBSERVED_AT_MS
            )

    def test_stale_last_candle_fails_closed(self):
        with self.assertRaisesRegex(CurrentInputBlocked, "MODEL_INPUT_INVALID"):
            validate_closed_candles(
                _candles(), observed_at_ms=OBSERVED_AT_MS + MINUTE_MS
            )

    def test_open_or_future_candle_fails_closed(self):
        with self.assertRaisesRegex(CurrentInputBlocked, "MODEL_INPUT_INVALID"):
            validate_closed_candles(
                _candles(), observed_at_ms=LAST_OPEN_MS + 30_000
            )

    def test_versioned_paper_authorization_does_not_enable_real_trading(self):
        bundle_hash = authorize_current_model_for_paper(
            Path("strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"),
            paper_authorized=True,
            trading_approval=False,
            real_orders=False,
            wallet=False,
            signing=False,
        )
        self.assertEqual(len(bundle_hash), 64)

    def test_frozen_model_produces_normalized_eleven_bucket_probabilities(self):
        bounds = tuple(
            [(None, 99_500.0)]
            + [(99_500.0 + index * 100.0, 99_600.0 + index * 100.0) for index in range(9)]
            + [(100_400.0, None)]
        )
        probabilities = frozen_model_probabilities(
            _model_candles(),
            observed_at_ms=OBSERVED_AT_MS,
            checkpoint_minutes=60,
            bucket_bounds=bounds,
            contract_path=Path(
                "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
            ),
            paper_authorized=True,
        )
        self.assertEqual(len(probabilities), 11)
        self.assertEqual(sum(item.value for item in probabilities), 1_000_000)

    def test_public_fee_schedule_uses_documented_taker_formula_and_provenance(self):
        evidence = fee_evidence_from_public_schedule(
            token_id="yes-token",
            price_micros=500_000,
            fee_schedule={"rate": 0.07, "exponent": 1, "takerOnly": True},
        )
        self.assertEqual(evidence.fee_per_share_usd_micros, 17_500)
        self.assertIn("POLYMARKET_PUBLIC_FEE_SCHEDULE", evidence.provenance)

    def test_unknown_fee_curve_fails_closed(self):
        with self.assertRaisesRegex(CurrentInputBlocked, "MISSING_FEE_PROVENANCE"):
            fee_evidence_from_public_schedule(
                token_id="yes-token",
                price_micros=500_000,
                fee_schedule={"rate": 0.02, "exponent": 2, "takerOnly": True},
            )


class ExecutionEvidenceTests(unittest.TestCase):
    def test_exact_vwap5_uses_actual_ask_depth(self):
        book = MarketBook("yes-token")
        book.asks = {200_000: 2_000_000, 300_000: 4_000_000}
        book.book_hash = "provider-book-hash"
        book.source_timestamp_ms = 123

        result = exact_vwap5(book)

        self.assertEqual(result.vwap5_micros, 260_000)
        self.assertEqual(result.available_depth_shares_micros, 6_000_000)

    def test_insufficient_five_share_depth_is_blocked(self):
        book = MarketBook("yes-token")
        book.asks = {200_000: 2_000_000, 300_000: 2_999_999}
        book.book_hash = "provider-book-hash"
        book.source_timestamp_ms = 123

        with self.assertRaisesRegex(
            CurrentInputBlocked, "INSUFFICIENT_FIVE_SHARE_DEPTH"
        ):
            exact_vwap5(book)

    def test_fee_requires_explicit_provenance(self):
        with self.assertRaisesRegex(
            CurrentInputBlocked, "MISSING_FEE_PROVENANCE"
        ):
            FeeEvidenceV1(
                provenance="",
                source_sha256=SHA_A,
                fee_per_share_usd_micros=1_000,
            )

    def test_evidence_key_is_canonical_and_evidence_is_immutable(self):
        evidence = CurrentExecutionEvidenceV1.create(
            market_id="event-1",
            market_date="2026-08-11",
            token_id="yes-token",
            checkpoint_minutes=60,
            candle_first_open_time_ms=FIRST_OPEN_MS,
            candle_last_open_time_ms=LAST_OPEN_MS,
            candles_sha256=SHA_A,
            model_bundle_sha256=SHA_B,
            book_sha256=SHA_C,
            fee_provenance_sha256=SHA_D,
            bucket_index=5,
            model_probability_micros=300_000,
            market_q_micros=200_000,
            vwap5_micros=210_000,
            available_depth_shares_micros=6_000_000,
            fee_per_share_usd_micros=1_000,
            book_source_timestamp_ms=OBSERVED_AT_MS - 1_000,
            observed_at_ms=OBSERVED_AT_MS,
            price_history_source_sha256=SHA_E,
        )

        canonical = evidence.canonical_dict(include_evidence_key=False)
        expected_key = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.assertEqual(evidence.schema_version, "CURRENT_EXECUTION_EVIDENCE_V1")
        self.assertEqual(evidence.closed_candle_count, 181)
        self.assertEqual(evidence.evidence_key, expected_key)
        with self.assertRaises(FrozenInstanceError):
            evidence.market_id = "changed"

    def test_builds_canonical_v1_executable_checkpoint_input(self):
        books = []
        for index in range(11):
            book = MarketBook(f"yes-{index}")
            book.asks = {200_000 + index * 10_000: 6_000_000}
            book.book_hash = f"provider-hash-{index}"
            book.source_timestamp_ms = OBSERVED_AT_MS - 1_000
            books.append(book)
        fees = tuple(
            FeeEvidenceV1(
                provenance="MANAGER_SUPPLIED_PUBLIC_READ_ONLY_FEE_EVIDENCE",
                source_sha256=hashlib.sha256(f"fee-{index}".encode()).hexdigest(),
                fee_per_share_usd_micros=1_000,
            )
            for index in range(11)
        )
        price_history = StrictPriceHistoryEvidence(
            provenance="CLOB_PRICE_HISTORY",
            source_sha256=SHA_E,
            checkpoint_timestamp_ms=OBSERVED_AT_MS,
            observation_timestamp_ms=OBSERVED_AT_MS - 1_000,
        )

        result = build_executable_checkpoint_input(
            checkpoint_minutes=60,
            model_probabilities=tuple(
                ProbabilityMicros(50_000 + index * 10_000)
                for index in range(11)
            ),
            market_q_yes=tuple(
                ProbabilityMicros(100_000 + index * 10_000)
                for index in range(11)
            ),
            yes_books=tuple(books),
            no_token_ids=tuple(f"no-{index}" for index in range(11)),
            fee_evidence=fees,
            prior_position=False,
            price_history_evidence=price_history,
        )

        self.assertIs(type(result), V1ExecutableCheckpointInput)
        self.assertEqual(len(result.buckets), 11)
        self.assertEqual(result.buckets[5].model_p, 0.10)
        self.assertEqual(result.buckets[5].market_q_yes, 0.15)
        self.assertEqual(result.buckets[5].vwap5, 0.25)
        self.assertEqual(result.buckets[5].confirmed_fee, 0.001)
        self.assertEqual(result.buckets[5].no_token_id, "no-5")
        self.assertIs(result.strict_price_history_evidence, price_history)
        self.assertIsNone(result.pf1_snapshot_evidence)

    def test_capture_rejects_evidence_that_disagrees_with_input_bucket(self):
        books = []
        for index in range(11):
            book = MarketBook(f"yes-{index}")
            book.asks = {200_000: 6_000_000}
            book.book_hash = f"provider-hash-{index}"
            book.source_timestamp_ms = OBSERVED_AT_MS - 1_000
            books.append(book)
        checkpoint_input = build_executable_checkpoint_input(
            checkpoint_minutes=60,
            model_probabilities=tuple(ProbabilityMicros(100_000) for _ in range(11)),
            market_q_yes=tuple(ProbabilityMicros(200_000) for _ in range(11)),
            yes_books=tuple(books),
            no_token_ids=tuple(f"no-{index}" for index in range(11)),
            fee_evidence=tuple(
                FeeEvidenceV1("PUBLIC_READ_ONLY", SHA_A, 1_000)
                for _ in range(11)
            ),
            prior_position=False,
            price_history_evidence=StrictPriceHistoryEvidence(
                "CLOB_PRICE_HISTORY", SHA_E, OBSERVED_AT_MS, OBSERVED_AT_MS - 1
            ),
        )
        evidence = CurrentExecutionEvidenceV1.create(
            market_id="event-1",
            market_date="2026-08-11",
            token_id="yes-5",
            checkpoint_minutes=60,
            candle_first_open_time_ms=FIRST_OPEN_MS,
            candle_last_open_time_ms=LAST_OPEN_MS,
            candles_sha256=SHA_A,
            model_bundle_sha256=SHA_B,
            book_sha256=SHA_C,
            fee_provenance_sha256=SHA_D,
            bucket_index=5,
            model_probability_micros=100_000,
            market_q_micros=200_000,
            vwap5_micros=300_000,
            available_depth_shares_micros=6_000_000,
            fee_per_share_usd_micros=1_000,
            book_source_timestamp_ms=OBSERVED_AT_MS - 1_000,
            observed_at_ms=OBSERVED_AT_MS,
            price_history_source_sha256=SHA_E,
        )

        with self.assertRaisesRegex(ValueError, "CURRENT_CAPTURE_MISMATCH"):
            CurrentInputCaptureV1(checkpoint_input, evidence)


class FrozenModelBoundaryTests(unittest.TestCase):
    def test_frozen_model_contract_fails_closed_for_current_use(self):
        contract = Path(
            "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
        )
        with self.assertRaisesRegex(CurrentInputBlocked, "MODEL_INPUT_INVALID"):
            assert_current_model_enabled(contract)


if __name__ == "__main__":
    unittest.main()
