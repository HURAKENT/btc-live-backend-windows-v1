from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from src.api import create_api_app
from src.current_input import frozen_model_probabilities
from src.fixed_point import ProbabilityMicros
from src.mvp_current_runtime import CurrentMarketSnapshotV1, StrictACurrentInputSource
from src.mvp_paper_runtime import MvpPaperRuntime
from src.outbox import OutboxBroker
from src.polymarket_provider import MarketBook
from src.storage import SqliteStore
from src.strategy_v1 import evaluate_strict_a
from tests.test_current_input import OBSERVED_AT_MS, _model_candles


class MvpVerticalAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_strict_to_paper_rest_ws_dashboard_vertical(self):
        contract_path = Path(
            "strategy_sources/frozen/contracts/STRICT_A_MODEL_FROZEN.json"
        )
        bounds = tuple(
            [(None, 99_500.0)]
            + [
                (99_500.0 + index * 100.0, 99_600.0 + index * 100.0)
                for index in range(9)
            ]
            + [(100_400.0, None)]
        )
        model = frozen_model_probabilities(
            _model_candles(),
            observed_at_ms=OBSERVED_AT_MS,
            checkpoint_minutes=60,
            bucket_bounds=bounds,
            contract_path=contract_path,
            paper_authorized=True,
        )
        favorite = max(range(11), key=lambda index: model[index].value)
        books = []
        for index in range(11):
            book = MarketBook(f"yes-{index}")
            book.asks = {20_000: 6_000_000}
            book.book_hash = f"book-{index}"
            book.source_timestamp_ms = OBSERVED_AT_MS - 1_000
            books.append(book)
        snapshot = CurrentMarketSnapshotV1(
            market_id="market-vertical",
            market_date="2026-08-11",
            bucket_bounds=bounds,
            yes_token_ids=tuple(f"yes-{index}" for index in range(11)),
            no_token_ids=tuple(f"no-{index}" for index in range(11)),
            yes_books=tuple(books),
            market_q_yes=tuple(
                ProbabilityMicros(20_000 if index == favorite else 10_000)
                for index in range(11)
            ),
            fee_schedules=tuple(
                {"rate": 0.07, "exponent": 1, "takerOnly": True}
                for _ in range(11)
            ),
            price_history_source_sha256="a" * 64,
            price_history_observed_at_ms=OBSERVED_AT_MS - 1_000,
        )

        async def candles(_observed_at_ms):
            return _model_candles()

        async def market(_observed_at_ms):
            return snapshot

        source = StrictACurrentInputSource(
            candle_loader=candles,
            market_loader=market,
            prior_position_loader=lambda _market_date: False,
            clock_ms=lambda: OBSERVED_AT_MS,
            contract_path=contract_path,
            paper_authorized=True,
        )
        due = SimpleNamespace(
            origin="LIVE",
            schedule=SimpleNamespace(
                strategy_id="YES_STRICT_A_OPERATIONAL",
                market_id="market-vertical",
                checkpoint_minutes=60,
                schedule_key="vertical-schedule",
            ),
        )
        capture = await source.capture(due=due, current=False)
        evaluation = evaluate_strict_a(
            "YES_STRICT_A_OPERATIONAL",
            60,
            capture.executable_input.buckets,
            prior_position=False,
            price_history_evidence=capture.executable_input.strict_price_history_evidence,
        )
        self.assertTrue(evaluation.accepted, evaluation.reason)

        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStore.open(Path(directory) / "runtime.sqlite3")
            store.migrate()
            market_json = json.dumps(
                {"market_date": snapshot.market_date, "market_id": snapshot.market_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            store.persist_market_identity(
                market_id=snapshot.market_id,
                payload_json=market_json,
                payload_sha256=hashlib.sha256(market_json.encode()).hexdigest(),
                updated_at_ms=OBSERVED_AT_MS,
            )
            broker = OutboxBroker()
            result = MvpPaperRuntime(store, broker).execute(
                evaluation_key="vertical-evaluation",
                input_snapshot_hash="b" * 64,
                evaluation=evaluation,
                evidence=source.evidence_for("vertical-schedule"),
                evaluated_at_ms=OBSERVED_AT_MS,
            )
            self.assertEqual(result.fill.shares_micros, 5_000_000)
            read_store = store.open_read_store()
            client = TestClient(TestServer(create_api_app(read_store, broker)))
            await client.start_server()
            try:
                bootstrap = await (await client.get("/api/v1/bootstrap")).json()
                self.assertEqual(bootstrap["current_market_identity"]["payload"]["market_date"], "2026-08-11")
                self.assertEqual(bootstrap["paper_positions"][0]["filled_shares_micros"], 5_000_000)
                self.assertEqual((await client.get("/dashboard")).status, 200)
                websocket = await client.ws_connect(
                    "/ws/v1/events?after_event_id=0"
                )
                topics = []
                for _ in range(bootstrap["last_event_id"]):
                    topics.append((await websocket.receive_json(timeout=2))["topic"])
                await websocket.close()
                self.assertIn("signal.strict_a", topics)
                self.assertIn("paper.fill", topics)
                self.assertIn("paper.account", topics)
            finally:
                await client.close()
                read_store.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
