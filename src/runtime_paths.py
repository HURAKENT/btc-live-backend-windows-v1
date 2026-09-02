from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Mapping


DATA_ROOT_ENVIRONMENT_VARIABLE = "BTC_DAILY_RANGE_DATA_ROOT"
DATABASE_FILENAME = "btc_daily_range.sqlite3"


class RuntimePathError(ValueError):
    """A stable, user-facing runtime path configuration failure."""


@dataclass(frozen=True)
class RuntimePaths:
    data_root: PurePath
    runtime_root: PurePath
    database_path: PurePath
    backup_root: PurePath
    log_root: PurePath
    diagnostics_root: PurePath


def _is_windows(platform_name: str) -> bool:
    return platform_name == "nt" or platform_name.startswith("win")


def _path_class(platform_name: str):
    if _is_windows(platform_name):
        return Path if os.name == "nt" else PureWindowsPath
    return Path if os.name != "nt" else PurePosixPath


def _normalize_absolute_data_root(
    value: str | PurePath,
    *,
    platform_name: str,
) -> PurePath:
    if not isinstance(value, (str, PurePath)) or not str(value):
        raise RuntimePathError("INVALID_DATA_ROOT_TYPE")
    path_class = _path_class(platform_name)
    requested = path_class(str(value))
    if _is_windows(platform_name):
        windows_path = PureWindowsPath(str(value))
        if windows_path.drive.startswith("\\\\"):
            raise RuntimePathError("DATA_ROOT_UNC_NOT_SUPPORTED")
    if not requested.is_absolute():
        raise RuntimePathError("DATA_ROOT_NOT_ABSOLUTE")
    if isinstance(requested, Path):
        return requested.resolve(strict=False)
    return requested


def resolve_data_root(
    explicit_data_root: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: PurePath | None = None,
) -> PurePath:
    """Resolve CLI, environment, then OS default without filesystem writes."""

    environment = os.environ if environ is None else environ
    selected_platform = sys.platform if platform_name is None else platform_name
    if explicit_data_root is not None:
        return _normalize_absolute_data_root(
            explicit_data_root,
            platform_name=selected_platform,
        )
    if DATA_ROOT_ENVIRONMENT_VARIABLE in environment:
        return _normalize_absolute_data_root(
            environment[DATA_ROOT_ENVIRONMENT_VARIABLE],
            platform_name=selected_platform,
        )

    path_class = _path_class(selected_platform)
    selected_home = path_class(str(Path.home() if home is None else home))
    if _is_windows(selected_platform):
        default_root = selected_home / "Documents" / "BTC Daily Range"
    else:
        xdg_value = environment.get("XDG_DATA_HOME")
        xdg_root = path_class(xdg_value) if xdg_value else None
        default_root = (
            xdg_root / "btc_daily_range"
            if xdg_root is not None and xdg_root.is_absolute()
            else selected_home / ".local" / "share" / "btc_daily_range"
        )
    return _normalize_absolute_data_root(
        default_root,
        platform_name=selected_platform,
    )


def derive_runtime_paths(data_root: PurePath) -> RuntimePaths:
    runtime_root = data_root / "runtime"
    return RuntimePaths(
        data_root=data_root,
        runtime_root=runtime_root,
        database_path=runtime_root / DATABASE_FILENAME,
        backup_root=data_root / "backups",
        log_root=data_root / "logs",
        diagnostics_root=data_root / "diagnostics",
    )


def resolve_database_path(
    value: str,
    *,
    project_root: Path | None = None,
) -> Path:
    if type(value) is not str or not value:
        raise RuntimePathError("INVALID_DATABASE_PATH_TYPE")
    requested = Path(value)
    if not requested.is_absolute():
        raise RuntimePathError("DATABASE_PATH_NOT_ABSOLUTE")
    resolved = requested.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise RuntimePathError("DATABASE_PATH_IS_FILESYSTEM_ROOT")
    if project_root is not None and resolved == project_root.resolve(strict=False):
        raise RuntimePathError("DATABASE_PATH_IS_REPOSITORY_ROOT")
    if not resolved.name:
        raise RuntimePathError("DATABASE_PATH_FILENAME_EMPTY")
    if resolved.suffix != ".sqlite3":
        raise RuntimePathError("DATABASE_PATH_SUFFIX")
    if resolved.exists() and resolved.is_dir():
        raise RuntimePathError("DATABASE_PATH_IS_DIRECTORY")
    return resolved


def resolve_runtime_paths(
    *,
    data_root: str | None = None,
    database_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: PurePath | None = None,
    project_root: Path | None = None,
) -> RuntimePaths:
    environment = os.environ if environ is None else environ
    has_configured_root = (
        data_root is not None
        or DATA_ROOT_ENVIRONMENT_VARIABLE in environment
    )
    root = resolve_data_root(
        data_root,
        environ=environment,
        platform_name=platform_name,
        home=home,
    )
    paths = derive_runtime_paths(root)
    if database_path is None:
        return paths

    legacy_database_path = resolve_database_path(
        database_path,
        project_root=project_root,
    )
    if has_configured_root and legacy_database_path != paths.database_path:
        raise RuntimePathError("DATA_ROOT_DATABASE_PATH_CONFLICT")
    return replace(paths, database_path=legacy_database_path)
