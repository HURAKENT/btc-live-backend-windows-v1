from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


TAUS = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
HORIZONS = (30, 60, 120, 240, 360, 480, 720, 1080)
MODELS = {
    30: "volatility_scaled_empirical",
    60: "student_t",
    120: "historical_analog",
    240: "volatility_scaled_empirical",
    360: "volatility_scaled_empirical",
    480: "volatility_scaled_empirical",
    720: "volatility_scaled_empirical",
    1080: "unconditional_empirical",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def unique(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if not result or value != result[-1]:
            result.append(value)
    return result


def search_right(values: list[float], value: float) -> int:
    low, high = 0, len(values)
    while low < high:
        middle = (low + high) // 2
        if value < values[middle]:
            high = middle
        else:
            low = middle + 1
    return low


def interp(x: float, xp: list[float], fp: tuple[float, ...]) -> float:
    if x < xp[0]:
        return 0.0
    if x > xp[-1]:
        return 1.0
    right = search_right(xp, x)
    if right == 0:
        return fp[0]
    if right >= len(xp):
        return fp[-1]
    left = right - 1
    if xp[right] == xp[left]:
        return fp[left]
    weight = (x - xp[left]) / (xp[right] - xp[left])
    return fp[left] * (1.0 - weight) + fp[right] * weight


def contract_quantiles(contract: dict, row: dict) -> list[float]:
    horizon = int(row["horizon_minutes"])
    spec = contract["horizons"][str(horizon)]
    model = spec["selected_model"]
    scale = max(float(row["sigma_180m"]) * math.sqrt(horizon), 1e-8)
    if model == "unconditional_empirical":
        return list(spec["log_return_quantiles"])
    if model in {"volatility_scaled_empirical", "student_t"}:
        return [scale * value for value in spec["standardized_quantiles"]]
    edges = spec["vol_ratio_edges"]
    vb = max(0, min(search_right(edges, float(row["vol_ratio_30_180"])) - 1, len(edges) - 2))
    pos_edges = [0.0, 0.25, 0.5, 0.75, 1.000001]
    pb = max(0, min(search_right(pos_edges, float(row["position_inside_bucket"])) - 1, 3))
    standardized = spec["bin_quantiles"].get(f"{vb}:{pb}", spec["fallback_quantiles"])
    return [scale * value for value in standardized]


def main() -> None:
    user = Path.home()
    codex = user / "Documents" / "Codex"
    lab = codex / "btc_edge_search_lab_v1" / "recovery" / "btc_terminal_distribution_bias_lab_v1"
    matrix_path = codex / "btc_early_horizon_single_entry_v1_output" / "checkpoint_matrix_170x11x11.parquet"
    prediction_path = lab / "artifacts" / "terminal_model_oof_predictions.parquet"
    observation_paths = sorted((lab / "data" / "terminal_dataset" / "observations").rglob("*.parquet"))
    observation_columns = [
        "settlement_date", "horizon_minutes", "terminal_log_return", "sigma_180m",
        "vol_ratio_30_180", "position_inside_bucket", "spot",
    ]
    observations = pa.concat_tables([
        pq.read_table(path, columns=observation_columns) for path in observation_paths
    ]).to_pylist()
    train = [row for row in observations if str(row["settlement_date"]) < "2025-12-29"]
    prediction_columns = [
        "settlement_date", "outer_year", "horizon_minutes", "spot",
        "projected_sigma_usd", *[f"q{int(tau * 100):02d}_terminal_price" for tau in TAUS],
    ]
    predictions = pq.read_table(prediction_path, columns=prediction_columns).to_pylist()
    contract = {
        "schema_version": "TERMINAL_DISTRIBUTION_RECOVERY_V1",
        "training_cutoff_exclusive": "2025-12-29",
        "quantiles": list(TAUS),
        "source_sha256": {
            "checkpoint_matrix_170": sha256(matrix_path),
            "terminal_model_oof_predictions": sha256(prediction_path),
            "training_observations": {path.name + ":" + path.parent.name: sha256(path) for path in observation_paths},
        },
        "horizons": {},
    }
    for horizon in HORIZONS:
        rows = [row for row in train if int(row["horizon_minutes"]) == horizon]
        model = MODELS[horizon]
        spec = {"selected_model": model, "training_rows": len(rows)}
        y = [float(row["terminal_log_return"]) for row in rows]
        scale = [max(float(row["sigma_180m"]) * math.sqrt(horizon), 1e-8) for row in rows]
        standardized = [value / divisor for value, divisor in zip(y, scale)]
        if model == "unconditional_empirical":
            spec["log_return_quantiles"] = [quantile(y, tau) for tau in TAUS]
        elif model == "volatility_scaled_empirical":
            spec["standardized_quantiles"] = [quantile(standardized, tau) for tau in TAUS]
        elif model == "student_t":
            row = next(
                item for item in predictions
                if int(item["outer_year"]) == 2026 and int(item["horizon_minutes"]) == horizon
            )
            row_scale = float(row["projected_sigma_usd"]) / float(row["spot"])
            spec["standardized_quantiles"] = [
                math.log(float(row[f"q{int(tau * 100):02d}_terminal_price"]) / float(row["spot"])) / row_scale
                for tau in TAUS
            ]
        else:
            ratios = [float(row["vol_ratio_30_180"]) for row in rows]
            edges = unique([quantile(ratios, tau) for tau in (0.0, 0.33, 0.67, 1.0)])
            spec["vol_ratio_edges"] = edges
            spec["fallback_quantiles"] = [quantile(standardized, tau) for tau in TAUS]
            bins: dict[str, list[float]] = {}
            for row, value in zip(rows, standardized):
                vb = max(0, min(search_right(edges, float(row["vol_ratio_30_180"])) - 1, len(edges) - 2))
                pb = max(0, min(search_right([0.0, 0.25, 0.5, 0.75, 1.000001], float(row["position_inside_bucket"])) - 1, 3))
                bins.setdefault(f"{vb}:{pb}", []).append(value)
            spec["bin_quantiles"] = {
                key: [quantile(values, tau) for tau in TAUS]
                for key, values in bins.items() if len(values) >= 40
            }
            spec["bin_counts"] = {key: len(values) for key, values in bins.items()}
        contract["horizons"][str(horizon)] = spec

    matrix_rows = pq.read_table(matrix_path).to_pylist()
    expected: dict[tuple[str, int], list[dict]] = {}
    for row in matrix_rows:
        expected.setdefault((str(row["market_date"]), int(row["horizon_minutes"])), []).append(row)
    feature_rows = {
        (str(row["settlement_date"]), int(row["horizon_minutes"])): row
        for row in observations
        if str(row["settlement_date"]).startswith("2026-")
    }
    differences = []
    for key, buckets in expected.items():
        if key[1] not in HORIZONS:
            continue
        row = feature_rows[key]
        log_quantiles = contract_quantiles(contract, row)
        prices = [float(row["spot"]) * math.exp(value) for value in log_quantiles]
        unique_prices = unique(sorted(prices))
        unique_taus = tuple(TAUS[prices.index(value)] for value in unique_prices)
        ordered = sorted(buckets, key=lambda value: int(value["bucket_index"]))
        probabilities = []
        for bucket in ordered:
            lower = bucket.get("lower_bound")
            upper = bucket.get("upper_bound")
            if lower is None and upper is None:
                title = str(bucket["bucket_title"]).replace("$", "").replace(",", "").replace(" ", "")
                if title.startswith("<"):
                    lower, upper = None, float(title.lstrip("<="))
                elif title.startswith(">"):
                    lower, upper = float(title.lstrip(">=")), None
                elif title.endswith("+"):
                    lower, upper = float(title[:-1]), None
                else:
                    parts = title.replace("–", "-").split("-")
                    lower, upper = float(parts[0]), float(parts[1])
            low = 0.0 if lower is None else interp(float(lower), unique_prices, unique_taus)
            high = 1.0 if upper is None else interp(float(upper), unique_prices, unique_taus)
            probabilities.append(max(0.0, high - low))
        total = max(sum(probabilities), 1e-12)
        probabilities = [value / total for value in probabilities]
        for bucket, actual in zip(ordered, probabilities):
            difference = abs(float(bucket["model_p"]) - actual)
            if difference > 1e-12:
                differences.append((key, int(bucket["bucket_index"]), float(bucket["model_p"]), actual, difference))
    print(json.dumps({
        "contract": contract,
        "parity": {
            "comparisons": sum(1 for key in expected if key[1] in HORIZONS) * 11,
            "difference_count": len(differences),
            "max_difference": max((row[-1] for row in differences), default=0.0),
            "first_differences": differences[:20],
        },
    }, sort_keys=True))


if __name__ == "__main__":
    main()
