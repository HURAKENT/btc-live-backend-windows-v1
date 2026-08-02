from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.models import SourceEvent
from src.outbox import OutboxBroker
from src.runtime_adapters import (
    MarketDiscovery,
    MarketDiscoveryPair,
    MarketReconciliation,
)
from src.runtime_orchestrator import C1RuntimeOrchestrator
from src.storage import SqliteStore
from tests.test_polymarket_provider import discovery_event
from tests.test_runtime_orchestrator import (
    FakeBinanceRuntimeAdapter,
    runtime_event,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def event(event_id: str, day: int, hour: int = 16) -> dict:
    return discovery_event(
        event_id=event_id,
        end_date=f"2026-08-{day:02d}T{hour:02d}:00:00Z",
        current_naming=True,
    )


class MarketPairSelectionTests(unittest.TestCase):
    def api(self):
        from src.market_rollover import (
            MarketPair,
            build_market_pair,
            market_identity_sha256,
        )

        return MarketPair, build_market_pair, market_identity_sha256

    def test_two_earliest_identities_are_current_and_next(self):
        pair_type, build, _ = self.api()

        pair = build(
            [event("later", 4), event("current", 2), event("next", 3)],
            now_utc=NOW,
        )

        self.assertIs(type(pair), pair_type)
        self.assertEqual(pair.current.event_id, "current")
        self.assertEqual(pair.next.event_id, "next")
        self.assertEqual(len(pair.current.market_ids), 11)
        self.assertEqual(len(pair.next.asset_ids), 22)

    def test_selection_is_independent_of_input_order(self):
        _, build, _ = self.api()
        payload = [event("current", 2), event("next", 3), event("later", 4)]

        self.assertEqual(
            build(payload, now_utc=NOW),
            build(list(reversed(payload)), now_utc=NOW),
        )

    def test_exact_duplicate_event_identity_is_idempotent(self):
        _, build, _ = self.api()
        current = event("current", 2)

        pair = build(
            [current, copy.deepcopy(current), event("next", 3)],
            now_utc=NOW,
        )

        self.assertEqual(pair.current.event_id, "current")

    def test_same_event_id_with_changed_identity_is_conflict(self):
        _, build, _ = self.api()
        first = event("same", 2)
        changed = event("same", 3)

        with self.assertRaisesRegex(
            ValueError,
            "MARKET_ROLLOVER_IDENTITY_CONFLICT",
        ):
            build([first, changed, event("next", 4)], now_utc=NOW)

    def test_equal_current_resolution_is_ambiguous(self):
        _, build, _ = self.api()

        with self.assertRaisesRegex(
            ValueError,
            "AMBIGUOUS_CURRENT_MARKET_IDENTITY",
        ):
            build(
                [event("current-a", 2), event("current-b", 2), event("next", 3)],
                now_utc=NOW,
            )

    def test_equal_next_resolution_is_ambiguous(self):
        _, build, _ = self.api()

        with self.assertRaisesRegex(
            ValueError,
            "AMBIGUOUS_NEXT_MARKET_IDENTITY",
        ):
            build(
                [event("current", 2), event("next-a", 3), event("next-b", 3)],
                now_utc=NOW,
            )

    def test_missing_or_malformed_next_is_data_gap(self):
        _, build, _ = self.api()
        malformed = event("malformed-next", 3)
        malformed["markets"].pop()

        with self.assertRaisesRegex(
            ValueError,
            "NEXT_BTC_DAILY_RANGE_DATA_GAP",
        ):
            build([event("current", 2), malformed], now_utc=NOW)

    def test_expired_only_payload_is_data_gap(self):
        _, build, _ = self.api()

        with self.assertRaisesRegex(ValueError, "BTC_DAILY_RANGE_DATA_GAP"):
            build([event("expired", 1, 11)], now_utc=NOW)

    def test_current_and_next_cannot_share_event_identity(self):
        _, build, _ = self.api()
        duplicate = event("same", 2)

        with self.assertRaisesRegex(
            ValueError,
            "NEXT_BTC_DAILY_RANGE_DATA_GAP",
        ):
            build([duplicate, copy.deepcopy(duplicate)], now_utc=NOW)

    def test_identity_hash_is_deterministic_and_order_sensitive(self):
        _, build, digest = self.api()
        pair = build([event("current", 2), event("next", 3)], now_utc=NOW)

        self.assertEqual(
            digest(pair.current),
            digest(pair.current),
        )
        self.assertNotEqual(digest(pair.current), digest(pair.next))
        self.assertEqual(len(digest(pair.current)), 64)

    def test_pair_is_immutable(self):
        _, build, _ = self.api()
        pair = build([event("current", 2), event("next", 3)], now_utc=NOW)

        with self.assertRaises((AttributeError, TypeError)):
            pair.current = pair.next


class RolloverLifecycleTests(unittest.TestCase):
    def api(self):
        from src.market_rollover import (
            ROLLOVER_SEQUENCE,
            MarketRolloverLifecycle,
        )

        return ROLLOVER_SEQUENCE, MarketRolloverLifecycle

    def test_lifecycle_follows_exact_sequence(self):
        sequence, lifecycle_type = self.api()
        lifecycle = lifecycle_type()

        for state in sequence[1:]:
            self.assertTrue(lifecycle.transition(state))

        self.assertEqual(lifecycle.states, sequence)
        self.assertEqual(lifecycle.current_state, "CURRENT_LIVE")

    def test_duplicate_transition_is_idempotent(self):
        _, lifecycle_type = self.api()
        lifecycle = lifecycle_type()

        self.assertFalse(lifecycle.transition("CURRENT_LIVE"))
        self.assertTrue(lifecycle.transition("NEXT_DISCOVERED"))
        self.assertFalse(lifecycle.transition("NEXT_DISCOVERED"))

    def test_out_of_order_transition_fails_closed(self):
        _, lifecycle_type = self.api()
        lifecycle = lifecycle_type()

        with self.assertRaisesRegex(ValueError, "INVALID_ROLLOVER_TRANSITION"):
            lifecycle.transition("NEXT_RECONCILED")
        self.assertEqual(lifecycle.current_state, "CURRENT_LIVE")

    def test_blocked_lifecycle_cannot_cut_over(self):
        _, lifecycle_type = self.api()
        lifecycle = lifecycle_type()
        lifecycle.transition("NEXT_DISCOVERED")
        self.assertTrue(lifecycle.block("NEXT_DISCOVERY_FAILED"))

        self.assertEqual(lifecycle.current_state, "ROLLOVER_BLOCKED")
        self.assertEqual(lifecycle.blocker, "NEXT_DISCOVERY_FAILED")
        with self.assertRaisesRegex(ValueError, "ROLLOVER_BLOCKED"):
            lifecycle.transition("NEXT_BUFFERING")

    def test_lifecycle_adds_no_task_or_network_side_effect(self):
        _, lifecycle_type = self.api()
        lifecycle = lifecycle_type()

        self.assertFalse(hasattr(lifecycle, "task"))
        self.assertFalse(hasattr(lifecycle, "session"))


class FakeRolloverPolymarketAdapter:
    def __init__(self, *, incomplete_next: bool = False) -> None:
        self.current_assets = tuple(
            f"current-asset-{index}" for index in range(22)
        )
        self.next_assets = tuple(
            f"next-asset-{index}" for index in range(22)
        )
        self.later_assets = tuple(
            f"later-asset-{index}" for index in range(22)
        )
        self.current = self._discovery("current-event", self.current_assets)
        self.next = self._discovery("next-event", self.next_assets)
        self.later = self._discovery("later-event", self.later_assets)
        self.pair_calls = 0
        self.incomplete_next = incomplete_next
        self.current_ready = __import__("asyncio").Event()
        self.current_cancelled = False
        self.next_cancelled = False
        self.next_subscriptions = 0
        self.next_queue = None
        self.closed = 0

    @staticmethod
    def _discovery(market_id: str, assets: tuple[str, ...]) -> MarketDiscovery:
        identity_json = json.dumps(
            {"asset_ids": list(assets), "event_id": market_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        return MarketDiscovery(
            market_identity_json=identity_json,
            market_id=market_id,
            market_ids=tuple(
                f"{market_id}-market-{index}" for index in range(11)
            ),
            asset_ids=assets,
            historical_depth="NOT_AVAILABLE_NOT_REQUIRED",
        )

    @staticmethod
    def _books(discovery: MarketDiscovery, count: int = 22):
        return tuple(
            runtime_event(
                "polymarket",
                f"{discovery.market_id}:book:{asset_id}",
                "POLYMARKET_BOOK",
                index + 1,
                asset_id=asset_id,
            )
            for index, asset_id in enumerate(discovery.asset_ids[:count])
        )

    async def discover_market_pair(self):
        self.pair_calls += 1
        if self.pair_calls == 1:
            return MarketDiscoveryPair(current=self.current, next=self.next)
        return MarketDiscoveryPair(current=self.next, next=self.later)

    async def reconcile_current_books(self, discovery):
        count = (
            21
            if self.incomplete_next and discovery.market_id == "next-event"
            else 22
        )
        return MarketReconciliation(
            market_identity_json=discovery.market_identity_json,
            market_id=discovery.market_id,
            market_ids=discovery.market_ids,
            asset_ids=discovery.asset_ids,
            current_events=self._books(discovery, count),
            historical_depth=discovery.historical_depth,
        )

    async def recover(self, reconciliation, *, start_ts, end_ts, **_kwargs):
        return []

    async def stream(self, *, asset_ids, queue):
        self.current_ready.set()
        await queue.put(
            runtime_event(
                "polymarket",
                "current-event:live",
                "POLYMARKET_BOOK",
                120_001,
                asset_id=self.current_assets[0],
            )
        )
        try:
            await __import__("asyncio").Event().wait()
        finally:
            self.current_cancelled = True

    async def wait_stream_ready(self):
        await self.current_ready.wait()

    async def stream_next(self, *, asset_ids, queue, ready_event):
        self.next_subscriptions += 1
        self.next_queue = queue
        ready_event.set()
        await queue.put(
            runtime_event(
                "polymarket",
                "next-event:live",
                "POLYMARKET_BOOK",
                180_001,
                asset_id=asset_ids[0],
            )
        )
        try:
            await __import__("asyncio").Event().wait()
        finally:
            self.next_cancelled = True

    async def close(self):
        self.closed += 1


class RuntimeRolloverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteStore.open(Path(self.temp.name, "rollover.sqlite3"))
        self.store.migrate()
        self.polymarket = FakeRolloverPolymarketAdapter()
        try:
            self.runtime = self._runtime(self.polymarket)
        except BaseException:
            self.store.close()
            self.temp.cleanup()
            raise

    async def asyncTearDown(self):
        await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    def _runtime(self, polymarket):
        rollover_count = 0
        later_cycle = __import__("asyncio").Event()

        async def immediate_rollover(_discovery):
            nonlocal rollover_count
            rollover_count += 1
            if rollover_count > 1:
                await later_cycle.wait()

        return C1RuntimeOrchestrator(
            run_id="c3-rollover-run",
            store=self.store,
            broker=OutboxBroker(),
            binance_adapter=FakeBinanceRuntimeAdapter(),
            polymarket_adapter=polymarket,
            binance_start_ms=60_000,
            binance_end_ms=60_000,
            history_start_ts=1,
            history_end_ts=2,
            clock_ms=lambda: 1_000,
            enable_market_rollover=True,
            rollover_wait=immediate_rollover,
        )

    async def test_complete_rollover_switches_identity_after_reconciliation(self):
        await self.runtime.start()
        await self.runtime.wait_rollover()

        self.assertEqual(self.runtime.status().market_id, "next-event")
        self.assertEqual(self.store.count("market_catalog"), 2)
        self.assertEqual(self.polymarket.next_subscriptions, 1)
        self.assertTrue(self.polymarket.current_cancelled)
        self.assertEqual(
            self.runtime.rollover_summary.status,
            "C3_MARKET_ROLLOVER_PASS",
        )
        self.assertEqual(self.runtime.writer_consumer_count, 1)

    async def test_rollover_persists_exact_state_sequence(self):
        await self.runtime.start()
        await self.runtime.wait_rollover()

        rows = self.store.rows(
            """
            SELECT status FROM incidents
            WHERE incident_key LIKE 'rollover:c3-rollover-run:%'
            ORDER BY incident_id
            """
        )
        self.assertEqual(
            tuple(row[0] for row in rows),
            (
                "NEXT_DISCOVERED",
                "NEXT_BUFFERING",
                "NEXT_RECONCILED",
                "CUTOVER_COMMITTED",
                "CURRENT_LIVE",
                "C3_MARKET_ROLLOVER_PASS",
            ),
        )

    async def test_incomplete_next_keeps_current_projection_live(self):
        await self.runtime.stop()
        self.polymarket = FakeRolloverPolymarketAdapter(incomplete_next=True)
        self.runtime = self._runtime(self.polymarket)

        await self.runtime.start()
        with self.assertRaisesRegex(RuntimeError, "ROLLOVER_BLOCKED"):
            await self.runtime.wait_rollover()

        self.assertEqual(self.runtime.status().market_id, "current-event")
        self.assertTrue(self.runtime.status().live_ready)
        self.assertEqual(self.store.count("market_catalog"), 1)
        self.assertEqual(self.store.scalar(
            "SELECT COUNT(*) FROM source_events WHERE natural_key LIKE 'next-event:%'"
        ), 0)

    async def test_wait_rollover_is_idempotent_after_pass(self):
        await self.runtime.start()
        await self.runtime.wait_rollover()
        await self.runtime.wait_rollover()

        self.assertEqual(self.polymarket.next_subscriptions, 1)
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status = 'C3_MARKET_ROLLOVER_PASS'"
            ),
            1,
        )

    async def test_promoted_stream_events_are_committed_not_buffered(self):
        await self.runtime.start()
        await self.runtime.wait_rollover()
        before = self.store.count("source_events")

        await self.polymarket.next_queue.put(
            runtime_event(
                "polymarket",
                "next-event:post-cutover-live",
                "POLYMARKET_BOOK",
                240_001,
                asset_id=self.polymarket.next_assets[1],
            )
        )
        await self.runtime.wait_for_source_idle()

        self.assertEqual(self.store.count("source_events"), before + 1)
        self.assertEqual(self.runtime.pending_source_events, 0)

    async def test_stop_cleans_current_next_and_rollover_tasks(self):
        await self.runtime.start()
        await self.runtime.wait_rollover()
        await self.runtime.stop()

        self.assertEqual(self.runtime.pending_owned_tasks, ())
        self.assertTrue(self.polymarket.next_cancelled)


if __name__ == "__main__":
    unittest.main()
