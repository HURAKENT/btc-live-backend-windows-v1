from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in (None, ""):
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    _PROJECT_ROOT_TEXT = str(_PROJECT_ROOT)
    if _PROJECT_ROOT_TEXT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT_TEXT)

from src.strategy_c5_activation import (
    verify_c5_activation,
    write_c5_acceptance_outputs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic C5 activation acceptance outputs."
    )
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--verified-at-utc", required=True)
    args = parser.parse_args(argv)
    report = verify_c5_activation(args.project_root.resolve())
    write_c5_acceptance_outputs(
        report,
        report_path=args.report.resolve(),
        matrix_path=args.matrix.resolve(),
        source_commit=args.source_commit,
        verified_at_utc=args.verified_at_utc,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
