from __future__ import annotations

import argparse
import asyncio
import sys
from functools import partial
from pathlib import Path

from src.app import BackendRuntime, LiveBackend, run_backend
from src.integration_endpoints import load_integration_endpoints
from src.runtime_orchestrator import build_default_runtime_orchestrator
from src.runtime_paths import (
    RuntimePathError,
    derive_runtime_paths,
    resolve_data_root,
    resolve_database_path as _resolve_database_path,
    resolve_runtime_paths,
)


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "mvp_runtime_v1.json"
DATABASE_PATH = derive_runtime_paths(resolve_data_root(environ={})).database_path
DEFAULT_DATABASE_PATH = DATABASE_PATH

DatabasePathError = RuntimePathError


class _ConfigArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DatabasePathError(f"INVALID_RUNTIME_ARGUMENT: {message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ConfigArgumentParser(add_help=True)
    parser.add_argument("--data-root")
    parser.add_argument("--database-path")
    parser.add_argument("--integration-test-mode", action="store_true")
    parser.add_argument("--integration-endpoints")
    return parser


def resolve_database_path(value: str | None) -> Path:
    if value is None:
        return DEFAULT_DATABASE_PATH
    return _resolve_database_path(value, project_root=PROJECT_ROOT)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _build_parser().parse_args(argv)
        paths = resolve_runtime_paths(
            data_root=arguments.data_root,
            database_path=arguments.database_path,
            project_root=PROJECT_ROOT,
        )
        database_path = Path(paths.database_path)
        if arguments.integration_test_mode != (
            arguments.integration_endpoints is not None
        ):
            raise DatabasePathError("INVALID_INTEGRATION_MODE_ARGUMENTS")
        endpoints = None
        if arguments.integration_test_mode:
            endpoint_path = Path(arguments.integration_endpoints)
            if not endpoint_path.is_absolute():
                raise DatabasePathError(
                    "INTEGRATION_ENDPOINTS_PATH_NOT_ABSOLUTE"
                )
            endpoints = load_integration_endpoints(endpoint_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
    except (DatabasePathError, RuntimePathError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 30

    runtime = None
    if endpoints is not None:
        runtime = BackendRuntime(
            orchestrator_factory=partial(
                build_default_runtime_orchestrator,
                integration_endpoints=endpoints,
            ),
            api_bind_override=(
                endpoints.api_bind_host,
                endpoints.api_bind_port,
            ),
        )
    backend_kwargs = {
        "config_path": CONFIG_PATH,
        "database_path": database_path,
    }
    if runtime is not None:
        backend_kwargs["runtime"] = runtime
    backend = LiveBackend(**backend_kwargs)
    return asyncio.run(run_backend(backend))


if __name__ == "__main__":
    raise SystemExit(main())
