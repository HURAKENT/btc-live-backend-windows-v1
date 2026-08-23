from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from src.performance_metrics import build_metrics
from src.performance_repository import PerformanceRepository
from src.storage import SqliteReadStore


PERFORMANCE_QUERY_SCHEMA_VERSION = "PERFORMANCE_QUERY_V1"
DEFAULT_CALCULATION_VERSION = "PERFORMANCE_METRICS_V1"
SOURCE_VIEWS = ("HISTORICAL", "FORWARD", "COMBINED")
OBSERVATION_LIMIT_MAX = 100


class PerformanceQueryService:
    """Read-only facade for current strategy-performance materializations."""

    def __init__(
        self,
        read_store: SqliteReadStore,
        *,
        project_root: Path | None = None,
        calculation_version: str = DEFAULT_CALCULATION_VERSION,
    ) -> None:
        if type(read_store) is not SqliteReadStore:
            raise ValueError("INVALID_PERFORMANCE_QUERY_READ_STORE")
        if type(calculation_version) is not str or not calculation_version:
            raise ValueError("INVALID_PERFORMANCE_QUERY_CALCULATION_VERSION")
        self.read_store = read_store
        self.repository = PerformanceRepository(read_store)
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[1]
        )
        self.calculation_version = calculation_version
        self._strategies = _load_strategy_status(self.project_root)

    def strategies(self) -> dict[str, Any]:
        rows = [self._strategy_summary(row) for row in self._strategies]
        return {
            "calculation_version": self.calculation_version,
            "schema_version": PERFORMANCE_QUERY_SCHEMA_VERSION,
            "strategies": rows,
            "strategy_count": len(rows),
            "source_views": list(SOURCE_VIEWS),
        }

    def strategy_detail(self, strategy_id: str) -> dict[str, Any]:
        strategy = self._require_strategy(strategy_id)
        return {
            **self._strategy_metadata(strategy),
            "calculation_version": self.calculation_version,
            "schema_version": PERFORMANCE_QUERY_SCHEMA_VERSION,
            "views": {
                source_view: self._view_payload(strategy_id, source_view)
                for source_view in SOURCE_VIEWS
            },
            "provenance_views": self._provenance_views(strategy_id),
        }

    def timeseries(self, strategy_id: str, *, source_view: str) -> dict[str, Any]:
        self._require_strategy(strategy_id)
        _validate_source_view(source_view)
        view = self._view_payload(strategy_id, source_view)
        if view["status"] != "READY":
            return {
                "calculation_version": self.calculation_version,
                "schema_version": PERFORMANCE_QUERY_SCHEMA_VERSION,
                "source_view": source_view,
                "status": "NOT_READY",
                "strategy_id": strategy_id,
                "timeseries": {},
            }
        return {
            "calculation_version": self.calculation_version,
            "schema_version": PERFORMANCE_QUERY_SCHEMA_VERSION,
            "source_view": source_view,
            "status": "READY",
            "strategy_id": strategy_id,
            "timeseries": view["timeseries"],
        }

    def observations(
        self,
        strategy_id: str,
        *,
        limit: int = 100,
        after_observation_key: str | None = None,
    ) -> dict[str, Any]:
        self._require_strategy(strategy_id)
        _validate_limit(limit)
        if after_observation_key is not None and after_observation_key == "":
            raise ValueError("INVALID_PERFORMANCE_OBSERVATION_CURSOR")
        rows = self.read_store.rows(
            """
            SELECT observation.payload_json, resolution.payload_json
            FROM strategy_performance_observations AS observation
            LEFT JOIN strategy_performance_resolutions AS resolution
              ON resolution.observation_key = observation.observation_key
             AND NOT EXISTS (
                    SELECT 1
                    FROM strategy_performance_resolutions AS successor
                    WHERE successor.supersedes_resolution_key = resolution.resolution_key
                )
            WHERE observation.strategy_id = ?
              AND (? IS NULL OR observation.observation_key > ?)
            ORDER BY observation.market_date DESC,
                     observation.checkpoint_minutes DESC,
                     observation.observation_key ASC
            LIMIT ?
            """,
            (strategy_id, after_observation_key, after_observation_key, limit + 1),
        )
        page = [_effective_observation_payload(row[0], row[1]) for row in rows[:limit]]
        return {
            "has_more": len(rows) > limit,
            "limit": limit,
            "next_after_observation_key": (
                page[-1]["observation_key"] if len(rows) > limit and page else None
            ),
            "observations": page,
            "schema_version": PERFORMANCE_QUERY_SCHEMA_VERSION,
            "strategy_id": strategy_id,
        }

    def status(self) -> dict[str, Any]:
        sources = self.read_store.sources()
        latest_source = _max_optional(row["last_source_timestamp_ms"] for row in sources)
        latest_received = _max_optional(
            row["last_received_timestamp_ms"] for row in sources
        )
        catchup = self.repository.read_effective_catchup_classifications()
        catchup_counts = dict(sorted(Counter(row.classification for row in catchup).items()))
        materialization = self.read_store.rows(
            """
            SELECT strategy_id, source_view, calculation_version,
                   revision_key, generated_at_ms, payload_sha256
            FROM strategy_performance_materialization_revisions
            WHERE status = 'COMPLETE' AND is_current = 1
            ORDER BY strategy_id ASC, source_view ASC, calculation_version ASC
            """
        )
        latest_generated = _max_optional(row[4] for row in materialization)
        return {
            "blocking_reason": _blocking_reason(catchup_counts),
            "calculation_version": self.calculation_version,
            "catchup": {
                "counts": catchup_counts,
                "latest": [_catchup_payload(row) for row in catchup[-10:]],
            },
            "database_health": self.read_store.health(),
            "latest_market": self.read_store.current_market_identity(),
            "performance": {
                "current_revision_count": len(materialization),
                "latest_generated_at_ms": latest_generated,
                "revisions": [
                    {
                        "calculation_version": row[2],
                        "generated_at_ms": row[4],
                        "payload_sha256": row[5],
                        "revision_key": row[3],
                        "source_view": row[1],
                        "strategy_id": row[0],
                    }
                    for row in materialization
                ],
            },
            "schema_version": PERFORMANCE_QUERY_SCHEMA_VERSION,
            "source_freshness": {
                "latest_received_timestamp_ms": latest_received,
                "latest_source_timestamp_ms": latest_source,
                "sources": sources,
            },
        }

    def _strategy_summary(self, strategy: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **self._strategy_metadata(strategy),
            "views": {
                source_view: self._view_summary(strategy["strategy_id"], source_view)
                for source_view in SOURCE_VIEWS
            },
        }

    def _strategy_metadata(self, strategy: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "activation_reason_code": strategy["activation_reason_code"],
            "activation_status": strategy["activation_status"],
            "eligibility_labels": _eligibility_labels(strategy),
            "evaluator_key": strategy["evaluator_key"],
            "family": _family(strategy),
            "parent_strategy_id": strategy["parent_strategy_id"],
            "paper_eligible": strategy["paper_eligible"],
            "registry_index": strategy["registry_index"],
            "strategy_id": strategy["strategy_id"],
            "version": strategy["version"],
        }

    def _view_summary(self, strategy_id: str, source_view: str) -> dict[str, Any]:
        payload = self._view_payload(strategy_id, source_view)
        if payload["status"] != "READY":
            return {"source_view": source_view, "status": "NOT_READY"}
        return {
            "generated_at_ms": payload["revision"]["generated_at_ms"],
            "metrics": payload["metrics"],
            "payload_sha256": payload["revision"]["payload_sha256"],
            "revision_key": payload["revision"]["revision_key"],
            "source_view": source_view,
            "status": "READY",
        }

    def _view_payload(self, strategy_id: str, source_view: str) -> dict[str, Any]:
        materialized = self.repository.read_current_materialization(
            strategy_id=strategy_id,
            source_view=source_view,
            calculation_version=self.calculation_version,
        )
        if materialized is None:
            return {"source_view": source_view, "status": "NOT_READY"}
        aggregates = materialized["aggregates"]
        scalar = [
            row
            for row in aggregates
            if row.window_kind == "ALL" and row.window_key == "ALL"
        ]
        if len(scalar) != 1:
            raise ValueError("PERFORMANCE_QUERY_SCALAR_AGGREGATE_MISSING")
        revision = materialized["revision"]
        return {
            "aggregates": [
                {
                    "payload": _plain(row.payload),
                    "payload_sha256": row.payload_sha256,
                    "window_key": row.window_key,
                    "window_kind": row.window_kind,
                }
                for row in aggregates
            ],
            "metrics": _plain(scalar[0].payload),
            "provenance": _plain(revision.provenance),
            "revision": {
                "children_sha256": revision.children_sha256,
                "calculation_version": revision.calculation_version,
                "generated_at_ms": revision.generated_at_ms,
                "payload_sha256": revision.payload_sha256,
                "revision_key": revision.revision_key,
                "source_ledger_revision": revision.source_ledger_revision,
                "source_ledger_sha256": revision.source_ledger_sha256,
            },
            "source_view": source_view,
            "status": "READY",
            "timeseries": _timeseries_by_kind(materialized["timeseries"]),
        }

    def _provenance_views(self, strategy_id: str) -> dict[str, Any]:
        observations = self.repository.read_observations(strategy_id=strategy_id)
        resolutions = self.repository.read_effective_resolutions()
        result: dict[str, Any] = {}
        for source_view in ("ORIGINAL", "RECOVERED", "FORWARD"):
            selected = [
                row
                for row in observations
                if (
                    source_view == "ORIGINAL"
                    and row.source_layer == "HISTORICAL"
                    and not row.provenance_run_id.startswith("RECOVERED_RETROSPECTIVE:")
                )
                or (
                    source_view == "RECOVERED"
                    and row.source_layer == "HISTORICAL"
                    and row.provenance_run_id.startswith("RECOVERED_RETROSPECTIVE:")
                )
                or (source_view == "FORWARD" and row.source_layer == "FORWARD")
            ]
            selected_keys = {row.observation_key for row in selected}
            selected_resolutions = {
                key: value for key, value in resolutions.items() if key in selected_keys
            }
            as_of_date = max((row.market_date for row in selected), default="1970-01-01")
            result[source_view] = {
                "status": "READY",
                "metrics": build_metrics(
                    selected,
                    selected_resolutions,
                    source_view=source_view,
                    as_of_date=as_of_date,
                ),
            }
        return result

    def _require_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        if strategy_id == "ALL":
            raise ValueError("PERFORMANCE_PORTFOLIO_TOTAL_FORBIDDEN")
        if type(strategy_id) is not str or not strategy_id:
            raise ValueError("INVALID_PERFORMANCE_STRATEGY_ID")
        for strategy in self._strategies:
            if strategy["strategy_id"] == strategy_id:
                return strategy
        raise KeyError(strategy_id)


def _load_strategy_status(project_root: Path) -> tuple[dict[str, Any], ...]:
    path = project_root / "reports" / "STRATEGY_47_STATUS_MATRIX.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("strategies")
    if type(rows) is not list or len(rows) != 47:
        raise ValueError("PERFORMANCE_QUERY_STRATEGY_STATUS_INVALID")
    return tuple(dict(row) for row in rows)


def _effective_observation_payload(
    observation_payload_json: str,
    resolution_payload_json: str | None,
) -> dict[str, Any]:
    payload = json.loads(observation_payload_json)
    payload["provenance_kind"] = (
        "RECOVERED_RETROSPECTIVE"
        if payload["source_layer"] == "HISTORICAL"
        and payload["provenance_run_id"].startswith("RECOVERED_RETROSPECTIVE:")
        else "ORIGINAL_BASELINE"
        if payload["source_layer"] == "HISTORICAL"
        else "GENUINE_FORWARD"
    )
    if resolution_payload_json is None:
        return payload
    payload["raw_scoring_status"] = payload["scoring_status"]
    payload["raw_scoring_reason_code"] = payload["scoring_reason_code"]
    payload["scoring_status"] = "RESOLVED"
    payload["scoring_reason_code"] = "SETTLED"
    payload["resolution"] = json.loads(resolution_payload_json)
    return payload


def _family(strategy: Mapping[str, Any]) -> str:
    if strategy["version"] == "V2":
        return "VOLATILITY_V2"
    evaluator = strategy["evaluator_key"]
    if evaluator == "STRICT_A_COMPOSED_V1":
        return "STRICT_A"
    if evaluator == "PF1_COMPOSED_V1":
        return "PF1"
    return "HISTORICAL_V1"


def _eligibility_labels(strategy: Mapping[str, Any]) -> list[str]:
    if strategy["activation_reason_code"] == "V2_FROZEN_POLICY_NOT_ROBUST_RESEARCH_ONLY":
        return ["RESEARCH_ONLY", "NOT_ROBUST"]
    if strategy["activation_status"] == "PAPER_EVALUATION_ENABLED":
        return ["PAPER_EVALUATION_ENABLED"]
    if strategy["activation_status"] == "DISABLED_MISSING_EXECUTION_DATA":
        return ["MISSING_EXECUTION_DATA"]
    return [str(strategy["activation_status"])]


def _timeseries_by_kind(rows: list[Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.series_kind, []).append(_plain(row.payload))
    return grouped


def _catchup_payload(row: Any) -> dict[str, Any]:
    return {
        "catchup_key": row.catchup_key,
        "classification": row.classification,
        "classified_at_ms": row.classified_at_ms,
        "market_date": row.market_date,
        "market_id": row.market_id,
        "reason_code": row.reason_code,
        "revision": row.revision,
        "source_event_identity": row.source_event_identity,
    }


def _blocking_reason(counts: Mapping[str, int]) -> str | None:
    if counts.get("DATA_GAP", 0) > 0:
        return "DATA_GAP"
    return None


def _validate_source_view(source_view: str) -> None:
    if source_view not in SOURCE_VIEWS:
        raise ValueError("INVALID_PERFORMANCE_SOURCE_VIEW")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= OBSERVATION_LIMIT_MAX:
        raise ValueError("INVALID_PERFORMANCE_LIMIT")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _max_optional(values: Any) -> int | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None
