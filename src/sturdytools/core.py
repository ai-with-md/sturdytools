"""The ``sturdy`` decorator."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import random
import threading
import time
from typing import Any, Callable, Optional, Tuple, Type, Union

from .breaker import CircuitBreaker
from .errors import CircuitOpenError, ToolTimeout
from .ratelimit import RateLimiter, as_limiter

logger = logging.getLogger("sturdytools")

_MISSING = object()

ErrorTypes = Union[Type[BaseException], Tuple[Type[BaseException], ...]]
OnError = Union[str, Callable[[str, BaseException, int], Any]]


def error_message(tool_name: str, exc: BaseException, attempts: int) -> str:
    """Turn a failure into a short message a language model can act on."""
    if isinstance(exc, CircuitOpenError):
        return (
            f"Error: the tool '{tool_name}' is paused after repeated failures. "
            f"It can be tried again in about {exc.retry_in:.0f} seconds. "
            "Use another approach or tell the user it is unavailable."
        )
    tries = f"{attempts} attempt" + ("" if attempts == 1 else "s")
    return (
        f"Error: the tool '{tool_name}' failed after {tries} "
        f"({type(exc).__name__}: {exc}). "
        "Check the arguments, try a different approach, or tell the user it is unavailable."
    )


class _Policy:
    """Everything the sync and async wrappers share."""

    def __init__(
        self,
        name: str,
        retries: int,
        retry_on: ErrorTypes,
        backoff: float,
        max_backoff: float,
        fallback: Any,
        on_error: OnError,
        on_retry: Optional[Callable[[int, BaseException, float], Any]],
    ):
        self.name = name
        self.retries = retries
        self.retry_on = retry_on
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.fallback = fallback
        self.on_error = on_error
        self.on_retry = on_retry

    def is_transient(self, exc: BaseException) -> bool:
        return isinstance(exc, self.retry_on)

    def retry_delay(self, attempt: int, exc: BaseException) -> Optional[float]:
        """Seconds to wait before the next attempt, or None to stop."""
        if attempt > self.retries or not self.is_transient(exc):
            return None
        ceiling = min(self.max_backoff, self.backoff * (2 ** (attempt - 1)))
        delay = ceiling / 2 + random.uniform(0, ceiling / 2)
        logger.warning(
            "tool %r failed on attempt %d (%s: %s), retrying in %.2fs",
            self.name, attempt, type(exc).__name__, exc, delay,
        )
        if self.on_retry is not None:
            self.on_retry(attempt, exc, delay)
        return delay

    def give_up(self, exc: BaseException, attempts: int, args: tuple, kwargs: dict) -> Any:
        """Final failure: use the fallback, return a message, or raise."""
        if self.fallback is not _MISSING:
            logger.warning("tool %r failed, using fallback (%s: %s)", self.name, type(exc).__name__, exc)
            if callable(self.fallback):
                return self.fallback(*args, **kwargs)
            return self.fallback
        if self.on_error == "message":
            return error_message(self.name, exc, attempts)
        if callable(self.on_error):
            return self.on_error(self.name, exc, attempts)
        raise exc


def _update_breaker(breaker: Optional[CircuitBreaker], policy: _Policy, exc: BaseException) -> None:
    if breaker is None:
        return
    # Only temporary errors say the service is unhealthy. A bad argument from
    # the model, for example, means the service answered, so it counts as up.
    if policy.is_transient(exc):
        breaker.record_failure()
    else:
        breaker.record_success()


def _run_with_timeout(fn: Callable, args: tuple, kwargs: dict, timeout: Optional[float], name: str) -> Any:
    if timeout is None:
        return fn(*args, **kwargs)

    # Python cannot stop a running thread, so a call that times out keeps
    # running in the background. The thread is a daemon so it never blocks exit.
    ctx = contextvars.copy_context()
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = ctx.run(fn, *args, **kwargs)
        except BaseException as exc:  # handed back to the caller below
            box["error"] = exc

    worker = threading.Thread(target=target, name=f"sturdytools-{name}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise ToolTimeout(name, timeout)
    if "error" in box:
        raise box["error"]
    return box["value"]


async def _await_with_timeout(coro: Any, timeout: Optional[float], name: str) -> Any:
    if timeout is None:
        return await coro
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        raise ToolTimeout(name, timeout)
    return task.result()


def sturdy(
    func: Optional[Callable] = None,
    *,
    timeout: Optional[float] = None,
    retries: int = 0,
    retry_on: ErrorTypes = (Exception,),
    backoff: float = 0.5,
    max_backoff: float = 8.0,
    rate_limit: Union[str, RateLimiter, None] = None,
    breaker: Union[CircuitBreaker, bool, None] = None,
    fallback: Any = _MISSING,
    on_error: OnError = "raise",
    on_retry: Optional[Callable[[int, BaseException, float], Any]] = None,
) -> Any:
    """Make a tool function survive slow, flaky or failing dependencies.

    Works on plain and async functions and keeps the original name, docstring
    and signature, so agent frameworks still see the same tool.

    Args:
        timeout: seconds before one attempt is abandoned with ToolTimeout.
        retries: extra attempts after the first one fails.
        retry_on: errors that count as temporary. Only these are retried and
            counted by the breaker. Anything else goes straight to the final step.
        backoff: base wait before the first retry. It doubles each time, with
            random jitter, up to ``max_backoff``.
        rate_limit: ``"5/s"``, ``"60/min"`` or a shared RateLimiter.
        breaker: a CircuitBreaker (share one across tools that use the same
            service), or True for a default one per tool.
        fallback: value to return when the tool gives up. If callable it is
            called with the tool's arguments.
        on_error: what to do when the tool gives up and there is no fallback.
            ``"raise"`` re-raises the error, ``"message"`` returns a short error
            text the model can read, and a callable ``(name, error, attempts)``
            returns whatever you like.
        on_retry: called as ``(attempt, error, delay)`` before each retry.
    """
    if retries < 0:
        raise ValueError("retries must be zero or more")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    if backoff < 0 or max_backoff < 0:
        raise ValueError("backoff values must be zero or more")
    if not (on_error in ("raise", "message") or callable(on_error)):
        raise ValueError("on_error must be 'raise', 'message' or a callable")

    limiter = as_limiter(rate_limit)
    if breaker is True:
        breaker = CircuitBreaker()
    elif breaker is False:
        breaker = None

    def decorate(fn: Callable) -> Callable:
        name = getattr(fn, "__name__", "tool")
        policy = _Policy(name, retries, retry_on, backoff, max_backoff, fallback, on_error, on_retry)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 0
                while True:
                    try:
                        if breaker is not None:
                            breaker.before_call()
                    except CircuitOpenError as exc:
                        result = policy.give_up(exc, attempt, args, kwargs)
                        return await result if inspect.isawaitable(result) else result
                    attempt += 1
                    if limiter is not None:
                        await limiter.acquire_async()
                    try:
                        value = await _await_with_timeout(fn(*args, **kwargs), timeout, name)
                    except Exception as exc:
                        _update_breaker(breaker, policy, exc)
                        delay = policy.retry_delay(attempt, exc)
                        if delay is not None:
                            await asyncio.sleep(delay)
                            continue
                        result = policy.give_up(exc, attempt, args, kwargs)
                        return await result if inspect.isawaitable(result) else result
                    if breaker is not None:
                        breaker.record_success()
                    return value

            wrapper = async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 0
                while True:
                    try:
                        if breaker is not None:
                            breaker.before_call()
                    except CircuitOpenError as exc:
                        return policy.give_up(exc, attempt, args, kwargs)
                    attempt += 1
                    if limiter is not None:
                        limiter.acquire()
                    try:
                        value = _run_with_timeout(fn, args, kwargs, timeout, name)
                    except Exception as exc:
                        _update_breaker(breaker, policy, exc)
                        delay = policy.retry_delay(attempt, exc)
                        if delay is not None:
                            time.sleep(delay)
                            continue
                        return policy.give_up(exc, attempt, args, kwargs)
                    if breaker is not None:
                        breaker.record_success()
                    return value

            wrapper = sync_wrapper

        wrapper.breaker = breaker  # type: ignore[attr-defined]
        wrapper.limiter = limiter  # type: ignore[attr-defined]
        return wrapper

    if func is not None:
        return decorate(func)
    return decorate
