from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from functools import partial
from logging.handlers import RotatingFileHandler
from pathlib import Path

from run_backend import CONFIG_PATH, DatabasePathError
from src.app import BackendRuntime, LiveBackend, run_backend
from src.integration_endpoints import load_integration_endpoints
from src.runtime_orchestrator import build_default_runtime_orchestrator
from src.runtime_paths import (
    derive_runtime_paths,
    resolve_data_root,
    resolve_runtime_paths,
)
from src.windows_operations import StartupValidatedSqliteStore
from src.performance_engine import bootstrap_historical_performance
from src.performance_repository import PerformanceRepository


PROJECT_ROOT = Path(__file__).resolve().parent
_DEFAULT_RUNTIME_PATHS = derive_runtime_paths(resolve_data_root(environ={}))
DEFAULT_DATABASE_PATH = _DEFAULT_RUNTIME_PATHS.database_path
DEFAULT_LOG_PATH = _DEFAULT_RUNTIME_PATHS.log_root / "backend.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
_LOGGER_NAME = "btc_live_backend.windows"
_EXIT_REASONS = {
    1: "UNEXPECTED_FAILURE",
    20: "BACKEND_ALREADY_RUNNING",
    30: "BACKEND_CONFIG_FAILURE",
    40: "BACKEND_DATABASE_INTEGRITY_FAILURE",
}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DatabasePathError(f"INVALID_RUNTIME_ARGUMENT: {message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(add_help=True)
    parser.add_argument("--data-root")
    parser.add_argument("--database-path")
    parser.add_argument("--log-path")
    parser.add_argument("--integration-test-mode", action="store_true")
    parser.add_argument("--integration-endpoints")
    return parser


def configure_backend_logger(
    log_path: Path,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> logging.Logger:
    if not isinstance(log_path, Path):
        raise ValueError("INVALID_LOG_PATH_TYPE")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("INVALID_LOG_MAX_BYTES")
    if type(backup_count) is not int or backup_count < 1:
        raise ValueError("INVALID_LOG_BACKUP_COUNT")
    close_backend_logger()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s%(msecs)03d %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S.",
        )
    )
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def close_backend_logger() -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.flush()
        handler.close()


def bootstrap_strategy_performance(store) -> None:
    bootstrap_historical_performance(
        project_root=PROJECT_ROOT,
        repository=PerformanceRepository(store),
        as_of_date=datetime.now(timezone.utc).date(),
    )


def main(
    argv: list[str] | None = None,
    *,
    backend_factory=LiveBackend,
    backend_runner=run_backend,
) -> int:
    logger = None
    try:
        arguments = _build_parser().parse_args(argv)
        root_paths = derive_runtime_paths(resolve_data_root(arguments.data_root))
        log_path = (
            Path(arguments.log_path).resolve(strict=False)
            if arguments.log_path is not None
            else Path(root_paths.log_root / "backend.log")
        )
        logger = configure_backend_logger(log_path)
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
        runtime = BackendRuntime(performance_bootstrap=bootstrap_strategy_performance)
        if arguments.integration_test_mode:
            endpoint_path = Path(arguments.integration_endpoints)
            if not endpoint_path.is_absolute():
                raise DatabasePathError(
                    "INTEGRATION_ENDPOINTS_PATH_NOT_ABSOLUTE"
                )
            endpoints = load_integration_endpoints(endpoint_path)
            runtime = BackendRuntime(
                orchestrator_factory=partial(
                    build_default_runtime_orchestrator,
                    integration_endpoints=endpoints,
                ),
                api_bind_override=(
                    endpoints.api_bind_host,
                    endpoints.api_bind_port,
                ),
                performance_bootstrap=None,
            )

        database_path.parent.mkdir(parents=True, exist_ok=True)

        backend_kwargs = {
            "config_path": CONFIG_PATH,
            "database_path": database_path,
            "store_factory": StartupValidatedSqliteStore.open,
        }
        backend_kwargs["runtime"] = runtime
        logger.info(
            "backend startup requested database=%s",
            database_path,
        )
        backend = backend_factory(**backend_kwargs)
        exit_code = asyncio.run(backend_runner(backend))
        if exit_code == 0:
            logger.info("backend clean shutdown exit_code=0")
        else:
            logger.error(
                "backend stopped with exit_code=%d reason=%s",
                exit_code,
                _EXIT_REASONS.get(exit_code, "UNKNOWN_NON_CLEAN_EXIT"),
            )
        return exit_code
    except (DatabasePathError, OSError, ValueError) as error:
        if logger is not None:
            logger.error("backend configuration failure: %s", error)
        else:
            print(str(error), file=sys.stderr)
        return 30
    except BaseException:
        if logger is not None:
            logger.exception("backend launcher unexpected failure")
        return 1
    finally:
        close_backend_logger()


if __name__ == "__main__":
    raise SystemExit(main())
