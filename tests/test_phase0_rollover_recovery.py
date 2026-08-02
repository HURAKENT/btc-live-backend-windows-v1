from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.models import SourceEvent
from src.outbox import OutboxBroker
from src.runtime_adapters import MarketDiscovery, MarketDiscoveryPair
from src.runtime_orchestrator import C1RuntimeOrchestrator
from src.storage import SqliteStore
from tests.test_market_rollover import FakeRolloverPolymarketAdapter
from tests.test_runtime_orchestrator import (
    FakeBinanceRuntimeAdapter,
    runtime_event,
)


class SanitizedABCRolloverAdapter(FakeRolloverPolymarketAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.current_assets = tuple(f"a-asset-{index}" for index in range(22))
        self.next_assets = tuple(f"b-asset-{index}" for index in range(22))
        self.third_assets = tuple(f"c-asset-{index}" for index in range(22))
        self.fourth_assets = tuple(f"d-asset-{index}" for index in range(22))
        self.current = self._discovery("a-event", self.current_assets)
        self.next = self._discovery("b-event", self.next_assets)
        self.third = self._discovery("c-event", self.third_assets)
        self.fourth = self._discovery("d-event", self.fourth_assets)
        self.pair_calls = 0
        self.stream_queues: list[object] = []
        self.stream_cancelled: list[bool] = []
        self.after_c_pair_requested = asyncio.Event()

    async def discover_market_pair(self):
        self.pair_calls += 1
        if self.pair_calls == 1:
            return MarketDiscoveryPair(current=self.current, next=self.next)
        if self.pair_calls == 2:
            return MarketDiscoveryPair(current=self.next, next=self.third)
        self.after_c_pair_requested.set()
        return MarketDiscoveryPair(current=self.third, next=self.fourth)

    async def stream_next(self, *, asset_ids, queue, ready_event):
        self.next_subscriptions += 1
        self.next_queue = queue
        self.stream_queues.append(queue)
        ready_event.set()
        await queue.put(
            runtime_event(
                "polymarket",
                f"{asset_ids[0]}:live",
                "POLYMARKET_BOOK",
                180_000 + self.next_subscriptions,
                asset_id=asset_ids[0],
            )
        )
        try:
            await asyncio.Event().wait()
        finally:
            self.stream_cancelled.append(True)


class MismatchedRefreshAdapter(SanitizedABCRolloverAdapter):
    async def discover_market_pair(self):
        self.pair_calls += 1
        if self.pair_calls == 1:
            return MarketDiscoveryPair(current=self.current, next=self.next)
        return MarketDiscoveryPair(current=self.current, next=self.third)


class FailingPromotedStreamAdapter(SanitizedABCRolloverAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.stream_failures = [asyncio.Event(), asyncio.Event()]

    async def stream_next(self, *, asset_ids, queue, ready_event):
        index = self.next_subscriptions
        self.next_subscriptions += 1
        self.next_queue = queue
        self.stream_queues.append(queue)
        ready_event.set()
        await queue.put(
            runtime_event(
                "polymarket",
                f"{asset_ids[0]}:live",
                "POLYMARKET_BOOK",
                180_000 + self.next_subscriptions,
                asset_id=asset_ids[0],
            )
        )
        await self.stream_failures[index].wait()
        raise RuntimeError("SANITIZED_PROMOTED_STREAM_FAILURE")


def _runtime(
    *,
    run_id: str,
    store: SqliteStore,
    adapter: SanitizedABCRolloverAdapter,
    rollover_wait,
) -> C1RuntimeOrchestrator:
    return C1RuntimeOrchestrator(
        run_id=run_id,
        store=store,
        broker=OutboxBroker(),
        binance_adapter=FakeBinanceRuntimeAdapter(),
        polymarket_adapter=adapter,
        binance_start_ms=60_000,
        binance_end_ms=60_000,
        history_start_ts=1,
        history_end_ts=2,
        clock_ms=lambda: 1_000,
        enable_market_rollover=True,
        rollover_wait=rollover_wait,
    )


async def _abrupt_process_stop(runtime: C1RuntimeOrchestrator) -> None:
    tasks = tuple(runtime._tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class RecurringRolloverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name, "phase0.sqlite3")
        self.store = SqliteStore.open(self.database_path)
        self.store.migrate()
        self.adapter = SanitizedABCRolloverAdapter()
        self.gates = {
            "a-event": asyncio.Event(),
            "b-event": asyncio.Event(),
            "c-event": asyncio.Event(),
        }

        async def controlled_wait(discovery: MarketDiscovery) -> None:
            await self.gates[discovery.market_id].wait()

        self.runtime = _runtime(
            run_id="phase0-recurring",
            store=self.store,
            adapter=self.adapter,
            rollover_wait=controlled_wait,
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        self.store.close()
        self.temp.cleanup()

    async def test_rollover_recurs_from_a_to_b_to_c(self) -> None:
        await self.runtime.start()
        self.gates["a-event"].set()
        await self.runtime.wait_rollover()

        self.assertEqual(self.runtime.status().market_id, "b-event")
        self.assertEqual(self.adapter.pair_calls, 2)
        self.assertEqual(self.adapter.next_subscriptions, 1)

        self.gates["b-event"].set()
        await asyncio.wait_for(
            self.adapter.after_c_pair_requested.wait(), timeout=10
        )
        while self.runtime._rollover_cycle_count < 2:
            await asyncio.sleep(0)
        await self.runtime.wait_for_source_idle()

        self.assertEqual(self.runtime.status().market_id, "c-event")
        self.assertEqual(self.adapter.next_subscriptions, 2)
        self.assertEqual(self.store.count("market_catalog"), 3)
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status = 'C3_MARKET_ROLLOVER_PASS'"
            ),
            2,
        )
        self.assertEqual(self.runtime.writer_consumer_count, 1)

    async def test_refresh_identity_mismatch_fails_closed_after_committed_pass(
        self,
    ) -> None:
        await self.runtime.stop()
        self.adapter = MismatchedRefreshAdapter()

        async def immediate_rollover(_discovery: MarketDiscovery) -> None:
            return None

        self.runtime = _runtime(
            run_id="phase0-refresh-mismatch",
            store=self.store,
            adapter=self.adapter,
            rollover_wait=immediate_rollover,
        )

        await self.runtime.start()
        with self.assertRaisesRegex(
            RuntimeError,
            "ROLLOVER_REFRESH_CURRENT_IDENTITY_CONFLICT",
        ):
            await self.runtime.wait_rollover()

        self.assertEqual(self.runtime.status().market_id, "b-event")
        self.assertTrue(self.runtime.status().live_ready)
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status = 'C3_MARKET_ROLLOVER_PASS'"
            ),
            1,
        )
        self.assertEqual(
            self.store.scalar(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status = 'ROLLOVER_BLOCKED'"
            ),
            1,
        )

    async def test_promoted_stream_failure_is_fatal_while_next_cycle_armed(
        self,
    ) -> None:
        await self.runtime.stop()
        self.adapter = FailingPromotedStreamAdapter()
        first_gate = asyncio.Event()
        second_gate = asyncio.Event()

        async def controlled_wait(discovery: MarketDiscovery) -> None:
            gate = first_gate if discovery.market_id == "a-event" else second_gate
            await gate.wait()

        self.runtime = _runtime(
            run_id="phase0-promoted-failure",
            store=self.store,
            adapter=self.adapter,
            rollover_wait=controlled_wait,
        )
        await self.runtime.start()
        first_gate.set()
        await self.runtime.wait_rollover()
        self.adapter.stream_failures[0].set()

        async def wait_for_failure() -> None:
            while self.runtime.status().failure is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_failure(), timeout=10)
        self.assertIn(
            "SANITIZED_PROMOTED_STREAM_FAILURE",
            self.runtime.status().failure,
        )
        self.assertFalse(self.runtime.status().live_ready)

    async def test_stop_cancels_recurring_rollover_and_all_streams(self) -> None:
        await self.runtime.start()
        self.gates["a-event"].set()
        await self.runtime.wait_rollover()

        await self.runtime.stop()

        self.assertEqual(self.runtime.pending_owned_tasks, ())
        self.assertTrue(self.adapter.current_cancelled)
        self.assertGreaterEqual(len(self.adapter.stream_cancelled), 1)


class DurableCutoverRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_policy_at_each_other_durable_rollover_state(
        self,
    ) -> None:
        for state in (
            "NEXT_DISCOVERED",
            "NEXT_BUFFERING",
            "NEXT_RECONCILED",
            "CURRENT_LIVE",
        ):
            with self.subTest(state=state):
                await self._assert_restart_policy(state)

    async def test_partial_b_catalog_without_commit_marker_keeps_a_active(
        self,
    ) -> None:
        temp = tempfile.TemporaryDirectory()
        database_path = Path(temp.name, "phase0-partial.sqlite3")
        store = SqliteStore.open(database_path)
        store.migrate()
        adapter = SanitizedABCRolloverAdapter()
        identity_json = adapter.next.market_identity_json
        store.persist_market_identity(
            market_id=adapter.next.market_id,
            payload_json=identity_json,
            payload_sha256=hashlib.sha256(
                identity_json.encode("utf-8")
            ).hexdigest(),
            updated_at_ms=999,
        )

        async def do_not_roll(_discovery: MarketDiscovery) -> None:
            await asyncio.Event().wait()

        runtime = _runtime(
            run_id="phase0-partial-restart",
            store=store,
            adapter=adapter,
            rollover_wait=do_not_roll,
        )
        try:
            await runtime.start()
            self.assertTrue(runtime.status().live_ready)
            self.assertEqual(runtime.status().market_id, "a-event")
            self.assertEqual(adapter.pair_calls, 1)
        finally:
            await runtime.stop()
            store.close()
            temp.cleanup()

    async def test_restart_restores_durably_committed_b_not_stale_a(self) -> None:
        temp = tempfile.TemporaryDirectory()
        database_path = Path(temp.name, "phase0-restart.sqlite3")
        first_store = SqliteStore.open(database_path)
        first_store.migrate()
        first_adapter = SanitizedABCRolloverAdapter()

        async def immediate_rollover(_discovery: MarketDiscovery) -> None:
            return None

        first = _runtime(
            run_id="phase0-before-crash",
            store=first_store,
            adapter=first_adapter,
            rollover_wait=immediate_rollover,
        )
        durable_commit = asyncio.Event()
        never_continue = asyncio.Event()
        append_transition = first._append_rollover_incident

        async def pause_after_durable_commit(
            status: str,
            payload: dict,
            *,
            severity: str = "INFO",
        ) -> None:
            await append_transition(status, payload, severity=severity)
            if status == "CUTOVER_COMMITTED":
                durable_commit.set()
                await never_continue.wait()

        first._append_rollover_incident = pause_after_durable_commit
        try:
            await first.start()
            await asyncio.wait_for(durable_commit.wait(), timeout=10)
            self.assertEqual(
                first_store.scalar(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE status = 'CUTOVER_COMMITTED'"
                ),
                1,
            )
            await _abrupt_process_stop(first)
        finally:
            first_store.close()

        restarted_store = SqliteStore.open(database_path)
        restarted_store.migrate()
        restarted_adapter = SanitizedABCRolloverAdapter()

        async def do_not_roll_again(_discovery: MarketDiscovery) -> None:
            await asyncio.Event().wait()

        restarted = _runtime(
            run_id="phase0-after-crash",
            store=restarted_store,
            adapter=restarted_adapter,
            rollover_wait=do_not_roll_again,
        )
        try:
            await restarted.start()
            self.assertTrue(restarted.status().live_ready)
            self.assertEqual(restarted.status().market_id, "b-event")
            self.assertEqual(restarted_adapter.pair_calls, 2)
        finally:
            await restarted.stop()
            restarted_store.close()
            temp.cleanup()

    async def _assert_restart_policy(self, target_state: str) -> None:
        temp = tempfile.TemporaryDirectory()
        database_path = Path(temp.name, f"phase0-{target_state}.sqlite3")
        first_store = SqliteStore.open(database_path)
        first_store.migrate()
        first_adapter = SanitizedABCRolloverAdapter()

        async def immediate_rollover(_discovery: MarketDiscovery) -> None:
            return None

        first = _runtime(
            run_id=f"phase0-crash-{target_state}",
            store=first_store,
            adapter=first_adapter,
            rollover_wait=immediate_rollover,
        )
        reached = asyncio.Event()
        never_continue = asyncio.Event()
        append_transition = first._append_rollover_incident

        async def pause_at_target(
            status: str,
            payload: dict,
            *,
            severity: str = "INFO",
        ) -> None:
            await append_transition(status, payload, severity=severity)
            if status == target_state:
                reached.set()
                await never_continue.wait()

        first._append_rollover_incident = pause_at_target
        try:
            await first.start()
            await asyncio.wait_for(reached.wait(), timeout=10)
            await _abrupt_process_stop(first)
        finally:
            first_store.close()

        restarted_store = SqliteStore.open(database_path)
        restarted_store.migrate()
        restarted_adapter = SanitizedABCRolloverAdapter()
        replay_gate = asyncio.Event()

        async def controlled_restart(discovery: MarketDiscovery) -> None:
            await replay_gate.wait()

        restarted = _runtime(
            run_id=f"phase0-restart-{target_state}",
            store=restarted_store,
            adapter=restarted_adapter,
            rollover_wait=controlled_restart,
        )
        try:
            await restarted.start()
            committed = target_state == "CURRENT_LIVE"
            self.assertEqual(
                restarted.status().market_id,
                "b-event" if committed else "a-event",
            )
            if not committed:
                replay_gate.set()
                await restarted.wait_rollover()
                self.assertEqual(restarted.status().market_id, "b-event")
        finally:
            await restarted.stop()
            restarted_store.close()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
