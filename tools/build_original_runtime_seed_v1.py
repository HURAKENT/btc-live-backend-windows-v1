from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.original_runtime_seed import (
    EXPECTED_EXCLUDED_DATA,
    ORIGINAL_RUNTIME_SEED_ID,
    ORIGINAL_RUNTIME_SEED_MANIFEST_SCHEMA,
    ORIGINAL_RUNTIME_SEED_PACKAGER_VERSION,
    PINNED_AHR_PARITY_REPORT_SHA256,
)
from src.performance_historical import (
    HistoricalPerformanceObservationBuilder,
    HistoricalSettlementReconciler,
    PINNED_AHR_MANIFEST_SHA256,
    PinnedAhrArtifactLoader,
)


def build_seed(
    *, project_root: Path, output_dir: Path, ahr_run_dir: Path | None = None
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    bundle = PinnedAhrArtifactLoader(
        project_root=project_root, run_dir=ahr_run_dir
    ).load()
    parity_report_sha256 = _validated_parity_report(bundle.run_dir)
    observations = HistoricalPerformanceObservationBuilder(
        project_root=project_root, bundle=bundle
    ).build()
    resolutions = HistoricalSettlementReconciler(
        project_root=project_root, bundle=bundle
    ).reconcile(observations)

    observation_path = output_dir / "observations.jsonl"
    resolution_path = output_dir / "resolutions.jsonl"
    observation_path.write_bytes(
        "".join(row.payload_json + "\n" for row in observations).encode("utf-8")
    )
    resolution_path.write_bytes(
        "".join(row.payload_json + "\n" for row in resolutions).encode("utf-8")
    )

    status_path = project_root / "reports" / "STRATEGY_47_STATUS_MATRIX.json"
    rule_map_path = (
        project_root / "strategy_sources" / "frozen" / "STRATEGY_RULE_MAP_47.json"
    )
    status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    rule_map_payload = json.loads(rule_map_path.read_text(encoding="utf-8"))
    strategy_identities = sorted(
        (
            {
                "registry_index": row["registry_index"],
                "rule_spec_sha256": row["rule_spec_sha256"],
                "strategy_id": row["strategy_id"],
                "version": row["version"],
            }
            for row in status_payload["strategies"]
        ),
        key=lambda row: row["strategy_id"],
    )
    manifest = {
        "creation_method": {
            "ordering": "ACCEPTED_AHR_RESULT_ORDER",
            "script": "tools/build_original_runtime_seed_v1.py",
            "version": ORIGINAL_RUNTIME_SEED_PACKAGER_VERSION,
        },
        "excluded_data": sorted(EXPECTED_EXCLUDED_DATA),
        "frozen_contracts": [
            {
                "relative_path": "strategy_sources/frozen/STRATEGY_RULE_MAP_47.json",
                "schema_version": rule_map_payload["schema_version"],
                "sha256": _sha256(rule_map_path),
            },
            {
                "relative_path": "reports/STRATEGY_47_STATUS_MATRIX.json",
                "schema_version": status_payload["schema_version"],
                "sha256": _sha256(status_path),
            },
        ],
        "payload_files": [
            {
                "logical_type": "performance_observations",
                "record_count": len(observations),
                "relative_path": observation_path.name,
                "sha256": _sha256(observation_path),
            },
            {
                "logical_type": "performance_resolutions",
                "record_count": len(resolutions),
                "relative_path": resolution_path.name,
                "sha256": _sha256(resolution_path),
            },
        ],
        "provenance_classification": "ORIGINAL",
        "record_counts": {
            "accepted_observations": sum(row.accepted for row in observations),
            "observations": len(observations),
            "rejected_observations": sum(not row.accepted for row in observations),
            "resolutions": len(resolutions),
            "strategies": len({row.strategy_id for row in observations}),
        },
        "schema_compatibility": {
            "migration_version": 7,
            "observation_schema": "PERFORMANCE_OBSERVATION_V1",
            "resolution_schema": "PERFORMANCE_RESOLUTION_V1",
        },
        "schema_version": ORIGINAL_RUNTIME_SEED_MANIFEST_SCHEMA,
        "security_guards": {
            "authenticated_CLOB_writes": False,
            "real_orders": False,
            "signing": False,
            "trading_approval": False,
            "wallet": False,
        },
        "seed_id": ORIGINAL_RUNTIME_SEED_ID,
        "source_accepted_baseline": {
            "acceptance_sha256": bundle.acceptance_sha256,
            "input_manifest_file_sha256": PINNED_AHR_MANIFEST_SHA256,
            "input_manifest_semantic_sha256": bundle.input_manifest_sha256,
            "parity_report_sha256": parity_report_sha256,
            "path_name": f"historical_revalidation/{bundle.run_id}",
            "run_id": bundle.run_id,
            "settlements_sha256": bundle.settlement_artifact_sha256,
            "source_sha256": dict(sorted(bundle.source_sha256.items())),
            "strategy_results_sha256": bundle.result_artifact_sha256,
        },
        "strategy_identities": strategy_identities,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return {
        "manifest_sha256": _sha256(manifest_path),
        "observations": len(observations),
        "resolutions": len(resolutions),
        "seed_bytes": sum(
            path.stat().st_size
            for path in (manifest_path, observation_path, resolution_path)
        ),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_parity_report(run_dir: Path) -> str:
    path = Path(run_dir) / "PARITY_REPORT.json"
    if not path.is_file():
        raise ValueError("ORIGINAL_RUNTIME_SEED_PARITY_REPORT_MISSING")
    actual_hash = _sha256(path)
    if actual_hash != PINNED_AHR_PARITY_REPORT_SHA256:
        raise ValueError("ORIGINAL_RUNTIME_SEED_PARITY_REPORT_HASH_MISMATCH")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("ORIGINAL_RUNTIME_SEED_PARITY_REPORT_INVALID") from None
    if payload != {
        "comparisons": 5_063,
        "mismatches": [],
        "schema_version": "AHR_1_PARITY_REPORT_V1",
        "status": "PASS",
    }:
        raise ValueError("ORIGINAL_RUNTIME_SEED_PARITY_REPORT_INVALID")
    return actual_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the accepted ORIGINAL_RUNTIME_SEED_V1 from the pinned AHR oracle"
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    project_root = PROJECT_ROOT
    output_dir = args.output_dir or (
        project_root / "strategy_sources" / "frozen" / "original_runtime_seed_v1"
    )
    print(json.dumps(build_seed(project_root=project_root, output_dir=output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
