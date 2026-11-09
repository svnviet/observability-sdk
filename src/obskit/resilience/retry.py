import asyncio
import functools
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from obskit.workflow.tracker import WorkflowRun

log = logging.getLogger("obskit.resilience")
T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.5  # fraction of each delay that is randomized: 0 = fixed, 1 = full jitter
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    give_up_on: tuple[type[BaseException], ...] = ()  # never retried, even if also in retry_on

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def should_retry(self, exc: BaseException) -> bool:
        return isinstance(exc, self.retry_on) and not isinstance(exc, self.give_up_on)

    def delay_for(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Delay after a failed `attempt` (1-based): exponential, capped, with jitter."""
        raw = min(self.max_delay_s, self.base_delay_s * self.multiplier ** (attempt - 1))
        return raw * (1 - self.jitter * rng())


@dataclass(frozen=True)
class RetryEvent:
    attempt: int
    max_attempts: int
    exception: BaseException
    delay_s: float
    will_retry: bool


class Retry:
    """Runs a callable until it succeeds, a non-retryable error occurs, or attempts run out.

    The last exception is re-raised unchanged, so existing `except` clauses keep working.
    """

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self._on_retry = on_retry
        self._sleep = sleep
        self._async_sleep = async_sleep

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return self._run(lambda attempt: fn(*args, **kwargs))

    async def acall(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        return await self._arun(lambda attempt: fn(*args, **kwargs))

    def run_step(self, run: WorkflowRun, name: str, fn: Callable[[], T], *, step_id: str | None = None) -> T:
        """Retry `fn` as attempts of one workflow step: every attempt and failure is recorded."""

        def attempt_fn(attempt: int) -> T:
            with run.step(name, step_id=step_id, attempt=attempt, max_attempts=self.policy.max_attempts):
                return fn()

        return self._run(attempt_fn)

    async def arun_step(
        self, run: WorkflowRun, name: str, fn: Callable[[], Awaitable[T]], *, step_id: str | None = None
    ) -> T:
        async def attempt_fn(attempt: int) -> T:
            async with run.step(
                name, step_id=step_id, attempt=attempt, max_attempts=self.policy.max_attempts
            ):
                return await fn()

        return await self._arun(attempt_fn)

    def __call__(self, fn: Callable[..., T]) -> Callable[..., T]:
        """Decorator form; works for sync and async functions."""
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any):
                return await self.acall(fn, *args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return self.call(fn, *args, **kwargs)

        return wrapper

    def _run(self, attempt_fn: Callable[[int], T]) -> T:
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                return attempt_fn(attempt)
            except Exception as exc:
                delay = self._after_failure(attempt, exc)
                if delay is None:
                    raise
                self._sleep(delay)
        raise AssertionError("unreachable")

    async def _arun(self, attempt_fn: Callable[[int], Awaitable[T]]) -> T:
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                return await attempt_fn(attempt)
            except Exception as exc:
                delay = self._after_failure(attempt, exc)
                if delay is None:
                    raise
                await self._async_sleep(delay)
        raise AssertionError("unreachable")

    def _after_failure(self, attempt: int, exc: Exception) -> float | None:
        """Report the failure; return the delay before the next attempt, or None to stop."""
        will_retry = attempt < self.policy.max_attempts and self.policy.should_retry(exc)
        delay = self.policy.delay_for(attempt) if will_retry else 0.0
        event = RetryEvent(attempt, self.policy.max_attempts, exc, delay, will_retry)
        log.warning(
            "attempt %d/%d failed (%s)%s",
            attempt, self.policy.max_attempts, type(exc).__name__,
            f", retrying in {delay:.2f}s" if will_retry else ", giving up",
        )  # fmt: skip
        if self._on_retry:
            try:
                self._on_retry(event)
            except Exception:
                log.exception("on_retry callback failed")
        return delay if will_retry else None
