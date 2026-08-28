"""Failure isolation primitives for independently deployed specialists."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import monotonic
from typing import Callable

from .errors import CircuitOpen


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    state: CircuitState
    consecutive_failures: int
    retry_after_seconds: float


class CircuitBreaker:
    """Stop hammering a failed specialist and allow one timed recovery probe."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_seconds < 0:
            raise ValueError("circuit breaker limits are invalid")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._lock = Lock()

    def before_call(self) -> None:
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return
            elapsed = self._clock() - self._opened_at
            if self._state is CircuitState.OPEN and elapsed >= self.recovery_seconds:
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                return
            if self._state is CircuitState.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return
            retry_after = max(0.0, self.recovery_seconds - elapsed)
            raise CircuitOpen(f"specialist circuit is open; retry after {retry_after:.1f}s")

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._probe_in_flight = False

    def record_unattempted(self) -> None:
        """Hand back a half-open probe for a call that never reached the specialist.

        A refused resource lease says nothing about the specialist's health, so it
        must not count as a failure. The probe slot still has to be returned, or the
        circuit stays half-open with a probe permanently in flight and never closes
        again. Returning to open also restarts the recovery window, so a specialist
        blocked on a busy GPU is retried on a backoff instead of in a hot loop.
        """

        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            retry_after = 0.0
            if self._state is not CircuitState.CLOSED:
                retry_after = max(
                    0.0,
                    self.recovery_seconds - (self._clock() - self._opened_at),
                )
            return CircuitSnapshot(self._state, self._failures, retry_after)
