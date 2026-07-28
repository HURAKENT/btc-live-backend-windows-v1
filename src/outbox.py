from __future__ import annotations

from src.models import OutboxEvent, SignalRecord
from src.storage import SqliteStore


class OutboxBroker:
    def __init__(self) -> None:
        self._committed_event_ids: list[int] = []

    @property
    def committed_event_ids(self) -> tuple[int, ...]:
        return tuple(self._committed_event_ids)

    def publish_committed(self, event_id: int) -> None:
        if type(event_id) is not int:
            raise ValueError("INVALID_COMMITTED_EVENT_ID_TYPE")
        if event_id <= 0:
            raise ValueError("INVALID_COMMITTED_EVENT_ID")
        self._committed_event_ids.append(event_id)


def commit_signal_and_outbox(
    store: SqliteStore,
    signal: SignalRecord,
    *,
    topic: str,
    broker: OutboxBroker | None = None,
) -> tuple[int, int]:
    if type(store) is not SqliteStore:
        raise ValueError("INVALID_SQLITE_STORE_TYPE")
    return store.commit_signal_and_outbox(
        signal,
        topic=topic,
        broker=broker,
    )


def read_outbox_after(
    store: SqliteStore,
    event_id: int,
    *,
    limit: int,
) -> list[OutboxEvent]:
    if type(store) is not SqliteStore:
        raise ValueError("INVALID_SQLITE_STORE_TYPE")
    return store.read_outbox_after(event_id, limit)
