from __future__ import annotations

import asyncio
from pathlib import Path

from src.app import LiveBackend, run_backend


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "c0_c1_frozen_config.json"
DATABASE_PATH = PROJECT_ROOT / "data" / "runtime" / "btc_live_backend.sqlite3"


def main() -> int:
    backend = LiveBackend(
        config_path=CONFIG_PATH,
        database_path=DATABASE_PATH,
    )
    return asyncio.run(run_backend(backend))


if __name__ == "__main__":
    raise SystemExit(main())
