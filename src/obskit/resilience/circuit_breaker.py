import asyncio
import functools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypeVar

from obskit.core.exceptions import ObservabilityError

log = logging.getLogger("obskit.resilience")
T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(ObservabilityError):
    def __init__(self, name: str, retry_after_s: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_after_s:.1f}s")
        self.retry_after_s = retry_after_s


class CircuitBreaker:
    """Stops calling a failing dependency, then probes it before resuming.

    CLOSED: calls pass; `failure_threshold` consecutive failures open the circuit.
    OPEN: calls fail fast with CircuitOpenError until `recovery_timeout_s` passes.
    HALF_OPEN: up to `half_open_max_calls` probe calls; one success closes, one failure reopens.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
        half_open_max_calls: int = 1,
        expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_s
        self._half_open_max = half_open_max_calls
        self._expected = expected_exceptions
        self._on_change = on_state_change
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probes = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        self._before_call()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            self._after_failure(exc)
            raise
        self._after_success()
        return result

    async def acall(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except BaseException as exc:
            self._after_failure(exc)
            raise
        self._after_success()
        return result

    def __call__(self, fn: Callable[..., T]) -> Callable[..., T]:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any):
                return await self.acall(fn, *args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return self.call(fn, *args, **kwargs)

        return wrapper

    # --- state machine (all transitions under the lock) ---------------------------------

    def _before_call(self) -> None:
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError(self.name, max(0.0, self._opened_at + self._recovery - self._clock()))
            if self._state is CircuitState.HALF_OPEN:
                if self._probes >= self._half_open_max:
                    raise CircuitOpenError(self.name, 0.0)
                self._probes += 1

    def _after_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state is CircuitState.HALF_OPEN:
                self._transition(CircuitState.CLOSED)

    def _after_failure(self, exc: BaseException) -> None:
        with self._lock:
            if not isinstance(exc, self._expected):
                if self._state is CircuitState.HALF_OPEN:
                    self._probes = max(0, self._probes - 1)  # the probe did not tell us anything
                return
            if self._state is CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN)
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._transition(CircuitState.OPEN)

    def _maybe_half_open(self) -> None:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self._recovery:
            self._transition(CircuitState.HALF_OPEN)

    def _transition(self, new: CircuitState) -> None:
        old, self._state = self._state, new
        if new is CircuitState.OPEN:
            self._opened_at = self._clock()
        if new is not CircuitState.CLOSED:
            self._failures = self._failures if new is CircuitState.OPEN else 0
        self._probes = 0
        log.warning("circuit '%s' %s -> %s", self.name, old, new)
        if self._on_change:
            try:
                self._on_change(self.name, old, new)
            except Exception:
                log.exception("on_state_change callback failed")
