from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp

from src.binance_provider import iter_binance_backfill
from src.performance_catchup_source import (
    fetch_gamma_daily_range_inventory,
    project_gamma_daily_range_inventory,
)
from src.performance_engine import StrategyPerformanceEngine
from src.performance_forward import ForwardSettlementProjector
from src.performance_reconstruction import (
    RecoveredStrategyReconstructor,
    TerminalDistributionRecoveryContract,
    build_recovered_market_strategy_input,
)
from src.performance_repository import PerformanceRepository
from src.polymarket_provider import iter_price_history
from src.storage import SqliteStore


MINUTE_MS = 60_000


async def _history(
    session: aiohttp.ClientSession,
    asset_id: str,
    *,
    start_ts: int,
    end_ts: int,
    semaphore: asyncio.Semaphore,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    points: list[dict[str, Any]] = []
    async with semaphore:
        async for event in iter_price_history(
            session, asset_id=asset_id, start_ts=start_ts, end_ts=end_ts
        ):
            payload = json.loads(event.payload_json)
            points.append({
                "timestamp_ms": int(payload["timestamp_seconds"]) * 1000,
                "price": int(payload["price_micros"]) / 1_000_000,
            })
    return asset_id, tuple(points)


async def _acquire(
    session: aiohttp.ClientSession,
    market_payload: dict[str, Any],
    contract: TerminalDistributionRecoveryContract,
    semaphore: asyncio.Semaphore,
):
    resolution = datetime.fromisoformat(
        str(market_payload["resolution_utc"]).replace("Z", "+00:00")
    )
    resolution_ms = int(resolution.timestamp() * 1000)
    candles = []
    async with semaphore:
        async for event in iter_binance_backfill(
            session,
            resolution_ms - (1080 + 181) * MINUTE_MS,
            resolution_ms - 31 * MINUTE_MS,
        ):
            payload = json.loads(event.payload_json)
            candles.append({
                "close_time_ms": int(payload["close_time_ms"]),
                "close": float(payload["close"]),
            })
    histories = dict(await asyncio.gather(*[
        _history(
            session, str(asset_id),
            start_ts=resolution_ms // 1000 - 24 * 60 * 60,
            end_ts=resolution_ms // 1000 - 30 * 60,
            semaphore=semaphore,
        )
        for asset_id in market_payload["asset_ids"]
    ]))
    return build_recovered_market_strategy_input(
        market_payload=market_payload,
        history_by_asset=histories,
        binance_candles=tuple(candles),
        contract=contract,
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    output_db = Path(args.output_db).resolve()
    if args.source_db:
        if output_db.exists():
            raise ValueError("OUTPUT_DB_ALREADY_EXISTS")
        source_connection = sqlite3.connect(str(Path(args.source_db).resolve()))
        target_connection = sqlite3.connect(str(output_db))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
    elif output_db.exists():
        raise ValueError("OUTPUT_DB_ALREADY_EXISTS")
    store = SqliteStore.open(output_db)
    try:
        store.migrate()
        repository = PerformanceRepository(store)
        timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            inventory = await fetch_gamma_daily_range_inventory(
                session=session,
                gamma_events_url="https://gamma-api.polymarket.com/events/keyset",
                start_date=args.start_date,
                end_date=args.end_date,
            )
            observed_at_ms = int(datetime.now().timestamp() * 1000)
            projection = project_gamma_daily_range_inventory(
                events=inventory.events,
                start_date=args.start_date,
                end_date=args.end_date,
                observed_at_ms=observed_at_ms,
                inventory_complete=inventory.complete,
            )
            for market in projection.markets:
                existing = store.rows(
                    "SELECT payload_sha256 FROM market_catalog WHERE market_id = ?",
                    (market.market_id,),
                )
                if not existing:
                    store.persist_market_identity(
                        market_id=market.market_id,
                        payload_json=market.payload_json,
                        payload_sha256=market.payload_sha256,
                        updated_at_ms=market.observed_at_ms,
                    )
            for event in projection.events:
                store.append_source_event(event)

            contract = TerminalDistributionRecoveryContract.load(PROJECT_ROOT)
            semaphore = asyncio.Semaphore(args.concurrency)
            acquired = await asyncio.gather(*[
                _acquire(session, json.loads(market.payload_json), contract, semaphore)
                for market in projection.markets
            ], return_exceptions=True)

        receipts = []
        failures = []
        reconstructor = RecoveredStrategyReconstructor(
            project_root=PROJECT_ROOT, repository=repository
        )
        for market, item in zip(projection.markets, acquired):
            market_date = json.loads(market.payload_json)["market_date"]
            if isinstance(item, BaseException):
                failures.append({
                    "market_date": market_date,
                    "error_class": type(item).__name__,
                    "detail": str(item),
                })
                continue
            receipts.append(reconstructor.reconstruct(item))

        resolutions = ForwardSettlementProjector(
            store=store, repository=repository,
            reconciled_at_ms=int(datetime.now().timestamp() * 1000),
        ).project()
        resolution_results = [repository.append_resolution(row) for row in resolutions]
        if store.count("strategy_performance_observations"):
            StrategyPerformanceEngine(
                project_root=PROJECT_ROOT, repository=repository
            ).refresh_all(as_of_date=args.end_date)

        before_second = {
            table: store.count(table)
            for table in (
                "strategy_performance_observations",
                "strategy_performance_resolutions",
                "strategy_reconstruction_status",
                "strategy_performance_materialization_revisions",
            )
        }
        second = [reconstructor.reconstruct(item) for item in acquired if not isinstance(item, BaseException)]
        after_second = {
            table: store.count(table) for table in before_second
        }
        statuses = repository.read_reconstruction_statuses()
        observations = repository.read_observations()
        controlled_gap = None
        if args.controlled_gap_date:
            target_input = next(
                item for item in acquired
                if not isinstance(item, BaseException)
                and item.market_date == args.controlled_gap_date
            )
            target_observations = [
                row for row in observations
                if row.market_date == args.controlled_gap_date
                and row.provenance_run_id.startswith("RECOVERED_RETROSPECTIVE:")
            ]
            target_keys = {row.observation_key for row in target_observations}
            target_hashes = {row.observation_key: row.payload_sha256 for row in target_observations}
            placeholders = ",".join("?" for _ in target_keys)
            store._connection.execute("BEGIN IMMEDIATE")
            if target_keys:
                store._connection.execute(
                    f"DELETE FROM strategy_performance_resolutions WHERE observation_key IN ({placeholders})",
                    tuple(sorted(target_keys)),
                )
                store._connection.execute(
                    f"DELETE FROM strategy_performance_observations WHERE observation_key IN ({placeholders})",
                    tuple(sorted(target_keys)),
                )
            store._connection.execute(
                "DELETE FROM strategy_reconstruction_status WHERE market_date = ?",
                (args.controlled_gap_date,),
            )
            store._connection.commit()
            restored_receipt = reconstructor.reconstruct(target_input)
            restored_resolutions = ForwardSettlementProjector(
                store=store, repository=repository,
                reconciled_at_ms=int(datetime.now().timestamp() * 1000),
            ).project()
            for resolution in restored_resolutions:
                repository.append_resolution(resolution)
            StrategyPerformanceEngine(
                project_root=PROJECT_ROOT, repository=repository
            ).refresh_all(as_of_date=args.end_date)
            restored = repository.read_observations()
            restored_hashes = {
                row.observation_key: row.payload_sha256 for row in restored
                if row.market_date == args.controlled_gap_date
                and row.provenance_run_id.startswith("RECOVERED_RETROSPECTIVE:")
            }
            controlled_gap = {
                "market_date": args.controlled_gap_date,
                "deleted_observation_count": len(target_keys),
                "restored_observation_count": len(restored_hashes),
                "restored_identity_status_count": len(
                    repository.read_reconstruction_statuses(
                        market_date=args.controlled_gap_date
                    )
                ),
                "payload_hashes_identical": restored_hashes == target_hashes,
                "recovery_receipt": {
                    "observation_inserted_count": restored_receipt.observation_inserted_count,
                    "status_inserted_count": restored_receipt.status_inserted_count,
                },
            }
        return {
            "schema_version": "STRATEGY_GAP_RECOVERY_VERIFICATION_V1",
            "date_range": [args.start_date, args.end_date],
            "market_count": len(projection.markets),
            "absence_event_count": sum(event.event_type.endswith("ABSENT") for event in projection.events),
            "acquisition_failure_count": len(failures),
            "acquisition_failures": failures,
            "identity_status_count": len(statuses),
            "identity_status_counts": dict(sorted(Counter(row.status for row in statuses).items())),
            "reason_counts": dict(sorted(Counter(row.reason_code for row in statuses).items())),
            "recovered_identity_count": len({row.strategy_id for row in statuses if row.status == "RECOVERED"}),
            "recovered_observation_count": sum(
                row.provenance_run_id.startswith("RECOVERED_RETROSPECTIVE:")
                for row in observations
            ),
            "original_observation_count": sum(
                row.source_layer == "HISTORICAL"
                and not row.provenance_run_id.startswith("RECOVERED_RETROSPECTIVE:")
                for row in observations
            ),
            "forward_observation_count": sum(row.source_layer == "FORWARD" for row in observations),
            "resolution_inserted_count": sum(result.inserted for result in resolution_results),
            "second_recovery_receipts": [
                {
                    "observation_inserted_count": row.observation_inserted_count,
                    "observation_replayed_count": row.observation_replayed_count,
                    "status_inserted_count": row.status_inserted_count,
                    "status_replayed_count": row.status_replayed_count,
                }
                for row in second
            ],
            "idempotent_second_recovery": before_second == after_second,
            "counts_before_second": before_second,
            "counts_after_second": after_second,
            "controlled_gap": controlled_gap,
            "security": {
                "real_orders": False,
                "wallet": False,
                "signing": False,
                "authenticated_CLOB_writes": False,
                "trading_approval": False,
            },
        }
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-db", required=True)
    parser.add_argument("--source-db")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--controlled-gap-date")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
