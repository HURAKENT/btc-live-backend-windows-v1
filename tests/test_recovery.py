from __future__ import annotations

import asyncio
import unittest

from src.lifecycle import STARTUP_SEQUENCE
from src.models import SourceEvent
from src.recovery import (
    RecoveryBlockedError,
    RecoveryCoordinator,
    classify_recovered_evaluation,
    execute_cutover,
    reconcile_binance_minutes,
    reconcile_polymarket_state,
)


MINUTE_MS = 60_000


def minute(number, origin="REST_BACKFILL"):
    return SourceEvent.binance_closed_kline(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=number * MINUTE_MS,
        payload=f'{{"minute":{number}}}'.encode("utf-8"),
        received_timestamp_ms=number * MINUTE_MS + 1,
        recovery_origin=origin,
    )


class CommitStore:
    def __init__(self):
        self.natural_keys = set()
        self.committed_minutes = []

    def append(self, event):
        if event.natural_key in self.natural_keys:
            return False
        self.natural_keys.add(event.natural_key)
        self.committed_minutes.append(event.source_timestamp_ms // MINUTE_MS)
        return True


class FakeBinanceAdapter:
    def __init__(self, events=None, error=None):
        self.events = list(events or [])
        self.error = error

    async def backfill(self):
        if self.error is not None:
            raise self.error
        return list(self.events)


class FakePolymarketAdapter:
    def __init__(
        self,
        *,
        current_book=True,
        price_history=True,
        historical_depth=False,
        requires_depth=False,
    ):
        self.result = {
            "current_book": current_book,
            "price_history": price_history,
            "historical_depth": historical_depth,
            "requires_depth": requires_depth,
        }

    async def reconcile(self):
        return dict(self.result)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_live_buffer_is_drained_only_after_backfill_commit(self):
        store = CommitStore()
        trace = execute_cutover(
            backfill_events=[minute(1), minute(2)],
            buffered_events=[
                minute(2, "BUFFERED_DURING_RECOVERY"),
                minute(3, "BUFFERED_DURING_RECOVERY"),
            ],
            commit_event=store.append,
        )
        self.assertEqual(trace.committed_minutes, (1, 2, 3))
        self.assertEqual(trace.duplicate_count, 1)
        self.assertEqual(trace.duplicate_count_after_dedup, 0)
        self.assertLess(
            trace.backfill_complete_index,
            trace.buffer_drain_start_index,
        )

    def test_ten_minute_binance_gap_is_fully_recovered(self):
        recovered = [minute(index) for index in range(1, 11)]
        report = reconcile_binance_minutes(
            last_committed_open_ms=0,
            current_open_ms=11 * MINUTE_MS,
            recovered_events=recovered,
        )
        self.assertEqual(report.missing_closed_minutes, 0)
        self.assertEqual(report.recovered_closed_minutes, 10)
        self.assertFalse(report.blocker)

    def test_missing_minute_blocks_live_ready(self):
        recovered = [minute(index) for index in range(1, 11) if index != 6]
        report = reconcile_binance_minutes(
            last_committed_open_ms=0,
            current_open_ms=11 * MINUTE_MS,
            recovered_events=recovered,
        )
        self.assertTrue(report.blocker)
        self.assertEqual(report.missing_open_times, (6 * MINUTE_MS,))

    def test_current_open_and_future_minutes_are_not_canonical(self):
        report = reconcile_binance_minutes(
            last_committed_open_ms=0,
            current_open_ms=3 * MINUTE_MS,
            recovered_events=[minute(1), minute(2), minute(3), minute(4)],
        )
        self.assertEqual(
            report.canonical_open_times,
            (MINUTE_MS, 2 * MINUTE_MS),
        )

    def test_unordered_input_is_normalized_deterministically(self):
        report = reconcile_binance_minutes(
            last_committed_open_ms=0,
            current_open_ms=4 * MINUTE_MS,
            recovered_events=[minute(3), minute(1), minute(2)],
        )
        self.assertEqual(
            report.canonical_open_times,
            (MINUTE_MS, 2 * MINUTE_MS, 3 * MINUTE_MS),
        )

    def test_invalid_minute_cadence_fails_closed(self):
        invalid = SourceEvent.binance_closed_kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=MINUTE_MS + 1,
            payload=b"{}",
        )
        with self.assertRaisesRegex(ValueError, "INVALID_BINANCE_MINUTE_CADENCE"):
            reconcile_binance_minutes(
                last_committed_open_ms=0,
                current_open_ms=3 * MINUTE_MS,
                recovered_events=[invalid],
            )

    def test_current_polymarket_book_is_mandatory(self):
        report = reconcile_polymarket_state(
            price_history=True,
            current_book=False,
            historical_depth=False,
            requires_depth=False,
        )
        self.assertEqual(report.status, "BLOCKED_MISSING_CURRENT_BOOK")
        self.assertFalse(report.live_ready_allowed)

    def test_missing_historical_depth_is_explicitly_blocked(self):
        result = classify_recovered_evaluation(
            price_history=True,
            current_book=True,
            historical_depth=False,
            requires_depth=True,
        )
        self.assertEqual(result.status, "BLOCKED_MISSING_HISTORICAL_DEPTH")

    def test_depth_not_required_passes_with_book_and_price_history(self):
        report = reconcile_polymarket_state(
            price_history=True,
            current_book=True,
            historical_depth=False,
            requires_depth=False,
        )
        self.assertEqual(report.status, "PASS")
        self.assertTrue(report.live_ready_allowed)

    def test_recovered_classification_is_never_current_or_executable(self):
        result = classify_recovered_evaluation(
            price_history=True,
            current_book=True,
            historical_depth=True,
            requires_depth=True,
        )
        self.assertEqual(result.status, "RECOVERED_SIGNAL")
        self.assertEqual(result.origin, "RECOVERED_AFTER_DOWNTIME")
        self.assertFalse(result.execution_eligible)
        self.assertFalse(result.historical_signal_is_current_live_signal)
        self.assertTrue(result.current_reevaluation_required)

    async def test_coordinator_follows_frozen_state_order(self):
        states = []
        store = CommitStore()
        coordinator = RecoveryCoordinator(
            binance_adapter=FakeBinanceAdapter([minute(1), minute(2)]),
            polymarket_adapter=FakePolymarketAdapter(),
            buffered_events=[
                minute(2, "BUFFERED_DURING_RECOVERY"),
                minute(3, "BUFFERED_DURING_RECOVERY"),
            ],
            commit_event=store.append,
            last_committed_open_ms=0,
            current_open_ms=4 * MINUTE_MS,
            state_sink=lambda record: states.append(record["state"]),
        )
        report = await coordinator.run()
        self.assertTrue(report.live_ready)
        self.assertEqual(tuple(states), STARTUP_SEQUENCE)
        self.assertEqual(report.states, STARTUP_SEQUENCE)
        self.assertEqual(store.committed_minutes, [1, 2, 3])

    async def test_exception_sets_fail_closed_state_and_incident(self):
        incidents = []
        coordinator = RecoveryCoordinator(
            binance_adapter=FakeBinanceAdapter(error=RuntimeError("offline")),
            polymarket_adapter=FakePolymarketAdapter(),
            buffered_events=[],
            commit_event=CommitStore().append,
            last_committed_open_ms=0,
            current_open_ms=MINUTE_MS,
            incident_sink=incidents.append,
        )
        with self.assertRaisesRegex(RecoveryBlockedError, "RECOVERY_FAILED"):
            await coordinator.run()
        self.assertEqual(coordinator.current_state, "RECOVERY_BLOCKED")
        self.assertEqual(incidents[0]["severity"], "CRITICAL")
        self.assertNotEqual(coordinator.current_state, "LIVE_READY")

    async def test_missing_gap_prevents_coordinator_live_ready(self):
        coordinator = RecoveryCoordinator(
            binance_adapter=FakeBinanceAdapter([minute(1), minute(3)]),
            polymarket_adapter=FakePolymarketAdapter(),
            buffered_events=[],
            commit_event=CommitStore().append,
            last_committed_open_ms=0,
            current_open_ms=4 * MINUTE_MS,
        )
        with self.assertRaisesRegex(
            RecoveryBlockedError,
            "BINANCE_CONTINUITY_BLOCKED",
        ):
            await coordinator.run()
        self.assertEqual(coordinator.current_state, "RECOVERY_BLOCKED")

    async def test_repeated_recovery_is_idempotent(self):
        store = CommitStore()

        def coordinator():
            return RecoveryCoordinator(
                binance_adapter=FakeBinanceAdapter([minute(1), minute(2)]),
                polymarket_adapter=FakePolymarketAdapter(),
                buffered_events=[
                    minute(2, "BUFFERED_DURING_RECOVERY"),
                    minute(3, "BUFFERED_DURING_RECOVERY"),
                ],
                commit_event=store.append,
                last_committed_open_ms=0,
                current_open_ms=4 * MINUTE_MS,
            )

        first = await coordinator().run()
        second = await coordinator().run()
        self.assertTrue(first.live_ready)
        self.assertTrue(second.live_ready)
        self.assertEqual(store.committed_minutes, [1, 2, 3])
        self.assertEqual(len(store.natural_keys), 3)


if __name__ == "__main__":
    unittest.main()
