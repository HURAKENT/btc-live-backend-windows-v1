from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.current_input import (
    CurrentExecutionEvidenceV1,
    CurrentInputBlocked,
    FeeEvidenceV1,
    authorize_current_model_for_paper,
    build_executable_checkpoint_input,
    exact_vwap5,
    fee_evidence_from_public_schedule,
    frozen_model_probabilities,
    validate_closed_candles,
)
from src.fixed_point import ProbabilityMicros
from src.models import SourceEvent
from src.polymarket_provider import MarketBook
from src.runtime_orchestrator import CheckpointInputResult
from src.strategy_v1 import StrictPriceHistoryEvidence


STRICT_A_IDS = frozenset(
    {"YES_STRICT_A_OPERATIONAL", "YES_STRICT_A_T60", "YES_STRICT_A_T30"}
)


def _validate_bucket_bounds(
    bounds: tuple[tuple[float | None, float | None], ...],
) -> None:
    if len(bounds) != 11:
        raise CurrentInputBlocked("INCOMPLETE_MARKET_SET")
    for index, (lower, upper) in enumerate(bounds):
        if (index == 0) != (lower is None) or (index == len(bounds) - 1) != (
            upper is None
        ):
            raise CurrentInputBlocked("INVALID_BUCKET_BOUNDS")
        if lower is not None and not math.isfinite(lower):
            raise CurrentInputBlocked("INVALID_BUCKET_BOUNDS")
        if upper is not None and not math.isfinite(upper):
            raise CurrentInputBlocked("INVALID_BUCKET_BOUNDS")
        if lower is not None and upper is not None and lower >= upper:
            raise CurrentInputBlocked("INVALID_BUCKET_BOUNDS")
        if index and bounds[index - 1][1] != lower:
            raise CurrentInputBlocked("INVALID_BUCKET_BOUNDS")


@dataclass(frozen=True, slots=True)
class CurrentMarketSnapshotV1:
    market_id: str
    market_date: str
    bucket_bounds: tuple[tuple[float | None, float | None], ...]
    yes_token_ids: tuple[str, ...]
    no_token_ids: tuple[str, ...]
    yes_books: tuple[MarketBook, ...]
    market_q_yes: tuple[ProbabilityMicros, ...]
    fee_schedules: tuple[dict[str, Any], ...]
    price_history_source_sha256: str
    price_history_observed_at_ms: int


class StrictACurrentInputSource:
    def __init__(
        self,
        *,
        candle_loader: Callable[[int], Awaitable[tuple[SourceEvent, ...]]],
        market_loader: Callable[[int], Awaitable[CurrentMarketSnapshotV1]],
        prior_position_loader: Callable[[str], bool],
        clock_ms: Callable[[], int],
        contract_path: Path,
        paper_authorized: bool,
    ) -> None:
        self._candle_loader = candle_loader
        self._market_loader = market_loader
        self._prior_position_loader = prior_position_loader
        self._clock_ms = clock_ms
        self._contract_path = contract_path
        self._paper_authorized = paper_authorized
        self._captures: dict[str, CurrentExecutionEvidenceV1] = {}

    def evidence_for(self, schedule_key: str) -> CurrentExecutionEvidenceV1 | None:
        return self._captures.get(schedule_key)

    async def capture(self, *, due: Any, current: bool) -> CheckpointInputResult:
        strategy_id = due.schedule.strategy_id
        if strategy_id not in STRICT_A_IDS:
            return CheckpointInputResult(
                None, False, "BLOCKED_MISSING_PRODUCTION_MODEL_BUNDLE"
            )
        if due.origin == "RECOVERED_AFTER_DOWNTIME":
            return CheckpointInputResult(
                None, False, "RECOVERED_EXECUTION_FORBIDDEN"
            )
        observed_at_ms = self._clock_ms()
        try:
            candles = await self._candle_loader(observed_at_ms)
            market = await self._market_loader(observed_at_ms)
            if (
                type(market) is not CurrentMarketSnapshotV1
                or market.market_id != due.schedule.market_id
                or len(market.bucket_bounds) != 11
                or len(market.yes_token_ids) != 11
                or len(market.no_token_ids) != 11
                or len(market.yes_books) != 11
                or len(market.market_q_yes) != 11
                or len(market.fee_schedules) != 11
            ):
                raise CurrentInputBlocked("INCOMPLETE_MARKET_SET")
            _validate_bucket_bounds(market.bucket_bounds)
            candle_window = validate_closed_candles(
                candles, observed_at_ms=observed_at_ms
            )
            model_hash = authorize_current_model_for_paper(
                self._contract_path,
                paper_authorized=self._paper_authorized,
                trading_approval=False,
                real_orders=False,
                wallet=False,
                signing=False,
            )
            probabilities = frozen_model_probabilities(
                candles,
                observed_at_ms=observed_at_ms,
                checkpoint_minutes=due.schedule.checkpoint_minutes,
                bucket_bounds=market.bucket_bounds,
                contract_path=self._contract_path,
                paper_authorized=self._paper_authorized,
            )
            fee_evidence: list[FeeEvidenceV1] = []
            vwap = []
            for token_id, book, schedule in zip(
                market.yes_token_ids, market.yes_books, market.fee_schedules
            ):
                calculation = exact_vwap5(book)
                if (
                    book.source_timestamp_ms is None
                    or book.source_timestamp_ms > observed_at_ms
                    or observed_at_ms - book.source_timestamp_ms > 120_000
                ):
                    raise CurrentInputBlocked("STALE_CURRENT_BOOK")
                vwap.append(calculation)
                fee_evidence.append(
                    fee_evidence_from_public_schedule(
                        token_id=token_id,
                        price_micros=calculation.vwap5_micros,
                        fee_schedule=schedule,
                    )
                )
            history = StrictPriceHistoryEvidence(
                provenance="CLOB_PRICE_HISTORY",
                source_sha256=market.price_history_source_sha256,
                checkpoint_timestamp_ms=observed_at_ms,
                observation_timestamp_ms=market.price_history_observed_at_ms,
            )
            executable = build_executable_checkpoint_input(
                checkpoint_minutes=due.schedule.checkpoint_minutes,
                model_probabilities=probabilities,
                market_q_yes=market.market_q_yes,
                yes_books=market.yes_books,
                no_token_ids=market.no_token_ids,
                fee_evidence=tuple(fee_evidence),
                prior_position=self._prior_position_loader(market.market_date),
                price_history_evidence=history,
            )
            maximum_q = max(item.value for item in market.market_q_yes)
            favorites = [
                index
                for index, value in enumerate(market.market_q_yes)
                if value.value == maximum_q
            ]
            if len(favorites) == 1:
                index = favorites[0]
                book_hash = market.yes_books[index].book_hash
                if type(book_hash) is not str or not book_hash:
                    raise CurrentInputBlocked("MISSING_CURRENT_BOOK")
                evidence = CurrentExecutionEvidenceV1.create(
                    market_id=market.market_id,
                    market_date=market.market_date,
                    token_id=market.yes_token_ids[index],
                    checkpoint_minutes=due.schedule.checkpoint_minutes,
                    candle_first_open_time_ms=candle_window.candle_first_open_time_ms,
                    candle_last_open_time_ms=candle_window.candle_last_open_time_ms,
                    candles_sha256=candle_window.candles_sha256,
                    model_bundle_sha256=model_hash,
                    book_sha256=hashlib.sha256(book_hash.encode()).hexdigest(),
                    fee_provenance_sha256=fee_evidence[index].source_sha256,
                    bucket_index=index,
                    model_probability_micros=probabilities[index].value,
                    market_q_micros=market.market_q_yes[index].value,
                    vwap5_micros=vwap[index].vwap5_micros,
                    available_depth_shares_micros=vwap[index].available_depth_shares_micros,
                    fee_per_share_usd_micros=fee_evidence[index].fee_per_share_usd_micros,
                    book_source_timestamp_ms=market.yes_books[index].source_timestamp_ms,
                    observed_at_ms=observed_at_ms,
                    price_history_source_sha256=market.price_history_source_sha256,
                )
                self._captures[due.schedule.schedule_key] = evidence
            return CheckpointInputResult(executable, True, None)
        except CurrentInputBlocked as error:
            return CheckpointInputResult(None, False, error.reason_code)
        except (OSError, RuntimeError):
            return CheckpointInputResult(
                None, False, "CURRENT_PROVIDER_UNAVAILABLE"
            )
