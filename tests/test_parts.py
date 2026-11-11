import pytest

from sturdytools import CircuitBreaker, CircuitOpenError, RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# Rate limiter


@pytest.mark.parametrize(
    "spec, rate, per",
    [("5/s", 5, 1), ("60/min", 60, 60), ("1000/hour", 1000, 3600), ("10 / minutes", 10, 60)],
)
def test_parse_rate_limit(spec, rate, per):
    limiter = RateLimiter.parse(spec)
    assert limiter.rate == rate
    assert limiter.per == per


@pytest.mark.parametrize("spec", ["5", "five/s", "5/fortnight", ""])
def test_parse_rejects_bad_text(spec):
    with pytest.raises(ValueError):
        RateLimiter.parse(spec)


def test_limiter_spaces_calls_evenly():
    clock = FakeClock()
    limiter = RateLimiter(2, per=1, clock=clock)
    assert limiter.reserve() == 0
    assert limiter.reserve() == pytest.approx(0.5)
    assert limiter.reserve() == pytest.approx(1.0)


def test_limiter_refills_over_time():
    clock = FakeClock()
    limiter = RateLimiter(2, per=1, clock=clock)
    limiter.reserve()
    clock.now = 0.5
    assert limiter.reserve() == 0


def test_limiter_allows_burst():
    clock = FakeClock()
    limiter = RateLimiter(1, per=1, burst=3, clock=clock)
    assert [limiter.reserve() for _ in range(3)] == [0, 0, 0]
    assert limiter.reserve() == pytest.approx(1.0)


def test_limiter_does_not_save_up_more_than_burst():
    clock = FakeClock()
    limiter = RateLimiter(1, per=1, burst=2, clock=clock)
    clock.now = 100
    assert [limiter.reserve() for _ in range(2)] == [0, 0]
    assert limiter.reserve() > 0


# Circuit breaker


def test_breaker_opens_after_threshold():
    breaker = CircuitBreaker(failure_threshold=3, reset_after=10, clock=FakeClock())
    for _ in range(2):
        breaker.record_failure()
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=2, reset_after=10, clock=FakeClock())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_breaker_allows_one_trial_after_reset_time():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after=10, clock=clock)
    breaker.record_failure()
    clock.now = 9
    with pytest.raises(CircuitOpenError) as info:
        breaker.before_call()
    assert info.value.retry_in == pytest.approx(1)
    clock.now = 10
    breaker.before_call()
    assert breaker.state == "half_open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_successful_trial_closes_the_breaker():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"
    breaker.before_call()


def test_failed_trial_opens_the_breaker_again():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now = 15
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    clock.now = 20
    breaker.before_call()
