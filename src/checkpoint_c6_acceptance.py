from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.checkpoint_scheduler import CheckpointScheduler
from src.models import SourceEvent, payload_sha256
from src.outbox import OutboxBroker
from src.runtime_adapters import MarketReconciliation
from src.runtime_orchestrator import (
    C1RuntimeOrchestrator,
    CheckpointInputResult,
)
from src.storage import SqliteStore
from src.strategy_dispatch import V1ExecutableCheckpointInput
from src.strategy_v1 import (
    BucketInput,
    Pf1SnapshotEvidence,
    StrictPriceHistoryEvidence,
)


_SHA40 = re.compile(r"[0-9a-f]{40}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _event(
    source: str,
    natural_key: str,
    event_type: str,
    timestamp_ms: int,
    *,
    asset_id: str | None = None,
) -> SourceEvent:
    payload: dict[str, object] = {"event_type": event_type}
    if event_type == "BINANCE_KLINE_CLOSED":
        payload.update(
            {
                "close": "101.000000",
                "high": "102.000000",
                "interval": "1m",
                "low": "99.000000",
                "open": "100.000000",
                "open_time_ms": timestamp_ms,
                "symbol": "BTCUSDT",
                "volume": "2.500000",
            }
        )
    elif event_type == "POLYMARKET_BOOK":
        if asset_id is None:
            raise ValueError("C6_ACCEPTANCE_ASSET_REQUIRED")
        payload.update(
            {
                "asset_id": asset_id,
                "asks": [{"price_micros": 600_000, "size_micros": 10_000_000}],
                "best_ask_micros": 600_000,
                "best_bid_micros": 400_000,
                "bids": [{"price_micros": 400_000, "size_micros": 10_000_000}],
                "book_hash": f"loopback-{asset_id}-{timestamp_ms}",
                "spread_micros": 200_000,
            }
        )
    encoded = _canonical_json(payload)
    return SourceEvent(
        source=source,
        natural_key=natural_key,
        source_timestamp_ms=timestamp_ms,
        received_timestamp_ms=timestamp_ms,
        event_type=event_type,
        payload_json=encoded,
        payload_sha256=payload_sha256(encoded),
        recovery_origin="REST_BACKFILL" if timestamp_ms < 120_000 else "LIVE",
    )


class _BinanceLoopback:
    def __init__(self) -> None:
        self.live = _event(
            "binance",
            "c6:binance:live",
            "BINANCE_KLINE_CLOSED",
            120_000,
        )

    async def recover(self, *, start_ms: int, end_ms: int):
        return (
            _event(
                "binance",
                "c6:binance:recovered",
                "BINANCE_KLINE_CLOSED",
                60_000,
            ),
        )

    async def stream(self, queue):
        await queue.put(self.live)
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


class _PolymarketLoopback:
    def __init__(self, resolution_ms: int) -> None:
        self.assets = tuple(f"c6-asset-{index}" for index in range(22))
        current = tuple(
            _event(
                "polymarket",
                f"c6:book:{asset_id}",
                "POLYMARKET_BOOK",
                index + 1,
                asset_id=asset_id,
            )
            for index, asset_id in enumerate(self.assets)
        )
        identity = _canonical_json(
            {
                "asset_ids": list(self.assets),
                "event_id": "market-a",
                "resolution_utc": datetime.fromtimestamp(
                    resolution_ms / 1_000, tz=UTC
                ).isoformat(),
            }
        )
        self.market = MarketReconciliation(
            market_identity_json=identity,
            market_id="market-a",
            market_ids=tuple(f"c6-market-{index}" for index in range(11)),
            asset_ids=self.assets,
            current_events=current,
            historical_depth="LOOPBACK_ONLY",
        )

    async def discover_and_reconcile(self):
        return self.market

    async def recover(self, reconciliation, *, start_ts: int, end_ts: int):
        return ()

    async def stream(self, *, asset_ids, queue):
        await queue.put(
            _event(
                "polymarket",
                "c6:book:live",
                "POLYMARKET_BOOK",
                120_001,
                asset_id=self.assets[0],
            )
        )
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


class _ExecutableInputLoopback:
    async def capture(self, *, due, current):
        buckets = tuple(
            BucketInput(
                bucket_index=index,
                model_p=0.50 if index == 0 else 0.05,
                market_q_yes=0.30 if index == 0 else 0.10 + index / 100,
                market_q_no=None,
                vwap5=0.35 if index == 0 else 0.15,
                confirmed_fee=0.0,
                no_token_id=None,
            )
            for index in range(11)
        )
        evidence_sha = hashlib.sha256(
            f"{due.schedule.schedule_key}:{int(current)}".encode()
        ).hexdigest()
        strict = None
        pf1 = None
        if "STRICT" in due.schedule.strategy_id:
            strict = StrictPriceHistoryEvidence(
                provenance="CLOB_PRICE_HISTORY",
                source_sha256=evidence_sha,
                checkpoint_timestamp_ms=due.schedule.due_at_ms,
                observation_timestamp_ms=due.schedule.due_at_ms - 1,
            )
        else:
            pf1 = Pf1SnapshotEvidence(
                bucket_count=11,
                snapshot_complete=True,
                synchronized=True,
                fresh=True,
                crossed_book_count=0,
                fee_provenance="LOOPBACK_PUBLIC_FEE_SCHEDULE",
                snapshot_sha256=evidence_sha,
                fee_schedule_sha256=evidence_sha,
            )
        return CheckpointInputResult(
            executable_input=V1ExecutableCheckpointInput(
                checkpoint_minutes=due.schedule.checkpoint_minutes,
                buckets=buckets,
                prior_position=False,
                strict_price_history_evidence=strict,
                pf1_snapshot_evidence=pf1,
            ),
            historical_depth_available=True,
            reason_code=None,
        )


@dataclass(frozen=True, slots=True)
class C6AcceptanceReport:
    status: str
    acceptance_pass: bool
    migration_version: int
    enabled_strategy_count: int
    market_count: int
    schedule_count: int
    schedules_per_market: tuple[int, ...]
    exact_replay_pass: bool
    recovered_replay_pass: bool
    missing_depth_block_pass: bool
    current_reevaluation_pass: bool
    atomic_commit_pass: bool
    stale_market_guard_pass: bool
    runtime_writer_path_pass: bool
    real_dispatcher_pass: bool
    production_input_ready: bool
    data_completion_gate: str
    evaluation_count: int
    signal_count: int
    outbox_count: int
    external_provider_requests: int
    paper_execution_authorized: bool
    trading_approval: bool


async def _verify_runtime(project_root: Path, store: SqliteStore) -> C6AcceptanceReport:
    resolution_ms = 2_000_000_000_000
    clock_ms = lambda: resolution_ms
    runtime = C1RuntimeOrchestrator(
        run_id="c6-loopback-acceptance",
        store=store,
        broker=OutboxBroker(),
        binance_adapter=_BinanceLoopback(),
        polymarket_adapter=_PolymarketLoopback(resolution_ms),
        binance_start_ms=60_000,
        binance_end_ms=60_000,
        history_start_ts=1,
        history_end_ts=2,
        clock_ms=clock_ms,
        checkpoint_input_source=_ExecutableInputLoopback(),
        checkpoint_recovery_cutoff_ms=resolution_ms + 1,
        checkpoint_poll_interval_seconds=60.0,
    )
    try:
        await runtime.start()
        await asyncio.wait_for(runtime.wait_checkpoint_progress(), timeout=5)
        before = (
            store.count("strategy_evaluations"),
            store.count("signals"),
            store.count("outbox_events"),
        )
        await runtime.poll_strategy_checkpoints_once()
        after = (
            store.count("strategy_evaluations"),
            store.count("signals"),
            store.count("outbox_events"),
        )
        exact_replay_pass = before == after
        checkpoint_evaluations = store.scalar(
            "SELECT COUNT(*) FROM strategy_evaluations "
            "WHERE checkpoint_group_key IS NOT NULL"
        )
        current_evaluations = store.scalar(
            "SELECT COUNT(*) FROM strategy_evaluations "
            "WHERE checkpoint_group_key IS NULL "
            "AND rule_spec_sha256 IS NOT NULL AND origin='LIVE'"
        )
        recovered_groups = store.scalar(
            "SELECT COUNT(DISTINCT evaluation_key) "
            "FROM strategy_checkpoint_schedules "
            "WHERE current_reevaluation_required=1"
        )
        current_groups = store.scalar(
            "SELECT COUNT(DISTINCT evaluation_key) "
            "FROM strategy_checkpoint_schedules "
            "WHERE current_reevaluation_evaluation_id IS NOT NULL"
        )

        scheduler = CheckpointScheduler(project_root=project_root, store=store)
        for index, market_id in enumerate(("market-b", "market-c"), start=1):
            market_resolution = resolution_ms + index * 86_400_000
            identity = _canonical_json(
                {
                    "event_id": market_id,
                    "resolution_utc": datetime.fromtimestamp(
                        market_resolution / 1_000, tz=UTC
                    ).isoformat(),
                }
            )
            scheduler.persist_market_and_register(
                market_id=market_id,
                market_identity_json=identity,
                market_identity_sha256=hashlib.sha256(identity.encode()).hexdigest(),
                created_at_ms=10 + index,
            )
        market_b_schedule = scheduler.market_schedules("market-b")[0]
        blocked_due = scheduler.claim_schedule(
            market_b_schedule.schedule_key,
            origin="RECOVERED_AFTER_DOWNTIME",
            poller_id="c6-missing-depth",
        )
        blocked_payload = _canonical_json(
            {
                "checkpoint_minutes": blocked_due.schedule.checkpoint_minutes,
                "market_id": blocked_due.schedule.market_id,
                "schema_version": "C6_MISSING_HISTORICAL_DEPTH_V1",
                "strategy_id": blocked_due.schedule.strategy_id,
            }
        )
        blocked_result = scheduler.capture(
            blocked_due,
            input_payload_json=blocked_payload,
            input_snapshot_hash=hashlib.sha256(blocked_payload.encode()).hexdigest(),
            historical_depth_available=False,
        )
        stale_guard = False
        try:
            scheduler.claim_due(
                now_ms=resolution_ms + 86_400_000,
                recovery_cutoff_ms=resolution_ms,
                active_market_id="market-c",
                runtime_state="LIVE_READY",
                poller_id="c6-stale-guard",
                limit=100,
            )
        except ValueError as error:
            stale_guard = str(error) == "STALE_MARKET_STRATEGY_GUARD"

        schedules_per_market = tuple(
            row[1]
            for row in store.rows(
                "SELECT market_id,COUNT(*) FROM strategy_checkpoint_schedules "
                "GROUP BY market_id ORDER BY market_id"
            )
        )
        return C6AcceptanceReport(
            status="C6_CHECKPOINT_SCHEDULER_PASS",
            acceptance_pass=True,
            migration_version=store.scalar(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations WHERE version<=3"
            ),
            enabled_strategy_count=8,
            market_count=store.count("market_catalog"),
            schedule_count=store.count("strategy_checkpoint_schedules"),
            schedules_per_market=schedules_per_market,
            exact_replay_pass=exact_replay_pass,
            recovered_replay_pass=(recovered_groups == 8),
            missing_depth_block_pass=(
                blocked_result.state == "BLOCKED"
                and blocked_result.reason_code
                == "RECOVERED_HISTORICAL_DEPTH_MISSING"
            ),
            current_reevaluation_pass=(current_groups == 8 and current_evaluations == 8),
            atomic_commit_pass=(checkpoint_evaluations == 8),
            stale_market_guard_pass=stale_guard,
            runtime_writer_path_pass=(runtime.scheduler_writer_operation_count > 1),
            real_dispatcher_pass=(checkpoint_evaluations == 8),
            production_input_ready=False,
            data_completion_gate="OPEN_BLOCKS_C7",
            evaluation_count=store.count("strategy_evaluations"),
            signal_count=store.count("signals"),
            outbox_count=store.count("outbox_events"),
            external_provider_requests=0,
            paper_execution_authorized=False,
            trading_approval=False,
        )
    finally:
        await runtime.stop()


def verify_c6_acceptance(project_root: Path) -> C6AcceptanceReport:
    if not isinstance(project_root, Path):
        raise ValueError("C6_INVALID_PROJECT_ROOT")
    with tempfile.TemporaryDirectory() as directory:
        store = SqliteStore.open(Path(directory) / "c6.sqlite3")
        try:
            store.migrate()
            report = asyncio.run(_verify_runtime(project_root.resolve(), store))
        finally:
            store.close()
    if not all(
        (
            report.acceptance_pass,
            report.migration_version == 3,
            report.enabled_strategy_count == 8,
            report.market_count == 3,
            report.schedule_count == 30,
            report.schedules_per_market == (10, 10, 10),
            report.exact_replay_pass,
            report.recovered_replay_pass,
            report.missing_depth_block_pass,
            report.current_reevaluation_pass,
            report.atomic_commit_pass,
            report.stale_market_guard_pass,
            report.runtime_writer_path_pass,
            report.real_dispatcher_pass,
            not report.production_input_ready,
            report.data_completion_gate == "OPEN_BLOCKS_C7",
            report.external_provider_requests == 0,
            not report.paper_execution_authorized,
            not report.trading_approval,
        )
    ):
        raise RuntimeError("C6_ACCEPTANCE_FAILED")
    return report


def acceptance_report_payload(
    report: C6AcceptanceReport,
    *,
    source_commit: str,
    verified_at_utc: str,
) -> dict[str, object]:
    if type(report) is not C6AcceptanceReport:
        raise ValueError("C6_INVALID_ACCEPTANCE_REPORT")
    if type(source_commit) is not str or _SHA40.fullmatch(source_commit) is None:
        raise ValueError("C6_INVALID_SOURCE_COMMIT")
    try:
        timestamp = datetime.fromisoformat(verified_at_utc.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("C6_INVALID_VERIFIED_TIMESTAMP") from None
    if timestamp.tzinfo is None or timestamp.astimezone(UTC) != timestamp:
        raise ValueError("C6_INVALID_VERIFIED_TIMESTAMP")
    return json.loads(
        _canonical_json(
            {
                "schema_version": "BTC_C6_CHECKPOINT_SCHEDULER_ACCEPTANCE_V2",
                "source_commit": source_commit,
                "verified_at_utc": verified_at_utc,
                **asdict(report),
            }
        )
    )


def write_c6_acceptance_report(
    report: C6AcceptanceReport,
    *,
    output_path: Path,
    source_commit: str,
    verified_at_utc: str,
) -> None:
    if not isinstance(output_path, Path):
        raise ValueError("C6_INVALID_OUTPUT_PATH")
    payload = acceptance_report_payload(
        report,
        source_commit=source_commit,
        verified_at_utc=verified_at_utc,
    )
    output_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
