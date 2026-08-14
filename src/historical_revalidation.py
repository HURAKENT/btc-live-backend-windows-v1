from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from src.historical_input_builder import (
    HistoricalArtifactPaths,
    HistoricalEvaluationUnit,
    HistoricalInputBuilder,
    NewHistoricalParentResult,
)
from src.strategy_dispatch import StrategyDispatcher, load_strategy_dispatcher
from src.strategy_v1 import V1Evaluation
from src.strategy_v1_parity import (
    historical_identity_bindings,
)
from src.historical_opportunity import HistoricalOpportunityMap
from src.strategy_v2 import V2OverlayEvaluation


EXIT_PASS = 0
EXIT_SOURCE_INTEGRITY = 10
EXIT_INPUT_CONSTRUCTION = 20
EXIT_DISPATCH = 30
EXIT_PARITY = 40
EXIT_SAFETY = 50
EXIT_INTERNAL = 60

_MODE = "BASELINE_170_EXACT_REPLAY"
_SMOKE_V1_IDS = (
    "NO_FADE_P1_U1_T18",
    "YES_STRICT_A_T60",
    "YES_PF1_T60",
)
_SMOKE_V2_IDS = ("YES_STRICT_A_T60_V2_VOL",)
_NONSEMANTIC_FIELDS = frozenset(
    {"run_id", "generated_at", "report_path", "generated_at_utc"}
)


@dataclass(frozen=True, slots=True)
class AhrRunConfig:
    project_root: Path
    run_root: Path
    artifacts: HistoricalArtifactPaths
    pyarrow_path: Path | None = None
    smoke_dates: tuple[str, ...] = ()
    smoke: bool = False


@dataclass(frozen=True, slots=True)
class ExpectedV1Component:
    universe: str
    semantic: Mapping[str, Any]


@dataclass(slots=True)
class AhrRunProgress:
    identities_discovered: int = 0
    identity_ids_dispatched: set[str] = field(default_factory=set)
    identity_ids_completed: set[str] = field(default_factory=set)
    dates_processed: set[str] = field(default_factory=set)
    families_touched: set[str] = field(default_factory=set)
    units_dispatched: int = 0
    units_completed: int = 0


def _apply_progress_to_acceptance(
    acceptance: dict[str, Any], progress: AhrRunProgress
) -> None:
    acceptance.update(
        {
            "strategies_discovered": progress.identities_discovered,
            "strategies_dispatched": len(progress.identity_ids_dispatched),
            "strategies_completed": len(progress.identity_ids_completed),
            "units_dispatched": progress.units_dispatched,
            "units_completed": progress.units_completed,
            "dates_processed": len(progress.dates_processed),
            "strategy_families": len(progress.families_touched),
        }
    )


class AhrError(RuntimeError):
    exit_code = EXIT_INTERNAL
    gate = "INTERNAL"


class AhrParityError(AhrError):
    exit_code = EXIT_PARITY
    gate = "PARITY"


class AhrSafetyError(AhrError):
    exit_code = EXIT_SAFETY
    gate = "SAFETY"


def historical_bindings(dispatcher: StrategyDispatcher):
    bindings = dispatcher.bindings
    if (
        len(bindings) != 47
        or sum(row.version == "V1" for row in bindings) != 34
        or sum(row.version == "V2" for row in bindings) != 13
    ):
        raise ValueError("AHR_REGISTRY_BINDING_DRIFT")
    return bindings


def evaluate_persist_compare(
    *,
    evaluate: Callable[[], Any],
    persist: Callable[[Any], None],
    load_expected: Callable[[], Any],
    compare: Callable[[Any, Any], None],
) -> Any:
    result = evaluate()
    persist(result)
    expected = load_expected()
    compare(result, expected)
    return result


def _build_v2_contract_unit(
    *,
    builder: HistoricalInputBuilder,
    strategy_id: str,
    market_date: str,
    parent_result: NewHistoricalParentResult,
) -> HistoricalEvaluationUnit | None:
    if not builder.is_v2_contract_opportunity(
        strategy_id=strategy_id,
        market_date=market_date,
        parent_result=parent_result,
    ):
        return None
    return builder.build_v2_unit(
        strategy_id=strategy_id,
        market_date=market_date,
        parent_result=parent_result,
    )


def compare_semantic(*, actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    keys = (set(actual) | set(expected)) - _NONSEMANTIC_FIELDS
    mismatches = {
        key: {"actual": actual.get(key), "expected": expected.get(key)}
        for key in sorted(keys)
        if actual.get(key) != expected.get(key)
    }
    if mismatches:
        raise AhrParityError(
            "AHR_SEMANTIC_PARITY_MISMATCH:"
            + json.dumps(mismatches, sort_keys=True, separators=(",", ":"))
        )


@contextlib.contextmanager
def offline_network_guard() -> Iterator[None]:
    original_create = socket.create_connection
    original_connect = socket.socket.connect

    def blocked(*args: Any, **kwargs: Any):
        raise AhrSafetyError("AHR_NETWORK_REQUEST_BLOCKED")

    socket.create_connection = blocked
    socket.socket.connect = blocked
    try:
        yield
    finally:
        socket.create_connection = original_create
        socket.socket.connect = original_connect


def snapshot_files(paths: tuple[Path, ...]) -> dict[str, str | None]:
    return {
        str(path.resolve(strict=False)): (
            _file_sha256(path) if path.is_file() else None
        )
        for path in paths
    }


def assert_files_unchanged(snapshot: Mapping[str, str | None]) -> None:
    for path_text, expected in snapshot.items():
        path = Path(path_text)
        actual = _file_sha256(path) if path.is_file() else None
        if actual != expected:
            raise AhrSafetyError(f"AHR_PRODUCTION_FILE_MODIFIED:{path}")


def validate_resume_manifest(
    *, state: Mapping[str, Any], current_manifest_sha256: str
) -> None:
    if state.get("input_manifest_sha256") != current_manifest_sha256:
        raise ValueError("AHR_RESUME_MANIFEST_MISMATCH")


def run_baseline(
    config: AhrRunConfig, *, resume_run_id: str | None = None
) -> int:
    project_root = Path(config.project_root).resolve()
    run_root = Path(config.run_root).resolve()
    run_id = resume_run_id or _new_run_id()
    run_dir = run_root / run_id
    if resume_run_id is None and run_dir.exists():
        run_id = _new_run_id(suffix=True)
        run_dir = run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=resume_run_id is not None)
    paths = _run_paths(run_dir)
    _initialize_artifacts(paths, run_id)
    smoke_mode = config.smoke or bool(config.smoke_dates)
    acceptance = _base_acceptance(run_id, smoke=smoke_mode)
    stage = "SOURCE_INTEGRITY"
    failed_strategy_id: str | None = None
    failed_market_date: str | None = None
    production_snapshot: dict[str, str | None] = {}
    results: list[dict[str, Any]] = []
    completed: set[str] = set()
    progress = AhrRunProgress()
    input_manifest_sha256 = ""
    try:
        dispatcher = load_strategy_dispatcher(project_root)
        bindings = historical_bindings(dispatcher)
        _verify_c4_receipt(project_root, bindings)
        builder = HistoricalInputBuilder(
            project_root=project_root,
            artifacts=config.artifacts,
            pyarrow_path=config.pyarrow_path,
        )
        source_hashes = builder.verify_sources()
        all_dates = builder.market_dates()
        if len(all_dates) != 170:
            raise ValueError(f"AHR_DATE_COUNT_MISMATCH:{len(all_dates)}")
        opportunities = builder.opportunity_map()
        opportunity_counts = opportunities.validate_canonical_counts()
        dates = (
            config.smoke_dates
            or (_select_smoke_dates(builder) if config.smoke else all_dates)
        )
        if any(date not in all_dates for date in dates) or len(set(dates)) != len(dates):
            raise ValueError("AHR_SMOKE_DATE_SET_INVALID")
        selected_bindings = _selected_bindings(bindings, smoke=smoke_mode)
        progress.identities_discovered = len(selected_bindings)
        manifest = {
            "schema_version": "AHR_1_INPUT_MANIFEST_V1",
            "mode": _MODE,
            "run_id": run_id,
            "source_sha256": dict(sorted(source_hashes.items())),
            "market_dates": list(dates),
            "strategy_ids": [row.strategy_id for row in selected_bindings],
            "opportunity_counts": opportunity_counts,
        }
        input_manifest_sha256 = _payload_sha256(manifest)
        manifest["input_manifest_sha256"] = input_manifest_sha256
        _write_json_atomic(paths["manifest"], manifest)

        if resume_run_id is not None:
            state = _read_json(paths["state"])
            validate_resume_manifest(
                state=state,
                current_manifest_sha256=input_manifest_sha256,
            )
            completed = set(str(item) for item in state.get("completed_units", []))
            raw_results = state.get("results", [])
            if type(raw_results) is not list or any(type(row) is not dict for row in raw_results):
                raise ValueError("AHR_RESUME_STATE_INVALID")
            results = list(raw_results)
            progress.units_completed = len(completed)
            progress.units_dispatched = len(completed)
            for unit_key in completed:
                parts = unit_key.split("|")
                if len(parts) >= 3:
                    strategy_id = parts[1]
                    progress.identity_ids_dispatched.add(strategy_id)
                    progress.dates_processed.add(parts[2])
                    progress.families_touched.add(
                        _family_label(dispatcher.binding(strategy_id))
                    )
        else:
            _write_state(paths["state"], run_id, input_manifest_sha256, completed, results)

        production_snapshot = snapshot_files(_production_guard_paths(project_root))
        stage = "INPUT_CONSTRUCTION"
        parent_results: dict[tuple[str, str], list[NewHistoricalParentResult]] = {}
        dispatched_ids: set[str] = set()
        completed_ids: set[str] = set()
        parity_count = 0

        with offline_network_guard():
            for binding in selected_bindings:
                if binding.version != "V1":
                    continue
                dispatched_ids.add(binding.strategy_id)
                for market_date in dates:
                    failed_strategy_id = binding.strategy_id
                    failed_market_date = market_date
                    if not builder.is_contract_opportunity(
                        strategy_id=binding.strategy_id,
                        market_date=market_date,
                    ):
                        continue
                    unit_key = f"V1|{binding.strategy_id}|{market_date}"
                    if unit_key in completed:
                        for row in results:
                            if (
                                row.get("unit_key") == unit_key
                                and row.get("accepted")
                            ):
                                parent = _parent_from_record(builder, row)
                                parent_results.setdefault(
                                    (binding.strategy_id, market_date), []
                                ).append(parent)
                        continue
                    unit = builder.build_v1_unit(
                        strategy_id=binding.strategy_id,
                        market_date=market_date,
                    )
                    stage = "DISPATCH"
                    progress.units_dispatched += 1
                    progress.identity_ids_dispatched.add(binding.strategy_id)
                    progress.dates_processed.add(market_date)
                    progress.families_touched.add(_family_label(binding))
                    evaluations = dispatcher.dispatch_historical(
                        strategy_id=binding.strategy_id,
                        request=unit.request,
                    )
                    if type(evaluations) is not tuple:
                        raise ValueError("AHR_V1_DISPATCH_RESULT_TYPE")
                    stage = "PARITY"
                    for evaluation in evaluations:
                        record = _v1_result_record(
                            binding.strategy_id,
                            market_date,
                            unit.input_sha256,
                            evaluation,
                            unit_key,
                            unit.request,
                        )
                        comparison_counts: list[int] = []
                        evaluate_persist_compare(
                            evaluate=lambda row=record: row,
                            persist=lambda row: _persist_result(
                                row, results, paths["results"]
                            ),
                            load_expected=lambda row=record: _load_expected_v1(
                                project_root, row, opportunities
                            ),
                            compare=lambda actual, expected: comparison_counts.append(
                                _compare_v1_expected_components(actual, expected)
                            ),
                        )
                        parity_count += comparison_counts[0]
                        if evaluation.accepted:
                            parent = _new_parent_result(builder, record)
                            parent_results.setdefault(
                                (binding.strategy_id, market_date), []
                            ).append(parent)
                    completed.add(unit_key)
                    progress.units_completed += 1
                    _write_state(
                        paths["state"],
                        run_id,
                        input_manifest_sha256,
                        completed,
                        results,
                    )
                    stage = "INPUT_CONSTRUCTION"
                completed_ids.add(binding.strategy_id)
                progress.identity_ids_completed.add(binding.strategy_id)

            for binding in selected_bindings:
                if binding.version != "V2":
                    continue
                dispatched_ids.add(binding.strategy_id)
                opportunity_count = 0
                for market_date in dates:
                    failed_strategy_id = binding.strategy_id
                    failed_market_date = market_date
                    for parent in parent_results.get(
                        (str(binding.parent_strategy_id), market_date), []
                    ):
                        stage = "INPUT_CONSTRUCTION"
                        unit = _build_v2_contract_unit(
                            builder=builder,
                            strategy_id=binding.strategy_id,
                            market_date=market_date,
                            parent_result=parent,
                        )
                        if unit is None:
                            continue
                        opportunity_count += 1
                        unit_key = (
                            f"V2|{binding.strategy_id}|{market_date}|"
                            f"{parent.checkpoint_minutes}|{parent.source_decision_sha256}"
                        )
                        if unit_key in completed:
                            continue
                        stage = "DISPATCH"
                        progress.units_dispatched += 1
                        progress.identity_ids_dispatched.add(binding.strategy_id)
                        progress.dates_processed.add(market_date)
                        progress.families_touched.add(_family_label(binding))
                        evaluation = dispatcher.dispatch_historical(
                            strategy_id=binding.strategy_id,
                            request=unit.request,
                        )
                        if type(evaluation) is not V2OverlayEvaluation:
                            raise ValueError("AHR_V2_DISPATCH_RESULT_TYPE")
                        record = _v2_result_record(
                            market_date,
                            unit.input_sha256,
                            evaluation,
                            unit_key,
                        )
                        stage = "PARITY"
                        evaluate_persist_compare(
                            evaluate=lambda row=record: row,
                            persist=lambda row: _persist_result(
                                row, results, paths["results"]
                            ),
                            load_expected=lambda row=record: _load_expected_v2(
                                project_root, row
                            ),
                            compare=lambda actual, expected: compare_semantic(
                                actual=_semantic_actual_v2(actual),
                                expected=expected,
                            ),
                        )
                        parity_count += 1
                        completed.add(unit_key)
                        progress.units_completed += 1
                        _write_state(
                            paths["state"],
                            run_id,
                            input_manifest_sha256,
                            completed,
                            results,
                        )
                if opportunity_count == 0:
                    raise ValueError(
                        f"AHR_V2_CONTRACT_OPPORTUNITY_MISSING:{binding.strategy_id}"
                    )
                completed_ids.add(binding.strategy_id)
                progress.identity_ids_completed.add(binding.strategy_id)

        stage = "SAFETY"
        assert_files_unchanged(production_snapshot)
        expected_strategy_count = 4 if smoke_mode else 47
        if len(dispatched_ids) != expected_strategy_count or len(completed_ids) != expected_strategy_count:
            raise ValueError(
                f"AHR_STRATEGY_COMPLETION_MISMATCH:{len(dispatched_ids)}:{len(completed_ids)}"
            )
        acceptance.update(
            {
                "status": "PASS",
                "exit_code": EXIT_PASS,
                "earliest_failed_gate": None,
                "strategies_expected": expected_strategy_count,
                "semantic_parity": "PASS",
                "parity_comparisons": parity_count,
            }
        )
        _apply_progress_to_acceptance(acceptance, progress)
        _write_json_atomic(
            paths["parity"],
            {
                "schema_version": "AHR_1_PARITY_REPORT_V1",
                "status": "PASS",
                "comparisons": parity_count,
                "mismatches": [],
            },
        )
        _append_log(paths["log"], "AHR_STATUS=PASS")
        _write_json_atomic(paths["acceptance"], acceptance)
        return EXIT_PASS
    except BaseException as exc:
        exit_code, gate = _classify_failure(exc, stage)
        if production_snapshot:
            try:
                assert_files_unchanged(production_snapshot)
            except AhrSafetyError as safety_exc:
                exc = safety_exc
                exit_code, gate = EXIT_SAFETY, "SAFETY"
        acceptance.update(
            {
                "status": "BLOCKED",
                "exit_code": exit_code,
                "earliest_failed_gate": gate,
                "failed_strategy_id": failed_strategy_id,
                "failed_market_date": failed_market_date,
                "error_class": type(exc).__name__,
                "error_message": str(exc),
                "semantic_parity": "BLOCKED",
            }
        )
        if gate == "INPUT_CONSTRUCTION":
            acceptance["input_construction_failures"] = int(
                acceptance.get("input_construction_failures", 0)
            ) + 1
        elif gate == "DISPATCH":
            acceptance["dispatch_failures"] = int(
                acceptance.get("dispatch_failures", 0)
            ) + 1
        _apply_progress_to_acceptance(acceptance, progress)
        _write_json_atomic(
            paths["parity"],
            {
                "schema_version": "AHR_1_PARITY_REPORT_V1",
                "status": "BLOCKED",
                "error_class": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        _append_log(paths["log"], f"AHR_STATUS=BLOCKED:{gate}:{exc}")
        _write_json_atomic(paths["acceptance"], acceptance)
        return exit_code


def _selected_bindings(bindings: tuple[Any, ...], *, smoke: bool) -> tuple[Any, ...]:
    if not smoke:
        return bindings
    selected = set(_SMOKE_V1_IDS + _SMOKE_V2_IDS)
    return tuple(binding for binding in bindings if binding.strategy_id in selected)


def _family_label(binding: Any) -> str:
    if binding.version == "V2":
        return "VOLATILITY_V2"
    if binding.evaluator_key == "STRICT_A_COMPOSED_V1":
        return "STRICT_A"
    if binding.evaluator_key == "PF1_COMPOSED_V1":
        return "PF1"
    return "HISTORICAL_V1"


def _select_smoke_dates(builder: HistoricalInputBuilder) -> tuple[str, ...]:
    opportunities = builder.opportunity_map()
    opportunities.validate_canonical_counts()
    selected: list[str] = []
    with offline_network_guard():
        for market_date in opportunities.historical_dates:
            if not all(
                builder.is_contract_opportunity(
                    strategy_id=strategy_id,
                    market_date=market_date,
                )
                for strategy_id in _SMOKE_V1_IDS
            ):
                continue
            unit = builder.build_v1_unit(
                strategy_id="YES_STRICT_A_T60",
                market_date=market_date,
            )
            evaluations = builder.dispatcher.dispatch_historical(
                strategy_id="YES_STRICT_A_T60",
                request=unit.request,
            )
            if type(evaluations) is not tuple or len(evaluations) != 1:
                raise ValueError("AHR_SMOKE_PARENT_SELECTION_RESULT")
            if evaluations[0].accepted:
                selected.append(market_date)
                if len(selected) == 3:
                    return tuple(selected)
    raise ValueError("AHR_SMOKE_PARENT_OPPORTUNITIES_MISSING")


def _verify_c4_receipt(project_root: Path, bindings: tuple[Any, ...]) -> None:
    path = project_root / "reports" / "C4_STRATEGY_47_ACCEPTANCE.json"
    payload = _read_json(path)
    ids = [binding.strategy_id for binding in bindings]
    if (
        payload.get("status") != "C4_STRATEGY_47_ACCEPTANCE_PASS"
        or payload.get("acceptance_pass") is not True
        or payload.get("strategy_count") != 47
        or payload.get("strategy_ids") != ids
        or payload.get("external_provider_requests") != 0
        or payload.get("trading_approval") is not False
    ):
        raise ValueError("AHR_C4_ACCEPTANCE_INVALID")


def _v1_result_record(
    strategy_id: str,
    market_date: str,
    input_sha256: str,
    result: V1Evaluation,
    unit_key: str,
    request: Any,
) -> dict[str, Any]:
    favorite_bucket_index = result.favorite_bucket_index
    if favorite_bucket_index is None:
        favorite_bucket_index = _reference_favorite_bucket_index(
            request, result.checkpoint_minutes
        )
    payload = {
        "unit_key": unit_key,
        "version": "V1",
        "strategy_id": strategy_id,
        "market_date": market_date,
        "checkpoint_minutes": result.checkpoint_minutes,
        "accepted": result.accepted,
        "reason": result.reason,
        "side": result.side,
        "selected_bucket_indices": list(result.selected_bucket_indices),
        "favorite_bucket_index": favorite_bucket_index,
        "model_probability_micros": _optional_micros(result.model_probability),
        "market_probability_micros": _optional_micros(result.market_probability),
        "stressed_reference_cost_micros": _optional_micros(
            result.stressed_reference_cost
        ),
        "input_sha256": input_sha256,
        "execution_historical_depth": "UNAVAILABLE",
    }
    payload["result_sha256"] = _payload_sha256(payload)
    return payload


def _reference_favorite_bucket_index(request: Any, checkpoint_minutes: int) -> int | None:
    checkpoints = tuple(
        checkpoint
        for checkpoint in request.checkpoints
        if checkpoint.checkpoint_minutes == checkpoint_minutes
    )
    if len(checkpoints) != 1:
        raise ValueError("AHR_V1_RESULT_CHECKPOINT_MISSING")
    buckets = checkpoints[0].buckets
    maximum = max(row.market_q_yes for row in buckets)
    tied = [row for row in buckets if abs(row.market_q_yes - maximum) <= 1e-9]
    return tied[0].bucket_index if len(tied) == 1 else None


def _v2_result_record(
    market_date: str,
    input_sha256: str,
    result: V2OverlayEvaluation,
    unit_key: str,
) -> dict[str, Any]:
    payload = {
        "unit_key": unit_key,
        "version": "V2",
        "strategy_id": result.overlay_strategy_id,
        "market_date": market_date,
        "checkpoint_minutes": int(result.horizon.removeprefix("T-").removesuffix("m")),
        "overlay_strategy_id": result.overlay_strategy_id,
        "parent_strategy_id": result.parent_strategy_id,
        "accepted": result.accepted,
        "reason_code": result.reason_code,
        "selected_buckets": list(result.selected_buckets),
        "horizon": result.horizon,
        "p_vol_side_micros": result.p_vol_side_micros,
        "stressed_q_3c_micros": result.stressed_q_3c_micros,
        "volatility_edge_micros": result.volatility_edge_micros,
        "actual_price_micros": result.actual_price_micros,
        "baseline_accept": result.baseline_accept,
        "source_decision_sha256": result.source_decision_sha256,
        "forecast_row_sha256": result.forecast_row_sha256,
        "input_sha256": input_sha256,
    }
    payload["result_sha256"] = _payload_sha256(payload)
    return payload


def _new_parent_result(
    builder: HistoricalInputBuilder, record: Mapping[str, Any]
) -> NewHistoricalParentResult:
    indices = tuple(int(item) for item in record["selected_bucket_indices"])
    rows = builder._bucket_source_rows(
        str(record["strategy_id"]),
        str(record["market_date"]),
        int(record["checkpoint_minutes"]),
    )
    selected = tuple(
        hashlib.sha256(str(rows[index]["bucket_title"]).encode("utf-8")).hexdigest()
        for index in indices
    )
    semantic = {
        key: record[key]
        for key in (
            "strategy_id",
            "market_date",
            "checkpoint_minutes",
            "accepted",
            "reason",
            "side",
            "selected_bucket_indices",
            "model_probability_micros",
            "market_probability_micros",
            "stressed_reference_cost_micros",
        )
    }
    source_sha = _payload_sha256(semantic)
    decision_identity = _payload_sha256(
        [record["strategy_id"], record["market_date"], record["checkpoint_minutes"], source_sha]
    )
    actual_price = record.get("market_probability_micros")
    if type(actual_price) is not int:
        raise ValueError("AHR_PARENT_ACTUAL_PRICE_MISSING")
    return NewHistoricalParentResult(
        market_date=str(record["market_date"]),
        strategy_id=str(record["strategy_id"]),
        decision_identity=decision_identity,
        side=str(record["side"]),
        checkpoint_minutes=int(record["checkpoint_minutes"]),
        selected_bucket_indices=indices,
        selected_buckets=selected,
        horizon=f"T-{record['checkpoint_minutes']}m",
        accepted=bool(record["accepted"]),
        actual_price_micros=actual_price,
        source_decision_sha256=source_sha,
    )


def _parent_from_record(
    builder: HistoricalInputBuilder, record: Mapping[str, Any]
) -> NewHistoricalParentResult:
    return _new_parent_result(builder, record)


def _load_expected_v1(
    project_root: Path,
    actual: Mapping[str, Any],
    opportunities: HistoricalOpportunityMap,
) -> tuple[ExpectedV1Component, ...]:
    strategy_id = str(actual["strategy_id"])
    market_date = str(actual["market_date"])
    checkpoint = int(actual["checkpoint_minutes"])
    parity_root = project_root / "strategy_sources" / "frozen" / "parity"
    market_hash = _payload_sha256(["market", market_date])
    if strategy_id in historical_identity_bindings():
        binding = historical_identity_bindings()[strategy_id]
        universes = opportunities.component_universes(
            strategy_id=strategy_id,
            market_date=market_date,
            checkpoint_minutes=checkpoint,
        )
        name = opportunities.parity_fixture_name(strategy_id)
        for row in _jsonl(parity_root / name):
            if (
                row.get("record_type") != "decision"
                or row.get("market_identity_sha256") != market_hash
                or row.get("checkpoint_minutes") != checkpoint
            ):
                continue
            components: list[ExpectedV1Component] = []
            for universe in universes:
                matches = [
                    expected
                    for expected in row.get("expectations", [])
                    if expected.get("family") == binding.family
                    and expected.get("partition") == binding.partition
                    and expected.get("universe") == universe
                ]
                if len(matches) != 1:
                    break
                components.append(
                    ExpectedV1Component(
                        universe=universe,
                        semantic=_historical_expected_semantic(
                            strategy_id, checkpoint, row, matches[0]
                        ),
                    )
                )
            if len(components) == len(universes) and components:
                return tuple(components)
        raise AhrParityError(
            f"AHR_EXPECTED_V1_EVIDENCE_MISSING:{strategy_id}:{market_date}:{checkpoint}"
        )

    path = parity_root / opportunities.parity_fixture_name(strategy_id)
    for row in _jsonl(path):
        if row.get("market_identity_sha256") != market_hash:
            continue
        for expected in row.get("expectations", []):
            if expected.get("strategy_id") == strategy_id:
                accepted = bool(expected["expected_accept"])
                return (
                    ExpectedV1Component(
                        universe="ALL",
                        semantic={
                            "strategy_id": strategy_id,
                            "accepted": accepted,
                            "checkpoint_minutes": expected[
                                "expected_checkpoint_minutes"
                            ]
                            if accepted
                            else None,
                            "selected_bucket_indices": expected[
                                "expected_selected_bucket_indices"
                            ]
                            if accepted
                            else [],
                            "stressed_reference_cost_micros": int(
                                expected["expected_stressed_cost_micros"]
                            )
                            if accepted
                            else 0,
                        },
                    ),
                )
    raise AhrParityError(
        f"AHR_EXPECTED_V1_EVIDENCE_MISSING:{strategy_id}:{market_date}:{checkpoint}"
    )


def _historical_expected_semantic(
    strategy_id: str,
    checkpoint: int,
    row: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    selected_index = expected.get("expected_selected_bucket_index")
    model_micros = None
    if type(selected_index) is int:
        yes_model = int(row["buckets"][selected_index]["model_p_micros"])
        model_micros = (
            yes_model if expected["side"] == "YES" else 1_000_000 - yes_model
        )
    return {
        "strategy_id": strategy_id,
        "checkpoint_minutes": checkpoint,
        "accepted": bool(expected["expected_accept"]),
        "reason": "SIGNAL_ACCEPTED"
        if expected["expected_accept"]
        else expected["expected_reason"],
        "selected_bucket_indices": []
        if selected_index is None
        else [selected_index],
        "favorite_bucket_index": expected["expected_favorite_bucket_index"],
        "model_probability_micros": model_micros,
        "market_probability_micros": int(expected["expected_raw_q_micros"])
        if selected_index is not None
        else None,
        "stressed_reference_cost_micros": int(
            expected["expected_stressed_q_micros"]
        )
        if selected_index is not None
        else None,
    }


def _compare_v1_expected_components(
    actual: Mapping[str, Any],
    expected_components: tuple[ExpectedV1Component, ...],
) -> int:
    if not expected_components:
        raise AhrParityError("AHR_EXPECTED_V1_COMPONENTS_EMPTY")
    for component in expected_components:
        compare_semantic(
            actual=_semantic_actual_v1(actual, component.semantic),
            expected=component.semantic,
        )
    return len(expected_components)


def _semantic_actual_v1(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> dict[str, Any]:
    if "reason" in expected:
        return {key: actual.get(key) for key in expected}
    accepted = bool(actual["accepted"])
    stressed = 0
    if accepted:
        if actual.get("stressed_reference_cost_micros") is not None:
            stressed = int(actual["stressed_reference_cost_micros"])
        else:
            market = actual.get("market_probability_micros")
            selected = actual.get("selected_bucket_indices", [])
            if type(market) is not int:
                raise ValueError("AHR_V1_STRESSED_COST_MISSING")
            stressed = min(1_000_000, market + 30_000 * len(selected))
    return {
        "strategy_id": actual["strategy_id"],
        "accepted": accepted,
        "checkpoint_minutes": actual["checkpoint_minutes"] if accepted else None,
        "selected_bucket_indices": actual["selected_bucket_indices"] if accepted else [],
        "stressed_reference_cost_micros": stressed,
    }


def _load_expected_v2(project_root: Path, actual: Mapping[str, Any]) -> dict[str, Any]:
    path = (
        project_root
        / "strategy_sources"
        / "frozen"
        / "parity"
        / "VOL_OVERLAY_DECISION_PARITY.jsonl"
    )
    for row in _jsonl(path):
        if (
            row.get("overlay_strategy_id") == actual.get("overlay_strategy_id")
            and row.get("forecast_row_sha256") == actual.get("forecast_row_sha256")
            and row.get("horizon") == actual.get("horizon")
            and row.get("selected_buckets") == actual.get("selected_buckets")
        ):
            accepted = bool(row["expected_accept"])
            stressed = int(row["expected_stressed_q_micros"])
            p_vol = int(row["p_vol_side_micros"])
            reason = (
                "VOLATILITY_OVERLAY_ACCEPTED"
                if accepted
                else "VOLATILITY_EDGE_BELOW_THRESHOLD"
            )
            return {
                "overlay_strategy_id": row["overlay_strategy_id"],
                "parent_strategy_id": row["parent_strategy_id"],
                "accepted": accepted,
                "reason_code": reason,
                "selected_buckets": row["selected_buckets"],
                "p_vol_side_micros": p_vol,
                "stressed_q_3c_micros": stressed,
                "volatility_edge_micros": p_vol - stressed,
            }
    raise AhrParityError(
        f"AHR_EXPECTED_V2_EVIDENCE_MISSING:{actual.get('strategy_id')}:{actual.get('market_date')}"
    )


def _semantic_actual_v2(actual: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: actual[key]
        for key in (
            "overlay_strategy_id",
            "parent_strategy_id",
            "accepted",
            "reason_code",
            "selected_buckets",
            "p_vol_side_micros",
            "stressed_q_3c_micros",
            "volatility_edge_micros",
        )
    }


def _persist_result(
    record: dict[str, Any], results: list[dict[str, Any]], path: Path
) -> None:
    results.append(record)
    _write_results_atomic(path, results)


def _write_results_atomic(path: Path, results: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as parquet

    rows = [
        {
            "unit_key": str(row.get("unit_key", "")),
            "strategy_id": str(row.get("strategy_id", "")),
            "market_date": str(row.get("market_date", "")),
            "version": str(row.get("version", "")),
            "checkpoint_minutes": int(row.get("checkpoint_minutes", 0)),
            "record_json": json.dumps(
                row, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        }
        for row in results
    ]
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                ("unit_key", pa.string()),
                ("strategy_id", pa.string()),
                ("market_date", pa.string()),
                ("version", pa.string()),
                ("checkpoint_minutes", pa.int64()),
                ("record_json", pa.string()),
            ]
        ),
    )
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    try:
        parquet.write_table(table, temporary)
        with open(temporary, "rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _initialize_artifacts(paths: Mapping[str, Path], run_id: str) -> None:
    _write_json_atomic(paths["acceptance"], _base_acceptance(run_id, smoke=False))
    _write_json_atomic(paths["manifest"], {"status": "PENDING"})
    _write_results_atomic(paths["results"], [])
    _write_json_atomic(paths["parity"], {"status": "PENDING"})
    if not paths["state"].exists():
        _write_json_atomic(paths["state"], {"status": "PENDING"})
    if not paths["log"].exists():
        _write_text_atomic(paths["log"], "AHR_RUN_INITIALIZED\n")


def _base_acceptance(run_id: str, *, smoke: bool) -> dict[str, Any]:
    return {
        "schema_version": "AHR_1_ACCEPTANCE_V1",
        "mode": _MODE,
        "run_id": run_id,
        "smoke": smoke,
        "status": "BLOCKED",
        "exit_code": EXIT_INTERNAL,
        "earliest_failed_gate": "INTERNAL",
        "dates_expected": 3 if smoke else 170,
        "dates_processed": 0,
        "strategies_expected": 4 if smoke else 47,
        "strategies_discovered": 0,
        "strategies_dispatched": 0,
        "strategies_completed": 0,
        "input_construction_failures": 0,
        "dispatch_failures": 0,
        "unexpected_skips": 0,
        "lookahead_violations": 0,
        "network_requests": 0,
        "semantic_parity": "BLOCKED",
        "production_db_modified": False,
        "production_scheduler_modified": False,
        "real_orders": False,
        "wallet": False,
        "signing": False,
        "trading_approval": False,
        "failed_strategy_id": None,
        "failed_market_date": None,
        "error_class": None,
        "error_message": None,
    }


def _write_state(
    path: Path,
    run_id: str,
    manifest_sha256: str,
    completed: set[str],
    results: list[dict[str, Any]],
) -> None:
    _write_json_atomic(
        path,
        {
            "schema_version": "AHR_1_RUN_STATE_V1",
            "run_id": run_id,
            "input_manifest_sha256": manifest_sha256,
            "completed_units": sorted(completed),
            "last_completed_unit": max(completed) if completed else None,
            "results": results,
        },
    )


def _classify_failure(exc: BaseException, stage: str) -> tuple[int, str]:
    if isinstance(exc, AhrError):
        return exc.exit_code, exc.gate
    mapping = {
        "SOURCE_INTEGRITY": EXIT_SOURCE_INTEGRITY,
        "INPUT_CONSTRUCTION": EXIT_INPUT_CONSTRUCTION,
        "DISPATCH": EXIT_DISPATCH,
        "PARITY": EXIT_PARITY,
        "SAFETY": EXIT_SAFETY,
    }
    return mapping.get(stage, EXIT_INTERNAL), stage if stage in mapping else "INTERNAL"


def _production_guard_paths(project_root: Path) -> tuple[Path, ...]:
    roots = [project_root]
    if project_root.parent.name == ".worktrees":
        roots.append(project_root.parent.parent)
    paths: list[Path] = []
    for root in roots:
        paths.extend(
            (
                root / "data" / "runtime" / "btc_live_backend.sqlite3",
                root / "scripts" / "C9_TASK_SCHEDULER.ps1",
                root / "config" / "mvp_runtime_v1.json",
            )
        )
    return tuple(dict.fromkeys(paths))


def _run_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "acceptance": run_dir / "ACCEPTANCE.json",
        "manifest": run_dir / "INPUT_MANIFEST.json",
        "results": run_dir / "STRATEGY_RESULTS.parquet",
        "parity": run_dir / "PARITY_REPORT.json",
        "log": run_dir / "RUN_LOG.txt",
        "state": run_dir / "RUN_STATE.json",
    }


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                value = json.loads(line)
                if type(value) is not dict:
                    raise AhrParityError("AHR_EXPECTED_EVIDENCE_SCHEMA")
                yield value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError("AHR_JSON_OBJECT_REQUIRED")
    return value


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _write_text_atomic(path: Path, value: str) -> None:
    _write_bytes_atomic(path, value.encode("utf-8"))


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _append_log(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _payload_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_micros(value: float | None) -> int | None:
    if value is None:
        return None
    return int(
        (Decimal(str(value)) * Decimal(1_000_000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )


def _new_run_id(*, suffix: bool = False) -> str:
    value = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return value + ("_1" if suffix else "")


def default_artifacts() -> HistoricalArtifactPaths:
    user = Path.home()
    codex = user / "Documents" / "Codex"
    return HistoricalArtifactPaths(
        source_pack_70=user / "Downloads" / "btc_daily_range_merged_70_v1_10.zip",
        source_pack_100=user / "Downloads" / "btc_daily_range_validation_100_merged_v2_2(1).zip",
        checkpoint_matrix_170=codex / "btc_early_horizon_single_entry_v1_output" / "checkpoint_matrix_170x11x11.parquet",
        atlas_170=codex / "btc_edge_search_lab_v1" / "recovery" / "btc_terminal_distribution_bias_lab_v1" / "artifacts" / "actual_bucket_probabilities_170.parquet",
        markets_170=codex / "btc_edge_search_lab_v1" / "data" / "normalized" / "unified_research_visible_170_v1" / "markets.parquet",
        settlements_170=codex / "btc_edge_search_lab_v1" / "data" / "normalized" / "unified_research_visible_170_v1" / "settlements.parquet",
        no_checkpoint_coverage=codex / "btc_no_historical_price_collector_v1" / "full_runs" / "merged" / "checkpoint_coverage.csv",
        confirmation_dates=codex / "btc_edge_search_lab_v1" / "recovery" / "btc_no_strategy_confirmation_136_v1" / "data" / "CONFIRMATION_DATES_136.csv",
        vol_forecast_ledger=codex / "btc_volatility_overlay_audit_v1_output" / "MODEL_FORECAST_LEDGER.parquet",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline AHR-1 historical revalidation")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--pyarrow-path", type=Path)
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    config = AhrRunConfig(
        project_root=project_root,
        run_root=project_root / "reports" / "historical_revalidation",
        artifacts=default_artifacts(),
        pyarrow_path=args.pyarrow_path,
        smoke=args.smoke,
    )
    code = run_baseline(config, resume_run_id=args.resume)
    run_dirs = sorted(config.run_root.glob("*"), key=lambda path: path.name)
    if run_dirs:
        receipt = _read_json(run_dirs[-1] / "ACCEPTANCE.json")
        print(f"DATES={receipt.get('dates_processed', 0)}")
        print(f"STRATEGY_FAMILIES={receipt.get('strategy_families', 0)}")
        print(f"INPUT_FAILURES={receipt.get('input_construction_failures', 0)}")
        print(f"DISPATCH_FAILURES={receipt.get('dispatch_failures', 0)}")
        print(f"PARITY={receipt.get('semantic_parity', 'BLOCKED')}")
        print(f"NETWORK_REQUESTS={receipt.get('network_requests', 0)}")
        print(f"PRODUCTION_DB_MODIFIED={str(receipt.get('production_db_modified', False)).lower()}")
        print(f"AHR_RUN_ID={receipt.get('run_id')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
