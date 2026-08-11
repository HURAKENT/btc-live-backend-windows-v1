from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> None:
    if len(argv) != 6:
        raise SystemExit(2)
    database_path = Path(argv[1])
    project_root = Path(argv[2])
    schedule_key = argv[3]
    payload_path = Path(argv[4])
    expected_payload_hash = argv[5]
    sys.path.insert(0, str(project_root))

    from src.checkpoint_scheduler import CheckpointScheduler
    from src.storage import SqliteStore

    payload = payload_path.read_text(encoding="utf-8")
    if hashlib.sha256(payload.encode()).hexdigest() != expected_payload_hash:
        raise RuntimeError("C10_CRASH_HELPER_PAYLOAD_HASH_MISMATCH")
    store = SqliteStore.open(database_path)
    store.migrate()
    scheduler = CheckpointScheduler(project_root=project_root, store=store)
    due = scheduler.claim_schedule(
        schedule_key,
        origin="LIVE",
        poller_id="c10-crash-worker",
    )
    captured = scheduler.capture(
        due,
        input_payload_json=payload,
        input_snapshot_hash=expected_payload_hash,
        historical_depth_available=False,
    )
    if captured.state != "CAPTURED":
        raise RuntimeError("C10_CRASH_HELPER_CAPTURE_NOT_COMMITTED")
    os._exit(91)


if __name__ == "__main__":
    main(sys.argv)
