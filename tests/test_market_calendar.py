from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.market_calendar import MarketCatchupClassifier
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MarketCatchupClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temporary.name) / "runtime.sqlite3")
        self.store.migrate()
        self.repository = PerformanceRepository(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_classifies_dates_from_only_persisted_market_and_source_evidence(self) -> None:
        self._market(
            "market-resolved",
            {
                "market_date": "2026-07-08",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
            },
        )
        self._market(
            "market-pending",
            {
                "market_date": "2026-07-09",
                "resolved": False,
                "active": True,
            },
        )
        self._source_event(
            natural_key="absence:2026-07-10",
            event_type="BTC_DAILY_RANGE_MARKET_ABSENT",
            payload={
                "market_date": "2026-07-10",
                "reason_code": "PROVIDER_CONFIRMED_NO_DAILY_RANGE_MARKET",
            },
        )

        rows = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=1_000,
        ).classify_dates(
            [
                "2026-07-08",
                "2026-07-09",
                "2026-07-10",
                "2026-07-11",
            ]
        )
        for row in rows:
            self.repository.append_catchup_classification(row)

        self.assertEqual(
            [(row.market_date, row.classification, row.reason_code) for row in rows],
            [
                ("2026-07-08", "RESOLVED", "PERSISTED_MARKET_RESOLVED"),
                ("2026-07-09", "PENDING", "PERSISTED_MARKET_PENDING"),
                ("2026-07-10", "EXPECTED_ABSENT", "PERSISTED_EXPECTED_ABSENCE"),
                ("2026-07-11", "DATA_GAP", "MISSING_PERSISTED_MARKET_EVIDENCE"),
            ],
        )
        self.assertEqual(self.store.count("strategy_performance_catchup"), 4)

    def test_reclassifying_changed_evidence_supersedes_previous_revision(self) -> None:
        first = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=1_000,
        ).classify_dates(["2026-07-12"])[0]
        self.repository.append_catchup_classification(first)
        self._market(
            "market-later-resolved",
            {
                "market_date": "2026-07-12",
                "resolved": True,
                "winning_bucket_identity": "bucket-mid",
            },
        )

        second = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=2_000,
        ).classify_dates(["2026-07-12"])[0]
        self.repository.append_catchup_classification(second)

        effective = self.repository.read_effective_catchup_classifications()
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].classification, "RESOLVED")
        self.assertEqual(effective[0].revision, 2)
        self.assertEqual(effective[0].supersedes_catchup_key, first.catchup_key)

    def test_reclassifying_unchanged_evidence_replays_existing_revision(self) -> None:
        first = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=1_000,
        ).classify_dates(["2026-07-13"])[0]
        self.assertEqual(
            self.repository.append_catchup_classification(first).outcome,
            "INSERTED",
        )

        second = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=2_000,
        ).classify_dates(["2026-07-13"])[0]

        self.assertEqual(second, first)
        self.assertEqual(
            self.repository.append_catchup_classification(second).outcome,
            "REPLAYED",
        )
        self.assertEqual(self.store.count("strategy_performance_catchup"), 1)

    def test_canonical_resolution_event_supersedes_pending_market_state(self) -> None:
        self._market(
            "btc-range-2026-07-12",
            {"market_date": "2026-07-12", "resolved": False, "active": True},
        )
        first = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=1_000,
        ).classify_dates(["2026-07-12"])[0]
        self.repository.append_catchup_classification(first)
        self._source_event(
            natural_key="resolution:2026-07-12",
            event_type="POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-12",
                "market_date": "2026-07-12",
                "resolved": True,
                "winning_bucket_identity": "bucket-mid",
                "winning_bucket_count": 1,
            },
        )

        second = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=2_000,
        ).classify_dates(["2026-07-12"])[0]
        self.repository.append_catchup_classification(second)

        self.assertEqual(first.classification, "PENDING")
        self.assertEqual(second.classification, "RESOLVED")
        self.assertEqual(second.reason_code, "PERSISTED_PUBLIC_RESOLUTION")
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.supersedes_catchup_key, first.catchup_key)

    def test_resolution_correction_chain_selects_effective_event(self) -> None:
        self._market(
            "btc-range-2026-07-12",
            {"market_date": "2026-07-12", "resolved": False, "active": True},
        )
        self._source_event(
            natural_key="resolution:first",
            event_type="POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-12",
                "market_date": "2026-07-12",
                "resolved": True,
                "winning_bucket_identity": "bucket-low",
                "winning_bucket_count": 1,
            },
        )
        self._source_event(
            natural_key="resolution:correction",
            event_type="POLYMARKET_DAILY_RANGE_MARKET_RESOLVED",
            payload={
                "market_id": "btc-range-2026-07-12",
                "market_date": "2026-07-12",
                "resolved": True,
                "winning_bucket_identity": "bucket-mid",
                "winning_bucket_count": 1,
                "supersedes_source_event_natural_key": "resolution:first",
            },
        )

        row = MarketCatchupClassifier(
            store=self.store,
            classified_at_ms=2_000,
        ).classify_dates(["2026-07-12"])[0]

        self.assertEqual(row.classification, "RESOLVED")
        self.assertEqual(row.source_event_identity, "resolution:correction")

    def test_absence_and_market_for_same_date_fail_closed(self) -> None:
        self._market(
            "market-contradiction",
            {"market_date": "2026-07-14", "resolved": False},
        )
        self._source_event(
            natural_key="absence:2026-07-14",
            event_type="BTC_DAILY_RANGE_MARKET_ABSENT",
            payload={"market_date": "2026-07-14"},
        )

        with self.assertRaisesRegex(
            ValueError, "MARKET_CATCHUP_CONTRADICTORY_EVIDENCE"
        ):
            MarketCatchupClassifier(
                store=self.store,
                classified_at_ms=1_000,
            ).classify_dates(["2026-07-14"])

    def _market(self, market_id: str, payload: dict[str, object]) -> None:
        payload_json = _canonical(payload)
        self.store.rows(
            """
            INSERT INTO market_catalog(market_id, payload_json, payload_sha256, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (market_id, payload_json, _sha(payload_json), 1),
        )

    def _source_event(
        self,
        *,
        natural_key: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        payload_json = _canonical(payload)
        self.store.rows(
            """
            INSERT INTO source_events(
                source, natural_key, source_timestamp_ms, received_timestamp_ms,
                event_type, payload_json, payload_sha256, recovery_origin,
                committed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "polymarket",
                natural_key,
                1,
                1,
                event_type,
                payload_json,
                _sha(payload_json),
                "LIVE",
                1,
            ),
        )


if __name__ == "__main__":
    unittest.main()
