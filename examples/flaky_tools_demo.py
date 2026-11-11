"""See what sturdytools does with a flaky tool. No API keys needed.

Run it with:
    python examples/flaky_tools_demo.py

The "weather service" below fails on purpose: it drops connections, hangs, and
then goes down completely. Each section shows how a wrapped tool behaves
compared with a plain one.
"""

import logging
import random
import sys
import time

from sturdytools import CircuitBreaker, sturdy

logging.basicConfig(level=logging.WARNING, format="  log: %(message)s", stream=sys.stdout)
random.seed(7)


class FakeWeatherService:
    def __init__(self):
        self.calls = 0
        self.down = False

    def fetch(self, city):
        self.calls += 1
        if self.down:
            raise ConnectionError("weather service unreachable")
        if self.calls % 3 == 1:
            raise ConnectionError("connection reset by peer")
        if self.calls % 5 == 0:
            time.sleep(2)  # hangs
        return f"{city}: 18C, light rain"


service = FakeWeatherService()


def plain_get_weather(city: str) -> str:
    """Current weather for a city."""
    return service.fetch(city)


@sturdy(timeout=0.5, retries=3, backoff=0.1, on_error="message")
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return service.fetch(city)


shared_breaker = CircuitBreaker(failure_threshold=3, reset_after=5)


@sturdy(timeout=0.5, retries=1, backoff=0.1, breaker=shared_breaker, on_error="message")
def get_forecast(city: str) -> str:
    """Three day forecast for a city."""
    return service.fetch(city)


def section(title):
    print(f"\n{title}\n" + "-" * len(title))


section("1. Plain tool, first call hits a dropped connection")
try:
    print(plain_get_weather("Seattle"))
except ConnectionError as exc:
    print(f"  crashed: ConnectionError: {exc}")

section("2. Wrapped tool: retries, and gives up on calls that hang")
for city in ["Seattle", "Denver", "Austin", "Boston"]:
    started = time.monotonic()
    answer = get_weather(city)
    print(f"  {answer}  ({time.monotonic() - started:.2f}s)")

section("3. Service goes down: the model gets a message instead of a crash")
service.down = True
print(f"  {get_weather('Chicago')}")

section("4. Circuit breaker: after 3 failures, calls stop hitting the service")
for city in ["Miami", "Phoenix", "Portland"]:
    before = service.calls
    answer = get_forecast(city)
    print(f"  service calls made: {service.calls - before}  breaker: {shared_breaker.state}")
    print(f"  {answer}")
