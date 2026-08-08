from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from src.strategy_v1 import (
    BucketInput,
    V1Evaluation,
    evaluate_no_fade_historical,
    evaluate_pf1_historical,
    evaluate_strict_historical,
)


_SCHEMA_VERSION = "C4_V1_HISTORICAL_PARITY_FIXTURE_V1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_KEYS = frozenset(
    {
        "covered_strategy_ids",
        "execution_eligibility_covered",
        "historical_trade_economics_only",
        "record_type",
        "schema_version",
        "shares",
        "source_population",
        "stress_micros",
        "trading_approval",
    }
)
_DECISION_KEYS = frozenset(
    {
        "buckets",
        "case_identity_sha256",
        "checkpoint_minutes",
        "expectations",
        "market_identity_sha256",
        "price_history_observation_timestamp_ms",
        "price_history_snapshot_sha256",
        "price_history_target_timestamp_ms",
        "record_type",
    }
)
_BUCKET_KEYS = frozenset(
    {
        "actual_winner",
        "bucket_index",
        "model_p_micros",
        "q_no_micros",
        "q_yes_micros",
    }
)
_EXPECTATION_KEYS = frozenset(
    {
        "expected_accept",
        "expected_favorite_bucket_index",
        "expected_pnl_micros",
        "expected_raw_q_micros",
        "expected_reason",
        "expected_selected_bucket_index",
        "expected_stressed_q_micros",
        "expected_turnover_micros",
        "expected_won",
        "family",
        "partition",
        "side",
        "universe",
    }
)
_HISTORICAL_ACCEPT_REASONS = frozenset(
    {
        "SIGNAL_ACCEPTED",
        "EXECUTION_REJECTED_MISSING_DEPTH",
        "EXECUTABLE_EDGE_GATE_REJECTED",
        "NO_EXECUTION_NOT_CALCULABLE",
        "NO_EXECUTION_INSUFFICIENT_DEPTH",
        "NO_EXECUTABLE_EDGE_GATE_REJECTED",
    }
)


@dataclass(frozen=True, slots=True)
class HistoricalIdentityBinding:
    family: str
    universe: str
    partition: str
    policy: str
    checkpoints: tuple[int, ...]


_BINDINGS: Mapping[str, HistoricalIdentityBinding] = MappingProxyType(
    {
        "YES_STRICT_A_T60": HistoricalIdentityBinding(
            "STRICT_A", "ALL", "NA", "FIXED", (60,)
        ),
        "YES_STRICT_A_OPERATIONAL": HistoricalIdentityBinding(
            "STRICT_A", "ALL", "NA", "FALLBACK", (60, 30)
        ),
        "YES_STRICT_A_T30": HistoricalIdentityBinding(
            "STRICT_A", "ALL", "NA", "FIXED", (30,)
        ),
        "YES_PF1_OPERATIONAL": HistoricalIdentityBinding(
            "PF1", "ALL", "NA", "FALLBACK", (60, 30)
        ),
        "YES_PF1_T60": HistoricalIdentityBinding(
            "PF1", "ALL", "NA", "FIXED", (60,)
        ),
        "YES_PF1_T30": HistoricalIdentityBinding(
            "PF1", "ALL", "NA", "FIXED", (30,)
        ),
        "YES_PF1_T6H": HistoricalIdentityBinding(
            "PF1", "ALL", "NA", "FIXED", (360,)
        ),
        "YES_PF1_T8H": HistoricalIdentityBinding(
            "PF1", "ALL", "NA", "FIXED", (480,)
        ),
        "NO_FADE_P1_U2_T18": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P1", "FIXED", (1080,)
        ),
        "NO_FADE_P1_T60_U1_EQUALS_U2": HistoricalIdentityBinding(
            "NO_FADE", "U1|U2", "P1", "EQUALITY", (60,)
        ),
        "NO_FADE_P2_U2_T60": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P2", "FIXED", (60,)
        ),
        "NO_FADE_P2_U1_T60": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P2", "FIXED", (60,)
        ),
        "NO_FADE_P1_U2_OPERATIONAL": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P1", "FALLBACK", (60, 30)
        ),
        "NO_FADE_P2_U2_T12": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P2", "FIXED", (720,)
        ),
        "NO_FADE_P1_U1_OPERATIONAL": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P1", "FALLBACK", (60, 30)
        ),
        "NO_FADE_P1_U1_EARLY_LATEST_ONLY": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P1", "LATEST_ONLY", (720,)
        ),
        "NO_FADE_P1_U1_T18": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P1", "FIXED", (1080,)
        ),
        "NO_FADE_P2_U2_OPERATIONAL": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P2", "FALLBACK", (60, 30)
        ),
        "NO_FADE_P1_U1_EARLY_COMBINED": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P1", "INDEPENDENT", (1080, 720)
        ),
        "NO_FADE_P1_U2_T12": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P1", "FIXED", (720,)
        ),
        "NO_FADE_P2_U1_OPERATIONAL": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P2", "FALLBACK", (60, 30)
        ),
        "NO_FADE_P1_U1_T12": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P1", "FIXED", (720,)
        ),
        "NO_FADE_P1_U1_EARLY_EARLIEST_ONLY": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P1", "EARLIEST_ONLY", (1080,)
        ),
        "NO_FADE_P2_U2_T30": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P2", "FIXED", (30,)
        ),
        "NO_FADE_P2_U1_T30": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P2", "FIXED", (30,)
        ),
        "NO_FADE_P2_U1_T12": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P2", "FIXED", (720,)
        ),
        "NO_FADE_P2_U2_T4H": HistoricalIdentityBinding(
            "NO_FADE", "U2", "P2", "FIXED", (240,)
        ),
        "NO_FADE_P2_U1_T2H": HistoricalIdentityBinding(
            "NO_FADE", "U1", "P2", "FIXED", (120,)
        ),
    }
)

_EARLY_CONFIDENCE_IDENTITIES = frozenset(
    {
        "NO_FADE_P1_T60_U1_EQUALS_U2",
        "NO_FADE_P1_U1_EARLY_COMBINED",
        "NO_FADE_P1_U1_EARLY_EARLIEST_ONLY",
        "NO_FADE_P1_U1_EARLY_LATEST_ONLY",
        "NO_FADE_P1_U1_T12",
        "NO_FADE_P1_U1_T18",
        "NO_FADE_P1_U2_T12",
        "NO_FADE_P1_U2_T18",
        "YES_PF1_T30",
        "YES_PF1_T60",
        "YES_PF1_T6H",
    }
)
_EARLY_HORIZON_IDENTITIES = frozenset(_BINDINGS) - _EARLY_CONFIDENCE_IDENTITIES
_POPULATION_IDENTITIES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "EARLY_CONFIDENCE": _EARLY_CONFIDENCE_IDENTITIES,
        "EARLY_HORIZON": _EARLY_HORIZON_IDENTITIES,
    }
)


@dataclass(frozen=True, slots=True)
class V1HistoricalIdentityParity:
    strategy_id: str
    fixture_decision_count: int
    expected_trade_count: int
    actual_trade_count: int
    expected_wins: int
    actual_wins: int
    expected_losses: int
    actual_losses: int
    expected_turnover_micros: int
    actual_turnover_micros: int
    expected_pnl_micros: int
    actual_pnl_micros: int
    expected_roi: str
    actual_roi: str
    expected_trade_identity_sha256: str
    actual_trade_identity_sha256: str
    selected_checkpoint_counts: tuple[tuple[int, int], ...]
    parity_pass: bool


@dataclass(frozen=True, slots=True)
class V1HistoricalParityReport:
    fixture_sha256: str | None
    fixture_record_count: int
    strategy_count: int
    identities: tuple[V1HistoricalIdentityParity, ...]
    execution_eligibility_covered: bool
    historical_trade_economics_only: bool
    parity_pass: bool


@dataclass(frozen=True, slots=True)
class _Component:
    market_identity_sha256: str
    checkpoint_minutes: int
    family: str
    universe: str
    partition: str
    side: str
    expected_accept: bool
    actual_accept: bool
    expected_selected_bucket_index: int | None
    actual_selected_bucket_index: int | None
    expected_won: bool
    actual_won: bool
    expected_turnover_micros: int
    actual_turnover_micros: int
    expected_pnl_micros: int
    actual_pnl_micros: int


def historical_identity_bindings() -> Mapping[str, HistoricalIdentityBinding]:
    return _BINDINGS


def historical_population_identities() -> Mapping[str, frozenset[str]]:
    return _POPULATION_IDENTITIES


def verify_v1_historical_parity(fixture_path: Path) -> V1HistoricalParityReport:
    if not isinstance(fixture_path, Path):
        raise ValueError("C4_V1_PARITY_FIXTURE_PATH_TYPE")
    fixture_bytes = fixture_path.read_bytes()
    if not fixture_bytes:
        raise ValueError("C4_V1_PARITY_FIXTURE_EMPTY")
    try:
        text = fixture_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("C4_V1_PARITY_FIXTURE_UTF8") from exc
    if not text.endswith("\n"):
        raise ValueError("C4_V1_PARITY_FIXTURE_NONCANONICAL")
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ValueError(f"C4_V1_PARITY_FIXTURE_EMPTY_LINE:{line_number}")
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"C4_V1_PARITY_FIXTURE_JSON:{line_number}") from exc
        if _canonical_json(payload) != line:
            raise ValueError(f"C4_V1_PARITY_FIXTURE_NONCANONICAL:{line_number}")
        if type(payload) is not dict:
            raise ValueError(f"C4_V1_PARITY_FIXTURE_SCHEMA:{line_number}")
        rows.append(payload)
    report = verify_v1_historical_parity_rows(rows)
    return V1HistoricalParityReport(
        fixture_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
        fixture_record_count=report.fixture_record_count,
        strategy_count=report.strategy_count,
        identities=report.identities,
        execution_eligibility_covered=report.execution_eligibility_covered,
        historical_trade_economics_only=report.historical_trade_economics_only,
        parity_pass=report.parity_pass,
    )


def verify_v1_historical_parity_rows(
    records: list[dict[str, object]],
) -> V1HistoricalParityReport:
    if type(records) is not list or not records:
        raise ValueError("C4_V1_PARITY_FIXTURE_SEQUENCE")
    manifest = _validate_manifest(records[0])
    covered_ids = tuple(manifest["covered_strategy_ids"])
    components: dict[tuple[str, int, str, str, str], _Component] = {}
    seen_cases: set[str] = set()
    for index, record in enumerate(records[1:], start=2):
        for component in _validate_and_replay_record(record, index, seen_cases):
            key = (
                component.market_identity_sha256,
                component.checkpoint_minutes,
                component.family,
                component.universe,
                component.partition,
            )
            if key in components:
                raise ValueError("C4_V1_PARITY_DUPLICATE_COMPONENT")
            components[key] = component
    identities = tuple(
        _verify_identity(strategy_id, _BINDINGS[strategy_id], components)
        for strategy_id in covered_ids
    )
    return V1HistoricalParityReport(
        fixture_sha256=None,
        fixture_record_count=len(records),
        strategy_count=len(identities),
        identities=identities,
        execution_eligibility_covered=False,
        historical_trade_economics_only=True,
        parity_pass=all(identity.parity_pass for identity in identities),
    )


def _validate_manifest(payload: object) -> dict[str, object]:
    if type(payload) is not dict or frozenset(payload) != _MANIFEST_KEYS:
        raise ValueError("C4_V1_PARITY_MANIFEST_SCHEMA")
    if payload["record_type"] != "manifest" or payload["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("C4_V1_PARITY_MANIFEST_VERSION")
    if payload["shares"] != 5 or payload["stress_micros"] != 30_000:
        raise ValueError("C4_V1_PARITY_MANIFEST_ECONOMICS")
    for name in (
        "execution_eligibility_covered",
        "historical_trade_economics_only",
        "trading_approval",
    ):
        if type(payload[name]) is not bool:
            raise ValueError(f"C4_V1_PARITY_MANIFEST_BOOLEAN:{name}")
    if (
        payload["execution_eligibility_covered"]
        or not payload["historical_trade_economics_only"]
        or payload["trading_approval"]
    ):
        raise ValueError("C4_V1_PARITY_MANIFEST_SCOPE")
    population = payload["source_population"]
    if type(population) is not str or population not in _POPULATION_IDENTITIES:
        raise ValueError("C4_V1_PARITY_MANIFEST_POPULATION")
    identities = payload["covered_strategy_ids"]
    if (
        type(identities) is not list
        or not identities
        or any(type(item) is not str or item not in _BINDINGS for item in identities)
        or len(set(identities)) != len(identities)
        or identities != sorted(identities)
    ):
        raise ValueError("C4_V1_PARITY_MANIFEST_IDENTITIES")
    if not set(identities).issubset(_POPULATION_IDENTITIES[population]):
        raise ValueError("C4_V1_PARITY_MANIFEST_POPULATION_IDENTITY_MISMATCH")
    return payload


def _validate_and_replay_record(
    payload: object, line_number: int, seen_cases: set[str]
) -> tuple[_Component, ...]:
    source = f"line={line_number}"
    if type(payload) is not dict or frozenset(payload) != _DECISION_KEYS:
        raise ValueError(f"C4_V1_PARITY_DECISION_SCHEMA:{source}")
    if payload["record_type"] != "decision":
        raise ValueError(f"C4_V1_PARITY_RECORD_TYPE:{source}")
    case_identity = _hash(payload["case_identity_sha256"], "case", source)
    if case_identity in seen_cases:
        raise ValueError("C4_V1_PARITY_DUPLICATE_CASE")
    seen_cases.add(case_identity)
    market_identity = _hash(payload["market_identity_sha256"], "market", source)
    snapshot_hash = _hash(
        payload["price_history_snapshot_sha256"], "price_history", source
    )
    checkpoint = _integer(payload["checkpoint_minutes"], "checkpoint", source)
    target = _integer(
        payload["price_history_target_timestamp_ms"], "target_timestamp", source
    )
    observation = _integer(
        payload["price_history_observation_timestamp_ms"],
        "observation_timestamp",
        source,
    )
    if checkpoint <= 0 or target < 0 or observation < 0 or observation > target:
        raise ValueError(f"C4_V1_PARITY_TIMESTAMP:{source}")
    buckets = _validate_buckets(payload["buckets"], source)
    expectations = payload["expectations"]
    if type(expectations) is not list or not expectations:
        raise ValueError(f"C4_V1_PARITY_EXPECTATIONS:{source}")
    components = []
    seen_expectations: set[tuple[str, str, str]] = set()
    for offset, expectation in enumerate(expectations):
        expectation_source = f"{source}:expectation={offset}"
        parsed = _validate_expectation(expectation, expectation_source)
        expectation_key = (parsed["family"], parsed["universe"], parsed["partition"])
        if expectation_key in seen_expectations:
            raise ValueError("C4_V1_PARITY_DUPLICATE_EXPECTATION")
        seen_expectations.add(expectation_key)
        result, actual_accept, actual_reason = _replay(
            family=parsed["family"],
            universe=parsed["universe"],
            partition=parsed["partition"],
            checkpoint=checkpoint,
            buckets=buckets,
            snapshot_hash=snapshot_hash,
            target=target,
            observation=observation,
        )
        actual_selected = (
            result.selected_bucket_indices[0]
            if len(result.selected_bucket_indices) == 1
            else None
        )
        if actual_accept != parsed["expected_accept"]:
            raise ValueError(f"C4_V1_PARITY_DECISION_MISMATCH:{expectation_source}")
        if actual_selected != parsed["expected_selected_bucket_index"]:
            raise ValueError(f"C4_V1_PARITY_SELECTION_MISMATCH:{expectation_source}")
        actual_favorite = _favorite_bucket_index(buckets)
        if actual_favorite != parsed["expected_favorite_bucket_index"]:
            raise ValueError(f"C4_V1_PARITY_FAVORITE_MISMATCH:{expectation_source}")
        if actual_reason != parsed["expected_reason"]:
            raise ValueError(
                "C4_V1_PARITY_REASON_MISMATCH:"
                f"{expectation_source}:family={parsed['family']}:"
                f"expected={parsed['expected_reason']}:actual={actual_reason}"
            )

        actual_raw = 0
        # A rejected source row can legitimately have no selected bucket; its
        # persisted `won` value is then non-trade metadata and has no replayable
        # bucket outcome.  It must not participate in trade economics.
        actual_won = parsed["expected_won"]
        if actual_selected is not None:
            selected = buckets[actual_selected]
            actual_raw = _to_micros(
                selected.market_q_yes
                if parsed["side"] == "YES"
                else float(selected.market_q_no)
            )
            actual_won = (
                _bucket_winner(payload["buckets"], actual_selected)
                if parsed["side"] == "YES"
                else not _bucket_winner(payload["buckets"], actual_selected)
            )
        actual_stressed = (
            min(1_000_000, actual_raw + 30_000)
            if actual_selected is not None
            else 0
        )
        actual_turnover = 5 * actual_stressed if actual_accept else 0
        actual_pnl = (
            5 * ((1_000_000 if actual_won else 0) - actual_stressed)
            if actual_accept
            else 0
        )
        if actual_raw != parsed["expected_raw_q_micros"]:
            raise ValueError(f"C4_V1_PARITY_RAW_Q_MISMATCH:{expectation_source}")
        if actual_stressed != parsed["expected_stressed_q_micros"]:
            raise ValueError(f"C4_V1_PARITY_STRESSED_Q_MISMATCH:{expectation_source}")
        if actual_won != parsed["expected_won"]:
            raise ValueError(f"C4_V1_PARITY_OUTCOME_MISMATCH:{expectation_source}")
        if actual_turnover != parsed["expected_turnover_micros"]:
            raise ValueError(f"C4_V1_PARITY_TURNOVER_MISMATCH:{expectation_source}")
        if actual_pnl != parsed["expected_pnl_micros"]:
            raise ValueError(f"C4_V1_PARITY_PNL_MISMATCH:{expectation_source}")
        components.append(
            _Component(
                market_identity_sha256=market_identity,
                checkpoint_minutes=checkpoint,
                family=parsed["family"],
                universe=parsed["universe"],
                partition=parsed["partition"],
                side=parsed["side"],
                expected_accept=parsed["expected_accept"],
                actual_accept=actual_accept,
                expected_selected_bucket_index=parsed[
                    "expected_selected_bucket_index"
                ],
                actual_selected_bucket_index=actual_selected,
                expected_won=parsed["expected_won"],
                actual_won=actual_won,
                expected_turnover_micros=parsed["expected_turnover_micros"],
                actual_turnover_micros=actual_turnover,
                expected_pnl_micros=parsed["expected_pnl_micros"],
                actual_pnl_micros=actual_pnl,
            )
        )
    return tuple(components)


def _validate_buckets(payload: object, source: str) -> tuple[BucketInput, ...]:
    if type(payload) is not list or len(payload) != 11:
        raise ValueError(f"C4_V1_PARITY_BUCKET_COUNT:{source}")
    rows: list[BucketInput] = []
    for index, item in enumerate(payload):
        item_source = f"{source}:bucket={index}"
        if type(item) is not dict or frozenset(item) != _BUCKET_KEYS:
            raise ValueError(f"C4_V1_PARITY_BUCKET_SCHEMA:{item_source}")
        bucket_index = _integer(item["bucket_index"], "bucket_index", item_source)
        if bucket_index != index:
            raise ValueError(f"C4_V1_PARITY_BUCKET_ORDER:{item_source}")
        if type(item["actual_winner"]) is not bool:
            raise ValueError(f"C4_V1_PARITY_BUCKET_WINNER:{item_source}")
        rows.append(
            BucketInput(
                bucket_index=bucket_index,
                model_p=_probability_micros(
                    item["model_p_micros"], "model_p", item_source
                ),
                market_q_yes=_probability_micros(
                    item["q_yes_micros"], "q_yes", item_source
                ),
                market_q_no=_probability_micros(
                    item["q_no_micros"], "q_no", item_source
                ),
                vwap5=None,
                confirmed_fee=0.0,
                no_token_id=None,
            )
        )
    if sum(bool(item["actual_winner"]) for item in payload) != 1:
        raise ValueError(f"C4_V1_PARITY_BUCKET_WINNER_COUNT:{source}")
    return tuple(rows)


def _validate_expectation(payload: object, source: str) -> dict[str, Any]:
    if type(payload) is not dict or frozenset(payload) != _EXPECTATION_KEYS:
        raise ValueError(f"C4_V1_PARITY_EXPECTATION_SCHEMA:{source}")
    family = _choice(payload["family"], {"STRICT_A", "PF1", "NO_FADE"}, "family", source)
    universe = _choice(payload["universe"], {"ALL", "U1", "U2"}, "universe", source)
    partition = _choice(payload["partition"], {"NA", "P1", "P2"}, "partition", source)
    side = _choice(payload["side"], {"YES", "NO"}, "side", source)
    if (family == "NO_FADE") != (side == "NO"):
        raise ValueError(f"C4_V1_PARITY_EXPECTATION_SIDE:{source}")
    if family == "NO_FADE":
        if universe == "ALL" or partition == "NA":
            raise ValueError(f"C4_V1_PARITY_EXPECTATION_SCOPE:{source}")
    elif universe != "ALL" or partition != "NA":
        raise ValueError(f"C4_V1_PARITY_EXPECTATION_SCOPE:{source}")
    parsed: dict[str, Any] = {
        "family": family,
        "universe": universe,
        "partition": partition,
        "side": side,
    }
    for name in ("expected_accept", "expected_won"):
        if type(payload[name]) is not bool:
            raise ValueError(f"C4_V1_PARITY_EXPECTATION_BOOLEAN:{name}:{source}")
        parsed[name] = payload[name]
    for name in (
        "expected_favorite_bucket_index",
        "expected_selected_bucket_index",
    ):
        value = payload[name]
        if value is not None and (type(value) is not int or not 0 <= value <= 10):
            raise ValueError(f"C4_V1_PARITY_EXPECTATION_INDEX:{name}:{source}")
        parsed[name] = value
    reason = payload["expected_reason"]
    if reason is not None and (type(reason) is not str or not reason):
        raise ValueError(f"C4_V1_PARITY_EXPECTATION_REASON:{source}")
    parsed["expected_reason"] = reason
    for name in (
        "expected_pnl_micros",
        "expected_raw_q_micros",
        "expected_stressed_q_micros",
        "expected_turnover_micros",
    ):
        parsed[name] = _integer(payload[name], name, source)
    for name in ("expected_raw_q_micros", "expected_stressed_q_micros"):
        if not 0 <= parsed[name] <= 1_000_000:
            raise ValueError(f"C4_V1_PARITY_EXPECTATION_RANGE:{name}:{source}")
    if parsed["expected_turnover_micros"] < 0:
        raise ValueError(f"C4_V1_PARITY_EXPECTATION_RANGE:turnover:{source}")
    return parsed


def _replay(
    *,
    family: str,
    universe: str,
    partition: str,
    checkpoint: int,
    buckets: tuple[BucketInput, ...],
    snapshot_hash: str,
    target: int,
    observation: int,
) -> tuple[V1Evaluation, bool, str | None]:
    if family == "STRICT_A":
        identity = {60: "YES_STRICT_A_T60", 30: "YES_STRICT_A_T30"}.get(
            checkpoint
        )
        if identity is None:
            raise ValueError("C4_V1_PARITY_STRICT_CHECKPOINT")
        result = evaluate_strict_historical(identity, checkpoint, buckets)
        return result, result.accepted, None if result.accepted else result.reason
    if family == "PF1":
        identity = {
            30: "YES_PF1_T30",
            60: "YES_PF1_T60",
            360: "YES_PF1_T6H",
            480: "YES_PF1_T8H",
        }.get(checkpoint)
        if identity is None:
            raise ValueError("C4_V1_PARITY_PF1_CHECKPOINT")
        result = evaluate_pf1_historical(identity, checkpoint, buckets)
        return result, result.accepted, None if result.accepted else result.reason
    identity = _representative_no_fade_identity(universe, partition, checkpoint)
    result = evaluate_no_fade_historical(identity, checkpoint, buckets)
    return result, result.accepted, None if result.accepted else result.reason


def _representative_no_fade_identity(
    universe: str, partition: str, checkpoint: int
) -> str:
    for strategy_id, binding in _BINDINGS.items():
        if (
            binding.family == "NO_FADE"
            and binding.universe == universe
            and binding.partition == partition
            and checkpoint in binding.checkpoints
        ):
            return strategy_id
    raise ValueError("C4_V1_PARITY_NO_FADE_CHECKPOINT")


def _verify_identity(
    strategy_id: str,
    binding: HistoricalIdentityBinding,
    components: Mapping[tuple[str, int, str, str, str], _Component],
) -> V1HistoricalIdentityParity:
    candidates = [
        component
        for component in components.values()
        if component.family == binding.family
        and component.partition == binding.partition
        and (
            component.universe == binding.universe
            or binding.policy == "EQUALITY" and component.universe in {"U1", "U2"}
        )
        and component.checkpoint_minutes in binding.checkpoints
    ]
    if not candidates:
        raise ValueError(f"C4_V1_PARITY_IDENTITY_NO_EVIDENCE:{strategy_id}")
    expected_trades, actual_trades = _apply_policy(strategy_id, binding, candidates)
    expected = _aggregate(strategy_id, expected_trades, expected=True)
    actual = _aggregate(strategy_id, actual_trades, expected=False)
    checkpoints: dict[int, int] = {}
    for component in actual_trades:
        checkpoints[component.checkpoint_minutes] = (
            checkpoints.get(component.checkpoint_minutes, 0) + 1
        )
    parity = expected == actual
    return V1HistoricalIdentityParity(
        strategy_id=strategy_id,
        fixture_decision_count=len(candidates),
        expected_trade_count=expected[0],
        actual_trade_count=actual[0],
        expected_wins=expected[1],
        actual_wins=actual[1],
        expected_losses=expected[0] - expected[1],
        actual_losses=actual[0] - actual[1],
        expected_turnover_micros=expected[2],
        actual_turnover_micros=actual[2],
        expected_pnl_micros=expected[3],
        actual_pnl_micros=actual[3],
        expected_roi=_roi(expected[3], expected[2]),
        actual_roi=_roi(actual[3], actual[2]),
        expected_trade_identity_sha256=expected[4],
        actual_trade_identity_sha256=actual[4],
        selected_checkpoint_counts=tuple(sorted(checkpoints.items())),
        parity_pass=parity,
    )


def _apply_policy(
    strategy_id: str,
    binding: HistoricalIdentityBinding,
    candidates: list[_Component],
) -> tuple[list[_Component], list[_Component]]:
    if binding.policy == "EQUALITY":
        by_universe = {
            universe: [item for item in candidates if item.universe == universe]
            for universe in ("U1", "U2")
        }
        for expected in (True, False):
            first = _trade_relation_set(strategy_id, by_universe["U1"], expected)
            second = _trade_relation_set(strategy_id, by_universe["U2"], expected)
            if first != second:
                raise ValueError("C4_V1_PARITY_U1_U2_TRADE_SET_MISMATCH")
        canonical = by_universe["U1"]
        return (
            [item for item in canonical if item.expected_accept],
            [item for item in canonical if item.actual_accept],
        )
    by_market: dict[str, dict[int, _Component]] = {}
    for component in candidates:
        per_market = by_market.setdefault(component.market_identity_sha256, {})
        if component.checkpoint_minutes in per_market:
            raise ValueError("C4_V1_PARITY_DUPLICATE_POLICY_CHECKPOINT")
        per_market[component.checkpoint_minutes] = component
    expected: list[_Component] = []
    actual: list[_Component] = []
    required = set(binding.checkpoints)
    for market in sorted(by_market):
        checkpoint_rows = by_market[market]
        if set(checkpoint_rows) != required:
            raise ValueError(f"C4_V1_PARITY_POLICY_CHECKPOINT_GAP:{strategy_id}")
        ordered = [checkpoint_rows[checkpoint] for checkpoint in binding.checkpoints]
        expected.extend(_select_policy(binding.policy, ordered, expected=True))
        actual.extend(_select_policy(binding.policy, ordered, expected=False))
    return expected, actual


def _select_policy(
    policy: str, ordered: list[_Component], *, expected: bool
) -> list[_Component]:
    accepted = (
        (lambda item: item.expected_accept)
        if expected
        else (lambda item: item.actual_accept)
    )
    if policy == "FIXED":
        return [ordered[0]] if accepted(ordered[0]) else []
    if policy == "FALLBACK":
        for item in ordered:
            if accepted(item):
                return [item]
        return []
    if policy == "INDEPENDENT":
        return [item for item in ordered if accepted(item)]
    if policy == "EARLIEST_ONLY":
        return [ordered[0]] if accepted(ordered[0]) else []
    if policy == "LATEST_ONLY":
        return [ordered[-1]] if accepted(ordered[-1]) else []
    raise ValueError("C4_V1_PARITY_UNKNOWN_POLICY")


def _aggregate(
    strategy_id: str, trades: list[_Component], *, expected: bool
) -> tuple[int, int, int, int, str]:
    identities = []
    wins = turnover = pnl = 0
    for item in trades:
        selected = (
            item.expected_selected_bucket_index
            if expected
            else item.actual_selected_bucket_index
        )
        if selected is None:
            raise ValueError("C4_V1_PARITY_ACCEPTED_WITHOUT_SELECTION")
        won = item.expected_won if expected else item.actual_won
        wins += int(won)
        turnover += (
            item.expected_turnover_micros if expected else item.actual_turnover_micros
        )
        pnl += item.expected_pnl_micros if expected else item.actual_pnl_micros
        identities.append(
            hashlib.sha256(
                _canonical_json(
                    [
                        strategy_id,
                        item.market_identity_sha256,
                        item.checkpoint_minutes,
                        selected,
                        item.side,
                    ]
                ).encode("utf-8")
            ).hexdigest()
        )
    identity_hash = hashlib.sha256(
        _canonical_json(sorted(identities)).encode("utf-8")
    ).hexdigest()
    return len(trades), wins, turnover, pnl, identity_hash


def _trade_relation_set(
    strategy_id: str, rows: list[_Component], expected: bool
) -> set[tuple[object, ...]]:
    result: set[tuple[object, ...]] = set()
    for item in rows:
        accepted = item.expected_accept if expected else item.actual_accept
        if not accepted:
            continue
        result.add(
            (
                item.market_identity_sha256,
                item.checkpoint_minutes,
                item.expected_selected_bucket_index
                if expected
                else item.actual_selected_bucket_index,
                item.side,
                item.expected_won if expected else item.actual_won,
                item.expected_turnover_micros
                if expected
                else item.actual_turnover_micros,
                item.expected_pnl_micros if expected else item.actual_pnl_micros,
            )
        )
    return result


def _bucket_winner(payload: object, bucket_index: int) -> bool:
    assert type(payload) is list
    value = payload[bucket_index]["actual_winner"]
    assert type(value) is bool
    return value


def _favorite_bucket_index(buckets: tuple[BucketInput, ...]) -> int | None:
    maximum = max(row.market_q_yes for row in buckets)
    tied = tuple(
        row.bucket_index
        for row in buckets
        if abs(row.market_q_yes - maximum) <= 1e-9
    )
    return tied[0] if len(tied) == 1 else None


def _probability_micros(value: object, name: str, source: str) -> float:
    integer = _integer(value, name, source)
    if not 0 <= integer <= 1_000_000:
        raise ValueError(f"C4_V1_PARITY_PROBABILITY:{name}:{source}")
    return integer / 1_000_000


def _to_micros(value: float) -> int:
    return int(Decimal(str(value)) * Decimal(1_000_000))


def _integer(value: object, name: str, source: str) -> int:
    if type(value) is not int:
        raise ValueError(f"C4_V1_PARITY_INTEGER:{name}:{source}")
    return value


def _hash(value: object, name: str, source: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"C4_V1_PARITY_SHA256:{name}:{source}")
    return value


def _choice(value: object, allowed: set[str], name: str, source: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError(f"C4_V1_PARITY_CHOICE:{name}:{source}")
    return value


def _roi(pnl_micros: int, turnover_micros: int) -> str:
    if turnover_micros == 0:
        return "0"
    with localcontext() as context:
        context.prec = 50
        value = Decimal(pnl_micros) / Decimal(turnover_micros)
    return format(value.normalize(), "f")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
