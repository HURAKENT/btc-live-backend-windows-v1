from __future__ import annotations

import argparse
import csv
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


if __package__ in (None, ""):
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    _PROJECT_ROOT_TEXT = str(_PROJECT_ROOT)
    if _PROJECT_ROOT_TEXT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT_TEXT)

from src.strategy_v1_other_parity import verify_v1_other_parity_rows


_GIT_SHA40 = re.compile(r"[0-9a-f]{40}")
_SOURCE_HASHES = {
    "actual_bucket_probabilities_170.parquet": "66e81c1864c353be6b52318891c23439d4090baaedec22fbca5adb547a6b25fa",
    "markets.parquet": "8c197dc99a1acf48a0c85166ffeba8af0dff9536d19d3beab1d0991faa1fa6ec",
    "settlements.parquet": "ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1",
    "checkpoint_coverage.csv": "4d4a1f9c634990b2941ac3dc90e88660331030da8255dfdbf1b2bb26d48d4967",
    "CONFIRMATION_DATES_136.csv": "2f87e5bcb0078a10e3a58453c3c4f1c6eb5e7d0057bfb359f5fb873eb15aeea8",
    "CONFIRMATION_TRADE_LEDGER.csv": "74e0c479e81558f38c5f152a0ec62c923fe6eda080dda75167f8015a8da51440",
    "BASKET_TRADE_CLASSIFICATION.csv": "6d6d71d03002a5c3e62566b5ddd4cc20d18204affff6debaa6c461c3618d5361",
}
_IDENTITIES = [
    "NO_A0",
    "NO_A2",
    "NO_B2",
    "NO_C1",
    "YES_FAVORITE_NEIGHBOR_BASKET",
    "YES_FAVORITE_ONLY",
]


def build_records(
    *,
    atlas_rows: list[dict[str, object]],
    market_rows: list[dict[str, object]],
    settlement_rows: list[dict[str, object]],
    coverage_rows: list[dict[str, str]],
    confirmation_dates: set[str],
    confirmation_rows: list[dict[str, str]],
    basket_rows: list[dict[str, str]],
) -> list[dict[str, object]]:
    if len(confirmation_dates) != 136:
        raise ValueError("C4_V1_OTHER_CONFIRMATION_DATE_COUNT")
    market_token: dict[tuple[str, str], str] = {}
    for row in market_rows:
        key = (str(row["market_date"]), str(row["market_id"]))
        token = str(row["no_token_id"])
        if key in market_token and market_token[key] != token:
            raise ValueError("C4_V1_OTHER_MARKET_TOKEN_CONFLICT")
        market_token[key] = token
    coverage: dict[tuple[str, str, int], int] = {}
    for row in coverage_rows:
        if row.get("exists") != "True" or row.get("no_lookahead") != "True":
            raise ValueError("C4_V1_OTHER_COVERAGE_INVALID")
        checkpoint = {"T-60": 60, "T-30": 30}.get(row.get("checkpoint"))
        if checkpoint is None:
            continue
        key = (row["market_date"], row["no_token_id"], checkpoint)
        value = _micros(row["price"])
        if key in coverage and coverage[key] != value:
            raise ValueError("C4_V1_OTHER_COVERAGE_CONFLICT")
        coverage[key] = value
    settlement_winner = {
        str(row["market_date"]): (
            None
            if row.get("independent_winner_bucket") in (None, "")
            else str(row["independent_winner_bucket"])
        )
        for row in settlement_rows
    }

    groups: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for row in atlas_rows:
        checkpoint = int(row["horizon_minutes"])
        if checkpoint in (30, 60):
            groups[(str(row["market_date"]), checkpoint)].append(row)
    dates = sorted({date for date, _ in groups})
    if len(dates) != 170:
        raise ValueError("C4_V1_OTHER_ATLAS_DATE_COUNT")
    confirmation = _confirmation_map(confirmation_rows)
    basket = _basket_map(basket_rows)
    records: list[dict[str, object]] = [
        {
            "covered_strategy_ids": _IDENTITIES,
            "record_type": "manifest",
            "schema_version": "C4_V1_CONFIRMATION_BASKET_PARITY_V1",
            "shares": 5,
            "source_hashes": dict(sorted(_SOURCE_HASHES.items())),
            "trading_approval": False,
        }
    ]
    for market_date in dates:
        buckets_by_checkpoint: dict[int, list[dict[str, object]]] = {}
        for checkpoint in (60, 30):
            source = sorted(
                groups.get((market_date, checkpoint), []),
                key=lambda row: int(row["bucket_index"]),
            )
            if len(source) != 11 or [int(row["bucket_index"]) for row in source] != list(range(11)):
                raise ValueError("C4_V1_OTHER_EXPECTED_11_BUCKETS")
            converted = []
            for row in source:
                market_id = str(row["market_id"])
                token = market_token.get((market_date, market_id))
                if token is None:
                    raise ValueError("C4_V1_OTHER_MARKET_TOKEN_MISSING")
                q_yes = _micros(row["market_q_raw"])
                q_no = (
                    coverage.get((market_date, token, checkpoint))
                    if market_date in confirmation_dates
                    else 1_000_000 - q_yes
                )
                if q_no is None:
                    raise ValueError("C4_V1_OTHER_ACTUAL_NO_MISSING")
                converted.append(
                    {
                        "actual_winner": bool(row["actual_winner"]),
                        "bucket_index": int(row["bucket_index"]),
                        "model_p_micros": _micros(row["model_p"]),
                        "no_token_identity_sha256": _digest(["no-token", token]),
                        "q_no_micros": q_no,
                        "q_yes_micros": q_yes,
                    }
                )
            buckets_by_checkpoint[checkpoint] = converted
        expectations: list[dict[str, object]] = []
        confirmation_winner_index = None
        winner_title = settlement_winner.get(market_date)
        if winner_title is not None:
            winner_matches = [
                int(row["bucket_index"])
                for row in groups[(market_date, 60)]
                if str(row.get("bucket_title")) == winner_title
            ]
            if len(winner_matches) != 1:
                raise ValueError("C4_V1_OTHER_CONFIRMATION_WINNER_MAPPING")
            confirmation_winner_index = winner_matches[0]
        if market_date in confirmation_dates:
            for strategy_id in _IDENTITIES[:4]:
                expectations.append(
                    _confirmation_expectation(strategy_id, confirmation.get((strategy_id[3:], market_date)))
                )
        for strategy_id, variant in (
            ("YES_FAVORITE_NEIGHBOR_BASKET", "B"),
            ("YES_FAVORITE_ONLY", "favorite-only control"),
        ):
            expectations.append(
                _basket_expectation(strategy_id, basket.get((variant, market_date)))
            )
        records.append(
            {
                "confirmation_cohort": market_date in confirmation_dates,
                "confirmation_winner_bucket_index": confirmation_winner_index,
                "expectations": expectations,
                "market_identity_sha256": _digest(["market", market_date]),
                "record_type": "market_decisions",
                "t30_buckets": buckets_by_checkpoint[30],
                "t60_buckets": buckets_by_checkpoint[60],
            }
        )
    report = verify_v1_other_parity_rows(records)
    if not report.parity_pass or report.strategy_count != 6 or report.decision_count != 884:
        raise ValueError("C4_V1_OTHER_PARITY_FAILED")
    return records


def _confirmation_map(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    if len(rows) != 87:
        raise ValueError("C4_V1_OTHER_CONFIRMATION_LEDGER_COUNT")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["concept_id"], row["market_date"])
        if key in result:
            raise ValueError("C4_V1_OTHER_CONFIRMATION_DUPLICATE")
        result[key] = row
    return result


def _basket_map(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    if len(rows) != 280:
        raise ValueError("C4_V1_OTHER_BASKET_LEDGER_COUNT")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if row["strategy_variant"] not in {"B", "favorite-only control"}:
            continue
        key = (row["strategy_variant"], row["market_date"])
        if key in result:
            raise ValueError("C4_V1_OTHER_BASKET_DUPLICATE")
        result[key] = row
    return result


def _confirmation_expectation(
    strategy_id: str, row: dict[str, str] | None
) -> dict[str, object]:
    if row is None:
        return _rejected(strategy_id)
    checkpoint = {"T-60": 60, "T-30": 30}[row["checkpoint"]]
    selected = int(row["bucket_index"])
    won = row["no_won"] == "True"
    cost = min(1_000_000, _micros(row["actual_qNO"]) + 30_000)
    return _accepted(strategy_id, checkpoint, [selected], won, cost)


def _basket_expectation(
    strategy_id: str, row: dict[str, str] | None
) -> dict[str, object]:
    if row is None:
        return _rejected(strategy_id)
    checkpoint = {"T-60": 60, "T-30": 30}[row["checkpoint"]]
    selected = [int(part) for part in row["selected_bucket_indices"].split("|")]
    won = row["won"] == "True"
    cost = _micros(row["basket_stressed_cost"])
    return _accepted(strategy_id, checkpoint, selected, won, cost)


def _accepted(
    strategy_id: str,
    checkpoint: int,
    selected: list[int],
    won: bool,
    cost: int,
) -> dict[str, object]:
    return {
        "expected_accept": True,
        "expected_checkpoint_minutes": checkpoint,
        "expected_pnl_micros": 5 * ((1_000_000 if won else 0) - cost),
        "expected_selected_bucket_indices": selected,
        "expected_stressed_cost_micros": cost,
        "expected_turnover_micros": 5 * cost,
        "expected_won": won,
        "strategy_id": strategy_id,
    }


def _rejected(strategy_id: str) -> dict[str, object]:
    return {
        "expected_accept": False,
        "expected_checkpoint_minutes": None,
        "expected_pnl_micros": 0,
        "expected_selected_bucket_indices": [],
        "expected_stressed_cost_micros": 0,
        "expected_turnover_micros": 0,
        "expected_won": False,
        "strategy_id": strategy_id,
    }


def canonical_jsonl(records: list[dict[str, object]]) -> bytes:
    return "".join(_canonical_json(row) + "\n" for row in records).encode("utf-8")


def _micros(value: object) -> int:
    if isinstance(value, str):
        decimal = Decimal(value)
    elif type(value) in (int, float) and type(value) is not bool and math.isfinite(float(value)):
        decimal = Decimal(str(value))
    else:
        raise ValueError("C4_V1_OTHER_INVALID_NUMBER")
    result = int((decimal * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    if not 0 <= result <= 1_000_000:
        raise ValueError("C4_V1_OTHER_PROBABILITY_RANGE")
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build sanitized C4 V1 confirmation/basket parity evidence.")
    parser.add_argument("--atlas", type=Path, required=True)
    parser.add_argument("--markets", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--settlements", type=Path, required=True)
    parser.add_argument("--confirmation-dates", type=Path, required=True)
    parser.add_argument("--confirmation-ledger", type=Path, required=True)
    parser.add_argument("--basket-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pyarrow-path", type=Path)
    args = parser.parse_args(argv)
    if _GIT_SHA40.fullmatch(args.source_commit) is None:
        raise ValueError("C4_V1_OTHER_SOURCE_COMMIT")
    paths = {
        "actual_bucket_probabilities_170.parquet": args.atlas,
        "markets.parquet": args.markets,
        "settlements.parquet": args.settlements,
        "checkpoint_coverage.csv": args.coverage,
        "CONFIRMATION_DATES_136.csv": args.confirmation_dates,
        "CONFIRMATION_TRADE_LEDGER.csv": args.confirmation_ledger,
        "BASKET_TRADE_CLASSIFICATION.csv": args.basket_ledger,
    }
    for name, path in paths.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != _SOURCE_HASHES[name]:
            raise ValueError(f"C4_V1_OTHER_SOURCE_HASH_MISMATCH:{name}")
    if args.pyarrow_path is not None:
        sys.path.insert(0, str(args.pyarrow_path.resolve()))
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("C4_V1_OTHER_PYARROW_REQUIRED_FOR_BUILD_ONLY") from exc
    atlas_rows = parquet.read_table(
        args.atlas,
        columns=["market_date", "horizon_minutes", "bucket_index", "bucket_title", "market_id", "market_q_raw", "model_p", "actual_winner"],
    ).to_pylist()
    market_rows = parquet.read_table(
        args.markets,
        columns=["market_date", "market_id", "no_token_id"],
    ).to_pylist()
    settlement_rows = parquet.read_table(
        args.settlements,
        columns=["market_date", "independent_winner_bucket"],
    ).to_pylist()
    date_rows = _read_csv(args.confirmation_dates)
    date_field = next(iter(date_rows[0]))
    records = build_records(
        atlas_rows=atlas_rows,
        market_rows=market_rows,
        settlement_rows=settlement_rows,
        coverage_rows=_read_csv(args.coverage),
        confirmation_dates={row[date_field] for row in date_rows},
        confirmation_rows=_read_csv(args.confirmation_ledger),
        basket_rows=_read_csv(args.basket_ledger),
    )
    fixture = canonical_jsonl(records)
    receipt = {
        "converter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "decision_count": 884,
        "fixture_record_count": len(records),
        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
        "network_requests": 0,
        "pyarrow_version": pyarrow.__version__,
        "schema_version": "C4_V1_CONFIRMATION_BASKET_PARITY_RECEIPT_V1",
        "source_commit": args.source_commit,
        "source_hashes": dict(sorted(_SOURCE_HASHES.items())),
        "strategy_count": 6,
        "trading_approval": False,
    }
    _atomic_write(args.output, fixture)
    _atomic_write(args.receipt, (_canonical_json(receipt) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
