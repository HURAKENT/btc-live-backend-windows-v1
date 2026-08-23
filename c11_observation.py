from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.c11_observation import (
    ObservationError,
    append_event,
    build_report_skeleton,
    config_identity,
    database_summary,
    evaluate_future_acceptance,
    generate_run_id,
    host_runtime_summary,
    update_heartbeat,
    utc_now,
    write_json_atomic,
)


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "mvp_runtime_v1.json"
DEFAULT_OBSERVATION_ROOT = PROJECT_ROOT / "data" / "observations"
DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT / "data" / "runtime" / "btc_live_backend.sqlite3"
)
STATE_NAME = "state.json"
REPORT_NAME = "observation_report.json"
EVENTS_NAME = "observation.jsonl"
READY_NAME = "supervisor.ready"
STOP_NAME = "stop.request"
RESTART_NAME = "restart.request"
_LOCAL_SUPERVISORS: list[subprocess.Popen] = []


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ObservationError(f"INVALID_OBSERVATION_ARGUMENT: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="C11 durable observation controller")
    commands = parser.add_subparsers(dest="operation", required=True)
    start = commands.add_parser("start")
    start.add_argument("--observation-root", default=str(DEFAULT_OBSERVATION_ROOT))
    start.add_argument("--database-path", default=str(DEFAULT_DATABASE_PATH))
    start.add_argument("--integration-endpoints")
    start.add_argument("--poll-seconds", type=float, default=60.0)
    for name in ("status", "restart", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True)
        if name in ("restart", "stop"):
            command.add_argument("--timeout-seconds", type=float, default=45.0)
    supervise = commands.add_parser("_supervise")
    supervise.add_argument("--run-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.operation == "start":
            result = _start(arguments)
        elif arguments.operation == "status":
            result = _status(_absolute_run_dir(arguments.run_dir))
        elif arguments.operation == "restart":
            result = _request_and_wait(
                _absolute_run_dir(arguments.run_dir),
                request_name=RESTART_NAME,
                timeout_seconds=arguments.timeout_seconds,
            )
        elif arguments.operation == "stop":
            result = _request_and_wait(
                _absolute_run_dir(arguments.run_dir),
                request_name=STOP_NAME,
                timeout_seconds=arguments.timeout_seconds,
            )
        else:
            _supervise(_absolute_run_dir(arguments.run_dir))
            return 0
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (ObservationError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 50


def _start(arguments) -> dict[str, Any]:
    if os.name != "nt":
        raise ObservationError("C11_WINDOWS_REQUIRED")
    if arguments.poll_seconds < 1 or arguments.poll_seconds > 300:
        raise ObservationError("INVALID_OBSERVATION_POLL_SECONDS")
    observation_root = _absolute_path(arguments.observation_root)
    database_path = _absolute_path(arguments.database_path)
    endpoints_path = (
        _absolute_path(arguments.integration_endpoints)
        if arguments.integration_endpoints
        else None
    )
    integration_commit = _git_head()
    started_at_ns = time.time_ns()
    started_at = datetime.fromtimestamp(
        started_at_ns / 1_000_000_000, tz=UTC
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    run_id = generate_run_id(
        started_at_ns=started_at_ns,
        integration_commit=integration_commit,
        database_path=database_path,
    )
    run_dir = observation_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "backend.log"
    identity, security = config_identity(CONFIG_PATH)
    backend_arguments = [
        sys.executable,
        str(PROJECT_ROOT / "run_windows_backend.py"),
        "--database-path",
        str(database_path),
        "--log-path",
        str(log_path),
    ]
    api_host = "127.0.0.1"
    api_port = 8767
    if endpoints_path is not None:
        endpoint_payload = json.loads(endpoints_path.read_text(encoding="utf-8"))
        api_host = endpoint_payload["api_bind_host"]
        api_port = endpoint_payload["api_bind_port"]
        backend_arguments.extend(
            [
                "--integration-test-mode",
                "--integration-endpoints",
                str(endpoints_path),
            ]
        )
    state = {
        "schema_version": "C11_OBSERVATION_STATE_V1",
        "run_id": run_id,
        "started_at": started_at,
        "started_at_ns": started_at_ns,
        "started_monotonic": time.monotonic(),
        "integration_commit": integration_commit,
        "config_identity": identity,
        "database_path": str(database_path),
        "log_path": str(log_path),
        "events_path": str(run_dir / EVENTS_NAME),
        "report_path": str(run_dir / REPORT_NAME),
        "backend_launcher": str(PROJECT_ROOT / "run_windows_backend.py"),
        "backend_arguments": backend_arguments,
        "api_url": f"http://{api_host}:{api_port}",
        "host_runtime": host_runtime_summary(),
        "security_state": security,
        "poll_seconds": arguments.poll_seconds,
        "controller_status": "STARTING",
        "supervisor_pid": None,
        "backend_pid": None,
        "backend_alive": False,
        "process_start_count": 0,
        "process_restarts": 0,
        "pending_restart_recovery": False,
        "source_outages": 0,
        "pending_source_recovery": False,
        "recoveries": 0,
        "health_snapshots": 0,
        "last_heartbeat_monotonic": None,
        "heartbeat_max_gap_seconds": None,
        "last_source_health": {},
        "last_snapshot": None,
        "fatal_controller_errors": [],
    }
    write_json_atomic(run_dir / STATE_NAME, state)
    write_json_atomic(
        run_dir / REPORT_NAME,
        build_report_skeleton(
            run_id=run_id,
            started_at=started_at,
            integration_commit=integration_commit,
            config_identity=identity,
            database_path=database_path,
            log_path=log_path,
            host_runtime=state["host_runtime"],
            security_state=security,
        ),
    )
    append_event(
        run_dir / EVENTS_NAME,
        event="observation.started",
        payload={
            "integration_commit": integration_commit,
            "run_id": run_id,
        },
    )
    controller_log = (run_dir / "controller.log").open(
        "a", encoding="utf-8", newline="\n"
    )
    supervisor = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_supervise",
            "--run-dir",
            str(run_dir),
        ],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=controller_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    _LOCAL_SUPERVISORS.append(supervisor)
    controller_log.close()
    state["supervisor_pid"] = supervisor.pid
    write_json_atomic(run_dir / STATE_NAME, state)
    (run_dir / READY_NAME).write_text("ready\n", encoding="ascii")
    return {
        "status": "STARTED",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "started_at": started_at,
        "integration_commit": integration_commit,
    }


def _supervise(run_dir: Path) -> None:
    ready = run_dir / READY_NAME
    deadline = time.monotonic() + 15
    while not ready.exists():
        if time.monotonic() >= deadline:
            raise ObservationError("SUPERVISOR_READY_TIMEOUT")
        time.sleep(0.05)
    state = _read_state(run_dir)
    backend = None
    try:
        backend = _launch_backend(run_dir, state)
        while True:
            loop_started = time.monotonic()
            state = _read_state(run_dir)
            if (run_dir / STOP_NAME).exists():
                _clean_stop_backend(run_dir, state, backend)
                backend = None
                _finalize(run_dir, state)
                return
            if (run_dir / RESTART_NAME).exists():
                _clean_stop_backend(run_dir, state, backend)
                (run_dir / RESTART_NAME).unlink(missing_ok=True)
                state = _read_state(run_dir)
                state["process_restarts"] += 1
                state["pending_restart_recovery"] = True
                state["last_source_health"] = {}
                state["last_snapshot"] = None
                write_json_atomic(run_dir / STATE_NAME, state)
                append_event(
                    run_dir / EVENTS_NAME,
                    event="backend.restart_requested",
                    payload={"process_restarts": state["process_restarts"]},
                )
                backend = _launch_backend(run_dir, state)
            if backend.poll() is not None:
                state["backend_alive"] = False
                state["controller_status"] = "BLOCKED"
                state["fatal_controller_errors"].append(
                    f"BACKEND_EXITED:{backend.returncode}"
                )
                write_json_atomic(run_dir / STATE_NAME, state)
                append_event(
                    run_dir / EVENTS_NAME,
                    event="backend.unexpected_exit",
                    payload={"exit_code": backend.returncode},
                )
                _finalize(run_dir, state)
                return
            _capture_snapshot(run_dir, state)
            delay = max(0.05, state["poll_seconds"] - (time.monotonic() - loop_started))
            time.sleep(delay)
    except BaseException as error:
        state = _read_state(run_dir)
        state["controller_status"] = "BLOCKED"
        state["fatal_controller_errors"].append(type(error).__name__)
        write_json_atomic(run_dir / STATE_NAME, state)
        append_event(
            run_dir / EVENTS_NAME,
            event="observer.failure",
            payload={"error_type": type(error).__name__},
        )
        if backend is not None and backend.poll() is None:
            with _suppress_errors():
                _clean_stop_backend(run_dir, state, backend)
        _finalize(run_dir, state)
        raise


def _launch_backend(run_dir: Path, state: dict[str, Any]):
    backend_log = (run_dir / "backend.console.log").open(
        "a", encoding="utf-8", newline="\n"
    )
    process = subprocess.Popen(
        state["backend_arguments"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    backend_log.close()
    try:
        state["backend_pid"] = process.pid
        state["backend_alive"] = True
        state["process_start_count"] += 1
        state["controller_status"] = "RUNNING"
        write_json_atomic(run_dir / STATE_NAME, state)
        append_event(
            run_dir / EVENTS_NAME,
            event="backend.started",
            payload={
                "backend_pid": process.pid,
                "process_start_count": state["process_start_count"],
            },
        )
        return process
    except BaseException:
        if process.poll() is None:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        raise


def _clean_stop_backend(run_dir: Path, state: dict[str, Any], backend) -> None:
    if backend.poll() is None:
        backend.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            backend.wait(timeout=30)
        except subprocess.TimeoutExpired as error:
            state["fatal_controller_errors"].append("BACKEND_CLEAN_STOP_TIMEOUT")
            write_json_atomic(run_dir / STATE_NAME, state)
            raise ObservationError("BACKEND_CLEAN_STOP_TIMEOUT") from error
    if backend.returncode != 0:
        state["fatal_controller_errors"].append(
            f"BACKEND_NON_CLEAN_EXIT:{backend.returncode}"
        )
        write_json_atomic(run_dir / STATE_NAME, state)
        raise ObservationError("BACKEND_NON_CLEAN_EXIT")
    state["backend_alive"] = False
    state["backend_pid"] = None
    write_json_atomic(run_dir / STATE_NAME, state)
    append_event(
        run_dir / EVENTS_NAME,
        event="backend.clean_stop",
        payload={"exit_code": backend.returncode},
    )


def _capture_snapshot(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    bootstrap = _fetch_bootstrap(state["api_url"])
    source_health = (
        bootstrap.get("health", {}).get("source_health", {}) if bootstrap else {}
    )
    previous = state.get("last_source_health", {})
    if previous and any(
        previous.get(source) == "LIVE" and source_health.get(source) != "LIVE"
        for source in ("binance", "polymarket")
    ):
        state["source_outages"] += 1
        state["pending_source_recovery"] = True
        append_event(
            run_dir / EVENTS_NAME,
            event="source.outage",
            payload={"source_health": source_health},
        )
    if state.get("pending_source_recovery") is True and previous and any(
        previous.get(source) != "LIVE" and source_health.get(source) == "LIVE"
        for source in ("binance", "polymarket")
    ):
        state["pending_source_recovery"] = False
        state["recoveries"] += 1
        append_event(
            run_dir / EVENTS_NAME,
            event="source.recovered",
            payload={"source_health": source_health},
        )
    if source_health:
        state["last_source_health"] = source_health
    if (
        state.get("pending_restart_recovery") is True
        and bootstrap
        and bootstrap.get("health", {}).get("status") == "PASS"
    ):
        state["pending_restart_recovery"] = False
        state["recoveries"] += 1
        append_event(
            run_dir / EVENTS_NAME,
            event="backend.restart_recovered",
            payload={"process_restarts": state["process_restarts"]},
        )
    database = database_summary(Path(state["database_path"]))
    now = utc_now()
    observed_monotonic = time.monotonic()
    update_heartbeat(state, observed_monotonic=observed_monotonic)
    current = bootstrap.get("current_market_identity") if bootstrap else None
    readiness = bootstrap.get("execution_readiness") if bootstrap else None
    snapshot = {
        "run_id": state["run_id"],
        "observed_at": now,
        "elapsed_seconds": max(
            0.0, observed_monotonic - state["started_monotonic"]
        ),
        "backend_alive": state["backend_alive"],
        "health_status": (
            bootstrap.get("health", {}).get("status") if bootstrap else "UNAVAILABLE"
        ),
        "current_market": current,
        "source_health": source_health,
        "last_checkpoint": database["last_checkpoint"],
        "paper_account": database["paper_account"],
        "open_positions": database["open_positions"],
        "incident_count": database["incidents"],
        "last_error_or_block_reason": (
            (readiness or {}).get("reason_code")
            or (database["last_incident"] or {}).get("status")
            or (bootstrap.get("health", {}).get("runtime_readiness", {}).get("failure") if bootstrap else None)
        ),
    }
    state["last_snapshot"] = snapshot
    write_json_atomic(run_dir / STATE_NAME, state)
    append_event(run_dir / EVENTS_NAME, event="health.snapshot", payload=snapshot)
    return snapshot


def _status(run_dir: Path) -> dict[str, Any]:
    state = _read_state(run_dir)
    snapshot = {
        "health_status": "STARTING",
        "current_market": None,
        "source_health": {},
        "last_checkpoint": None,
        "paper_account": None,
        "open_positions": 0,
        "incident_count": 0,
        "last_error_or_block_reason": None,
    }
    snapshot.update(state.get("last_snapshot") or {})
    snapshot.update(
        {
            "run_id": state["run_id"],
            "elapsed_seconds": max(
                0.0,
                time.monotonic() - state["started_monotonic"],
            ),
            "backend_alive": bool(state.get("backend_alive"))
            and _pid_alive(state.get("backend_pid")),
            "controller_status": state["controller_status"],
            "process_restarts": state["process_restarts"],
            "source_outages": state["source_outages"],
            "recoveries": state["recoveries"],
        }
    )
    return snapshot


def _request_and_wait(
    run_dir: Path, *, request_name: str, timeout_seconds: float
) -> dict[str, Any]:
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise ObservationError("INVALID_OBSERVATION_TIMEOUT")
    before = _read_state(run_dir)
    if request_name == STOP_NAME and before["controller_status"] == "FINALIZED":
        return _status(run_dir)
    if request_name == RESTART_NAME and before["controller_status"] != "RUNNING":
        raise ObservationError("OBSERVATION_NOT_RUNNING")
    (run_dir / request_name).write_text(utc_now() + "\n", encoding="ascii")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = _read_state(run_dir)
        if request_name == STOP_NAME and current["controller_status"] == "FINALIZED":
            _reap_local_supervisors()
            return _status(run_dir)
        if (
            request_name == RESTART_NAME
            and current["process_restarts"] > before["process_restarts"]
            and current["backend_alive"]
            and current.get("pending_restart_recovery") is False
        ):
            return _status(run_dir)
    raise ObservationError("OBSERVATION_CONTROL_TIMEOUT")


def _finalize(run_dir: Path, state: dict[str, Any]) -> None:
    state = _read_state(run_dir)
    database = database_summary(Path(state["database_path"]))
    ended_at = utc_now()
    elapsed_seconds = max(0.0, time.monotonic() - state["started_monotonic"])
    report = json.loads((run_dir / REPORT_NAME).read_text(encoding="utf-8"))
    report.update(database)
    report.update(
        {
            "ended_at": ended_at,
            "elapsed_seconds": elapsed_seconds,
            "actual_duration_hours": elapsed_seconds / 3600,
            "process_restarts": state["process_restarts"],
            "source_outages": state["source_outages"],
            "recoveries": state["recoveries"],
            "health_snapshots": state["health_snapshots"],
            "heartbeat_max_gap_seconds": state["heartbeat_max_gap_seconds"],
            "fatal_errors": database["fatal_errors"]
            + len(state["fatal_controller_errors"]),
            "final_report_from_real_observations": True,
        }
    )
    report.update(evaluate_future_acceptance(report))
    write_json_atomic(run_dir / REPORT_NAME, report)
    state["controller_status"] = "FINALIZED"
    state["backend_alive"] = False
    state["backend_pid"] = None
    state["ended_at"] = ended_at
    write_json_atomic(run_dir / STATE_NAME, state)
    append_event(
        run_dir / EVENTS_NAME,
        event="observation.finalized",
        payload={"blockers": report["blockers"], "status": report["status"]},
    )
    (run_dir / STOP_NAME).unlink(missing_ok=True)


def _fetch_bootstrap(api_url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            api_url + "/api/v1/bootstrap", timeout=2
        ) as response:
            payload = json.load(response)
        return payload if type(payload) is dict else None
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _read_state(run_dir: Path) -> dict[str, Any]:
    try:
        state = json.loads((run_dir / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ObservationError("OBSERVATION_STATE_UNREADABLE") from error
    if type(state) is not dict or state.get("schema_version") != "C11_OBSERVATION_STATE_V1":
        raise ObservationError("OBSERVATION_STATE_INVALID")
    return state


def _absolute_run_dir(value: str) -> Path:
    path = _absolute_path(value)
    if not path.is_dir():
        raise ObservationError("OBSERVATION_RUN_NOT_FOUND")
    return path


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ObservationError("OBSERVATION_PATH_NOT_ABSOLUTE")
    return path.resolve(strict=False)


def _git_head() -> str:
    commands = [["git", "rev-parse", "HEAD"]]
    git_pointer = PROJECT_ROOT / ".git"
    if git_pointer.is_file():
        try:
            pointer = git_pointer.read_text(encoding="utf-8").strip()
        except OSError:
            pointer = ""
        if pointer.startswith("gitdir: "):
            git_dir = pointer.removeprefix("gitdir: ")
            if (
                os.name == "nt"
                and git_dir.startswith("/mnt/")
                and len(git_dir) > 7
                and git_dir[5].isalpha()
                and git_dir[6] == "/"
            ):
                git_dir = (
                    git_dir[5].upper()
                    + ":\\"
                    + git_dir[7:].replace("/", "\\")
                )
            commands.insert(
                0,
                [
                    "git",
                    f"--git-dir={git_dir}",
                    f"--work-tree={PROJECT_ROOT}",
                    "rev-parse",
                    "HEAD",
                ],
            )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        commit = result.stdout.strip()
        if result.returncode == 0 and len(commit) == 40:
            return commit
    raise ObservationError("INTEGRATION_COMMIT_UNAVAILABLE")


def _pid_alive(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reap_local_supervisors() -> None:
    for process in tuple(_LOCAL_SUPERVISORS):
        process.poll()
        if process.returncode is not None:
            _LOCAL_SUPERVISORS.remove(process)


class _suppress_errors:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
