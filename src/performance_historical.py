from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.historical_input_builder import HistoricalInputBuilder
from src.historical_revalidation import _new_parent_result, default_artifacts
from src.performance_models import (
    PerformanceObservation,
    canonical_identity,
    canonical_sha256,
    v1_performance_price,
    v2_performance_price,
    PerformanceResolution,
)
from src.strategy_dispatch import load_strategy_dispatcher


PINNED_AHR_RUN_ID = "20260814T205244105513Z"
PINNED_AHR_ACCEPTANCE_SHA256 = "20fa45912f1a892eb13c40deb5c5e8c3508fc642bf72db7f90c6676cebec1d0c"
PINNED_AHR_RESULTS_SHA256 = "26fad48c7d705bc4954fe65dfaefa7b3b87e1af77a7e94dfffe79c383aa38d84"
PINNED_SETTLEMENTS_SHA256 = "ea9ed11c1aa7a975c499bcd27332fdbfa0dd927cf2ef4323e89b372516c4e8e1"
PINNED_AHR_MANIFEST_SHA256 = "aa7d24fdcfe91fdcc9418d3cd83608fc5c8722efd7c678677ae45087c0e8759f"
PINNED_AHR_MANIFEST_SEMANTIC_SHA256 = "3591523173ccdf820623c2e11d05e3e9981b33bda9d790f1d1b969a47291e11a"


@dataclass(frozen=True, slots=True)
class AhrHistoricalBundle:
    run_id: str
    run_dir: Path
    acceptance_sha256: str
    result_artifact_sha256: str
    input_manifest_sha256: str
    settlement_artifact_sha256: str
    source_sha256: Mapping[str, str]
    results: tuple[Mapping[str, Any], ...]


class PinnedAhrArtifactLoader:
    """Fail-closed loader for the one accepted historical engineering baseline."""

    def __init__(self, *, project_root: Path, run_dir: Path | None = None,
                 pyarrow_path: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.run_dir = Path(run_dir).resolve() if run_dir is not None else None
        self.pyarrow_path = pyarrow_path

    def load(self) -> AhrHistoricalBundle:
        run_dir = self._resolve_run_dir()
        acceptance_path = run_dir / "ACCEPTANCE.json"
        acceptance_hash = _required_hash(acceptance_path)
        if acceptance_hash != PINNED_AHR_ACCEPTANCE_SHA256:
            raise ValueError("PERFORMANCE_AHR_ACCEPTANCE_HASH_MISMATCH")
        acceptance = _read_object(acceptance_path)
        if any((
            acceptance.get("run_id") != PINNED_AHR_RUN_ID,
            acceptance.get("status") != "PASS",
            acceptance.get("semantic_parity") != "PASS",
            acceptance.get("exit_code") != 0,
            acceptance.get("network_requests") != 0,
            acceptance.get("trading_approval") is not False,
            acceptance.get("real_orders") is not False,
            acceptance.get("wallet") is not False,
            acceptance.get("signing") is not False,
        )):
            raise ValueError("PERFORMANCE_AHR_ACCEPTANCE_NOT_CANONICAL_PASS")

        manifest_path = run_dir / "INPUT_MANIFEST.json"
        if _required_hash(manifest_path) != PINNED_AHR_MANIFEST_SHA256:
            raise ValueError("PERFORMANCE_AHR_INPUT_MANIFEST_FILE_HASH_MISMATCH")
        manifest = _read_object(manifest_path)
        declared_manifest_hash = manifest.get("input_manifest_sha256")
        unhashed_manifest = dict(manifest)
        unhashed_manifest.pop("input_manifest_sha256", None)
        actual_manifest_hash = canonical_sha256(unhashed_manifest)
        if (declared_manifest_hash != actual_manifest_hash
                or actual_manifest_hash != PINNED_AHR_MANIFEST_SEMANTIC_SHA256):
            raise ValueError("PERFORMANCE_AHR_INPUT_MANIFEST_HASH_MISMATCH")
        if (manifest.get("run_id") != PINNED_AHR_RUN_ID
                or manifest.get("schema_version") != "AHR_1_INPUT_MANIFEST_V1"):
            raise ValueError("PERFORMANCE_AHR_INPUT_MANIFEST_INVALID")

        results_path = run_dir / "STRATEGY_RESULTS.parquet"
        results_hash = _required_hash(results_path)
        if results_hash != PINNED_AHR_RESULTS_SHA256:
            raise ValueError("PERFORMANCE_AHR_RESULTS_HASH_MISMATCH")

        input_builder = HistoricalInputBuilder(
            project_root=self.project_root,
            artifacts=default_artifacts(),
            pyarrow_path=self.pyarrow_path,
        )
        observed_sources = input_builder.verify_sources()
        if manifest.get("source_sha256") != observed_sources:
            raise ValueError("PERFORMANCE_AHR_SOURCE_MANIFEST_MISMATCH")
        if observed_sources.get("settlements_170") != PINNED_SETTLEMENTS_SHA256:
            raise ValueError("PERFORMANCE_AHR_SETTLEMENT_HASH_MISMATCH")

        results = _read_results(results_path, pyarrow_path=self.pyarrow_path)
        counts = {
            "records": len(results),
            "accepted": sum(bool(row["accepted"]) for row in results),
            "rejected": sum(not bool(row["accepted"]) for row in results),
            "strategies": len({str(row["strategy_id"]) for row in results}),
        }
        if counts != {"records": 4_994, "accepted": 1_850,
                      "rejected": 3_144, "strategies": 47}:
            raise ValueError(f"PERFORMANCE_AHR_RESULT_COUNT_MISMATCH:{counts}")
        return AhrHistoricalBundle(
            run_id=PINNED_AHR_RUN_ID,
            run_dir=run_dir,
            acceptance_sha256=acceptance_hash,
            result_artifact_sha256=results_hash,
            input_manifest_sha256=actual_manifest_hash,
            settlement_artifact_sha256=PINNED_SETTLEMENTS_SHA256,
            source_sha256=observed_sources,
            results=results,
        )

    def _resolve_run_dir(self) -> Path:
        if self.run_dir is not None:
            if self.run_dir.name != PINNED_AHR_RUN_ID:
                raise ValueError("PERFORMANCE_AHR_RUN_ID_MISMATCH")
            return self.run_dir
        return (_canonical_checkout_root(self.project_root) / "reports"
                / "historical_revalidation" / PINNED_AHR_RUN_ID)


class HistoricalPerformanceObservationBuilder:
    def __init__(self, *, project_root: Path, bundle: AhrHistoricalBundle) -> None:
        self.project_root = Path(project_root).resolve()
        self.bundle = bundle
        self.dispatcher = load_strategy_dispatcher(self.project_root)
        self.input_builder = HistoricalInputBuilder(
            project_root=self.project_root, artifacts=default_artifacts()
        )
        market_rows = _read_parquet_rows(
            Path(default_artifacts().markets_170), pyarrow_path=None
        )
        event_ids_by_date: dict[str, set[str]] = {}
        for market_row in market_rows:
            event_ids_by_date.setdefault(str(market_row["market_date"]), set()).add(
                str(market_row["event_id"])
            )
        if len(event_ids_by_date) != 170 or any(
            len(event_ids) != 1 for event_ids in event_ids_by_date.values()
        ):
            raise ValueError("PERFORMANCE_HISTORICAL_MARKET_IDENTITY_AMBIGUOUS")
        self.market_id_by_date = {
            market_date: next(iter(event_ids))
            for market_date, event_ids in event_ids_by_date.items()
        }
        self._v1_input_hashes: dict[tuple[str, str], str] = {}
        status_payload = _read_object(
            self.project_root / "reports" / "STRATEGY_47_STATUS_MATRIX.json"
        )
        self.status_by_id = {
            row["strategy_id"]: row for row in status_payload.get("strategies", [])
        }
        if set(self.status_by_id) != {
            binding.strategy_id for binding in self.dispatcher.bindings
        }:
            raise ValueError("PERFORMANCE_STATUS_MATRIX_IDENTITY_MISMATCH")

    def build(self) -> tuple[PerformanceObservation, ...]:
        v1_by_coordinates = {
            (str(row["strategy_id"]), str(row["market_date"]),
             int(row["checkpoint_minutes"])): row
            for row in self.bundle.results if row["version"] == "V1"
        }
        observations = tuple(
            self._build_one(row, v1_by_coordinates=v1_by_coordinates)
            for row in self.bundle.results
        )
        if len({row.observation_key for row in observations}) != len(observations):
            raise ValueError("PERFORMANCE_HISTORICAL_OBSERVATION_KEY_COLLISION")
        return observations

    def _build_one(self, row: Mapping[str, Any], *,
                   v1_by_coordinates: Mapping[tuple[str, str, int], Mapping[str, Any]]) -> PerformanceObservation:
        strategy_id = str(row["strategy_id"])
        market_date = str(row["market_date"])
        checkpoint = int(row["checkpoint_minutes"])
        binding = self.dispatcher.binding(strategy_id)
        status = self.status_by_id[strategy_id]
        if binding.version != row["version"] or binding.registry_index != status["registry_index"]:
            raise ValueError("PERFORMANCE_HISTORICAL_REGISTRY_DRIFT")

        accepted = bool(row["accepted"])
        parent_strategy_id: str | None = None
        source_decision_identity: str | None = None
        reference_price: int | None = None
        performance_price: int | None = None
        price_basis = "NOT_APPLICABLE"
        if binding.version == "V1":
            input_key = (strategy_id, market_date)
            if input_key not in self._v1_input_hashes:
                self._v1_input_hashes[input_key] = self.input_builder.build_v1_unit(
                    strategy_id=strategy_id, market_date=market_date
                ).input_sha256
            if row["input_sha256"] != self._v1_input_hashes[input_key]:
                raise ValueError("PERFORMANCE_HISTORICAL_INPUT_HASH_MISMATCH")
            selected = self._v1_selected_buckets(row)
            side = str(row["side"])
            horizon = f"T-{checkpoint}m"
            reason = str(row["reason"])
            if accepted:
                reference_price = _optional_int(row.get("market_probability_micros"))
                performance_price, price_basis = v1_performance_price(
                    stressed_reference_cost_micros=_optional_int(row.get("stressed_reference_cost_micros")),
                    market_probability_micros=reference_price,
                    selected_leg_count=len(selected),
                )
        else:
            selected = tuple(str(item) for item in row["selected_buckets"])
            parent_strategy_id = str(row["parent_strategy_id"])
            if parent_strategy_id != binding.parent_strategy_id:
                raise ValueError("PERFORMANCE_HISTORICAL_PARENT_BINDING_MISMATCH")
            parent = v1_by_coordinates.get((parent_strategy_id, market_date, checkpoint))
            if parent is None:
                raise ValueError("PERFORMANCE_HISTORICAL_PARENT_RESULT_MISSING")
            parent_result = _new_parent_result(self.input_builder, parent)
            if tuple(row["selected_buckets"]) != parent_result.selected_buckets:
                raise ValueError("PERFORMANCE_HISTORICAL_PARENT_BUCKET_MISMATCH")
            if row["source_decision_sha256"] != parent_result.source_decision_sha256:
                raise ValueError("PERFORMANCE_HISTORICAL_PARENT_PROVENANCE_MISMATCH")
            if parent_result.side != binding.schedule["side"]:
                raise ValueError("PERFORMANCE_HISTORICAL_PARENT_SIDE_MISMATCH")
            unit = self.input_builder.build_v2_unit(
                strategy_id=strategy_id, market_date=market_date,
                parent_result=parent_result,
            )
            if row["input_sha256"] != unit.input_sha256:
                raise ValueError("PERFORMANCE_HISTORICAL_INPUT_HASH_MISMATCH")
            source_decision_identity = parent_result.decision_identity
            side = parent_result.side
            horizon = str(row["horizon"])
            reason = str(row["reason_code"])
            if accepted:
                reference_price = _optional_int(row.get("actual_price_micros"))
                performance_price, price_basis = v2_performance_price(
                    stressed_q_3c_micros=int(row["stressed_q_3c_micros"]),
                    actual_price_micros=reference_price,
                )

        result_hash = str(row["result_sha256"])
        market_id = self.market_id_by_date.get(market_date)
        if market_id is None:
            raise ValueError("PERFORMANCE_HISTORICAL_MARKET_IDENTITY_MISSING")
        logical_coordinates = {
            "checkpoint_minutes": checkpoint,
            "horizon": horizon,
            "market_date": market_date,
            "market_id": market_id,
            "parent_strategy_id": parent_strategy_id,
            "side": side,
            "source_decision_identity": source_decision_identity,
            "strategy_id": strategy_id,
            "strategy_version": binding.version,
        }
        return PerformanceObservation.create(
            observation_key=canonical_identity(
                "performance-observation-historical",
                {"checkpoint_minutes": checkpoint,
                 "market_date": market_date, "market_id": market_id,
                 "mode": "BASELINE_170_EXACT_REPLAY",
                 "strategy_id": strategy_id, "unit_key": row["unit_key"]},
            ),
            logical_decision_key=canonical_identity(
                "performance-logical-decision", logical_coordinates,
            ),
            source_layer="HISTORICAL", strategy_id=strategy_id,
            strategy_version=binding.version, family=_family_label(binding),
            registry_index=binding.registry_index,
            activation_status=str(status["activation_status"]),
            activation_reason_code=str(status["activation_reason_code"]),
            market_id=market_id, market_date=market_date,
            evaluation_key=None, signal_identity_key=None,
            parent_strategy_id=parent_strategy_id,
            source_decision_identity=source_decision_identity,
            checkpoint_minutes=checkpoint, horizon=horizon, side=side,
            selected_buckets=selected, accepted=accepted, emitted=False,
            reason_code=reason, reference_price_micros=reference_price,
            performance_price_micros=performance_price,
            performance_price_basis=price_basis,
            scoring_status="RESOLUTION_PENDING" if accepted else "REJECTED",
            scoring_reason_code="SETTLEMENT_PENDING" if accepted else "EVALUATOR_REJECTED",
            observed_at_ms=_date_epoch_ms(market_date), source_created_at_ms=None,
            provenance_run_id=self.bundle.run_id,
            source_result_sha256=result_hash, input_sha256=str(row["input_sha256"]),
        )

    def _v1_selected_buckets(self, row: Mapping[str, Any]) -> tuple[str, ...]:
        source_rows = self.input_builder._bucket_source_rows(
            str(row["strategy_id"]), str(row["market_date"]),
            int(row["checkpoint_minutes"]),
        )
        try:
            return tuple(
                hashlib.sha256(str(source_rows[int(index)]["bucket_title"]).encode("utf-8")).hexdigest()
                for index in row["selected_bucket_indices"]
            )
        except (IndexError, KeyError, TypeError, ValueError):
            raise ValueError("PERFORMANCE_HISTORICAL_BUCKET_MAPPING_INVALID") from None


class HistoricalSettlementReconciler:
    """Scores accepted historical observations from the hash-pinned settlement artifact."""

    def __init__(self, *, project_root: Path, bundle: AhrHistoricalBundle,
                 pyarrow_path: Path | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.bundle = bundle
        self.pyarrow_path = pyarrow_path

    def reconcile(self, observations: tuple[PerformanceObservation, ...]) -> tuple[PerformanceResolution, ...]:
        path = Path(default_artifacts().settlements_170)
        if _required_hash(path) != self.bundle.settlement_artifact_sha256:
            raise ValueError("PERFORMANCE_AHR_SETTLEMENT_HASH_MISMATCH")
        rows = _read_parquet_rows(path, pyarrow_path=self.pyarrow_path)
        if len(rows) != 170 or len({str(row["market_date"]) for row in rows}) != 170:
            raise ValueError("PERFORMANCE_AHR_SETTLEMENT_COVERAGE_MISMATCH")
        settlements: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for row in rows:
            independent = row.get("independent_winner_bucket")
            if independent is not None and (
                row.get("independent_match") is not True
                or independent != row.get("derived_winner_bucket")
            ):
                raise ValueError("PERFORMANCE_AHR_SETTLEMENT_CONFLICT")
            title = str(row["derived_winner_bucket"])
            settlements[str(row["market_date"])] = (
                hashlib.sha256(title.encode("utf-8")).hexdigest(), row
            )

        resolutions: list[PerformanceResolution] = []
        for observation in observations:
            if not observation.accepted:
                continue
            settlement = settlements.get(observation.market_date)
            if settlement is None:
                raise ValueError("PERFORMANCE_AHR_SETTLEMENT_MISSING")
            winning_bucket, settlement_row = settlement
            selected_won = winning_bucket in observation.selected_buckets
            won = selected_won if observation.side == "YES" else not selected_won
            settlement_identity = canonical_identity(
                "historical-settlement",
                {"artifact_sha256": self.bundle.settlement_artifact_sha256,
                 "market_date": observation.market_date,
                 "winning_bucket_identity": winning_bucket},
            )
            resolutions.append(PerformanceResolution.create_for_observation(
                observation,
                resolution_key=canonical_identity(
                    "performance-resolution-historical",
                    {"observation_key": observation.observation_key,
                     "settlement_identity": settlement_identity, "revision": 1},
                ),
                revision=1, supersedes_resolution_key=None,
                settlement_identity=settlement_identity,
                settlement_source_event_id=None,
                winning_bucket_identity=winning_bucket,
                won=won, resolution_date=observation.market_date,
                resolved_at_ms=None,
                provenance_json={
                    "artifact_sha256": self.bundle.settlement_artifact_sha256,
                    "dataset_version": settlement_row["dataset_version"],
                    "event_end_utc": settlement_row["event_end_utc"],
                    "source_hash": settlement_row["source_hash"],
                },
            ))
        if len(resolutions) != sum(row.accepted for row in observations):
            raise ValueError("PERFORMANCE_AHR_RESOLUTION_COUNT_MISMATCH")
        return tuple(resolutions)


def _read_results(path: Path, *, pyarrow_path: Path | None) -> tuple[Mapping[str, Any], ...]:
    if pyarrow_path is not None:
        import sys
        resolved = str(Path(pyarrow_path).resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("PERFORMANCE_AHR_PYARROW_REQUIRED") from exc
    table = parquet.read_table(path)
    expected_columns = {"unit_key", "strategy_id", "market_date", "version",
                        "checkpoint_minutes", "record_json"}
    if set(table.column_names) != expected_columns:
        raise ValueError("PERFORMANCE_AHR_RESULT_SCHEMA_MISMATCH")
    results: list[Mapping[str, Any]] = []
    for physical in table.to_pylist():
        row = json.loads(physical["record_json"])
        if type(row) is not dict:
            raise ValueError("PERFORMANCE_AHR_RESULT_RECORD_INVALID")
        for name in ("unit_key", "strategy_id", "market_date", "version", "checkpoint_minutes"):
            if physical[name] != row.get(name):
                raise ValueError("PERFORMANCE_AHR_RESULT_PHYSICAL_MISMATCH")
        declared = row.get("result_sha256")
        unhashed = dict(row)
        unhashed.pop("result_sha256", None)
        if declared != canonical_sha256(unhashed):
            raise ValueError("PERFORMANCE_AHR_RESULT_ROW_HASH_MISMATCH")
        results.append(row)
    return tuple(results)


def _read_parquet_rows(path: Path, *, pyarrow_path: Path | None) -> tuple[Mapping[str, Any], ...]:
    if pyarrow_path is not None:
        import sys
        resolved = str(Path(pyarrow_path).resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("PERFORMANCE_AHR_PYARROW_REQUIRED") from exc
    return tuple(parquet.read_table(path).to_pylist())


def _canonical_checkout_root(project_root: Path) -> Path:
    root = Path(project_root).resolve()
    return root.parent.parent if root.parent.name == ".worktrees" else root


def _required_hash(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"PERFORMANCE_AHR_ARTIFACT_MISSING:{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"PERFORMANCE_AHR_ARTIFACT_MISSING:{path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"PERFORMANCE_AHR_JSON_INVALID:{path.name}") from None
    if type(value) is not dict:
        raise ValueError(f"PERFORMANCE_AHR_JSON_INVALID:{path.name}")
    return value


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _family_label(binding: object) -> str:
    version = getattr(binding, "version")
    evaluator_key = getattr(binding, "evaluator_key")
    if version == "V2":
        return "VOLATILITY_V2"
    if evaluator_key == "STRICT_A_COMPOSED_V1":
        return "STRICT_A"
    if evaluator_key == "PF1_COMPOSED_V1":
        return "PF1"
    return "HISTORICAL_V1"


def _date_epoch_ms(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)
