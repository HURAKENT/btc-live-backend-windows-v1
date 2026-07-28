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
