from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from src.fixed_point import ProbabilityMicros
from src.models import SourceEvent
from src.polymarket_provider import MarketBook
from src.strategy_dispatch import V1ExecutableCheckpointInput
from src.strategy_v1 import BucketInput, StrictPriceHistoryEvidence


MINUTE_MS = 60_000
REQUIRED_CLOSED_CANDLES = 181
FIVE_SHARES_MICROS = 5_000_000
_SHA256_HEX = frozenset("0123456789abcdef")


class CurrentInputBlocked(ValueError):
    """A frozen MVP readiness reason that prevents current execution."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _require_nonnegative_int(value: Any, reason_code: str) -> int:
    if type(value) is not int or value < 0:
        raise CurrentInputBlocked(reason_code)
    return value


def _require_sha256(value: Any, reason_code: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise CurrentInputBlocked(reason_code)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class ClosedCandleWindowV1:
    candle_first_open_time_ms: int
    candle_last_open_time_ms: int
    closed_candle_count: int
    candles_sha256: str


def validate_closed_candles(
    candles: tuple[SourceEvent, ...], *, observed_at_ms: int
) -> ClosedCandleWindowV1:
    """Validate the exact latest closed 181-minute model window."""

    _require_nonnegative_int(observed_at_ms, "MODEL_INPUT_INVALID")
    if type(candles) is not tuple or len(candles) != REQUIRED_CLOSED_CANDLES:
        raise CurrentInputBlocked("MISSING_181_CLOSED_CANDLES")

    payloads: list[Mapping[str, Any]] = []
    open_times: list[int] = []
    for candle in candles:
        if (
            type(candle) is not SourceEvent
            or candle.source != "binance"
            or candle.event_type != "BINANCE_KLINE_CLOSED"
        ):
            raise CurrentInputBlocked("MODEL_INPUT_INVALID")
        try:
            payload = json.loads(candle.payload_json)
        except (json.JSONDecodeError, TypeError):
            raise CurrentInputBlocked("MODEL_INPUT_INVALID") from None
        if (
            type(payload) is not dict
            or payload.get("symbol") != "BTCUSDT"
            or payload.get("interval") != "1m"
            or type(payload.get("open_time_ms")) is not int
            or type(payload.get("close_time_ms")) is not int
            or payload["open_time_ms"] != candle.source_timestamp_ms
            or payload["close_time_ms"] != payload["open_time_ms"] + MINUTE_MS - 1
            or payload["close_time_ms"] >= observed_at_ms
        ):
            raise CurrentInputBlocked("MODEL_INPUT_INVALID")
        payloads.append(payload)
        open_times.append(payload["open_time_ms"])

    if any(
        current != previous + MINUTE_MS
        for previous, current in zip(open_times, open_times[1:])
    ):
        raise CurrentInputBlocked("MISSING_181_CLOSED_CANDLES")

    expected_last_open_ms = (
        observed_at_ms // MINUTE_MS * MINUTE_MS - MINUTE_MS
    )
    if open_times[-1] != expected_last_open_ms:
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")

    candles_sha256 = hashlib.sha256(
        _canonical_json(payloads).encode("utf-8")
    ).hexdigest()
    return ClosedCandleWindowV1(
        candle_first_open_time_ms=open_times[0],
        candle_last_open_time_ms=open_times[-1],
        closed_candle_count=REQUIRED_CLOSED_CANDLES,
        candles_sha256=candles_sha256,
    )


@dataclass(frozen=True, slots=True)
class Vwap5EvidenceV1:
    vwap5_micros: int
    available_depth_shares_micros: int


def exact_vwap5(book: MarketBook) -> Vwap5EvidenceV1:
    """Calculate a five-share buy VWAP from current ask levels in micros."""

    if (
        type(book) is not MarketBook
        or not book.asks
        or type(book.book_hash) is not str
        or not book.book_hash
        or type(book.source_timestamp_ms) is not int
        or book.source_timestamp_ms < 0
    ):
        raise CurrentInputBlocked("MISSING_CURRENT_BOOK")

    available_depth = 0
    for price_micros, shares_micros in book.asks.items():
        if (
            type(price_micros) is not int
            or not 0 <= price_micros <= 1_000_000
            or type(shares_micros) is not int
            or shares_micros < 0
        ):
            raise CurrentInputBlocked("MISSING_CURRENT_BOOK")
        available_depth += shares_micros
    if available_depth < FIVE_SHARES_MICROS:
        raise CurrentInputBlocked("INSUFFICIENT_FIVE_SHARE_DEPTH")

    remaining = FIVE_SHARES_MICROS
    weighted_price = 0
    for price_micros, shares_micros in sorted(book.asks.items()):
        taken = min(remaining, shares_micros)
        weighted_price += price_micros * taken
        remaining -= taken
        if remaining == 0:
            break
    vwap5_micros = (
        weighted_price + FIVE_SHARES_MICROS // 2
    ) // FIVE_SHARES_MICROS
    return Vwap5EvidenceV1(
        vwap5_micros=vwap5_micros,
        available_depth_shares_micros=available_depth,
    )


@dataclass(frozen=True, slots=True)
class FeeEvidenceV1:
    provenance: str
    source_sha256: str
    fee_per_share_usd_micros: int

    def __post_init__(self) -> None:
        if type(self.provenance) is not str or not self.provenance:
            raise CurrentInputBlocked("MISSING_FEE_PROVENANCE")
        _require_sha256(self.source_sha256, "MISSING_FEE_PROVENANCE")
        _require_nonnegative_int(
            self.fee_per_share_usd_micros,
            "MISSING_FEE_PROVENANCE",
        )


def build_executable_checkpoint_input(
    *,
    checkpoint_minutes: int,
    model_probabilities: tuple[ProbabilityMicros, ...],
    market_q_yes: tuple[ProbabilityMicros, ...],
    yes_books: tuple[MarketBook, ...],
    no_token_ids: tuple[str, ...],
    fee_evidence: tuple[FeeEvidenceV1, ...],
    prior_position: bool,
    price_history_evidence: StrictPriceHistoryEvidence,
) -> V1ExecutableCheckpointInput:
    """Compose the existing executable input from provenance-checked values."""

    sequences = (
        model_probabilities,
        market_q_yes,
        yes_books,
        no_token_ids,
        fee_evidence,
    )
    if any(type(sequence) is not tuple or len(sequence) != 11 for sequence in sequences):
        raise CurrentInputBlocked("INCOMPLETE_MARKET_SET")
    if (
        any(type(value) is not ProbabilityMicros for value in model_probabilities)
        or any(type(value) is not ProbabilityMicros for value in market_q_yes)
        or any(type(value) is not MarketBook for value in yes_books)
        or any(type(value) is not FeeEvidenceV1 for value in fee_evidence)
        or any(type(value) is not str or not value for value in no_token_ids)
        or len(set(no_token_ids)) != 11
    ):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    if type(prior_position) is not bool:
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    if type(price_history_evidence) is not StrictPriceHistoryEvidence:
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")

    buckets = tuple(
        BucketInput(
            bucket_index=index,
            model_p=model_probabilities[index].value / 1_000_000,
            market_q_yes=market_q_yes[index].value / 1_000_000,
            market_q_no=None,
            vwap5=exact_vwap5(yes_books[index]).vwap5_micros / 1_000_000,
            confirmed_fee=(
                fee_evidence[index].fee_per_share_usd_micros / 1_000_000
            ),
            no_token_id=no_token_ids[index],
        )
        for index in range(11)
    )
    return V1ExecutableCheckpointInput(
        checkpoint_minutes=checkpoint_minutes,
        buckets=buckets,
        prior_position=prior_position,
        strict_price_history_evidence=price_history_evidence,
        pf1_snapshot_evidence=None,
    )


@dataclass(frozen=True, slots=True)
class CurrentExecutionEvidenceV1:
    schema_version: str
    evidence_key: str
    market_id: str
    market_date: str
    token_id: str
    checkpoint_minutes: int
    candle_first_open_time_ms: int
    candle_last_open_time_ms: int
    closed_candle_count: int
    candles_sha256: str
    model_bundle_sha256: str
    book_sha256: str
    fee_provenance_sha256: str
    bucket_index: int
    model_probability_micros: int
    market_q_micros: int
    vwap5_micros: int
    available_depth_shares_micros: int
    fee_per_share_usd_micros: int
    book_source_timestamp_ms: int
    observed_at_ms: int
    price_history_source_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "CURRENT_EXECUTION_EVIDENCE_V1":
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_SCHEMA")
        for value in (self.market_id, self.market_date, self.token_id):
            if type(value) is not str or not value:
                raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_IDENTITY")
        try:
            if date.fromisoformat(self.market_date).isoformat() != self.market_date:
                raise ValueError
        except ValueError:
            raise ValueError(
                "INVALID_CURRENT_EXECUTION_EVIDENCE_MARKET_DATE"
            ) from None
        if self.checkpoint_minutes not in (60, 30):
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_CHECKPOINT")
        if self.closed_candle_count != REQUIRED_CLOSED_CANDLES:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_CANDLES")
        for value in (
            self.candle_first_open_time_ms,
            self.candle_last_open_time_ms,
            self.available_depth_shares_micros,
            self.fee_per_share_usd_micros,
            self.book_source_timestamp_ms,
            self.observed_at_ms,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_UNIT")
        if self.candle_first_open_time_ms > self.candle_last_open_time_ms:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_CANDLES")
        if self.book_source_timestamp_ms > self.observed_at_ms:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_TIME")
        if self.available_depth_shares_micros < FIVE_SHARES_MICROS:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_DEPTH")
        if type(self.bucket_index) is not int or not 0 <= self.bucket_index <= 10:
            raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_BUCKET")
        for value in (
            self.model_probability_micros,
            self.market_q_micros,
            self.vwap5_micros,
        ):
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError("INVALID_CURRENT_EXECUTION_EVIDENCE_PRICE")
        for value in (
            self.evidence_key,
            self.candles_sha256,
            self.model_bundle_sha256,
            self.book_sha256,
            self.fee_provenance_sha256,
            self.price_history_source_sha256,
        ):
            _require_sha256(value, "INVALID_CURRENT_EXECUTION_EVIDENCE_SHA256")
        expected_key = hashlib.sha256(
            _canonical_json(
                self.canonical_dict(include_evidence_key=False)
            ).encode("utf-8")
        ).hexdigest()
        if self.evidence_key != expected_key:
            raise ValueError("CURRENT_EXECUTION_EVIDENCE_KEY_MISMATCH")

    def canonical_dict(self, *, include_evidence_key: bool) -> dict[str, Any]:
        payload = {
            "available_depth_shares_micros": self.available_depth_shares_micros,
            "book_sha256": self.book_sha256,
            "book_source_timestamp_ms": self.book_source_timestamp_ms,
            "bucket_index": self.bucket_index,
            "candle_first_open_time_ms": self.candle_first_open_time_ms,
            "candle_last_open_time_ms": self.candle_last_open_time_ms,
            "candles_sha256": self.candles_sha256,
            "checkpoint_minutes": self.checkpoint_minutes,
            "closed_candle_count": self.closed_candle_count,
            "fee_per_share_usd_micros": self.fee_per_share_usd_micros,
            "fee_provenance_sha256": self.fee_provenance_sha256,
            "market_date": self.market_date,
            "market_id": self.market_id,
            "market_q_micros": self.market_q_micros,
            "model_bundle_sha256": self.model_bundle_sha256,
            "model_probability_micros": self.model_probability_micros,
            "observed_at_ms": self.observed_at_ms,
            "price_history_source_sha256": self.price_history_source_sha256,
            "schema_version": self.schema_version,
            "token_id": self.token_id,
            "vwap5_micros": self.vwap5_micros,
        }
        if include_evidence_key:
            payload["evidence_key"] = self.evidence_key
        return payload

    @classmethod
    def create(cls, **fields: Any) -> CurrentExecutionEvidenceV1:
        payload = {
            "schema_version": "CURRENT_EXECUTION_EVIDENCE_V1",
            "closed_candle_count": REQUIRED_CLOSED_CANDLES,
            **fields,
        }
        evidence_key = hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()
        return cls(evidence_key=evidence_key, **payload)


@dataclass(frozen=True, slots=True)
class CurrentInputCaptureV1:
    executable_input: V1ExecutableCheckpointInput
    evidence: CurrentExecutionEvidenceV1

    def __post_init__(self) -> None:
        if (
            type(self.executable_input) is not V1ExecutableCheckpointInput
            or type(self.evidence) is not CurrentExecutionEvidenceV1
        ):
            raise ValueError("CURRENT_CAPTURE_MISMATCH")
        bucket = self.executable_input.buckets[self.evidence.bucket_index]
        if (
            self.executable_input.checkpoint_minutes
            != self.evidence.checkpoint_minutes
            or round(bucket.model_p * 1_000_000)
            != self.evidence.model_probability_micros
            or round(bucket.market_q_yes * 1_000_000)
            != self.evidence.market_q_micros
            or bucket.vwap5 is None
            or round(bucket.vwap5 * 1_000_000) != self.evidence.vwap5_micros
            or round(bucket.confirmed_fee * 1_000_000)
            != self.evidence.fee_per_share_usd_micros
            or self.executable_input.strict_price_history_evidence is None
            or self.executable_input.strict_price_history_evidence.source_sha256
            != self.evidence.price_history_source_sha256
            or self.executable_input.pf1_snapshot_evidence is not None
        ):
            raise ValueError("CURRENT_CAPTURE_MISMATCH")


def assert_current_model_enabled(contract_path: Path) -> str:
    """Return the exact bundle hash only when its contract permits live use."""

    if type(contract_path) is not Path:
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    try:
        raw = contract_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID") from None
    if (
        type(payload) is not dict
        or payload.get("bundle_id")
        != "STRICT_A_HISTORICAL_FIDELITY_MODEL_BUNDLE_V1"
        or payload.get("live_enabled") is not True
        or payload.get("features", {}).get("closed_candles")
        != REQUIRED_CLOSED_CANDLES
    ):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    return hashlib.sha256(raw).hexdigest()


def authorize_current_model_for_paper(
    contract_path: Path,
    *,
    paper_authorized: bool,
    trading_approval: bool,
    real_orders: bool,
    wallet: bool,
    signing: bool,
) -> str:
    """Authorize evaluation only under the versioned local-paper safety profile."""

    if (
        paper_authorized is not True
        or trading_approval is not False
        or real_orders is not False
        or wallet is not False
        or signing is not False
    ):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    try:
        raw = contract_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID") from None
    if (
        type(payload) is not dict
        or payload.get("bundle_id")
        != "STRICT_A_HISTORICAL_FIDELITY_MODEL_BUNDLE_V1"
        or payload.get("live_enabled") is not False
        or payload.get("features", {}).get("closed_candles")
        != REQUIRED_CLOSED_CANDLES
    ):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    return hashlib.sha256(raw).hexdigest()


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    def continued_fraction(left: float, right: float, value: float) -> float:
        tiny = 1e-300
        qab = left + right
        qap = left + 1.0
        qam = left - 1.0
        c = 1.0
        d = 1.0 - qab * value / qap
        d = 1.0 / max(abs(d), tiny) * (1.0 if d >= 0 else -1.0)
        result = d
        for index in range(1, 201):
            twice = 2 * index
            coefficient = index * (right - index) * value / (
                (qam + twice) * (left + twice)
            )
            d = 1.0 + coefficient * d
            d = 1.0 / max(abs(d), tiny) * (1.0 if d >= 0 else -1.0)
            c = 1.0 + coefficient / c
            if abs(c) < tiny:
                c = tiny
            result *= d * c
            coefficient = -(
                (left + index) * (qab + index) * value
                / ((left + twice) * (qap + twice))
            )
            d = 1.0 + coefficient * d
            d = 1.0 / max(abs(d), tiny) * (1.0 if d >= 0 else -1.0)
            c = 1.0 + coefficient / c
            if abs(c) < tiny:
                c = tiny
            delta = d * c
            result *= delta
            if abs(delta - 1.0) <= 3e-14:
                return result
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")

    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * continued_fraction(a, b, x) / a
    return 1.0 - front * continued_fraction(b, a, 1.0 - x) / b


def _student_t_cdf(value: float, *, df: float, loc: float, scale: float) -> float:
    standardized = (value - loc) / scale
    if standardized == 0.0:
        return 0.5
    beta = _regularized_incomplete_beta(
        df / 2.0, 0.5, df / (df + standardized * standardized)
    )
    return 1.0 - beta / 2.0 if standardized > 0 else beta / 2.0


def _quantile_cdf(value: float, quantiles: list[float], probabilities: list[float]) -> float:
    if value <= quantiles[0]:
        return probabilities[0]
    if value >= quantiles[-1]:
        return probabilities[-1]
    for index in range(1, len(quantiles)):
        if value <= quantiles[index]:
            fraction = (value - quantiles[index - 1]) / (
                quantiles[index] - quantiles[index - 1]
            )
            return probabilities[index - 1] + fraction * (
                probabilities[index] - probabilities[index - 1]
            )
    raise CurrentInputBlocked("MODEL_INPUT_INVALID")


def frozen_model_probabilities(
    candles: tuple[SourceEvent, ...],
    *,
    observed_at_ms: int,
    checkpoint_minutes: int,
    bucket_bounds: tuple[tuple[float | None, float | None], ...],
    contract_path: Path,
    paper_authorized: bool,
) -> tuple[ProbabilityMicros, ...]:
    """Evaluate the frozen Strict A distribution without third-party numerics."""

    validate_closed_candles(candles, observed_at_ms=observed_at_ms)
    authorize_current_model_for_paper(
        contract_path,
        paper_authorized=paper_authorized,
        trading_approval=False,
        real_orders=False,
        wallet=False,
        signing=False,
    )
    if checkpoint_minutes not in (30, 60) or len(bucket_bounds) != 11:
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    try:
        bundle = json.loads(contract_path.read_text(encoding="utf-8"))
        closes = [float(json.loads(item.payload_json)["close"]) for item in candles]
        returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:])]
        mean = sum(returns) / len(returns)
        sigma = math.sqrt(
            sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        )
        spot = closes[-1]
        horizon_scale = sigma * math.sqrt(checkpoint_minutes)
        if not math.isfinite(horizon_scale) or horizon_scale <= 0 or spot <= 0:
            raise ValueError
        spec = bundle["models"][str(checkpoint_minutes)]
        probability_quantiles = [float(item) for item in bundle["probability_quantiles"]]
    except (KeyError, TypeError, ValueError, ZeroDivisionError, json.JSONDecodeError):
        raise CurrentInputBlocked("MODEL_INPUT_INVALID") from None

    def cdf(price: float | None, *, upper: bool) -> float:
        if price is None:
            return 1.0 if upper else 0.0
        if type(price) not in (int, float) or price <= 0 or not math.isfinite(price):
            raise CurrentInputBlocked("MODEL_INPUT_INVALID")
        z = math.log(float(price) / spot) / horizon_scale
        if checkpoint_minutes == 30:
            return _quantile_cdf(
                z,
                [float(item) for item in spec["quantile_z"]],
                probability_quantiles,
            )
        clipped = min(max(z, float(spec["clip_z"][0])), float(spec["clip_z"][1]))
        return _student_t_cdf(
            clipped,
            df=float(spec["df"]),
            loc=float(spec["loc"]),
            scale=float(spec["scale"]),
        )

    raw: list[float] = []
    for lower, upper in bucket_bounds:
        probability = max(0.0, cdf(upper, upper=True) - cdf(lower, upper=False))
        raw.append(probability)
    total = sum(raw)
    if not math.isfinite(total) or total <= 0:
        raise CurrentInputBlocked("MODEL_INPUT_INVALID")
    scaled = [value / total * 1_000_000 for value in raw]
    floors = [math.floor(value) for value in scaled]
    remainder = 1_000_000 - sum(floors)
    order = sorted(range(11), key=lambda index: (scaled[index] - floors[index], -index), reverse=True)
    for index in order[:remainder]:
        floors[index] += 1
    return tuple(ProbabilityMicros(value) for value in floors)


def fee_evidence_from_public_schedule(
    *, token_id: str, price_micros: int, fee_schedule: Mapping[str, Any]
) -> FeeEvidenceV1:
    """Apply the public taker formula; unsupported curves remain fail-closed."""

    if type(token_id) is not str or not token_id or type(fee_schedule) is not dict:
        raise CurrentInputBlocked("MISSING_FEE_PROVENANCE")
    if (
        type(price_micros) is not int
        or not 0 <= price_micros <= 1_000_000
        or fee_schedule.get("exponent") != 1
        or fee_schedule.get("takerOnly") is not True
    ):
        raise CurrentInputBlocked("MISSING_FEE_PROVENANCE")
    try:
        rate = Decimal(str(fee_schedule["rate"]))
        price = Decimal(price_micros) / Decimal(1_000_000)
        if not rate.is_finite() or rate < 0 or rate > 1:
            raise ValueError
        fee = (rate * price * (Decimal(1) - price)).quantize(
            Decimal("0.00001"), rounding=ROUND_HALF_UP
        )
    except (KeyError, InvalidOperation, ValueError):
        raise CurrentInputBlocked("MISSING_FEE_PROVENANCE") from None
    source = {
        "documentation": "https://docs.polymarket.com/trading/fees",
        "fee_schedule": dict(fee_schedule),
        "token_id": token_id,
    }
    return FeeEvidenceV1(
        provenance="POLYMARKET_PUBLIC_FEE_SCHEDULE",
        source_sha256=hashlib.sha256(_canonical_json(source).encode()).hexdigest(),
        fee_per_share_usd_micros=int(fee * Decimal(1_000_000)),
    )
