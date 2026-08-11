from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.storage import SqliteStore
from tests.test_paper import PaperLedgerTests


class _Publisher:
    def __init__(self) -> None:
        self.event_ids: list[int] = []

    def publish_committed(self, event_id: int) -> None:
        self.event_ids.append(event_id)


class MvpPaperStorageTests(unittest.TestCase):
    def test_paper_commit_uses_primary_store_and_existing_resumable_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            try:
                store.migrate()
                publisher = _Publisher()
                ledger = store.paper_ledger(publisher=publisher)
                ledger.initialize_account(updated_at_ms=1)

                result = ledger.execute(
                    PaperLedgerTests.signal(),
                    PaperLedgerTests.evidence(),
                    checked_at_ms=101,
                )

                events = store.read_outbox_after(0, 100)
                self.assertEqual(
                    [event.topic for event in events],
                    [
                        "paper.readiness",
                        "paper.intent",
                        "paper.fill",
                        "paper.position",
                        "paper.account",
                    ],
                )
                self.assertEqual(publisher.event_ids, [event.event_id for event in events])
                self.assertEqual(result.fill.shares_micros, 5_000_000)

                ledger.mark_to_market(
                    market_date="2026-08-11",
                    mark_price_micros=650_000,
                    updated_at_ms=102,
                )
                ledger.settle(
                    market_date="2026-08-11",
                    settlement_price_micros=1_000_000,
                    settled_at_ms=103,
                )
                updated_topics = [
                    event.topic for event in store.read_outbox_after(events[-1].event_id, 100)
                ]
                self.assertEqual(
                    updated_topics,
                    [
                        "paper.position",
                        "paper.account",
                        "paper.position",
                        "paper.account",
                    ],
                )

                read_store = store.open_read_store()
                try:
                    self.assertEqual(read_store.paper_account()["account_key"], "default")
                    self.assertEqual(len(read_store.paper_positions()), 1)
                    self.assertEqual(len(read_store.paper_fills()), 1)
                    self.assertTrue(read_store.paper_readiness()["ready"])
                finally:
                    read_store.close()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
