from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


EXPECTED_DURATION_HOURS = 48
EXPECTED_DURATION_SECONDS = EXPECTED_DURATION_HOURS * 60 * 60
MAX_HEARTBEAT_GAP_SECONDS = 5 * 60
REQUIRED_SECURITY_STATE = {
    "authenticated_clob_writes": False,
    "real_orders": False,
    "signing": False,
    "trading_approval": False,
    "wallet": False,
}


class ObservationError(RuntimeError):
    pass


def generate_run_id(
    *,
    started_at_ns: int,
    integration_commit: str,
    database_path: Path,
) -> str:
    if type(started_at_ns) is not int or started_at_ns < 0:
        raise ObservationError("INVALID_OBSERVATION_START_NS")
    if (
        type(integration_commit) is not str
        or len(integration_commit) != 40
        or any(character not in "0123456789abcdef" for character in integration_commit)
    ):
        raise ObservationError("INVALID_INTEGRATION_COMMIT")
    if not isinstance(database_path, Path) or not database_path.is_absolute():
        raise ObservationError("OBSERVATION_DATABASE_PATH_NOT_ABSOLUTE")
    started = datetime.fromtimestamp(started_at_ns / 1_000_000_000, tz=UTC)
    identity = json.dumps(
        {
            "database_path": str(database_path.resolve(strict=False)),
            "integration_commit": integration_commit,
            "started_at_ns": started_at_ns,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"c11-{started.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"


def config_identity(config_path: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if type(payload) is not dict:
        raise ObservationError("INVALID_RUNTIME_CONFIG")
    security_state = {
        "authenticated_clob_writes": payload.get("authenticated_clob_writes"),
        "real_orders": payload.get("real_orders_enabled"),
        "signing": payload.get("signing"),
        "trading_approval": payload.get("trading_approval"),
        "wallet": payload.get("wallet_enabled"),
    }
    if security_state != REQUIRED_SECURITY_STATE:
        raise ObservationError("OBSERVATION_SECURITY_BOUNDARY_OPEN")
    return (
        {
            "path": str(config_path.resolve(strict=False)),
            "schema_version": payload.get("schema_version"),
            "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        },
        security_state,
    )


def host_runtime_summary() -> dict[str, str]:
    return {
        "architecture": platform.machine(),
        "os": platform.system(),
        "os_release": platform.release(),
        "python": platform.python_version(),
    }


def build_report_skeleton(
    *,
    run_id: str,
    started_at: str,
    integration_commit: str,
    config_identity: dict[str, Any],
    database_path: Path,
    log_path: Path,
    host_runtime: dict[str, str],
    security_state: dict[str, bool],
) -> dict[str, Any]:
    return {
        "schema_version": "C11_OBSERVATION_REPORT_V1",
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": None,
        "elapsed_seconds": 0.0,
        "expected_duration_hours": EXPECTED_DURATION_HOURS,
        "actual_duration_hours": 0.0,
        "integration_commit": integration_commit,
        "config_identity": config_identity,
        "database": {
            "class": "SqliteStore",
            "path": str(database_path.resolve(strict=False)),
        },
        "log_path": str(log_path.resolve(strict=False)),
        "host_runtime": host_runtime,
        "process_restarts": 0,
        "source_outages": 0,
        "recoveries": 0,
        "market_rollovers": 0,
        "checkpoint_counts": {},
        "strict_a_evaluations": 0,
        "signals": 0,
        "paper_readiness_checks": 0,
        "paper_intents": 0,
        "fills": 0,
        "paper_positions": 0,
        "open_positions": 0,
        "duplicate_violations": 0,
        "accounting_violations": 0,
        "incidents": 0,
        "incident_status_counts": {},
        "fatal_errors": 0,
        "database_quick_check": None,
        "health_snapshots": 0,
        "heartbeat_max_gap_seconds": None,
        "security_state": dict(sorted(security_state.items())),
        "final_report_from_real_observations": False,
        "status": "BLOCKED",
        "blockers": ["OBSERVATION_NOT_FINALIZED"],
    }


def evaluate_future_acceptance(report: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    if report.get("elapsed_seconds", 0) < EXPECTED_DURATION_SECONDS:
        blockers.append("REAL_ELAPSED_48H_NOT_REACHED")
    if report.get("market_rollovers", 0) < 1:
        blockers.append("ROLLOVER_NOT_OBSERVED")
    if report.get("process_restarts", 0) != 2:
        blockers.append("EXACTLY_TWO_PLANNED_RESTARTS_NOT_OBSERVED")
    if report.get("source_outages", 0) < 1:
        blockers.append("CONTROLLED_NETWORK_OUTAGE_NOT_OBSERVED")
    required_recoveries = report.get("process_restarts", 0) + report.get(
        "source_outages", 0
    )
    if report.get("recoveries", 0) < required_recoveries:
        blockers.append("RECOVERY_EVIDENCE_INCOMPLETE")
    if report.get("duplicate_violations") != 0:
        blockers.append("DUPLICATE_VIOLATION")
    if report.get("accounting_violations") != 0:
        blockers.append("ACCOUNTING_VIOLATION")
    if report.get("fatal_errors") != 0:
        blockers.append("FATAL_ERROR_OBSERVED")
    if report.get("database_quick_check") != "ok":
        blockers.append("DATABASE_QUICK_CHECK_FAILED")
    heartbeat_gap = report.get("heartbeat_max_gap_seconds")
    if heartbeat_gap is None or heartbeat_gap > MAX_HEARTBEAT_GAP_SECONDS:
        blockers.append("HEARTBEAT_CONTINUITY_NOT_PROVED")
    if report.get("security_state") != REQUIRED_SECURITY_STATE:
        blockers.append("SECURITY_BOUNDARY_OPEN")
    if report.get("final_report_from_real_observations") is not True:
        blockers.append("FINAL_REPORT_NOT_FROM_REAL_OBSERVATIONS")
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "blockers": blockers,
    }


def database_summary(database_path: Path) -> dict[str, Any]:
    if not database_path.is_file():
        return _empty_database_summary("missing")
    connection = None
    try:
        connection = sqlite3.connect(
            f"{database_path.resolve(strict=False).as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
        checkpoint_counts = dict(
            connection.execute(
                "SELECT state, COUNT(*) FROM strategy_checkpoint_schedules "
                "GROUP BY state ORDER BY state"
            ).fetchall()
        )
        latest_checkpoint_row = connection.execute(
            "SELECT schedule_key, strategy_id, checkpoint_minutes, state, "
            "blocked_reason, updated_at_ms FROM strategy_checkpoint_schedules "
            "ORDER BY updated_at_ms DESC, schedule_id DESC LIMIT 1"
        ).fetchone()
        incident_status_counts = dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM incidents GROUP BY status ORDER BY status"
            ).fetchall()
        )
        last_incident_row = connection.execute(
            "SELECT status, severity, created_at_ms FROM incidents "
            "ORDER BY created_at_ms DESC, incident_id DESC LIMIT 1"
        ).fetchone()
        market_count = _count(connection, "market_catalog")
        account = _paper_account(connection)
        accounting_violations = _accounting_violations(connection, account)
        return {
            "database_quick_check": quick_check,
            "checkpoint_counts": checkpoint_counts,
            "last_checkpoint": (
                {
                    "schedule_key": latest_checkpoint_row[0],
                    "strategy_id": latest_checkpoint_row[1],
                    "checkpoint_minutes": latest_checkpoint_row[2],
                    "state": latest_checkpoint_row[3],
                    "blocked_reason": latest_checkpoint_row[4],
                    "updated_at_ms": latest_checkpoint_row[5],
                }
                if latest_checkpoint_row
                else None
            ),
            "signals": _count(connection, "signals"),
            "strict_a_evaluations": connection.execute(
                "SELECT COUNT(*) FROM strategy_evaluations "
                "WHERE strategy_id LIKE 'YES_STRICT_A_%'"
            ).fetchone()[0],
            "paper_readiness_checks": _count(
                connection, "paper_execution_readiness"
            ),
            "paper_intents": _count(connection, "paper_intents"),
            "fills": _count(connection, "paper_fills"),
            "paper_positions": _count(connection, "paper_positions"),
            "open_positions": connection.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status <> 'SETTLED'"
            ).fetchone()[0],
            "paper_account": account,
            "incidents": _count(connection, "incidents"),
            "incident_status_counts": incident_status_counts,
            "last_incident": (
                {
                    "status": last_incident_row[0],
                    "severity": last_incident_row[1],
                    "created_at_ms": last_incident_row[2],
                }
                if last_incident_row
                else None
            ),
            "fatal_errors": connection.execute(
                "SELECT COUNT(*) FROM incidents WHERE severity='CRITICAL'"
            ).fetchone()[0],
            "market_rollovers": max(market_count - 1, 0),
            "duplicate_violations": _duplicate_violations(connection),
            "accounting_violations": accounting_violations,
        }
    except sqlite3.Error as error:
        raise ObservationError("OBSERVATION_DATABASE_READ_FAILED") from error
    finally:
        if connection is not None:
            connection.close()


def update_heartbeat(
    state: dict[str, Any], *, observed_monotonic: float
) -> None:
    if (
        type(observed_monotonic) not in (int, float)
        or type(observed_monotonic) is bool
        or observed_monotonic < 0
    ):
        raise ObservationError("INVALID_HEARTBEAT_MONOTONIC")
    previous = state.get("last_heartbeat_monotonic")
    if previous is not None:
        gap = observed_monotonic - previous
        if gap < 0:
            raise ObservationError("MONOTONIC_CLOCK_REGRESSED")
        current_max = state.get("heartbeat_max_gap_seconds")
        state["heartbeat_max_gap_seconds"] = (
            gap if current_max is None else max(current_max, gap)
        )
    elif state.get("heartbeat_max_gap_seconds") is None:
        state["heartbeat_max_gap_seconds"] = 0.0
    state["last_heartbeat_monotonic"] = float(observed_monotonic)
    state["health_snapshots"] += 1


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(50):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)


def append_event(path: Path, *, event: str, payload: dict[str, Any]) -> None:
    record = {
        "event": event,
        "observed_at": utc_now(),
        "payload": payload,
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _paper_account(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT starting_bankroll_usd_micros, cash_usd_micros, "
        "open_cost_basis_usd_micros, equity_usd_micros, "
        "realized_pnl_usd_micros, unrealized_pnl_usd_micros, updated_at_ms "
        "FROM paper_accounts WHERE account_key='default'"
    ).fetchone()
    if row is None:
        return None
    names = (
        "starting_bankroll_usd_micros",
        "cash_usd_micros",
        "open_cost_basis_usd_micros",
        "equity_usd_micros",
        "realized_pnl_usd_micros",
        "unrealized_pnl_usd_micros",
        "updated_at_ms",
    )
    return dict(zip(names, row))


def _accounting_violations(
    connection: sqlite3.Connection, account: dict[str, Any] | None
) -> int:
    if account is None:
        return 1
    violations = int(
        account["equity_usd_micros"]
        != account["cash_usd_micros"]
        + account["open_cost_basis_usd_micros"]
        + account["unrealized_pnl_usd_micros"]
    )
    open_cost = connection.execute(
        "SELECT COALESCE(SUM(cost_basis_usd_micros), 0) FROM paper_positions "
        "WHERE status <> 'SETTLED'"
    ).fetchone()[0]
    realized = connection.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd_micros), 0) FROM paper_positions "
        "WHERE status = 'SETTLED'"
    ).fetchone()[0]
    violations += int(open_cost != account["open_cost_basis_usd_micros"])
    violations += int(realized != account["realized_pnl_usd_micros"])
    return violations


def _duplicate_violations(connection: sqlite3.Connection) -> int:
    queries = (
        "SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM signals GROUP BY identity_key HAVING c > 1)",
        "SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM paper_fills GROUP BY fill_key HAVING c > 1)",
        "SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM paper_intents GROUP BY market_date HAVING c > 1)",
    )
    return sum(connection.execute(query).fetchone()[0] or 0 for query in queries)


def _empty_database_summary(quick_check: str) -> dict[str, Any]:
    return {
        "database_quick_check": quick_check,
        "checkpoint_counts": {},
        "last_checkpoint": None,
        "signals": 0,
        "strict_a_evaluations": 0,
        "paper_readiness_checks": 0,
        "paper_intents": 0,
        "fills": 0,
        "paper_positions": 0,
        "open_positions": 0,
        "paper_account": None,
        "incidents": 0,
        "incident_status_counts": {},
        "last_incident": None,
        "fatal_errors": 0,
        "market_rollovers": 0,
        "duplicate_violations": 0,
        "accounting_violations": 1,
    }
