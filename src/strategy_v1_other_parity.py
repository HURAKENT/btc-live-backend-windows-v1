from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from src.strategy_v1 import (
    BucketInput,
    V1Evaluation,
    apply_identity_checkpoint_policy,
    evaluate_confirmation,
    evaluate_favorite_neighbor,
    evaluate_favorite_only,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCHEMA_VERSION = "C4_V1_CONFIRMATION_BASKET_PARITY_V1"
_IDENTITIES = (
    "NO_A0",
    "NO_A2",
    "NO_B2",
    "NO_C1",
    "YES_FAVORITE_NEIGHBOR_BASKET",
    "YES_FAVORITE_ONLY",
)
_CONFIRMATION = frozenset(_IDENTITIES[:4])
_BASKET = frozenset(_IDENTITIES[4:])
_MANIFEST_KEYS = frozenset(
    {
        "covered_strategy_ids",
        "record_type",
        "schema_version",
        "shares",
        "source_hashes",
        "trading_approval",
    }
)
_ROW_KEYS = frozenset(
    {
        "confirmation_cohort",
        "confirmation_winner_bucket_index",
        "expectations",
        "market_identity_sha256",
        "record_type",
        "t30_buckets",
        "t60_buckets",
    }
)
_BUCKET_KEYS = frozenset(
    {
        "actual_winner",
        "bucket_index",
        "model_p_micros",
        "no_token_identity_sha256",
        "q_no_micros",
        "q_yes_micros",
    }
)
_EXPECTATION_KEYS = frozenset(
    {
        "expected_accept",
        "expected_checkpoint_minutes",
        "expected_pnl_micros",
        "expected_selected_bucket_indices",
        "expected_stressed_cost_micros",
        "expected_turnover_micros",
        "expected_won",
        "strategy_id",
    }
)


@dataclass(frozen=True, slots=True)
class V1OtherIdentityParity:
    strategy_id: str
    decision_count: int
    expected_trade_count: int
    actual_trade_count: int
    expected_wins: int
    actual_wins: int
    expected_turnover_micros: int
    actual_turnover_micros: int
    expected_pnl_micros: int
    actual_pnl_micros: int
    expected_trade_identity_sha256: str
    actual_trade_identity_sha256: str
    parity_pass: bool


@dataclass(frozen=True, slots=True)
class V1OtherParityReport:
    fixture_sha256: str | None
    fixture_record_count: int
    strategy_count: int
    decision_count: int
    identities: tuple[V1OtherIdentityParity, ...]
    parity_pass: bool


@dataclass(slots=True)
class _Accumulator:
    decision_count: int = 0
    expected_trade_count: int = 0
    actual_trade_count: int = 0
    expected_wins: int = 0
    actual_wins: int = 0
    expected_turnover: int = 0
    actual_turnover: int = 0
    expected_pnl: int = 0
    actual_pnl: int = 0
    expected_ids: list[str] | None = None
    actual_ids: list[str] | None = None

    def __post_init__(self) -> None:
        self.expected_ids = []
        self.actual_ids = []


def verify_v1_other_parity(path: Path) -> V1OtherParityReport:
    if not isinstance(path, Path):
        raise ValueError("C4_V1_OTHER_PARITY_PATH_TYPE")
    payload = path.read_bytes()
    if not payload:
        raise ValueError("C4_V1_OTHER_PARITY_EMPTY")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("C4_V1_OTHER_PARITY_UTF8") from exc
    if not text.endswith("\n"):
        raise ValueError("C4_V1_OTHER_PARITY_NONCANONICAL")
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ValueError(f"C4_V1_OTHER_PARITY_EMPTY_LINE:{line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"C4_V1_OTHER_PARITY_JSON:{line_number}") from exc
        if type(value) is not dict or _canonical_json(value) != line:
            raise ValueError(f"C4_V1_OTHER_PARITY_NONCANONICAL:{line_number}")
        records.append(value)
    report = verify_v1_other_parity_rows(records)
    return V1OtherParityReport(
        fixture_sha256=hashlib.sha256(payload).hexdigest(),
        fixture_record_count=report.fixture_record_count,
        strategy_count=report.strategy_count,
        decision_count=report.decision_count,
        identities=report.identities,
        parity_pass=report.parity_pass,
    )


def verify_v1_other_parity_rows(
    records: list[dict[str, object]],
) -> V1OtherParityReport:
    if type(records) is not list or len(records) < 2:
        raise ValueError("C4_V1_OTHER_PARITY_RECORDS")
    _validate_manifest(records[0])
    accumulators = {identity: _Accumulator() for identity in _IDENTITIES}
    seen_markets: set[str] = set()
    for line_number, row in enumerate(records[1:], start=2):
        if type(row) is not dict or frozenset(row) != _ROW_KEYS:
            raise ValueError(f"C4_V1_OTHER_PARITY_ROW_SCHEMA:{line_number}")
        if row["record_type"] != "market_decisions":
            raise ValueError(f"C4_V1_OTHER_PARITY_ROW_TYPE:{line_number}")
        market_hash = _hash(row["market_identity_sha256"], "market", line_number)
        if market_hash in seen_markets:
            raise ValueError("C4_V1_OTHER_PARITY_DUPLICATE_MARKET")
        seen_markets.add(market_hash)
        if type(row["confirmation_cohort"]) is not bool:
            raise ValueError(f"C4_V1_OTHER_PARITY_COHORT:{line_number}")
        confirmation_winner = row["confirmation_winner_bucket_index"]
        if confirmation_winner is not None and (
            type(confirmation_winner) is not int or not 0 <= confirmation_winner <= 10
        ):
            raise ValueError(f"C4_V1_OTHER_PARITY_CONFIRMATION_WINNER:{line_number}")
        t60, winners60 = _buckets(row["t60_buckets"], line_number, 60)
        t30, winners30 = _buckets(row["t30_buckets"], line_number, 30)
        if winners60 != winners30:
            raise ValueError(f"C4_V1_OTHER_PARITY_WINNER_DRIFT:{line_number}")
        expectations = row["expectations"]
        required = set(_BASKET) | (set(_CONFIRMATION) if row["confirmation_cohort"] else set())
        if type(expectations) is not list or len(expectations) != len(required):
            raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATIONS:{line_number}")
        seen: set[str] = set()
        for expectation in expectations:
            parsed = _expectation(expectation, line_number)
            strategy_id = parsed["strategy_id"]
            if strategy_id not in required or strategy_id in seen:
                raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_ID:{line_number}")
            seen.add(strategy_id)
            result = _evaluate(strategy_id, t60, t30)
            actual_accept = result.accepted
            actual_selected = result.selected_bucket_indices if actual_accept else ()
            actual_checkpoint = result.checkpoint_minutes if actual_accept else None
            if actual_accept != parsed["expected_accept"]:
                raise ValueError(
                    "C4_V1_OTHER_PARITY_DECISION_MISMATCH:"
                    f"strategy={strategy_id}:market={market_hash}:"
                    f"expected={parsed['expected_accept']}:actual={actual_accept}:"
                    f"reason={result.reason}"
                )
            if actual_selected != parsed["expected_selected_bucket_indices"]:
                raise ValueError("C4_V1_OTHER_PARITY_SELECTION_MISMATCH")
            if actual_checkpoint != parsed["expected_checkpoint_minutes"]:
                raise ValueError("C4_V1_OTHER_PARITY_CHECKPOINT_MISMATCH")
            actual_cost = _stressed_cost_micros(strategy_id, result) if actual_accept else 0
            actual_won = (
                _won(
                    strategy_id,
                    actual_selected,
                    winners60,
                    confirmation_winner,
                )
                if actual_accept
                else parsed["expected_won"]
            )
            actual_turnover = 5 * actual_cost if actual_accept else 0
            actual_pnl = (
                5 * ((1_000_000 if actual_won else 0) - actual_cost)
                if actual_accept
                else 0
            )
            if actual_cost != parsed["expected_stressed_cost_micros"]:
                raise ValueError("C4_V1_OTHER_PARITY_COST_MISMATCH")
            if actual_won != parsed["expected_won"]:
                raise ValueError(
                    "C4_V1_OTHER_PARITY_OUTCOME_MISMATCH:"
                    f"strategy={strategy_id}:market={market_hash}:"
                    f"selected={actual_selected}:expected={parsed['expected_won']}:"
                    f"actual={actual_won}"
                )
            if actual_turnover != parsed["expected_turnover_micros"]:
                raise ValueError("C4_V1_OTHER_PARITY_TURNOVER_MISMATCH")
            if actual_pnl != parsed["expected_pnl_micros"]:
                raise ValueError("C4_V1_OTHER_PARITY_PNL_MISMATCH")
            _accumulate(
                accumulators[strategy_id],
                strategy_id=strategy_id,
                market_hash=market_hash,
                checkpoint=actual_checkpoint,
                selected=actual_selected,
                expected=parsed,
                actual_accept=actual_accept,
                actual_won=actual_won,
                actual_turnover=actual_turnover,
                actual_pnl=actual_pnl,
            )
        if seen != required:
            raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_GAP:{line_number}")
    identities = tuple(_finalize(identity, accumulators[identity]) for identity in _IDENTITIES)
    return V1OtherParityReport(
        fixture_sha256=None,
        fixture_record_count=len(records),
        strategy_count=len(identities),
        decision_count=sum(item.decision_count for item in accumulators.values()),
        identities=identities,
        parity_pass=all(item.parity_pass for item in identities),
    )


def _validate_manifest(value: object) -> None:
    if type(value) is not dict or frozenset(value) != _MANIFEST_KEYS:
        raise ValueError("C4_V1_OTHER_PARITY_MANIFEST_SCHEMA")
    if value["record_type"] != "manifest" or value["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("C4_V1_OTHER_PARITY_MANIFEST_VERSION")
    if value["covered_strategy_ids"] != list(_IDENTITIES) or value["shares"] != 5:
        raise ValueError("C4_V1_OTHER_PARITY_MANIFEST_IDENTITIES")
    if value["trading_approval"] is not False:
        raise ValueError("C4_V1_OTHER_PARITY_TRADING_APPROVAL")
    hashes = value["source_hashes"]
    if type(hashes) is not dict or len(hashes) < 5 or any(
        type(name) is not str or _SHA256.fullmatch(digest) is None
        for name, digest in hashes.items()
    ):
        raise ValueError("C4_V1_OTHER_PARITY_SOURCE_HASHES")


def _buckets(
    value: object, line_number: int, checkpoint: int
) -> tuple[tuple[BucketInput, ...], frozenset[int]]:
    if type(value) is not list or len(value) != 11:
        raise ValueError(f"C4_V1_OTHER_PARITY_BUCKET_COUNT:{line_number}:{checkpoint}")
    rows: list[BucketInput] = []
    winners: set[int] = set()
    for expected_index, item in enumerate(value):
        if type(item) is not dict or frozenset(item) != _BUCKET_KEYS:
            raise ValueError(f"C4_V1_OTHER_PARITY_BUCKET_SCHEMA:{line_number}")
        if item["bucket_index"] != expected_index or type(item["actual_winner"]) is not bool:
            raise ValueError(f"C4_V1_OTHER_PARITY_BUCKET_ORDER:{line_number}")
        if item["actual_winner"]:
            winners.add(expected_index)
        token_hash = _hash(item["no_token_identity_sha256"], "token", line_number)
        rows.append(
            BucketInput(
                bucket_index=expected_index,
                model_p=_probability(item["model_p_micros"], line_number),
                market_q_yes=_probability(item["q_yes_micros"], line_number),
                market_q_no=_probability(item["q_no_micros"], line_number),
                vwap5=None,
                confirmed_fee=0.0,
                no_token_id=token_hash,
            )
        )
    if len(winners) != 1:
        raise ValueError(f"C4_V1_OTHER_PARITY_WINNER_COUNT:{line_number}")
    return tuple(rows), frozenset(winners)


def _expectation(value: object, line_number: int) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != _EXPECTATION_KEYS:
        raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_SCHEMA:{line_number}")
    strategy_id = value["strategy_id"]
    if type(strategy_id) is not str or strategy_id not in _IDENTITIES:
        raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_STRATEGY:{line_number}")
    if type(value["expected_accept"]) is not bool or type(value["expected_won"]) is not bool:
        raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_BOOLEAN:{line_number}")
    selected = value["expected_selected_bucket_indices"]
    if type(selected) is not list or any(type(item) is not int or not 0 <= item <= 10 for item in selected):
        raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_SELECTION:{line_number}")
    checkpoint = value["expected_checkpoint_minutes"]
    if checkpoint is not None and checkpoint not in (30, 60):
        raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_CHECKPOINT:{line_number}")
    parsed = dict(value)
    parsed["expected_selected_bucket_indices"] = tuple(selected)
    for name in (
        "expected_pnl_micros",
        "expected_stressed_cost_micros",
        "expected_turnover_micros",
    ):
        if type(value[name]) is not int:
            raise ValueError(f"C4_V1_OTHER_PARITY_EXPECTATION_INTEGER:{line_number}")
    return parsed


def _evaluate(
    strategy_id: str,
    t60: tuple[BucketInput, ...],
    t30: tuple[BucketInput, ...],
) -> V1Evaluation:
    if strategy_id in _CONFIRMATION:
        return evaluate_confirmation(strategy_id, t60, t30=t30)
    evaluator = (
        evaluate_favorite_neighbor
        if strategy_id == "YES_FAVORITE_NEIGHBOR_BASKET"
        else evaluate_favorite_only
    )
    selected = apply_identity_checkpoint_policy(
        strategy_id,
        (evaluator(60, t60), evaluator(30, t30)),
    )
    return selected[0]


def _stressed_cost_micros(strategy_id: str, result: V1Evaluation) -> int:
    value = (
        result.market_probability + 0.03
        if strategy_id in _CONFIRMATION and result.market_probability is not None
        else result.stressed_reference_cost
    )
    if value is None:
        raise ValueError("C4_V1_OTHER_PARITY_ACCEPTED_WITHOUT_COST")
    return min(1_000_000, int(round(value * 1_000_000)))


def _won(
    strategy_id: str,
    selected: tuple[int, ...],
    winners: frozenset[int],
    confirmation_winner: int | None,
) -> bool:
    if strategy_id in _CONFIRMATION:
        return selected[0] != confirmation_winner
    return bool(set(selected) & winners)


def _accumulate(
    accumulator: _Accumulator,
    *,
    strategy_id: str,
    market_hash: str,
    checkpoint: int | None,
    selected: tuple[int, ...],
    expected: dict[str, object],
    actual_accept: bool,
    actual_won: bool,
    actual_turnover: int,
    actual_pnl: int,
) -> None:
    accumulator.decision_count += 1
    if expected["expected_accept"]:
        accumulator.expected_trade_count += 1
        accumulator.expected_wins += int(expected["expected_won"])
        accumulator.expected_turnover += int(expected["expected_turnover_micros"])
        accumulator.expected_pnl += int(expected["expected_pnl_micros"])
        assert accumulator.expected_ids is not None
        accumulator.expected_ids.append(_trade_id(strategy_id, market_hash, checkpoint, selected))
    if actual_accept:
        accumulator.actual_trade_count += 1
        accumulator.actual_wins += int(actual_won)
        accumulator.actual_turnover += actual_turnover
        accumulator.actual_pnl += actual_pnl
        assert accumulator.actual_ids is not None
        accumulator.actual_ids.append(_trade_id(strategy_id, market_hash, checkpoint, selected))


def _finalize(strategy_id: str, value: _Accumulator) -> V1OtherIdentityParity:
    assert value.expected_ids is not None and value.actual_ids is not None
    expected_hash = _set_hash(value.expected_ids)
    actual_hash = _set_hash(value.actual_ids)
    parity = (
        value.expected_trade_count == value.actual_trade_count
        and value.expected_wins == value.actual_wins
        and value.expected_turnover == value.actual_turnover
        and value.expected_pnl == value.actual_pnl
        and expected_hash == actual_hash
    )
    return V1OtherIdentityParity(
        strategy_id=strategy_id,
        decision_count=value.decision_count,
        expected_trade_count=value.expected_trade_count,
        actual_trade_count=value.actual_trade_count,
        expected_wins=value.expected_wins,
        actual_wins=value.actual_wins,
        expected_turnover_micros=value.expected_turnover,
        actual_turnover_micros=value.actual_turnover,
        expected_pnl_micros=value.expected_pnl,
        actual_pnl_micros=value.actual_pnl,
        expected_trade_identity_sha256=expected_hash,
        actual_trade_identity_sha256=actual_hash,
        parity_pass=parity,
    )


def _trade_id(strategy_id: str, market_hash: str, checkpoint: int | None, selected: tuple[int, ...]) -> str:
    return hashlib.sha256(
        _canonical_json([strategy_id, market_hash, checkpoint, list(selected)]).encode("utf-8")
    ).hexdigest()


def _set_hash(values: list[str]) -> str:
    return hashlib.sha256(_canonical_json(sorted(values)).encode("utf-8")).hexdigest()


def _hash(value: object, name: str, line_number: int) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"C4_V1_OTHER_PARITY_SHA256:{name}:{line_number}")
    return value


def _probability(value: object, line_number: int) -> float:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError(f"C4_V1_OTHER_PARITY_PROBABILITY:{line_number}")
    return value / 1_000_000


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
