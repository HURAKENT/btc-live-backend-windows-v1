from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.windows_operations import (
    DatabaseOperationsError,
    create_online_backup,
    restore_backup_to_copy,
    validate_database_copy,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="C9 SQLite operations")
    commands = parser.add_subparsers(dest="operation", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("source")
    backup.add_argument("destination")
    validate = commands.add_parser("validate")
    validate.add_argument("source")
    restore = commands.add_parser("restore-drill")
    restore.add_argument("source")
    restore.add_argument("destination")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _build_parser().parse_args(argv)
        source = _absolute_path(arguments.source)
        if arguments.operation == "backup":
            report = create_online_backup(
                source,
                _absolute_path(arguments.destination),
            )
        elif arguments.operation == "validate":
            report = validate_database_copy(source)
        else:
            report = restore_backup_to_copy(
                source,
                _absolute_path(arguments.destination),
            )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except (DatabaseOperationsError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 40


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise DatabaseOperationsError("DATABASE_PATH_NOT_ABSOLUTE")
    return path.resolve(strict=False)


if __name__ == "__main__":
    raise SystemExit(main())
