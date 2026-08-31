"""FR-5 — the circuit breaker: open fast, recover deliberately."""

import time
from datetime import UTC, datetime, timedelta

import pytest
import time_machine

from src.config import settings
from src.logistics.breaker import BreakerDecision, CircuitBreaker
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import CircuitOpenError

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TENANT = "tenant00001"
MAX_FAIL_FAST_SECONDS = 0.05


def breaker(provider: ProviderEnum = ProviderEnum.PATHAO, tenant: str = TENANT) -> CircuitBreaker:
    return CircuitBreaker(provider, tenant)


async def trip(target: CircuitBreaker) -> None:
    """Drive the breaker to open through the threshold, as a real caller would."""
    for _ in range(settings.BREAKER_FAILURE_THRESHOLD):
        await target.record_failure()


class TestOpening:
    async def test_ac_5_1_the_sixth_call_fails_fast_with_circuit_open(self):
        """AC-5.1 — no HTTP call, and an answer in well under 50ms."""
        await trip(breaker())

        started = time.monotonic()
        with pytest.raises(CircuitOpenError):
            await breaker().allow()
        assert time.monotonic() - started < MAX_FAIL_FAST_SECONDS

    async def test_ac_5_5_a_success_resets_the_failure_count(self):
        """AC-5.5 — four failures, a success, then a fifth failure leaves it closed."""
        target = breaker()
        for _ in range(settings.BREAKER_FAILURE_THRESHOLD - 1):
            await target.record_failure()
        await target.record_success()

        await target.record_failure()

        assert await target.allow() is BreakerDecision.CLOSED

    async def test_failures_outside_the_window_do_not_accumulate(self):
        """FR-5.2 — five failures spread over an afternoon are not a broken provider."""
        target = breaker()
        with time_machine.travel(START, tick=False) as traveller:
            for _ in range(settings.BREAKER_FAILURE_THRESHOLD * 2):
                await target.record_failure()
                traveller.shift(timedelta(seconds=settings.BREAKER_WINDOW_SECONDS + 1))

            assert await target.allow() is BreakerDecision.CLOSED


class TestRecovery:
    async def test_ac_5_2_after_the_open_window_the_next_request_probes(self):
        """AC-5.2 — 30 seconds later the breaker half-opens and lets one call through."""
        with time_machine.travel(START, tick=False) as traveller:
            await trip(breaker())

            traveller.shift(timedelta(seconds=settings.BREAKER_OPEN_SECONDS + 1))

            assert await breaker().allow() is BreakerDecision.PROBE

    async def test_only_one_caller_gets_the_probe(self):
        """FR-5.4 — half-open means *one* request, not a thundering herd at the sick provider."""
        with time_machine.travel(START, tick=False) as traveller:
            await trip(breaker())
            traveller.shift(timedelta(seconds=settings.BREAKER_OPEN_SECONDS + 1))

            assert await breaker().allow() is BreakerDecision.PROBE
            with pytest.raises(CircuitOpenError):
                await breaker().allow()

    async def test_ac_5_3_a_successful_probe_closes_the_breaker(self):
        """AC-5.3 — and the failure count goes back to zero with it."""
        with time_machine.travel(START, tick=False) as traveller:
            await trip(breaker())
            traveller.shift(timedelta(seconds=settings.BREAKER_OPEN_SECONDS + 1))
            await breaker().allow()

            await breaker().record_success()

            assert await breaker().allow() is BreakerDecision.CLOSED
            await breaker().record_failure()
            assert await breaker().allow() is BreakerDecision.CLOSED

    async def test_ac_5_4_a_failed_probe_reopens_for_another_window(self):
        """AC-5.4 — one hopeful call does not earn the provider a second chance."""
        with time_machine.travel(START, tick=False) as traveller:
            await trip(breaker())
            traveller.shift(timedelta(seconds=settings.BREAKER_OPEN_SECONDS + 1))
            await breaker().allow()

            await breaker().record_failure()

            with pytest.raises(CircuitOpenError):
                await breaker().allow()

            traveller.shift(timedelta(seconds=settings.BREAKER_OPEN_SECONDS + 1))
            assert await breaker().allow() is BreakerDecision.PROBE


class TestIsolation:
    async def test_ac_5_6_one_open_provider_does_not_affect_the_others(self):
        """AC-5.6 — the other couriers are queried normally."""
        await trip(breaker(ProviderEnum.PATHAO))

        assert await breaker(ProviderEnum.REDX).allow() is BreakerDecision.CLOSED
        assert await breaker(ProviderEnum.STEADFAST).allow() is BreakerDecision.CLOSED

    async def test_one_merchants_bad_password_does_not_open_the_circuit_for_everyone(self):
        """
        The divergence from FR-5.1's "per-provider" wording, asserted.

        FR-5.6 counts 401s toward the breaker. Keyed per provider alone, one merchant
        with a stale portal password would take Pathao offline for every other tenant.
        """
        await trip(breaker(tenant="tenant-a"))

        assert await breaker(tenant="tenant-b").allow() is BreakerDecision.CLOSED
