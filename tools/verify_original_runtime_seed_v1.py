from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.performance_engine import StrategyPerformanceEngine
from src.performance_repository import PerformanceRepository
from src.storage import SqliteStore


TABLES = (
    "strategy_performance_observations",
    "strategy_performance_resolutions",
    "strategy_performance_ingest_runs",
    "strategy_performance_materialization_revisions",
    "strategy_performance_aggregates",
    "strategy_performance_timeseries",
)


def verify_seed(*, project_root: Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        old_store = SqliteStore.open(root / "old-ahr.sqlite3")
        new_store = SqliteStore.open(root / "new-seed.sqlite3")
        try:
            old_store.migrate()
            new_store.migrate()
            old_result = StrategyPerformanceEngine(
                project_root=project_root,
                repository=PerformanceRepository(old_store),
            ).bootstrap_historical_from_ahr(as_of_date="2026-08-15")
            new_result = StrategyPerformanceEngine(
                project_root=project_root,
                repository=PerformanceRepository(new_store),
            ).bootstrap_historical(as_of_date="2026-08-15")
            tables = {
                table: {
                    "old": _table_receipt(old_store, table),
                    "new": _table_receipt(new_store, table),
                }
                for table in TABLES
            }
            for comparison in tables.values():
                comparison["equal"] = comparison["old"] == comparison["new"]
            guards = {
                "old": _guard_receipt(old_store),
                "new": _guard_receipt(new_store),
            }
            status = "PASS" if (
                all(item["equal"] for item in tables.values())
                and all(value == 0 for side in guards.values() for value in side.values())
            ) else "FAIL"
            return {
                "schema_version": "ORIGINAL_RUNTIME_SEED_PARITY_RECEIPT_V1",
                "status": status,
                "as_of_date": "2026-08-15",
                "old_bootstrap": asdict(old_result),
                "new_bootstrap": asdict(new_result),
                "tables": tables,
                "guards": guards,
            }
        finally:
            new_store.close()
            old_store.close()


def _table_receipt(store: SqliteStore, table: str) -> dict[str, object]:
    info = store._connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [row[1] for row in info]
    primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    if not primary:
        raise ValueError(f"ORIGINAL_RUNTIME_SEED_PARITY_PRIMARY_KEY_MISSING:{table}")
    rows = store._connection.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(primary)}"
    ).fetchall()
    content = json.dumps(
        {"columns": columns, "rows": [list(row) for row in rows]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"rows": len(rows), "sha256": hashlib.sha256(content).hexdigest()}


def _guard_receipt(store: SqliteStore) -> dict[str, int]:
    connection = store._connection
    return {
        "emitted_observations": connection.execute(
            "SELECT COUNT(*) FROM strategy_performance_observations WHERE emitted=1"
        ).fetchone()[0],
        "forward_observations": connection.execute(
            "SELECT COUNT(*) FROM strategy_performance_observations WHERE source_layer='FORWARD'"
        ).fetchone()[0],
        "paper_fills": store.count("paper_fills"),
        "paper_intents": store.count("paper_intents"),
        "recovered_rows": store.count("strategy_reconstruction_status"),
        "signals": store.count("signals"),
    }


def main() -> int:
    receipt = verify_seed(project_root=PROJECT_ROOT)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
