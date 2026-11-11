"""A small circuit breaker.

After ``failure_threshold`` failures in a row the breaker opens and calls are
rejected right away, without touching the failing service. Once
``reset_after`` seconds have passed, a single trial call is let through. If it
works the breaker closes again; if it fails the breaker stays open for another
``reset_after`` seconds.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .errors import CircuitOpenError

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        reset_after: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if reset_after <= 0:
            raise ValueError("reset_after must be positive")
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    @property
    def state(self) -> str:
        return self._state

    def before_call(self) -> None:
        """Raise CircuitOpenError if the call should not go through."""
        with self._lock:
            if self._state == CLOSED:
                return
            waited = self._clock() - self._opened_at
            if self._state == OPEN and waited >= self.reset_after:
                self._state = HALF_OPEN
            if self._state == HALF_OPEN and not self._trial_running:
                self._trial_running = True
                return
            raise CircuitOpenError(retry_in=max(0.0, self.reset_after - waited))

    def record_success(self) -> None:
        with self._lock:
            self._state = CLOSED
            self._failures = 0
            self._trial_running = False

    def record_failure(self) -> None:
        with self._lock:
            self._trial_running = False
            if self._state == HALF_OPEN:
                self._open()
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._open()

    def reset(self) -> None:
        """Close the breaker by hand, for example after fixing the service."""
        self.record_success()

    def _open(self) -> None:
        self._state = OPEN
        self._opened_at = self._clock()

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(state={self._state!r}, failures={self._failures}, "
            f"failure_threshold={self.failure_threshold}, reset_after={self.reset_after:g})"
        )
