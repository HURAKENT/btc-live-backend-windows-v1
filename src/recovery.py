from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.lifecycle import STARTUP_SEQUENCE, LifecycleStateMachine
from src.models import SourceEvent


MINUTE_MS = 60_000
RECOVERY_ORIGINS = frozenset(
    {
        "LIVE",
        "REST_BACKFILL",
        "BUFFERED_DURING_RECOVERY",
        "RECOVERED_AFTER_DOWNTIME",
    }
)


class RecoveryBlockedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class C2RecoveryPlan:
    last_committed_open_ms: int
    request_start_ms: int
    request_end_ms: int
    current_open_ms: int
    expected_closed_minutes: int
    restart: bool
    anchor_replay: bool


@dataclass(frozen=True, slots=True)
class C2RecoverySummary:
    status: str
    live_ready_allowed: bool
    blockers: tuple[str, ...]
    binance_expected_closed_minutes: int
    binance_recovered_closed_minutes: int
    binance_missing_closed_minutes: int
    binance_duplicate_count_after_dedup: int
    market_count: int
    asset_count: int
    current_book_count: int
    history_completed_asset_count: int
    history_event_count: int
    buffered_event_count: int
    drained_event_count: int
    source_duplicate_count_after_dedup: int
    writer_consumer_count: int
    recovered_evaluation_committed: bool
    current_evaluation_committed: bool
    recovered_execution_eligible: bool
    current_execution_eligible: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_count": self.asset_count,
            "binance_duplicate_count_after_dedup": (
                self.binance_duplicate_count_after_dedup
            ),
            "binance_expected_closed_minutes": (
                self.binance_expected_closed_minutes
            ),
            "binance_missing_closed_minutes": (
                self.binance_missing_closed_minutes
            ),
            "binance_recovered_closed_minutes": (
                self.binance_recovered_closed_minutes
            ),
            "blockers": list(self.blockers),
            "buffered_event_count": self.buffered_event_count,
            "current_book_count": self.current_book_count,
            "current_evaluation_committed": (
                self.current_evaluation_committed
            ),
            "current_execution_eligible": self.current_execution_eligible,
            "drained_event_count": self.drained_event_count,
            "history_completed_asset_count": (
                self.history_completed_asset_count
            ),
            "history_event_count": self.history_event_count,
            "live_ready_allowed": self.live_ready_allowed,
            "market_count": self.market_count,
            "recovered_evaluation_committed": (
                self.recovered_evaluation_committed
            ),
            "recovered_execution_eligible": (
                self.recovered_execution_eligible
            ),
            "source_duplicate_count_after_dedup": (
                self.source_duplicate_count_after_dedup
            ),
            "status": self.status,
            "trading_approval": False,
            "writer_consumer_count": self.writer_consumer_count,
        }


@dataclass(frozen=True, slots=True)
class CutoverTrace:
    committed_minutes: tuple[int, ...]
    duplicate_count: int
    duplicate_count_after_dedup: int
    backfill_complete_index: int
    buffer_drain_start_index: int
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContinuityReport:
    expected_closed_minutes: int
    recovered_closed_minutes: int
    missing_closed_minutes: int
    missing_open_times: tuple[int, ...]
    canonical_open_times: tuple[int, ...]
    duplicate_count: int
    duplicate_count_after_dedup: int
    blocker: bool


@dataclass(frozen=True, slots=True)
class MarketReconciliationReport:
    current_book: bool
    price_history: bool
    historical_depth: bool
    requires_depth: bool
    status: str
    live_ready_allowed: bool


@dataclass(frozen=True, slots=True)
class RecoveryClassification:
    status: str
    origin: str
    execution_eligible: bool
    historical_signal_is_current_live_signal: bool
    current_reevaluation_required: bool


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    states: tuple[str, ...]
    live_ready: bool
    continuity: ContinuityReport
    market_reconciliation: MarketReconciliationReport
    cutover: CutoverTrace


def build_binance_recovery_plan(
    *,
    cursor: dict[str, Any] | None,
    latest_closed_open_ms: int,
) -> C2RecoveryPlan:
    if (
        type(latest_closed_open_ms) is not int
        or latest_closed_open_ms < MINUTE_MS
        or latest_closed_open_ms % MINUTE_MS != 0
    ):
        raise ValueError("INVALID_BINANCE_RECOVERY_BOUNDARY")

    if cursor is None:
        last_committed_open_ms = latest_closed_open_ms - MINUTE_MS
        restart = False
    else:
        if type(cursor) is not dict or cursor.get("source") != "binance":
            raise ValueError("INVALID_BINANCE_RECOVERY_CURSOR")
        decoded = cursor.get("cursor")
        updated_at_ms = cursor.get("updated_at_ms")
        if type(decoded) is not dict:
            raise ValueError("INVALID_BINANCE_RECOVERY_CURSOR")
        source_timestamp_ms = decoded.get("source_timestamp_ms")
        for value in (source_timestamp_ms, updated_at_ms):
            if (
                type(value) is not int
                or value < 0
                or value % MINUTE_MS != 0
                or value > latest_closed_open_ms
            ):
                raise ValueError("INVALID_BINANCE_RECOVERY_CURSOR")
        if source_timestamp_ms != updated_at_ms:
            raise ValueError("BINANCE_RECOVERY_CURSOR_CONFLICT")
        last_committed_open_ms = updated_at_ms
        restart = True

    expected = (
        latest_closed_open_ms - last_committed_open_ms
    ) // MINUTE_MS
    anchor_replay = expected == 0
    request_start_ms = (
        last_committed_open_ms
        if anchor_replay
        else last_committed_open_ms + MINUTE_MS
    )
    request_end_ms = latest_closed_open_ms
    return C2RecoveryPlan(
        last_committed_open_ms=last_committed_open_ms,
        request_start_ms=request_start_ms,
        request_end_ms=request_end_ms,
        current_open_ms=latest_closed_open_ms + MINUTE_MS,
        expected_closed_minutes=expected,
        restart=restart,
        anchor_replay=anchor_replay,
    )


def polymarket_history_cursor_source(asset_id: str) -> str:
    if type(asset_id) is not str or not asset_id:
        raise ValueError("INVALID_POLYMARKET_HISTORY_ASSET_ID")
    return f"polymarket-history:{asset_id}"


def resolve_polymarket_history_starts(
    *,
    asset_ids: Sequence[str],
    read_cursor: Callable[[str], dict[str, Any] | None],
    floor_start_ts: int,
    end_ts: int,
) -> dict[str, int | None]:
    if not callable(read_cursor):
        raise ValueError("INVALID_POLYMARKET_HISTORY_CURSOR_READER")
    for value in (floor_start_ts, end_ts):
        if type(value) is not int or value < 0:
            raise ValueError("INVALID_POLYMARKET_HISTORY_BOUNDARY")
    if end_ts < floor_start_ts:
        raise ValueError("INVALID_POLYMARKET_HISTORY_BOUNDARY")

    assets = tuple(asset_ids)
    if (
        any(type(asset_id) is not str or not asset_id for asset_id in assets)
        or len(set(assets)) != len(assets)
    ):
        raise ValueError("INVALID_POLYMARKET_HISTORY_ASSET_SET")

    starts: dict[str, int | None] = {}
    for asset_id in assets:
        cursor_source = polymarket_history_cursor_source(asset_id)
        cursor = read_cursor(cursor_source)
        if cursor is None:
            start = floor_start_ts
        else:
            if type(cursor) is not dict:
                raise ValueError("INVALID_POLYMARKET_HISTORY_CURSOR")
            decoded = cursor.get("cursor")
            updated_at_ms = cursor.get("updated_at_ms")
            if (
                type(updated_at_ms) is not int
                or updated_at_ms < 0
                or updated_at_ms % 1000 != 0
            ):
                raise ValueError("INVALID_POLYMARKET_HISTORY_CURSOR")
            if (
                cursor.get("source") != cursor_source
                or type(decoded) is not dict
                or type(decoded.get("source_timestamp_ms")) is not int
                or decoded["source_timestamp_ms"] != updated_at_ms
            ):
                raise ValueError("POLYMARKET_HISTORY_CURSOR_CONFLICT")
            natural_key = decoded.get("natural_key")
            natural_key_prefix = (
                f"polymarket:{asset_id}:price_history:"
                f"{updated_at_ms // 1000}:"
            )
            identity_hash = (
                natural_key.removeprefix(natural_key_prefix)
                if type(natural_key) is str
                and natural_key.startswith(natural_key_prefix)
                else ""
            )
            if (
                len(identity_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in identity_hash
                )
            ):
                raise ValueError("POLYMARKET_HISTORY_CURSOR_CONFLICT")
            if updated_at_ms // 1000 > end_ts:
                raise ValueError("POLYMARKET_HISTORY_CURSOR_REGRESSION")
            # A persisted cursor is authoritative for recent operational gap
            # recovery.  The floor is only a first-start bound; applying it to
            # an older cursor would silently discard the recoverable interval.
            start = updated_at_ms // 1000 + 60
        starts[asset_id] = None if start > end_ts else start
    return starts


def assess_c2_recovery(
    *,
    binance_expected_closed_minutes: int,
    binance_recovered_closed_minutes: int,
    binance_missing_closed_minutes: int,
    binance_duplicate_count_after_dedup: int,
    market_count: int,
    asset_count: int,
    current_book_count: int,
    history_completed_asset_count: int,
    history_event_count: int,
    buffered_event_count: int,
    drained_event_count: int,
    source_duplicate_count_after_dedup: int,
    writer_consumer_count: int,
    recovered_evaluation_committed: bool,
    current_evaluation_committed: bool,
    recovered_execution_eligible: bool,
    current_execution_eligible: bool,
) -> C2RecoverySummary:
    integer_values = {
        "binance_expected_closed_minutes": binance_expected_closed_minutes,
        "binance_recovered_closed_minutes": binance_recovered_closed_minutes,
        "binance_missing_closed_minutes": binance_missing_closed_minutes,
        "binance_duplicate_count_after_dedup": (
            binance_duplicate_count_after_dedup
        ),
        "market_count": market_count,
        "asset_count": asset_count,
        "current_book_count": current_book_count,
        "history_completed_asset_count": history_completed_asset_count,
        "history_event_count": history_event_count,
        "buffered_event_count": buffered_event_count,
        "drained_event_count": drained_event_count,
        "source_duplicate_count_after_dedup": (
            source_duplicate_count_after_dedup
        ),
        "writer_consumer_count": writer_consumer_count,
    }
    if any(
        type(value) is not int or value < 0
        for value in integer_values.values()
    ):
        raise ValueError("INVALID_C2_RECOVERY_SUMMARY_COUNT")
    boolean_values = {
        "recovered_evaluation_committed": recovered_evaluation_committed,
        "current_evaluation_committed": current_evaluation_committed,
        "recovered_execution_eligible": recovered_execution_eligible,
        "current_execution_eligible": current_execution_eligible,
    }
    if any(type(value) is not bool for value in boolean_values.values()):
        raise ValueError("INVALID_C2_RECOVERY_SUMMARY_FLAG")

    blockers: list[str] = []
    if (
        binance_missing_closed_minutes != 0
        or binance_recovered_closed_minutes
        != binance_expected_closed_minutes
    ):
        blockers.append("BINANCE_RECOVERY_INCOMPLETE")
    if binance_duplicate_count_after_dedup != 0:
        blockers.append("BINANCE_DUPLICATES_AFTER_DEDUP")
    if market_count != 11:
        blockers.append("POLYMARKET_MARKET_COUNT")
    if asset_count != 22:
        blockers.append("POLYMARKET_ASSET_COUNT")
    if current_book_count != asset_count:
        blockers.append("POLYMARKET_CURRENT_BOOKS_INCOMPLETE")
    if history_completed_asset_count != asset_count:
        blockers.append("POLYMARKET_HISTORY_INCOMPLETE")
    if drained_event_count != buffered_event_count:
        blockers.append("LIVE_BUFFER_DRAIN_INCOMPLETE")
    if source_duplicate_count_after_dedup != 0:
        blockers.append("SOURCE_DUPLICATES_AFTER_DEDUP")
    if writer_consumer_count != 1:
        blockers.append("ONE_WRITER_BOUNDARY")
    if not recovered_evaluation_committed:
        blockers.append("RECOVERED_EVALUATION_MISSING")
    if not current_evaluation_committed:
        blockers.append("CURRENT_EVALUATION_MISSING")
    if recovered_execution_eligible:
        blockers.append("RECOVERED_EVALUATION_EXECUTION_ELIGIBLE")
    if current_execution_eligible:
        blockers.append("CURRENT_EVALUATION_EXECUTION_ELIGIBLE")

    allowed = not blockers
    return C2RecoverySummary(
        status=(
            "C2_RECOVERY_HARDENING_PASS"
            if allowed
            else "C2_RECOVERY_BLOCKED"
        ),
        live_ready_allowed=allowed,
        blockers=tuple(blockers),
        **integer_values,
        **boolean_values,
    )


def _inserted(result: Any) -> bool:
    if type(result) is bool:
        return result
    inserted = getattr(result, "inserted", None)
    if type(inserted) is not bool:
        raise ValueError("INVALID_COMMIT_RESULT")
    return inserted


def _validate_event(event: SourceEvent) -> None:
    if type(event) is not SourceEvent:
        raise ValueError("INVALID_RECOVERY_EVENT_TYPE")
    if event.recovery_origin not in RECOVERY_ORIGINS:
        raise ValueError("INVALID_RECOVERY_ORIGIN")


def execute_cutover(
    *,
    backfill_events: Sequence[SourceEvent],
    buffered_events: Sequence[SourceEvent],
    commit_event: Callable[[SourceEvent], Any],
) -> CutoverTrace:
    operations: list[str] = []
    committed_minutes: list[int] = []
    duplicate_count = 0

    for event in backfill_events:
        _validate_event(event)
        operations.append(f"BACKFILL_COMMIT:{event.natural_key}")
        if _inserted(commit_event(event)):
            committed_minutes.append(event.source_timestamp_ms // MINUTE_MS)
        else:
            duplicate_count += 1

    operations.append("BACKFILL_COMPLETE")
    backfill_complete_index = len(operations) - 1
    operations.append("BUFFER_DRAIN_START")
    buffer_drain_start_index = len(operations) - 1

    for event in buffered_events:
        _validate_event(event)
        operations.append(f"BUFFER_COMMIT:{event.natural_key}")
        if _inserted(commit_event(event)):
            committed_minutes.append(event.source_timestamp_ms // MINUTE_MS)
        else:
            duplicate_count += 1

    return CutoverTrace(
        committed_minutes=tuple(committed_minutes),
        duplicate_count=duplicate_count,
        duplicate_count_after_dedup=0,
        backfill_complete_index=backfill_complete_index,
        buffer_drain_start_index=buffer_drain_start_index,
        operations=tuple(operations),
    )


def reconcile_binance_minutes(
    *,
    last_committed_open_ms: int,
    current_open_ms: int,
    recovered_events: Sequence[SourceEvent],
) -> ContinuityReport:
    if type(last_committed_open_ms) is not int:
        raise ValueError("INVALID_BINANCE_MINUTE_TYPE")
    if type(current_open_ms) is not int:
        raise ValueError("INVALID_BINANCE_MINUTE_TYPE")
    if (
        last_committed_open_ms < 0
        or current_open_ms <= last_committed_open_ms
        or last_committed_open_ms % MINUTE_MS != 0
        or current_open_ms % MINUTE_MS != 0
    ):
        raise ValueError("INVALID_BINANCE_MINUTE_CADENCE")

    expected = tuple(
        range(
            last_committed_open_ms + MINUTE_MS,
            current_open_ms,
            MINUTE_MS,
        )
    )
    expected_set = set(expected)
    observed_counts: dict[int, int] = {}
    for event in recovered_events:
        _validate_event(event)
        if event.source != "binance":
            raise ValueError("INVALID_BINANCE_RECOVERY_SOURCE")
        open_time_ms = event.source_timestamp_ms
        if open_time_ms % MINUTE_MS != 0:
            raise ValueError("INVALID_BINANCE_MINUTE_CADENCE")
        if open_time_ms <= last_committed_open_ms or open_time_ms >= current_open_ms:
            continue
        observed_counts[open_time_ms] = observed_counts.get(open_time_ms, 0) + 1

    canonical = tuple(sorted(observed_counts))
    missing = tuple(sorted(expected_set - set(canonical)))
    duplicate_count = sum(count - 1 for count in observed_counts.values())
    return ContinuityReport(
        expected_closed_minutes=len(expected),
        recovered_closed_minutes=len(expected_set & set(canonical)),
        missing_closed_minutes=len(missing),
        missing_open_times=missing,
        canonical_open_times=canonical,
        duplicate_count=duplicate_count,
        duplicate_count_after_dedup=0,
        blocker=bool(missing),
    )


def _require_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"INVALID_RECOVERY_CAPABILITY_TYPE: {field}")
    return value


def reconcile_polymarket_state(
    *,
    price_history: bool,
    current_book: bool,
    historical_depth: bool,
    requires_depth: bool,
) -> MarketReconciliationReport:
    price_history = _require_bool(price_history, "price_history")
    current_book = _require_bool(current_book, "current_book")
    historical_depth = _require_bool(historical_depth, "historical_depth")
    requires_depth = _require_bool(requires_depth, "requires_depth")

    if not current_book:
        status = "BLOCKED_MISSING_CURRENT_BOOK"
    elif not price_history:
        status = "BLOCKED_MISSING_PRICE_HISTORY"
    elif requires_depth and not historical_depth:
        status = "BLOCKED_MISSING_HISTORICAL_DEPTH"
    else:
        status = "PASS"
    return MarketReconciliationReport(
        current_book=current_book,
        price_history=price_history,
        historical_depth=historical_depth,
        requires_depth=requires_depth,
        status=status,
        live_ready_allowed=status == "PASS",
    )


def classify_recovered_evaluation(
    *,
    price_history: bool,
    current_book: bool,
    historical_depth: bool,
    requires_depth: bool,
) -> RecoveryClassification:
    reconciliation = reconcile_polymarket_state(
        price_history=price_history,
        current_book=current_book,
        historical_depth=historical_depth,
        requires_depth=requires_depth,
    )
    status = (
        "RECOVERED_SIGNAL"
        if reconciliation.live_ready_allowed
        else reconciliation.status
    )
    return RecoveryClassification(
        status=status,
        origin="RECOVERED_AFTER_DOWNTIME",
        execution_eligible=False,
        historical_signal_is_current_live_signal=False,
        current_reevaluation_required=True,
    )


class RecoveryCoordinator:
    def __init__(
        self,
        *,
        binance_adapter: Any,
        polymarket_adapter: Any,
        buffered_events: Sequence[SourceEvent],
        commit_event: Callable[[SourceEvent], Any],
        last_committed_open_ms: int,
        current_open_ms: int,
        state_sink: Callable[[dict[str, Any]], None] | None = None,
        incident_sink: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self._binance_adapter = binance_adapter
        self._polymarket_adapter = polymarket_adapter
        self._buffered_events = tuple(buffered_events)
        self._commit_event = commit_event
        self._last_committed_open_ms = last_committed_open_ms
        self._current_open_ms = current_open_ms
        self._incident_sink = incident_sink
        self._lifecycle = LifecycleStateMachine(state_sink=state_sink)

    @property
    def current_state(self) -> str | None:
        return self._lifecycle.current_state

    def _transition(self, state: str) -> None:
        self._lifecycle.transition(state)

    def _block(self, error: RecoveryBlockedError) -> None:
        self._lifecycle.fail_closed(str(error))
        if self._incident_sink is not None:
            self._incident_sink(
                {
                    "severity": "CRITICAL",
                    "source": "recovery",
                    "code": "RECOVERY_BLOCKED",
                    "detail": str(error),
                }
            )

    async def run(self) -> RecoveryReport:
        operations: list[str] = []
        committed_minutes: list[int] = []
        duplicate_count = 0
        try:
            for state in STARTUP_SEQUENCE[:6]:
                self._transition(state)

            backfill_events = tuple(await self._binance_adapter.backfill())
            for event in backfill_events:
                _validate_event(event)
                operations.append(f"BACKFILL_COMMIT:{event.natural_key}")
                if _inserted(self._commit_event(event)):
                    committed_minutes.append(
                        event.source_timestamp_ms // MINUTE_MS
                    )
                else:
                    duplicate_count += 1
            operations.append("BACKFILL_COMPLETE")
            backfill_complete_index = len(operations) - 1

            self._transition("POLYMARKET_BACKFILL")
            capabilities = await self._polymarket_adapter.reconcile()
            if type(capabilities) is not dict:
                raise ValueError("INVALID_POLYMARKET_RECONCILIATION_RESULT")

            self._transition("RECONCILIATION")
            continuity = reconcile_binance_minutes(
                last_committed_open_ms=self._last_committed_open_ms,
                current_open_ms=self._current_open_ms,
                recovered_events=backfill_events + self._buffered_events,
            )
            market = reconcile_polymarket_state(**capabilities)
            if continuity.blocker:
                raise RecoveryBlockedError("BINANCE_CONTINUITY_BLOCKED")
            if not market.live_ready_allowed:
                raise RecoveryBlockedError(market.status)

            self._transition("STRATEGY_REPLAY")
            self._transition("BUFFER_DRAIN")
            operations.append("BUFFER_DRAIN_START")
            buffer_drain_start_index = len(operations) - 1
            for event in self._buffered_events:
                _validate_event(event)
                operations.append(f"BUFFER_COMMIT:{event.natural_key}")
                if _inserted(self._commit_event(event)):
                    committed_minutes.append(
                        event.source_timestamp_ms // MINUTE_MS
                    )
                else:
                    duplicate_count += 1

            self._transition("LIVE_READY")
            cutover = CutoverTrace(
                committed_minutes=tuple(committed_minutes),
                duplicate_count=duplicate_count,
                duplicate_count_after_dedup=0,
                backfill_complete_index=backfill_complete_index,
                buffer_drain_start_index=buffer_drain_start_index,
                operations=tuple(operations),
            )
            return RecoveryReport(
                states=self._lifecycle.states,
                live_ready=True,
                continuity=continuity,
                market_reconciliation=market,
                cutover=cutover,
            )
        except RecoveryBlockedError as error:
            self._block(error)
            raise
        except Exception as error:
            blocked = RecoveryBlockedError(f"RECOVERY_FAILED: {error}")
            self._block(blocked)
            raise blocked from error
