from __future__ import annotations

import asyncio

from src.models import OutboxEvent, SignalRecord
from src.storage import SqliteStore


class BrokerSubscription:
    def __init__(
        self,
        broker: OutboxBroker,
        queue: asyncio.Queue[int],
    ) -> None:
        self._broker = broker
        self._queue = queue
        self._closed = False

    async def get(self) -> int:
        if self._closed:
            raise RuntimeError("OUTBOX_SUBSCRIPTION_CLOSED")
        return await self._queue.get()

    def close(self) -> None:
        if not self._closed:
            self._broker._unsubscribe(self._queue)
            self._closed = True


class OutboxBroker:
    def __init__(self) -> None:
        self._committed_event_ids: list[int] = []
        self._subscribers: set[asyncio.Queue[int]] = set()

    @property
    def committed_event_ids(self) -> tuple[int, ...]:
        return tuple(self._committed_event_ids)

    def publish_committed(self, event_id: int) -> None:
        if type(event_id) is not int:
            raise ValueError("INVALID_COMMITTED_EVENT_ID_TYPE")
        if event_id <= 0:
            raise ValueError("INVALID_COMMITTED_EVENT_ID")
        self._committed_event_ids.append(event_id)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event_id)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event_id)

    def subscribe(self, *, max_queue: int = 100) -> BrokerSubscription:
        if type(max_queue) is not int:
            raise ValueError("INVALID_SUBSCRIPTION_LIMIT_TYPE")
        if not 1 <= max_queue <= 1000:
            raise ValueError("INVALID_SUBSCRIPTION_LIMIT")
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=max_queue)
        self._subscribers.add(queue)
        return BrokerSubscription(self, queue)

    def _unsubscribe(self, queue: asyncio.Queue[int]) -> None:
        self._subscribers.discard(queue)


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
