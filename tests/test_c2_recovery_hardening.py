from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.models import SourceEvent, payload_sha256
from src.outbox import OutboxBroker
from src.runtime_adapters import MarketReconciliation, PolymarketRuntimeAdapter
from src.runtime_orchestrator import C1RuntimeOrchestrator
from src.storage import SqliteStore
from tests.test_runtime_orchestrator import (
    FakeBinanceRuntimeAdapter,
    FakePolymarketRuntimeAdapter,
)


MINUTE_MS = 60_000


def _history(asset_id: str, timestamp_seconds: int) -> SourceEvent:
    payload_json = json.dumps(
        {
            "asset_id": asset_id,
            "event_type": "price_history",
            "price_micros": 500_000,
            "timestamp_seconds": timestamp_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return SourceEvent(
        source="polymarket",
        natural_key=(
            f"polymarket:{asset_id}:price_history:{timestamp_seconds}:"
            f"{payload_sha256(payload_json)}"
        ),
        source_timestamp_ms=timestamp_seconds * 1000,
        received_timestamp_ms=timestamp_seconds * 1000,
        event_type="POLYMARKET_PRICE_HISTORY",
        payload_json=payload_json,
        payload_sha256=payload_sha256(payload_json),
        recovery_origin="REST_BACKFILL",
    )


class C2RecoveryPlanTests(unittest.TestCase):
    def _api(self):
        from src.recovery import (
            C2RecoveryPlan,
            build_binance_recovery_plan,
            polymarket_history_cursor_source,
            resolve_polymarket_history_starts,
        )

        return (
            C2RecoveryPlan,
            build_binance_recovery_plan,
            polymarket_history_cursor_source,
            resolve_polymarket_history_starts,
        )

    def test_first_start_recovers_latest_closed_minute(self):
        plan_type, build, _, _ = self._api()

        plan = build(cursor=None, latest_closed_open_ms=5 * MINUTE_MS)

        self.assertIs(type(plan), plan_type)
        self.assertEqual(plan.last_committed_open_ms, 4 * MINUTE_MS)
        self.assertEqual(plan.request_start_ms, 5 * MINUTE_MS)
        self.assertEqual(plan.request_end_ms, 5 * MINUTE_MS)
        self.assertEqual(plan.current_open_ms, 6 * MINUTE_MS)
        self.assertEqual(plan.expected_closed_minutes, 1)
        self.assertFalse(plan.restart)

    def test_restart_begins_strictly_after_persisted_cursor(self):
        _, build, _, _ = self._api()
        cursor = {
            "source": "binance",
            "cursor": {"source_timestamp_ms": 2 * MINUTE_MS},
            "cursor_json": "{}",
            "updated_at_ms": 2 * MINUTE_MS,
        }

        plan = build(cursor=cursor, latest_closed_open_ms=5 * MINUTE_MS)

        self.assertEqual(plan.last_committed_open_ms, 2 * MINUTE_MS)
        self.assertEqual(plan.request_start_ms, 3 * MINUTE_MS)
        self.assertEqual(plan.request_end_ms, 5 * MINUTE_MS)
        self.assertEqual(plan.expected_closed_minutes, 3)
        self.assertTrue(plan.restart)

    def test_current_cursor_requests_exact_anchor_replay_for_projection(self):
        _, build, _, _ = self._api()
        cursor = {
            "source": "binance",
            "cursor": {"source_timestamp_ms": 5 * MINUTE_MS},
            "cursor_json": "{}",
            "updated_at_ms": 5 * MINUTE_MS,
        }

        plan = build(cursor=cursor, latest_closed_open_ms=5 * MINUTE_MS)

        self.assertEqual(plan.request_start_ms, 5 * MINUTE_MS)
        self.assertEqual(plan.request_end_ms, 5 * MINUTE_MS)
        self.assertEqual(plan.expected_closed_minutes, 0)
        self.assertTrue(plan.anchor_replay)

    def test_regressed_future_or_misaligned_cursor_is_rejected(self):
        _, build, _, _ = self._api()
        for value in (5 * MINUTE_MS + 1, 6 * MINUTE_MS, True, "300000"):
            with self.subTest(value=value):
                cursor = {
                    "source": "binance",
                    "cursor": {"source_timestamp_ms": value},
                    "cursor_json": "{}",
                    "updated_at_ms": value,
                }
                with self.assertRaisesRegex(
                    ValueError,
                    "INVALID_BINANCE_RECOVERY_CURSOR",
                ):
                    build(cursor=cursor, latest_closed_open_ms=5 * MINUTE_MS)

    def test_cursor_payload_and_updated_at_must_agree(self):
        _, build, _, _ = self._api()
        with self.assertRaisesRegex(
            ValueError,
            "BINANCE_RECOVERY_CURSOR_CONFLICT",
        ):
            build(
                cursor={
                    "source": "binance",
                    "cursor": {"source_timestamp_ms": 2 * MINUTE_MS},
                    "cursor_json": "{}",
                    "updated_at_ms": 3 * MINUTE_MS,
                },
                latest_closed_open_ms=5 * MINUTE_MS,
            )

    def test_history_cursor_namespace_is_separate_from_books(self):
        _, _, history_source, _ = self._api()
        self.assertEqual(
            history_source("asset-00"),
            "polymarket-history:asset-00",
        )

    def test_per_asset_history_starts_use_persisted_cursor(self):
        _, _, history_source, resolve = self._api()
        cursors = {
            history_source("asset-00"): {
                "source": history_source("asset-00"),
                "cursor": {
                    "natural_key": (
                        "polymarket:asset-00:price_history:120:"
                        + "0" * 64
                    ),
                    "source_timestamp_ms": 120_000,
                },
                "updated_at_ms": 120_000,
            }
        }

        starts = resolve(
            asset_ids=("asset-00", "asset-01"),
            read_cursor=cursors.get,
            floor_start_ts=60,
            end_ts=300,
        )

        self.assertEqual(
            starts,
            {"asset-00": 180, "asset-01": 60},
        )

    def test_multi_hour_cursor_is_not_truncated_by_operational_floor(self):
        _, _, history_source, resolve = self._api()
        cursor_source = history_source("asset-00")

        starts = resolve(
            asset_ids=("asset-00",),
            read_cursor=lambda _source: {
                "source": cursor_source,
                "cursor": {
                    "natural_key": (
                        "polymarket:asset-00:price_history:120:"
                        + "0" * 64
                    ),
                    "source_timestamp_ms": 120_000,
                },
                "updated_at_ms": 120_000,
            },
            floor_start_ts=7_200,
            end_ts=10_800,
        )

        self.assertEqual(starts, {"asset-00": 180})

    def test_history_start_after_end_is_explicitly_none(self):
        _, _, history_source, resolve = self._api()
        cursors = {
            history_source("asset-00"): {
                "source": history_source("asset-00"),
                "cursor": {
                    "natural_key": (
                        "polymarket:asset-00:price_history:300:"
                        + "0" * 64
                    ),
                    "source_timestamp_ms": 300_000,
                },
                "updated_at_ms": 300_000,
            }
        }

        starts = resolve(
            asset_ids=("asset-00",),
            read_cursor=cursors.get,
            floor_start_ts=60,
            end_ts=300,
        )

        self.assertEqual(starts, {"asset-00": None})

    def test_history_cursor_source_and_timestamp_must_agree(self):
        _, _, history_source, resolve = self._api()
        cursor_source = history_source("asset-00")
        cases = (
            {
                "source": "polymarket-history:asset-other",
                "cursor": {"source_timestamp_ms": 120_000},
                "updated_at_ms": 120_000,
            },
            {
                "source": cursor_source,
                "cursor": {"source_timestamp_ms": 60_000},
                "updated_at_ms": 120_000,
            },
            {
                "source": cursor_source,
                "cursor": {"source_timestamp_ms": True},
                "updated_at_ms": 120_000,
            },
            {
                "source": cursor_source,
                "cursor": {
                    "natural_key": (
                        "polymarket:asset-other:price_history:120:hash"
                    ),
                    "source_timestamp_ms": 120_000,
                },
                "updated_at_ms": 120_000,
            },
        )

        for cursor in cases:
            with self.subTest(cursor=cursor):
                with self.assertRaisesRegex(
                    ValueError,
                    "POLYMARKET_HISTORY_CURSOR_CONFLICT",
                ):
                    resolve(
                        asset_ids=("asset-00",),
                        read_cursor=lambda _source, value=cursor: value,
                        floor_start_ts=60,
                        end_ts=300,
                    )

    def test_history_cursor_beyond_recovery_end_is_rejected(self):
        _, _, history_source, resolve = self._api()
        cursor_source = history_source("asset-00")

        with self.assertRaisesRegex(
            ValueError,
            "POLYMARKET_HISTORY_CURSOR_REGRESSION",
        ):
            resolve(
                asset_ids=("asset-00",),
                read_cursor=lambda _source: {
                    "source": cursor_source,
                    "cursor": {
                        "natural_key": (
                            "polymarket:asset-00:price_history:360:"
                            + "0" * 64
                        ),
                        "source_timestamp_ms": 360_000,
                    },
                    "updated_at_ms": 360_000,
                },
                floor_start_ts=60,
                end_ts=300,
            )


class C2RecoverySummaryTests(unittest.TestCase):
    def _summary(self, **overrides):
        from src.recovery import assess_c2_recovery

        values = {
            "binance_expected_closed_minutes": 10,
            "binance_recovered_closed_minutes": 10,
            "binance_missing_closed_minutes": 0,
            "binance_duplicate_count_after_dedup": 0,
            "market_count": 11,
            "asset_count": 22,
            "current_book_count": 22,
            "history_completed_asset_count": 22,
            "history_event_count": 44,
            "buffered_event_count": 3,
            "drained_event_count": 3,
            "source_duplicate_count_after_dedup": 0,
            "writer_consumer_count": 1,
            "recovered_evaluation_committed": True,
            "current_evaluation_committed": True,
            "recovered_execution_eligible": False,
            "current_execution_eligible": False,
        }
        values.update(overrides)
        return assess_c2_recovery(**values)

    def test_complete_recovery_summary_passes(self):
        summary = self._summary()

        self.assertEqual(summary.status, "C2_RECOVERY_HARDENING_PASS")
        self.assertTrue(summary.live_ready_allowed)
        self.assertEqual(summary.blockers, ())

    def test_each_load_bearing_gap_blocks(self):
        cases = {
            "binance_missing_closed_minutes": 1,
            "binance_duplicate_count_after_dedup": 1,
            "market_count": 10,
            "asset_count": 21,
            "current_book_count": 21,
            "history_completed_asset_count": 21,
            "drained_event_count": 2,
            "source_duplicate_count_after_dedup": 1,
            "writer_consumer_count": 2,
            "recovered_evaluation_committed": False,
            "current_evaluation_committed": False,
            "recovered_execution_eligible": True,
            "current_execution_eligible": True,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                summary = self._summary(**{field: value})
                self.assertEqual(summary.status, "C2_RECOVERY_BLOCKED")
                self.assertFalse(summary.live_ready_allowed)
                self.assertTrue(summary.blockers)

    def test_summary_payload_is_deterministic_and_has_no_trading_approval(self):
        first = self._summary().as_dict()
        second = self._summary().as_dict()

        self.assertEqual(first, second)
        self.assertFalse(first["trading_approval"])
        self.assertNotIn("order", json.dumps(first).lower())
        self.assertNotIn("wallet", json.dumps(first).lower())


class C2PolymarketRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _reconciliation(self) -> MarketReconciliation:
        assets = ("asset-00", "asset-01")
        return MarketReconciliation(
            market_identity_json='{"event_id":"event-1"}',
            market_id="event-1",
            market_ids=tuple(f"market-{index}" for index in range(11)),
            asset_ids=assets,
            current_events=(),
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )

    async def test_adapter_uses_exact_per_asset_history_starts(self):
        calls = []

        async def history(_session, *, asset_id, start_ts, end_ts):
            calls.append((asset_id, start_ts, end_ts))
            yield _history(asset_id, start_ts)

        adapter = PolymarketRuntimeAdapter(
            session=object(),
            stream=object(),
            event_loader=None,
            now_utc=None,
            history=history,
        )

        events = await adapter.recover(
            self._reconciliation(),
            start_ts=60,
            end_ts=300,
            start_ts_by_asset={"asset-00": 180, "asset-01": 60},
        )

        self.assertEqual(
            calls,
            [("asset-00", 180, 300), ("asset-01", 60, 300)],
        )
        self.assertEqual(len(events), 2)

    async def test_completed_asset_cursor_skips_network_history_call(self):
        calls = []

        async def history(*_args, **kwargs):
            calls.append(kwargs)
            if False:
                yield None

        adapter = PolymarketRuntimeAdapter(
            session=object(),
            stream=object(),
            event_loader=None,
            now_utc=None,
            history=history,
        )

        events = await adapter.recover(
            self._reconciliation(),
            start_ts=60,
            end_ts=300,
            start_ts_by_asset={"asset-00": None, "asset-01": None},
        )

        self.assertEqual(events, [])
        self.assertEqual(calls, [])

    async def test_wrong_asset_history_event_rejects_whole_result(self):
        async def history(_session, *, asset_id, start_ts, end_ts):
            if asset_id == "asset-00":
                yield _history(asset_id, start_ts)
            else:
                yield _history("asset-unknown", start_ts)

        adapter = PolymarketRuntimeAdapter(
            session=object(),
            stream=object(),
            event_loader=None,
            now_utc=None,
            history=history,
        )

        with self.assertRaisesRegex(
            ValueError,
            "RUNTIME_POLYMARKET_HISTORY_ASSET_MISMATCH",
        ):
            await adapter.recover(
                self._reconciliation(),
                start_ts=60,
                end_ts=300,
                start_ts_by_asset={"asset-00": 60, "asset-01": 60},
            )

    async def test_history_cursor_survives_store_reopen(self):
        from src.recovery import polymarket_history_cursor_source
        from src.runtime_orchestrator import C1RuntimeOrchestrator

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "c2.sqlite3")
            store = SqliteStore.open(path)
            store.migrate()
            event = _history("asset-00", 120)
            source = C1RuntimeOrchestrator._cursor_source(event)
            self.assertEqual(
                source,
                polymarket_history_cursor_source("asset-00"),
            )
            store.upsert_source_cursor(
                source=source,
                cursor_json=json.dumps(
                    {
                        "natural_key": event.natural_key,
                        "source_timestamp_ms": event.source_timestamp_ms,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                updated_at_ms=event.source_timestamp_ms,
            )
            store.close()

            reopened = SqliteStore.open(path)
            try:
                self.assertEqual(
                    reopened.read_source_cursor(source)["updated_at_ms"],
                    120_000,
                )
            finally:
                reopened.close()


class C2RuntimeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "runtime.sqlite3"))
        self.store.migrate()
        self.runtime = C1RuntimeOrchestrator(
            run_id="c2-summary-run",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=FakeBinanceRuntimeAdapter(),
            polymarket_adapter=FakePolymarketRuntimeAdapter(),
            binance_start_ms=MINUTE_MS,
            binance_end_ms=MINUTE_MS,
            history_start_ts=1,
            history_end_ts=2,
            clock_ms=lambda: 1_000,
        )

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    def test_runtime_rejects_inverted_binance_recovery_range(self):
        with self.assertRaisesRegex(
            ValueError,
            "INVALID_RUNTIME_RECOVERY_BOUNDARY",
        ):
            C1RuntimeOrchestrator(
                run_id="c2-invalid-range",
                store=self.store,
                broker=OutboxBroker(),
                binance_adapter=FakeBinanceRuntimeAdapter(),
                polymarket_adapter=FakePolymarketRuntimeAdapter(),
                binance_start_ms=2 * MINUTE_MS,
                binance_end_ms=MINUTE_MS,
                history_start_ts=1,
                history_end_ts=2,
            )

    async def test_runtime_persists_pass_summary_before_live_ready(self):
        await self.runtime.start()

        summary = self.runtime.recovery_summary
        self.assertEqual(summary.status, "C2_RECOVERY_HARDENING_PASS")
        self.assertTrue(self.runtime.status().live_ready)
        rows = self.store.rows(
            """
            SELECT status, payload_json
            FROM incidents
            WHERE incident_key = ?
            """,
            ("c2-recovery:c2-summary-run",),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "C2_RECOVERY_HARDENING_PASS")
        payload = json.loads(rows[0][1])
        self.assertEqual(payload, summary.as_dict())
        lifecycle = self.store.rows(
            """
            SELECT incident_id FROM incidents
            WHERE incident_key = ?
            """,
            ("lifecycle:c2-summary-run:10:LIVE_READY",),
        )
        self.assertTrue(lifecycle)
        self.assertLess(rows[0][0].find("BLOCKED"), 0)

    async def test_runtime_summary_proves_one_writer_and_full_market(self):
        await self.runtime.start()

        summary = self.runtime.recovery_summary
        self.assertEqual(summary.writer_consumer_count, 1)
        self.assertEqual(summary.market_count, 11)
        self.assertEqual(summary.asset_count, 22)
        self.assertEqual(summary.current_book_count, 22)
        self.assertEqual(summary.history_completed_asset_count, 22)
        self.assertEqual(
            summary.buffered_event_count,
            summary.drained_event_count,
        )

    async def test_runtime_summary_reads_persisted_evaluation_evidence(self):
        original_rows = SqliteStore.rows
        observed_sql: list[str] = []

        def recording_rows(store, sql, parameters=()):
            observed_sql.append(" ".join(sql.split()))
            return original_rows(store, sql, parameters)

        with mock.patch.object(SqliteStore, "rows", new=recording_rows):
            await self.runtime.start()

        evaluation_queries = [
            sql
            for sql in observed_sql
            if "FROM strategy_evaluations" in sql
            and "SELECT execution_eligible" in sql
        ]
        self.assertEqual(len(evaluation_queries), 2)
        self.assertFalse(
            self.runtime.recovery_summary.recovered_execution_eligible
        )
        self.assertFalse(
            self.runtime.recovery_summary.current_execution_eligible
        )

    async def test_runtime_duplicate_evidence_is_not_hardcoded(self):
        original_scalar = SqliteStore.scalar

        def duplicate_scalar(store, sql, parameters=()):
            if "C2_SOURCE_DUPLICATE_COUNT" in sql:
                return 1
            return original_scalar(store, sql, parameters)

        with mock.patch.object(
            SqliteStore,
            "scalar",
            new=duplicate_scalar,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "SOURCE_DUPLICATES_AFTER_DEDUP",
            ):
                await self.runtime.start()


if __name__ == "__main__":
    unittest.main()
