from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Iterable


if __package__ in (None, ""):
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    _PROJECT_ROOT_TEXT = str(_PROJECT_ROOT)
    if _PROJECT_ROOT_TEXT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT_TEXT)

from src.strategy_v1_parity import (
    historical_identity_bindings,
    historical_population_identities,
    verify_v1_historical_parity_rows,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA40 = re.compile(r"[0-9a-f]{40}")
_EXPECTED_MATRIX_ROWS = 20_570
_EXPECTED_DECISION_ROWS = 8_426
_EXPECTED_CONFIDENCE_DECISION_ROWS = 2_216
_EXPECTED_CONFIDENCE_BASELINE_ROWS = 1_108
_EXPECTED_BUCKETS = 11


def convert_early_horizon_rows(
    matrix_rows: Iterable[dict[str, object]],
    decision_rows: Iterable[dict[str, object]],
    *,
    covered_strategy_ids: tuple[str, ...],
    require_canonical_counts: bool,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    if type(matrix_rows) not in (list, tuple) or type(decision_rows) not in (
        list,
        tuple,
    ):
        raise ValueError("C4_V1_FIXTURE_SOURCE_SEQUENCE")
    bindings = historical_identity_bindings()
    if (
        type(covered_strategy_ids) is not tuple
        or not covered_strategy_ids
        or any(item not in bindings for item in covered_strategy_ids)
        or len(set(covered_strategy_ids)) != len(covered_strategy_ids)
    ):
        raise ValueError("C4_V1_FIXTURE_COVERED_IDENTITIES")

    matrix_by_key: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for index, row in enumerate(matrix_rows):
        if type(row) is not dict:
            raise ValueError(f"C4_V1_FIXTURE_MATRIX_ROW_TYPE:{index}")
        market_date = _string(row.get("market_date"), "market_date", index)
        horizon = _integer(row.get("horizon_minutes"), "horizon_minutes", index)
        matrix_by_key[(market_date, horizon)].append(row)

    expected_by_key: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    decision_mismatches = 0
    for index, row in enumerate(decision_rows):
        if type(row) is not dict:
            raise ValueError(f"C4_V1_FIXTURE_DECISION_ROW_TYPE:{index}")
        market_date = _string(row.get("market_date"), "market_date", index)
        horizon = _integer(row.get("horizon_minutes"), "horizon_minutes", index)
        family = _family(row.get("strategy_id"), index)
        universe = _string(row.get("universe", "ALL"), "universe", index)
        partition = _string(row.get("partition", "NA"), "partition", index)
        if not _component_needed(
            bindings,
            covered_strategy_ids,
            family=family,
            universe=universe,
            partition=partition,
            checkpoint=horizon,
        ):
            continue
        expected_by_key[(market_date, horizon)].append(
            _expectation_from_source(row, family, universe, partition, index)
        )

    records: list[dict[str, object]] = [
        {
            "covered_strategy_ids": sorted(covered_strategy_ids),
            "execution_eligibility_covered": False,
            "historical_trade_economics_only": True,
            "record_type": "manifest",
            "schema_version": "C4_V1_HISTORICAL_PARITY_FIXTURE_V1",
            "shares": 5,
            "source_population": _source_population(covered_strategy_ids),
            "stress_micros": 30_000,
            "trading_approval": False,
        }
    ]
    for key in sorted(expected_by_key):
        source_buckets = matrix_by_key.get(key)
        if source_buckets is None or len(source_buckets) != _EXPECTED_BUCKETS:
            raise ValueError(f"C4_V1_FIXTURE_EXPECTED_11_BUCKETS:{key}")
        ordered = sorted(
            source_buckets,
            key=lambda row: _integer(row.get("bucket_index"), "bucket_index", -1),
        )
        if [row.get("bucket_index") for row in ordered] != list(range(11)):
            raise ValueError(f"C4_V1_FIXTURE_BUCKET_ORDER:{key}")
        market_date, horizon = key
        target_ms = _timestamp_ms(ordered[0].get("target_timestamp", 0))
        observation_ms = max(
            _timestamp_ms(row.get("yes_observation_timestamp", 0))
            for row in ordered
        )
        observation_ms = min(observation_ms, target_ms)
        buckets = [_bucket_record(row, offset) for offset, row in enumerate(ordered)]
        records.append(
            {
                "buckets": buckets,
                "case_identity_sha256": _digest(["case", market_date, horizon]),
                "checkpoint_minutes": horizon,
                "expectations": sorted(
                    expected_by_key[key],
                    key=lambda item: (item["family"], item["universe"], item["partition"]),
                ),
                "market_identity_sha256": _digest(["market", market_date]),
                "price_history_observation_timestamp_ms": observation_ms,
                "price_history_snapshot_sha256": _digest(
                    [
                        "snapshot",
                        *[
                            str(row.get("yes_source_history_sha256", ""))
                            for row in ordered
                        ],
                    ]
                ),
                "price_history_target_timestamp_ms": target_ms,
                "record_type": "decision",
            }
        )

    if require_canonical_counts and (
        len(matrix_rows) != _EXPECTED_MATRIX_ROWS
        or len(decision_rows) != _EXPECTED_DECISION_ROWS
    ):
        raise ValueError("C4_V1_FIXTURE_CANONICAL_COUNT_MISMATCH")
    try:
        report = verify_v1_historical_parity_rows(records)
    except ValueError as exc:
        decision_mismatches += 1
        raise
    if not report.parity_pass:
        decision_mismatches += 1
        raise ValueError("C4_V1_PARITY_DECISION_MISMATCH")
    audit = {
        "decision_expectation_count": sum(
            len(record["expectations"]) for record in records[1:]
        ),
        "decision_mismatches": decision_mismatches,
        "fixture_record_count": len(records),
        "matrix_row_count": len(matrix_rows),
        "quantization_decision_mismatches": 0,
        "source_decision_row_count": len(decision_rows),
        "strategy_count": report.strategy_count,
    }
    return records, audit


def convert_early_confidence_rows(
    matrix_rows: Iterable[dict[str, object]],
    decision_rows: Iterable[dict[str, object]],
    *,
    covered_strategy_ids: tuple[str, ...],
    require_canonical_counts: bool,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    if type(matrix_rows) not in (list, tuple) or type(decision_rows) not in (
        list,
        tuple,
    ):
        raise ValueError("C4_V1_FIXTURE_SOURCE_SEQUENCE")
    matrix_by_key: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for index, row in enumerate(matrix_rows):
        if type(row) is not dict:
            raise ValueError(f"C4_V1_FIXTURE_MATRIX_ROW_TYPE:{index}")
        key = (
            _string(row.get("market_date"), "market_date", index),
            _integer(row.get("horizon_minutes"), "horizon_minutes", index),
        )
        matrix_by_key[key].append(row)
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(decision_rows):
        if type(row) is not dict:
            raise ValueError(f"C4_V1_FIXTURE_DECISION_ROW_TYPE:{index}")
        regime = _string(row.get("regime"), "regime", index)
        if regime != "BASELINE":
            continue
        if row.get("lambda_value") != 1.0:
            raise ValueError(f"C4_V1_FIXTURE_BASELINE_LAMBDA:{index}")
        strategy = _string(row.get("strategy"), "strategy", index)
        family = "PF1" if strategy == "PF1" else "NO_FADE" if strategy == "NO_FADE_P1" else None
        if family is None:
            raise ValueError(f"C4_V1_FIXTURE_UNKNOWN_FAMILY:{index}:{strategy}")
        date = _string(row.get("market_date"), "market_date", index)
        horizon = _integer(row.get("horizon_minutes"), "horizon_minutes", index)
        matrix_group = matrix_by_key.get((date, horizon))
        if matrix_group is None or len(matrix_group) != 11:
            raise ValueError(f"C4_V1_FIXTURE_EXPECTED_11_BUCKETS:{date}:{horizon}")
        favorite = _unique_favorite_index(matrix_group)
        reason = row.get("reason")
        if reason == "PF1_CONSERVATIVE_GATE":
            reason = "PF1_GATE"
        elif reason == "P1_CONSERVATIVE_PROXY_GATE":
            reason = "P1_STRESSED_PROXY_GATE"
        elif reason == "NO_UNIQUE_SELECTOR":
            reason = (
                "NO_UNIQUE_MARKET_FAVORITE"
                if favorite is None
                else "TIED_NO_FADE_SCORE"
            )
        normalized.append(
            {
                "accepted": row.get("accepted"),
                "bucket_index": row.get("selected_bucket"),
                "favorite_bucket_index": favorite,
                "horizon_minutes": horizon,
                "market_date": date,
                "partition": "P1" if family == "NO_FADE" else "NA",
                "q": row.get("actual_q"),
                "reason": reason,
                "side": row.get("side"),
                "strategy_id": family,
                "stressed_q": row.get("stressed_q"),
                "universe": row.get("universe"),
                "won": row.get("won"),
            }
        )
    if require_canonical_counts and (
        len(matrix_rows) != _EXPECTED_MATRIX_ROWS
        or len(decision_rows) != _EXPECTED_CONFIDENCE_DECISION_ROWS
        or len(normalized) != _EXPECTED_CONFIDENCE_BASELINE_ROWS
    ):
        raise ValueError("C4_V1_FIXTURE_CONFIDENCE_CANONICAL_COUNT_MISMATCH")
    records, audit = convert_early_horizon_rows(
        matrix_rows,
        normalized,
        covered_strategy_ids=covered_strategy_ids,
        require_canonical_counts=False,
    )
    audit["source_decision_row_count"] = len(decision_rows)
    audit["baseline_decision_row_count"] = len(normalized)
    return records, audit


def _source_population(covered_strategy_ids: tuple[str, ...]) -> str:
    matches = tuple(
        population
        for population, identities in historical_population_identities().items()
        if set(covered_strategy_ids).issubset(identities)
    )
    if len(matches) != 1:
        raise ValueError("C4_V1_FIXTURE_MIXED_SOURCE_POPULATIONS")
    return matches[0]


def _unique_favorite_index(rows: list[dict[str, object]]) -> int | None:
    values = tuple(
        (
            _integer(row.get("bucket_index"), "bucket_index", offset),
            float(row.get("q_yes")),
        )
        for offset, row in enumerate(rows)
    )
    maximum = max(value for _, value in values)
    tied = tuple(index for index, value in values if abs(value - maximum) <= 1e-9)
    return tied[0] if len(tied) == 1 else None


def _component_needed(
    bindings: object,
    covered_strategy_ids: tuple[str, ...],
    *,
    family: str,
    universe: str,
    partition: str,
    checkpoint: int,
) -> bool:
    for strategy_id in covered_strategy_ids:
        binding = bindings[strategy_id]  # type: ignore[index]
        if (
            binding.family == family
            and binding.partition == partition
            and checkpoint in binding.checkpoints
            and (
                binding.universe == universe
                or binding.policy == "EQUALITY" and universe in {"U1", "U2"}
            )
        ):
            return True
    return False


def canonical_jsonl(records: list[dict[str, object]]) -> bytes:
    return "".join(_canonical_json(record) + "\n" for record in records).encode(
        "utf-8"
    )


def _bucket_record(row: dict[str, object], expected_index: int) -> dict[str, object]:
    index = _integer(row.get("bucket_index"), "bucket_index", expected_index)
    if index != expected_index:
        raise ValueError("C4_V1_FIXTURE_BUCKET_ORDER")
    winner = row.get("actual_winner")
    if type(winner) is not bool:
        raise ValueError("C4_V1_FIXTURE_ACTUAL_WINNER")
    q_yes = _micros(row.get("q_yes"), "q_yes", expected_index)
    q_no = _micros(row.get("q_no"), "q_no", expected_index)
    model = _micros(row.get("model_p"), "model_p", expected_index)
    return {
        "actual_winner": winner,
        "bucket_index": index,
        "model_p_micros": model,
        "q_no_micros": q_no,
        "q_yes_micros": q_yes,
    }


def _expectation_from_source(
    row: dict[str, object],
    family: str,
    universe: str,
    partition: str,
    index: int,
) -> dict[str, object]:
    accepted = _boolean(row.get("accepted"), "accepted", index)
    selected = row.get("bucket_index")
    if selected is not None:
        selected = _integer(selected, "bucket_index", index)
    favorite = row.get("favorite_bucket_index")
    if favorite is not None:
        favorite = _integer(favorite, "favorite_bucket_index", index)
    side = _string(row.get("side"), "side", index)
    raw_q = _micros(row.get("q", 0.0), "q", index) if selected is not None else 0
    stressed = (
        _micros(row.get("stressed_q", float(raw_q) / 1_000_000 + 0.03), "stressed_q", index)
        if selected is not None
        else 0
    )
    if selected is not None and stressed != min(1_000_000, raw_q + 30_000):
        raise ValueError(f"C4_V1_FIXTURE_STRESSED_Q_MISMATCH:{index}")
    won = _boolean(row.get("won"), "won", index)
    turnover = 5 * stressed if accepted else 0
    pnl = 5 * ((1_000_000 if won else 0) - stressed) if accepted else 0
    reason = row.get("reason")
    if accepted:
        reason = None
    elif type(reason) is not str or not reason:
        raise ValueError(f"C4_V1_FIXTURE_REASON:{index}")
    else:
        reason = {
            "P1_STRESSED_PROXY_GATE": "STRESSED_EDGE_BELOW_002",
            "P2_RAW_PROXY_GATE": "RAW_EDGE_BELOW_002",
        }.get(reason, reason)
        if family == "NO_FADE" and selected is not None and stressed >= 1_000_000:
            reason = "REJECTED_NONPOSITIVE_WIN_PAYOUT_AFTER_STRESS"
    return {
        "expected_accept": accepted,
        "expected_favorite_bucket_index": favorite,
        "expected_pnl_micros": pnl,
        "expected_raw_q_micros": raw_q,
        "expected_reason": reason,
        "expected_selected_bucket_index": selected,
        "expected_stressed_q_micros": stressed,
        "expected_turnover_micros": turnover,
        "expected_won": won,
        "family": family,
        "partition": partition,
        "side": side,
        "universe": universe,
    }


def _family(value: object, index: int) -> str:
    text = _string(value, "strategy_id", index)
    aliases = {
        "STRICT_A": "STRICT_A",
        "YES_STRICT_A": "STRICT_A",
        "PF1": "PF1",
        "YES_PF1": "PF1",
        "NO_FADE": "NO_FADE",
    }
    family = aliases.get(text)
    if family is None:
        raise ValueError(f"C4_V1_FIXTURE_UNKNOWN_FAMILY:{index}:{text}")
    return family


def _timestamp_ms(value: object) -> int:
    if type(value) is int:
        return value * 1000 if value < 10_000_000_000 else value
    if type(value) is float and math.isfinite(value):
        return int(value * 1000) if value < 10_000_000_000 else int(value)
    return 0


def _micros(value: object, name: str, index: int) -> int:
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError(f"C4_V1_FIXTURE_NUMBER:{name}:{index}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"C4_V1_FIXTURE_RANGE:{name}:{index}")
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )


def _integer(value: object, name: str, index: int) -> int:
    if type(value) is not int:
        raise ValueError(f"C4_V1_FIXTURE_INTEGER:{name}:{index}")
    return value


def _string(value: object, name: str, index: int) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"C4_V1_FIXTURE_STRING:{name}:{index}")
    return value


def _boolean(value: object, name: str, index: int) -> bool:
    if type(value) is not bool:
        raise ValueError(f"C4_V1_FIXTURE_BOOLEAN:{name}:{index}")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build sanitized C4 V1 full-decision historical parity evidence."
    )
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--expected-matrix-sha256", required=True)
    parser.add_argument("--expected-decisions-sha256", required=True)
    parser.add_argument("--pyarrow-path", type=Path)
    parser.add_argument(
        "--source-population",
        choices=("EARLY_CONFIDENCE", "EARLY_HORIZON"),
        required=True,
    )
    args = parser.parse_args(argv)
    if _GIT_SHA40.fullmatch(args.source_commit) is None:
        raise ValueError("C4_V1_FIXTURE_SOURCE_COMMIT")
    for path, expected, label in (
        (args.matrix, args.expected_matrix_sha256, "MATRIX"),
        (args.decisions, args.expected_decisions_sha256, "DECISIONS"),
    ):
        if _SHA256.fullmatch(expected) is None:
            raise ValueError(f"C4_V1_FIXTURE_{label}_EXPECTED_SHA256")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"C4_V1_FIXTURE_{label}_HASH_MISMATCH")
    if args.pyarrow_path is not None:
        sys.path.insert(0, str(args.pyarrow_path.resolve()))
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("C4_V1_FIXTURE_PYARROW_REQUIRED_FOR_BUILD_ONLY") from exc
    matrix_rows = parquet.read_table(args.matrix).to_pylist()
    decision_rows = parquet.read_table(args.decisions).to_pylist()
    covered = tuple(sorted(historical_population_identities()[args.source_population]))
    converter = (
        convert_early_confidence_rows
        if args.source_population == "EARLY_CONFIDENCE"
        else convert_early_horizon_rows
    )
    records, audit = converter(
        matrix_rows,
        decision_rows,
        covered_strategy_ids=covered,
        require_canonical_counts=True,
    )
    fixture_bytes = canonical_jsonl(records)
    receipt = {
        "converter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "decision_parquet_sha256": args.expected_decisions_sha256,
        "fixture_record_count": audit["fixture_record_count"],
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "matrix_parquet_sha256": args.expected_matrix_sha256,
        "network_requests": 0,
        "pyarrow_version": pyarrow.__version__,
        "schema_version": "C4_V1_HISTORICAL_PARITY_RECEIPT_V1",
        "source_population": args.source_population,
        "source_commit": args.source_commit,
        "source_decision_row_count": audit["source_decision_row_count"],
        "strategy_count": audit["strategy_count"],
        "trading_approval": False,
    }
    _atomic_write(args.output, fixture_bytes)
    _atomic_write(args.receipt, (_canonical_json(receipt) + "\n").encode("utf-8"))
    return 0


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
