from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterable


if __package__ in (None, ""):
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    _PROJECT_ROOT_TEXT = str(_PROJECT_ROOT)
    if _PROJECT_ROOT_TEXT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT_TEXT)

from src.strategy_v2 import V2_OVERLAY_BINDINGS


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GIT_SHA40_PATTERN = re.compile(r"[0-9a-f]{40}")
_EXPECTED_SELECTED_ROWS = 694
_EXPECTED_STRATEGIES = 13
_AMBIGUITY_MICROS = Decimal("0.5")
_SOURCE_FIELDS = frozenset(
    {
        "strategy_id",
        "checkpoint_minutes",
        "selected_bucket",
        "side",
        "baseline_accept",
        "actual_q",
        "stressed_q_3c",
        "shares",
        "won",
        "number_of_legs",
        "source_decision_sha256",
        "forecast_row_sha256",
        "p_vol_side",
        "regime",
        "accepted",
        "turnover_5_shares",
        "pnl_5_shares",
    }
)
_CHILD_BY_PARENT_MODE = {
    (binding.parent_strategy_id, binding.regime): child_id
    for child_id, binding in V2_OVERLAY_BINDINGS.items()
}


def convert_overlay_rows(
    source_rows: Iterable[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    if type(source_rows) not in (list, tuple):
        raise ValueError("C4_V2_FIXTURE_SOURCE_SEQUENCE")
    converted: list[dict[str, object]] = []
    source_count = 0
    decision_mismatches = 0
    stress_mismatches = 0
    ambiguities = 0
    for index, source_row in enumerate(source_rows):
        source_count += 1
        if type(source_row) is not dict:
            raise ValueError(f"C4_V2_FIXTURE_SOURCE_ROW_TYPE:{index}")
        if not _SOURCE_FIELDS.issubset(source_row):
            raise ValueError(f"C4_V2_FIXTURE_SOURCE_SCHEMA:{index}")
        parent_id = _string(source_row["strategy_id"], "strategy_id", index)
        regime = _string(source_row["regime"], "regime", index)
        child_id = _CHILD_BY_PARENT_MODE.get((parent_id, regime))
        if child_id is None:
            continue

        checkpoint = _integer(
            source_row["checkpoint_minutes"], "checkpoint_minutes", index
        )
        shares = _integer(source_row["shares"], "shares", index)
        if shares != 5:
            raise ValueError(f"C4_V2_FIXTURE_SHARES:{index}")
        leg_count = _integer(source_row["number_of_legs"], "number_of_legs", index)
        if leg_count <= 0:
            raise ValueError(f"C4_V2_FIXTURE_LEG_COUNT:{index}")
        selected_text = _string(
            source_row["selected_bucket"], "selected_bucket", index
        )
        selected_parts = tuple(part.strip() for part in selected_text.split(" | "))
        if any(not part for part in selected_parts) or len(selected_parts) != leg_count:
            raise ValueError(f"C4_V2_FIXTURE_LEG_COUNT_MISMATCH:{index}")
        selected_hashes = [
            hashlib.sha256(part.encode("utf-8")).hexdigest()
            for part in selected_parts
        ]
        if len(set(selected_hashes)) != len(selected_hashes):
            raise ValueError(f"C4_V2_FIXTURE_DUPLICATE_LEG:{index}")

        side = _string(source_row["side"], "side", index)
        binding = V2_OVERLAY_BINDINGS[child_id]
        if side != binding.side:
            raise ValueError(f"C4_V2_FIXTURE_SIDE_MISMATCH:{index}")
        baseline_accept = _boolean(
            source_row["baseline_accept"], "baseline_accept", index
        )
        expected_accept = _boolean(source_row["accepted"], "accepted", index)
        won = _boolean(source_row["won"], "won", index)
        source_decision_sha256 = _sha256(
            source_row["source_decision_sha256"], "source_decision_sha256", index
        )
        forecast_row_sha256 = _sha256(
            source_row["forecast_row_sha256"], "forecast_row_sha256", index
        )

        actual_q = _finite_float(source_row["actual_q"], "actual_q", index)
        stressed_q = _finite_float(
            source_row["stressed_q_3c"], "stressed_q_3c", index
        )
        p_vol_side = _finite_float(
            source_row["p_vol_side"], "p_vol_side", index
        )
        turnover = _finite_float(
            source_row["turnover_5_shares"], "turnover_5_shares", index
        )
        pnl = _finite_float(source_row["pnl_5_shares"], "pnl_5_shares", index)
        actual_micros = _micros(actual_q)
        stressed_micros = _micros(stressed_q)
        p_vol_micros = _micros(p_vol_side)
        turnover_micros = _micros_unbounded(turnover)
        pnl_micros = _micros_unbounded(pnl)
        recomputed_stress = min(1_000_000, actual_micros + 30_000 * leg_count)
        if stressed_micros != recomputed_stress:
            stress_mismatches += 1
            raise ValueError(f"C4_V2_FIXTURE_STRESS_MISMATCH:{index}")

        threshold_micros = 20_000 if regime == "VOL_CONFIRMATION" else 0
        raw_edge_micros = (
            Decimal(str(p_vol_side)) - Decimal(str(stressed_q))
        ) * Decimal(1_000_000)
        threshold_distance = abs(raw_edge_micros - Decimal(threshold_micros))
        if Decimal(0) < threshold_distance <= _AMBIGUITY_MICROS:
            ambiguities += 1
            raise ValueError(f"C4_V2_FIXTURE_QUANTIZATION_AMBIGUITY:{index}")
        quantized_accept = baseline_accept and (
            p_vol_micros - recomputed_stress >= threshold_micros
        )
        if quantized_accept != expected_accept:
            decision_mismatches += 1
            raise ValueError(f"C4_V2_FIXTURE_SOURCE_DECISION_MISMATCH:{index}")
        recomputed_turnover_micros = (
            5 * recomputed_stress if expected_accept else 0
        )
        recomputed_pnl_micros = (
            5 * ((1_000_000 if won else 0) - recomputed_stress)
            if expected_accept
            else 0
        )
        if turnover_micros != recomputed_turnover_micros:
            raise ValueError(f"C4_V2_FIXTURE_TURNOVER_MISMATCH:{index}")
        if pnl_micros != recomputed_pnl_micros:
            raise ValueError(f"C4_V2_FIXTURE_PNL_MISMATCH:{index}")

        converted.append(
            {
                "actual_price_micros": actual_micros,
                "baseline_accept": baseline_accept,
                "decision_identity": hashlib.sha256(
                    _canonical_json(
                        [child_id, source_decision_sha256, forecast_row_sha256]
                    ).encode("utf-8")
                ).hexdigest(),
                "expected_accept": expected_accept,
                "expected_pnl_micros": pnl_micros,
                "expected_stressed_q_micros": stressed_micros,
                "expected_turnover_micros": turnover_micros,
                "forecast_row_sha256": forecast_row_sha256,
                "horizon": f"T-{checkpoint}m",
                "overlay_strategy_id": child_id,
                "p_vol_side_micros": p_vol_micros,
                "parent_strategy_id": parent_id,
                "selected_buckets": selected_hashes,
                "side": side,
                "source_decision_sha256": source_decision_sha256,
                "won": won,
            }
        )

    audit = {
        "source_rows": source_count,
        "selected_rows": len(converted),
        "strategy_count": len(
            {str(row["overlay_strategy_id"]) for row in converted}
        ),
        "decision_mismatches": decision_mismatches,
        "stress_mismatches": stress_mismatches,
        "quantization_ambiguities": ambiguities,
    }
    return converted, audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the immutable C4 V2 parity fixture from audited Parquet."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--analysis-pack-sha256", required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    args = parser.parse_args(argv)

    _git_sha40(args.source_commit)
    for name in ("analysis_pack_sha256", "expected_input_sha256"):
        _sha256(getattr(args, name), name, -1)
    input_bytes = args.input.read_bytes()
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    if input_sha256 != args.expected_input_sha256:
        raise ValueError("C4_V2_FIXTURE_INPUT_HASH_MISMATCH")
    try:
        import pyarrow
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("C4_V2_FIXTURE_PYARROW_REQUIRED_FOR_BUILD_ONLY") from exc

    source_rows = parquet.read_table(args.input).to_pylist()
    fixture_rows, audit = convert_overlay_rows(source_rows)
    if (
        audit["selected_rows"] != _EXPECTED_SELECTED_ROWS
        or audit["strategy_count"] != _EXPECTED_STRATEGIES
        or audit["decision_mismatches"] != 0
        or audit["stress_mismatches"] != 0
        or audit["quantization_ambiguities"] != 0
    ):
        raise ValueError(f"C4_V2_FIXTURE_CANONICAL_COUNT_MISMATCH:{audit}")

    fixture_bytes = "".join(
        _canonical_json(row) + "\n" for row in fixture_rows
    ).encode("utf-8")
    converter_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    receipt = {
        "analysis_pack_sha256": args.analysis_pack_sha256,
        "converter_sha256": converter_sha256,
        "fixture_row_count": audit["selected_rows"],
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "input_parquet_sha256": input_sha256,
        "network_requests": 0,
        "pyarrow_version": pyarrow.__version__,
        "quantization_ambiguities": audit["quantization_ambiguities"],
        "quantization_policy": "Decimal(str(value))*1000000 ROUND_HALF_EVEN",
        "schema_version": "C4_V2_PARITY_FIXTURE_RECEIPT_V1",
        "source_commit": args.source_commit,
        "source_row_count": audit["source_rows"],
        "strategy_count": audit["strategy_count"],
        "trading_approval": False,
    }
    receipt_bytes = (
        json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(args.output, fixture_bytes)
    _atomic_write(args.receipt, receipt_bytes)
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


def _micros(value: float) -> int:
    micros = _micros_unbounded(value)
    if not 0 <= micros <= 1_000_000:
        raise ValueError("C4_V2_FIXTURE_PROBABILITY_RANGE")
    return micros


def _micros_unbounded(value: float) -> int:
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )


def _string(value: object, name: str, index: int) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"C4_V2_FIXTURE_STRING:{name}:{index}")
    return value


def _integer(value: object, name: str, index: int) -> int:
    if type(value) is not int:
        raise ValueError(f"C4_V2_FIXTURE_INTEGER:{name}:{index}")
    return value


def _boolean(value: object, name: str, index: int) -> bool:
    if type(value) is not bool:
        raise ValueError(f"C4_V2_FIXTURE_BOOLEAN:{name}:{index}")
    return value


def _finite_float(value: object, name: str, index: int) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError(f"C4_V2_FIXTURE_FLOAT:{name}:{index}")
    return value


def _sha256(value: object, name: str, index: int) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"C4_V2_FIXTURE_SHA256:{name}:{index}")
    return value


def _git_sha40(value: object) -> str:
    if type(value) is not str or _GIT_SHA40_PATTERN.fullmatch(value) is None:
        raise ValueError("C4_V2_FIXTURE_GIT_COMMIT")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
