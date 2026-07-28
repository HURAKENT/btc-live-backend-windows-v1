from __future__ import annotations

from collections.abc import Callable
from typing import Any


STARTUP_SEQUENCE = (
    "BOOTING",
    "SINGLE_INSTANCE_LOCK",
    "DATABASE_INTEGRITY",
    "SOURCE_DISCOVERY",
    "LIVE_BUFFERING",
    "BINANCE_BACKFILL",
    "POLYMARKET_BACKFILL",
    "RECONCILIATION",
    "STRATEGY_REPLAY",
    "BUFFER_DRAIN",
    "LIVE_READY",
)


class LifecycleStateMachine:
    def __init__(
        self,
        *,
        state_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._state_sink = state_sink
        self._states: list[str] = []
        self._next_index = 0
        self.current_state: str | None = None

    @property
    def states(self) -> tuple[str, ...]:
        return tuple(self._states)

    def transition(self, state: str) -> None:
        if self._next_index >= len(STARTUP_SEQUENCE):
            raise ValueError("STARTUP_SEQUENCE_ALREADY_COMPLETE")
        expected = STARTUP_SEQUENCE[self._next_index]
        if state != expected:
            raise ValueError(
                f"INVALID_STARTUP_TRANSITION: expected={expected} actual={state}"
            )
        self.current_state = state
        self._states.append(state)
        if self._state_sink is not None:
            self._state_sink(
                {
                    "state": state,
                    "sequence_index": self._next_index,
                }
            )
        self._next_index += 1

    def fail_closed(self, detail: str) -> None:
        self.current_state = "RECOVERY_BLOCKED"
        if self._state_sink is not None:
            self._state_sink(
                {
                    "state": "RECOVERY_BLOCKED",
                    "sequence_index": self._next_index,
                    "detail": detail,
                }
            )
