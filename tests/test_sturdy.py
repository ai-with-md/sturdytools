import asyncio
import inspect
import time

import pytest

from sturdytools import CircuitBreaker, CircuitOpenError, ToolTimeout, sturdy

FAST = dict(backoff=0.001, max_backoff=0.002)


def flaky(fail_times, error=ConnectionError("service down"), result="ok"):
    """A function that fails a set number of times, then works."""
    calls = {"n": 0}

    def tool(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise error
        return result

    return tool, calls


def test_returns_value_when_nothing_fails():
    @sturdy(retries=3)
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_retries_until_success():
    tool, calls = flaky(2)
    wrapped = sturdy(retries=3, **FAST)(tool)
    assert wrapped() == "ok"
    assert calls["n"] == 3


def test_raises_original_error_after_last_retry():
    tool, calls = flaky(10)
    wrapped = sturdy(retries=2, **FAST)(tool)
    with pytest.raises(ConnectionError):
        wrapped()
    assert calls["n"] == 3


def test_does_not_retry_errors_outside_retry_on():
    tool, calls = flaky(10, error=ValueError("bad city name"))
    wrapped = sturdy(retries=3, retry_on=ConnectionError, **FAST)(tool)
    with pytest.raises(ValueError):
        wrapped()
    assert calls["n"] == 1


def test_on_retry_is_called_for_each_retry():
    seen = []
    tool, _ = flaky(2)
    wrapped = sturdy(retries=3, on_retry=lambda attempt, exc, delay: seen.append(attempt), **FAST)(tool)
    wrapped()
    assert seen == [1, 2]


def test_backoff_grows_and_respects_cap():
    delays = []
    tool, _ = flaky(4)
    wrapped = sturdy(
        retries=4, backoff=0.01, max_backoff=0.02,
        on_retry=lambda attempt, exc, delay: delays.append(delay),
    )(tool)
    wrapped()
    assert 0.005 <= delays[0] <= 0.01
    assert all(d <= 0.02 for d in delays)


def test_timeout_raises_tool_timeout():
    @sturdy(timeout=0.05)
    def slow():
        time.sleep(1)

    started = time.monotonic()
    with pytest.raises(ToolTimeout) as info:
        slow()
    assert time.monotonic() - started < 0.5
    assert "slow" in str(info.value)


def test_timeout_is_retried():
    calls = {"n": 0}

    @sturdy(timeout=0.05, retries=1, **FAST)
    def sometimes_slow():
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(1)
        return "done"

    assert sometimes_slow() == "done"
    assert calls["n"] == 2


def test_error_inside_timed_call_is_passed_through():
    @sturdy(timeout=1)
    def broken():
        raise KeyError("missing")

    with pytest.raises(KeyError):
        broken()


def test_fallback_value():
    tool, _ = flaky(10)
    wrapped = sturdy(retries=1, fallback="no data", **FAST)(tool)
    assert wrapped() == "no data"


def test_fallback_callable_gets_the_arguments():
    tool, _ = flaky(10)
    wrapped = sturdy(fallback=lambda city: f"cached weather for {city}")(tool)
    assert wrapped("Denver") == "cached weather for Denver"


def test_on_error_message_returns_text_for_the_model():
    tool, _ = flaky(10)
    tool.__name__ = "get_weather"
    wrapped = sturdy(retries=2, on_error="message", **FAST)(tool)
    message = wrapped()
    assert isinstance(message, str)
    assert "get_weather" in message
    assert "3 attempts" in message
    assert "ConnectionError" in message


def test_on_error_callable():
    tool, _ = flaky(10)
    wrapped = sturdy(on_error=lambda name, exc, attempts: {"error": str(exc), "attempts": attempts})(tool)
    assert wrapped() == {"error": "service down", "attempts": 1}


def test_keeps_name_docstring_and_signature():
    def search(query: str, limit: int = 5) -> list:
        """Search the catalog."""
        return []

    wrapped = sturdy(retries=2)(search)
    assert wrapped.__name__ == "search"
    assert wrapped.__doc__ == "Search the catalog."
    assert inspect.signature(wrapped) == inspect.signature(search)


def test_works_without_parentheses():
    @sturdy
    def ping():
        return "pong"

    assert ping() == "pong"


def test_works_on_methods():
    class Client:
        def __init__(self):
            self.calls = 0

        @sturdy(retries=1, **FAST)
        def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError
            return self.calls

    assert Client().fetch() == 2


@pytest.mark.parametrize(
    "kwargs",
    [dict(retries=-1), dict(timeout=0), dict(backoff=-1), dict(on_error="ignore")],
)
def test_rejects_bad_settings(kwargs):
    with pytest.raises(ValueError):
        sturdy(**kwargs)


# Circuit breaker through the decorator


def test_breaker_stops_calling_a_failing_tool():
    breaker = CircuitBreaker(failure_threshold=2, reset_after=60)
    tool, calls = flaky(100)
    wrapped = sturdy(breaker=breaker, on_error="message")(tool)
    wrapped()
    wrapped()
    assert breaker.state == "open"
    message = wrapped()
    assert calls["n"] == 2
    assert "paused" in message


def test_breaker_open_raises_when_on_error_is_raise():
    breaker = CircuitBreaker(failure_threshold=1, reset_after=60)
    tool, _ = flaky(100)
    wrapped = sturdy(breaker=breaker)(tool)
    with pytest.raises(ConnectionError):
        wrapped()
    with pytest.raises(CircuitOpenError):
        wrapped()


def test_breaker_stops_retries_once_open():
    breaker = CircuitBreaker(failure_threshold=2, reset_after=60)
    tool, calls = flaky(100)
    wrapped = sturdy(retries=5, breaker=breaker, **FAST)(tool)
    with pytest.raises(CircuitOpenError):
        wrapped()
    assert calls["n"] == 2


def test_shared_breaker_covers_several_tools():
    breaker = CircuitBreaker(failure_threshold=2, reset_after=60)
    first, _ = flaky(100)
    second, second_calls = flaky(0)
    wrapped_first = sturdy(breaker=breaker, on_error="message")(first)
    wrapped_second = sturdy(breaker=breaker, on_error="message")(second)
    wrapped_first()
    wrapped_first()
    assert "paused" in wrapped_second()
    assert second_calls["n"] == 0


def test_bad_arguments_do_not_trip_the_breaker():
    breaker = CircuitBreaker(failure_threshold=2, reset_after=60)
    tool, _ = flaky(100, error=ValueError("bad input"))
    wrapped = sturdy(breaker=breaker, retry_on=ConnectionError, on_error="message")(tool)
    for _ in range(5):
        wrapped()
    assert breaker.state == "closed"


def test_breaker_true_gives_each_tool_its_own_breaker():
    a = sturdy(breaker=True)(lambda: 1)
    b = sturdy(breaker=True)(lambda: 2)
    assert isinstance(a.breaker, CircuitBreaker)
    assert a.breaker is not b.breaker


# Async tools


def test_async_retries_until_success():
    calls = {"n": 0}

    @sturdy(retries=2, **FAST)
    async def lookup():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError
        return "found"

    assert inspect.iscoroutinefunction(lookup)
    assert asyncio.run(lookup()) == "found"
    assert calls["n"] == 3


def test_async_timeout():
    @sturdy(timeout=0.05)
    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(ToolTimeout):
        asyncio.run(slow())


def test_async_message_on_failure():
    @sturdy(retries=1, on_error="message", **FAST)
    async def lookup():
        raise ConnectionError("no route")

    message = asyncio.run(lookup())
    assert "lookup" in message and "2 attempts" in message


def test_async_fallback_can_be_async():
    async def cached(city):
        return f"cached {city}"

    @sturdy(fallback=cached)
    async def weather(city):
        raise ConnectionError

    assert asyncio.run(weather("Austin")) == "cached Austin"


def test_async_rate_limit_spaces_calls():
    @sturdy(rate_limit="20/s")
    async def ping():
        return time.monotonic()

    async def run():
        return [await ping() for _ in range(4)]

    stamps = asyncio.run(run())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(gap >= 0.04 for gap in gaps)
