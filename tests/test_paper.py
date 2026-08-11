from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.paper import (
    CurrentExecutionEvidenceV1,
    PaperLedger,
    StrictASignalV1,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIVE_SHARES = 5_000_000


class PaperLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "paper.sqlite3"
        self.connection = sqlite3.connect(self.db_path, isolation_level=None)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            (PROJECT_ROOT / "migrations" / "0005_paper.sql").read_text(
                encoding="utf-8"
            )
        )
        self.ledger = PaperLedger(self.connection)
        self.ledger.initialize_account(updated_at_ms=1)

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    @staticmethod
    def evidence_key(market_date: str, checkpoint_minutes: int) -> str:
        return hashlib.sha256(
            f"evidence:{market_date}:{checkpoint_minutes}".encode()
        ).hexdigest()

    @staticmethod
    def signal(
        *,
        market_date: str = "2026-08-11",
        checkpoint_minutes: int = 60,
        origin: str = "CURRENT_LIVE_REEVALUATION",
        signal_key: str | None = None,
    ) -> StrictASignalV1:
        suffix = f"{market_date}:{checkpoint_minutes}"
        return StrictASignalV1(
            schema_version="STRICT_A_SIGNAL_V1",
            signal_key=signal_key or f"signal:{suffix}",
            evaluation_key=f"evaluation:{suffix}",
            strategy_id="YES_STRICT_A_OPERATIONAL",
            market_id=f"market:{market_date}",
            market_date=market_date,
            checkpoint_minutes=checkpoint_minutes,
            side="YES",
            bucket_index=3,
            token_id=f"yes-token:{market_date}",
            model_probability_micros=700_000,
            market_q_micros=500_000,
            vwap5_micros=600_000,
            fee_per_share_usd_micros=10_000,
            evidence_key=PaperLedgerTests.evidence_key(
                market_date, checkpoint_minutes
            ),
            input_snapshot_hash="1" * 64,
            execution_eligible=True,
            reason_code="READY",
            origin=origin,
            evaluated_at_ms=100,
        )

    @staticmethod
    def evidence(
        *,
        market_date: str = "2026-08-11",
        checkpoint_minutes: int = 60,
        available_depth_shares_micros: int = FIVE_SHARES,
    ) -> CurrentExecutionEvidenceV1:
        suffix = f"{market_date}:{checkpoint_minutes}"
        return CurrentExecutionEvidenceV1(
            schema_version="CURRENT_EXECUTION_EVIDENCE_V1",
            evidence_key=PaperLedgerTests.evidence_key(
                market_date, checkpoint_minutes
            ),
            market_id=f"market:{market_date}",
            market_date=market_date,
            token_id=f"yes-token:{market_date}",
            checkpoint_minutes=checkpoint_minutes,
            candle_first_open_time_ms=1,
            candle_last_open_time_ms=181,
            closed_candle_count=181,
            candles_sha256="2" * 64,
            model_bundle_sha256="3" * 64,
            book_sha256="4" * 64,
            fee_provenance_sha256="5" * 64,
            bucket_index=3,
            model_probability_micros=700_000,
            market_q_micros=500_000,
            vwap5_micros=600_000,
            available_depth_shares_micros=available_depth_shares_micros,
            fee_per_share_usd_micros=10_000,
            book_source_timestamp_ms=90,
            observed_at_ms=100,
            price_history_source_sha256="6" * 64,
        )

    def test_full_fill_has_deterministic_identity_and_exact_five_share_accounting(self):
        result = self.ledger.execute(
            self.signal(), self.evidence(), checked_at_ms=101
        )

        self.assertTrue(result.readiness.ready)
        self.assertEqual(result.intent.intent_key, "strict-a:2026-08-11")
        self.assertEqual(result.intent.requested_shares_micros, FIVE_SHARES)
        self.assertEqual(result.intent.status, "FILLED")
        self.assertEqual(result.fill.fill_key, "strict-a:2026-08-11:1")
        self.assertEqual(result.fill.shares_micros, FIVE_SHARES)
        self.assertEqual(result.fill.gross_cost_usd_micros, 3_000_000)
        self.assertEqual(result.fill.fee_usd_micros, 50_000)
        self.assertEqual(result.position.cost_basis_usd_micros, 3_050_000)
        self.assertEqual(result.position.unrealized_pnl_usd_micros, -50_000)
        self.assertEqual(result.account.cash_usd_micros, 996_950_000)
        self.assertEqual(result.account.open_cost_basis_usd_micros, 3_050_000)
        self.assertEqual(result.account.equity_usd_micros, 999_950_000)

    def test_partial_fill_uses_only_ready_contemporaneous_evidence(self):
        result = self.ledger.execute(
            self.signal(),
            self.evidence(),
            checked_at_ms=101,
            max_fill_shares_micros=2_000_000,
        )

        self.assertEqual(result.intent.status, "PARTIAL")
        self.assertEqual(result.fill.shares_micros, 2_000_000)
        self.assertEqual(result.position.status, "PARTIAL")
        self.assertEqual(result.position.cost_basis_usd_micros, 1_220_000)

    def test_missing_or_insufficient_evidence_blocks_without_intent_or_fill(self):
        missing = self.ledger.execute(self.signal(), None, checked_at_ms=101)
        shallow = self.ledger.execute(
            self.signal(market_date="2026-08-12"),
            self.evidence(
                market_date="2026-08-12",
                available_depth_shares_micros=4_999_999,
            ),
            checked_at_ms=102,
        )

        self.assertEqual(missing.readiness.reason_code, "MISSING_CURRENT_BOOK")
        self.assertIsNone(missing.intent)
        self.assertIsNone(missing.fill)
        self.assertEqual(
            shallow.readiness.reason_code, "INSUFFICIENT_FIVE_SHARE_DEPTH"
        )
        self.assertEqual(self.ledger.row_count("paper_intents"), 0)
        self.assertEqual(self.ledger.row_count("paper_fills"), 0)

    def test_recovered_signal_is_blocked_without_execution_state(self):
        result = self.ledger.execute(
            self.signal(origin="RECOVERED_AFTER_DOWNTIME"),
            self.evidence(),
            checked_at_ms=101,
        )

        self.assertEqual(
            result.readiness.reason_code, "RECOVERED_EXECUTION_FORBIDDEN"
        )
        self.assertIsNone(result.intent)
        self.assertEqual(self.ledger.row_count("paper_intents"), 0)
        self.assertEqual(self.ledger.row_count("paper_positions"), 0)

    def test_restart_replay_returns_same_rows_without_duplicates(self):
        first = self.ledger.execute(
            self.signal(), self.evidence(), checked_at_ms=101
        )
        restarted = PaperLedger(sqlite3.connect(self.db_path, isolation_level=None))
        try:
            replay = restarted.execute(
                self.signal(), self.evidence(), checked_at_ms=999
            )
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.intent, first.intent)
            self.assertEqual(replay.fill, first.fill)
            self.assertEqual(restarted.row_count("paper_intents"), 1)
            self.assertEqual(restarted.row_count("paper_fills"), 1)
            self.assertEqual(restarted.row_count("paper_positions"), 1)
        finally:
            restarted.close()

    def test_restart_replay_rejects_conflicting_immutable_execution(self):
        signal = self.signal()
        evidence = self.evidence()
        self.ledger.execute(signal, evidence, checked_at_ms=101)

        with self.assertRaisesRegex(
            ValueError, "PAPER_EXECUTION_IDENTITY_CONFLICT"
        ):
            self.ledger.execute(
                replace(signal, token_id="different-token"),
                replace(evidence, token_id="different-token"),
                checked_at_ms=102,
            )

    def test_t60_is_primary_and_t30_only_falls_back_without_logical_execution(self):
        fallback = self.ledger.execute(
            self.signal(market_date="2026-08-12", checkpoint_minutes=30),
            self.evidence(market_date="2026-08-12", checkpoint_minutes=30),
            checked_at_ms=101,
        )
        primary = self.ledger.execute(
            self.signal(), self.evidence(), checked_at_ms=102
        )
        blocked = self.ledger.execute(
            self.signal(checkpoint_minutes=30),
            self.evidence(checkpoint_minutes=30),
            checked_at_ms=103,
        )

        self.assertEqual(fallback.intent.checkpoint_minutes, 30)
        self.assertEqual(primary.intent.checkpoint_minutes, 60)
        self.assertEqual(
            blocked.readiness.reason_code,
            "T30_BLOCKED_EXISTING_LOGICAL_EXECUTION",
        )
        self.assertIsNone(blocked.intent)
        self.assertEqual(self.ledger.row_count("paper_positions"), 2)

    def test_mark_and_settlement_persist_signed_pnl_and_bankroll_invariants(self):
        self.ledger.execute(self.signal(), self.evidence(), checked_at_ms=101)
        marked_position, marked_account = self.ledger.mark_to_market(
            market_date="2026-08-11", mark_price_micros=800_000, updated_at_ms=102
        )
        settled_position, settled_account = self.ledger.settle(
            market_date="2026-08-11",
            settlement_price_micros=1_000_000,
            settled_at_ms=103,
        )

        self.assertEqual(marked_position.unrealized_pnl_usd_micros, 950_000)
        self.assertEqual(marked_account.equity_usd_micros, 1_000_950_000)
        self.assertEqual(settled_position.status, "SETTLED")
        self.assertEqual(settled_position.settlement_value_usd_micros, 5_000_000)
        self.assertEqual(settled_position.realized_pnl_usd_micros, 1_950_000)
        self.assertEqual(settled_account.cash_usd_micros, 1_001_950_000)
        self.assertEqual(settled_account.open_cost_basis_usd_micros, 0)
        self.assertEqual(settled_account.equity_usd_micros, 1_001_950_000)
        self.assertEqual(settled_account.realized_pnl_usd_micros, 1_950_000)
        self.assertEqual(settled_account.unrealized_pnl_usd_micros, 0)

        replay_position, replay_account = self.ledger.settle(
            market_date="2026-08-11",
            settlement_price_micros=1_000_000,
            settled_at_ms=999,
        )
        self.assertEqual(replay_position, settled_position)
        self.assertEqual(replay_account, settled_account)

    def test_insufficient_cash_and_invalid_states_fail_closed(self):
        self.connection.execute(
            "UPDATE paper_accounts SET cash_usd_micros = 1 WHERE account_key = 'default'"
        )
        blocked = self.ledger.execute(
            self.signal(), self.evidence(), checked_at_ms=101
        )
        self.assertEqual(blocked.readiness.reason_code, "INSUFFICIENT_PAPER_CASH")
        self.assertEqual(self.ledger.row_count("paper_fills"), 0)
        self.assertEqual(self.ledger.get_account().cash_usd_micros, 1)

        with self.assertRaisesRegex(ValueError, "INVALID_MARK_PRICE"):
            self.ledger.mark_to_market(
                market_date="2026-08-11", mark_price_micros=-1, updated_at_ms=102
            )
        with self.assertRaisesRegex(ValueError, "INVALID_FILL_SHARES"):
            self.ledger.execute(
                self.signal(market_date="2026-08-12"),
                self.evidence(market_date="2026-08-12"),
                checked_at_ms=102,
                max_fill_shares_micros=0,
            )


if __name__ == "__main__":
    unittest.main()
