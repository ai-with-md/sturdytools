"""A thread safe token bucket that works from sync and async code."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

_UNITS = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
}


class RateLimiter:
    """Allow at most ``rate`` calls every ``per`` seconds.

    ``burst`` is how many calls may go through back to back after a quiet
    period. The default of 1 spaces calls evenly, which is the safest choice
    for APIs with strict limits.

    One limiter can be shared by several tools that call the same API.
    """

    def __init__(
        self,
        rate: float,
        per: float = 1.0,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ):
        if rate <= 0 or per <= 0:
            raise ValueError("rate and per must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self.rate = rate
        self.per = per
        self.burst = burst
        self._fill_per_second = rate / per
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._lock = threading.Lock()

    @classmethod
    def parse(cls, spec: str, burst: int = 1) -> "RateLimiter":
        """Build a limiter from text such as ``"5/s"``, ``"60/min"`` or ``"1000/hour"``."""
        try:
            count, unit = spec.replace(" ", "").lower().split("/")
            if unit not in _UNITS and unit.endswith("s"):
                unit = unit[:-1]  # "minutes" -> "minute"
            return cls(float(count), per=_UNITS[unit], burst=burst)
        except (ValueError, KeyError):
            raise ValueError(
                f"could not read rate limit {spec!r}; use a form like '5/s', '60/min' or '1000/hour'"
            ) from None

    def reserve(self) -> float:
        """Take one slot and return how many seconds the caller must wait for it."""
        with self._lock:
            now = self._clock()
            refill = (now - self._last) * self._fill_per_second
            self._tokens = min(float(self.burst), self._tokens + refill)
            self._last = now
            self._tokens -= 1.0
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / self._fill_per_second

    def acquire(self) -> None:
        """Block until a call is allowed."""
        wait = self.reserve()
        if wait > 0:
            time.sleep(wait)

    async def acquire_async(self) -> None:
        """Wait, without blocking the event loop, until a call is allowed."""
        wait = self.reserve()
        if wait > 0:
            await asyncio.sleep(wait)

    def __repr__(self) -> str:
        return f"RateLimiter(rate={self.rate:g}, per={self.per:g}, burst={self.burst})"


def as_limiter(value: Optional[object]) -> Optional[RateLimiter]:
    if value is None or isinstance(value, RateLimiter):
        return value
    if isinstance(value, str):
        return RateLimiter.parse(value)
    raise TypeError("rate_limit must be a string like '5/s' or a RateLimiter")
