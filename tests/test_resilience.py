import asyncio
import threading
import time

import pytest

from obskit import WorkflowTracker
from obskit.resilience import (
    Bulkhead,
    BulkheadFull,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    IdempotencyGuard,
    MemoryIdempotencyStore,
    OperationTimeout,
    Retry,
    RetryPolicy,
    SqliteIdempotencyStore,
    run_with_timeout,
    with_timeout,
)


class Flaky:
    def __init__(self, failures, exc=ConnectionError):
        self.failures, self.exc, self.calls = failures, exc, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc("nope")
        return "ok"


NO_SLEEP = {"sleep": lambda s: None}


def test_retry_succeeds_after_transient_failures_and_reports_each_attempt():
    events = []
    retry = Retry(RetryPolicy(max_attempts=4), on_retry=events.append, **NO_SLEEP)
    fn = Flaky(2)
    assert retry.call(fn) == "ok" and fn.calls == 3
    assert [(e.attempt, e.will_retry) for e in events] == [(1, True), (2, True)]


def test_retry_gives_up_and_reraises_the_original_error():
    retry = Retry(RetryPolicy(max_attempts=3), **NO_SLEEP)
    fn = Flaky(99, ConnectionError)
    with pytest.raises(ConnectionError):
        retry.call(fn)
    assert fn.calls == 3  # permanent failures do not retry forever


def test_retry_skips_non_retryable_errors():
    retry = Retry(RetryPolicy(retry_on=(ConnectionError,), give_up_on=(PermissionError,)), **NO_SLEEP)
    for exc in (ValueError, PermissionError):
        fn = Flaky(99, exc)
        with pytest.raises(exc):
            retry.call(fn)
        assert fn.calls == 1


def test_backoff_grows_is_capped_and_jittered():
    p = RetryPolicy(base_delay_s=1, multiplier=2, max_delay_s=5, jitter=0.5)
    assert [p.delay_for(n, rng=lambda: 0.0) for n in (1, 2, 3, 4)] == [1, 2, 4, 5]
    assert p.delay_for(2, rng=lambda: 1.0) == 1.0  # full jitter draw halves the delay (2 * (1 - 0.5))


def test_async_retry_and_decorator():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    retry = Retry(RetryPolicy(max_attempts=3, jitter=0), async_sleep=fake_sleep)
    state = {"n": 0}

    @retry
    async def fetch():
        state["n"] += 1
        if state["n"] < 3:
            raise TimeoutError
        return state["n"]

    assert asyncio.run(fetch()) == 3 and slept == [0.5, 1.0]


def test_retry_run_step_records_every_attempt_in_the_workflow(sink):
    run = WorkflowTracker().start_workflow("p", workflow_id="wf-1")
    result = Retry(RetryPolicy(max_attempts=3), **NO_SLEEP).run_step(run, "render", Flaky(2, TimeoutError))
    assert result == "ok"
    failed = [e for e in sink.events if e["event_type"] == "STEP_FAILED"]
    assert [e["data"]["will_retry"] for e in failed] == [True, True]
    assert [e["data"]["attempt_number"] for e in sink.events if e["event_type"] == "STEP_COMPLETED"] == [3]


def test_retry_run_step_exhausted_leaves_step_failed(sink):
    run = WorkflowTracker().start_workflow("p")
    with pytest.raises(TimeoutError):
        Retry(RetryPolicy(max_attempts=2), **NO_SLEEP).run_step(run, "render", Flaky(9, TimeoutError))
    failed = [e for e in sink.events if e["event_type"] == "STEP_FAILED"]
    assert [e["data"]["will_retry"] for e in failed] == [True, False]  # the last attempt is final


def test_timeouts():
    with pytest.raises(OperationTimeout):
        run_with_timeout(time.sleep, 0.05, 1)
    assert run_with_timeout(lambda: 7, 1) == 7

    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(OperationTimeout):
        asyncio.run(with_timeout(slow(), 0.05))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def failing():
    raise ConnectionError("down")


def test_circuit_opens_after_threshold_then_probes_and_closes():
    clock, changes = Clock(), []
    cb = CircuitBreaker("api", failure_threshold=3, recovery_timeout_s=10, clock=clock,
                        on_state_change=lambda n, o, s: changes.append((o, s)))  # fmt: skip
    for _ in range(3):
        with pytest.raises(ConnectionError):
            cb.call(failing)
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError) as err:
        cb.call(lambda: "never runs")
    assert 0 < err.value.retry_after_s <= 10

    clock.t = 11  # recovery timeout passed
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.call(lambda: "probe ok") == "probe ok"
    assert cb.state is CircuitState.CLOSED
    assert changes == [
        (CircuitState.CLOSED, CircuitState.OPEN),
        (CircuitState.OPEN, CircuitState.HALF_OPEN),
        (CircuitState.HALF_OPEN, CircuitState.CLOSED),
    ]


def test_failed_probe_reopens_and_success_resets_failure_count():
    clock = Clock()
    cb = CircuitBreaker("api", failure_threshold=2, recovery_timeout_s=5, clock=clock)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(failing)
    clock.t = 6
    with pytest.raises(ConnectionError):
        cb.call(failing)  # the probe fails
    assert cb.state is CircuitState.OPEN
    clock.t = 12
    cb.call(lambda: 1)
    with pytest.raises(ConnectionError):
        cb.call(failing)
    assert cb.state is CircuitState.CLOSED  # one failure after recovery is below the threshold


def test_half_open_limits_concurrent_probes_and_ignores_unexpected_errors():
    clock = Clock()
    cb = CircuitBreaker("api", failure_threshold=1, recovery_timeout_s=1, half_open_max_calls=1,
                        expected_exceptions=(ConnectionError,), clock=clock)  # fmt: skip
    with pytest.raises(ConnectionError):
        cb.call(failing)
    clock.t = 2
    started, release = threading.Event(), threading.Event()

    def probe():
        started.set()
        release.wait(2)

    t = threading.Thread(target=lambda: cb.call(probe))
    t.start()
    started.wait(2)
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "second probe")
    release.set()
    t.join()
    assert cb.state is CircuitState.CLOSED

    def bug():
        raise KeyError("not a dependency failure")

    with pytest.raises(KeyError):
        cb.call(bug)
    assert cb.state is CircuitState.CLOSED  # a bug in our code does not trip the breaker


def test_async_circuit_breaker():
    cb = CircuitBreaker("api", failure_threshold=1, recovery_timeout_s=60)

    @cb
    async def call():
        raise ConnectionError

    with pytest.raises(ConnectionError):
        asyncio.run(call())
    with pytest.raises(CircuitOpenError):
        asyncio.run(call())


def test_bulkhead_rejects_when_full_and_releases():
    bh = Bulkhead("llm", max_concurrent=2)
    with bh:
        with bh:
            assert bh.active == 2
            with pytest.raises(BulkheadFull):
                bh.__enter__()
    assert bh.active == 0
    with bh:
        pass


def test_bulkhead_waits_up_to_max_wait():
    bh = Bulkhead("llm", 1, max_wait_s=1.0)
    entered = threading.Event()

    def hold():
        with bh:
            entered.set()
            time.sleep(0.2)

    t = threading.Thread(target=hold)
    t.start()
    entered.wait(2)
    with bh:  # waits for the holder to leave instead of failing
        pass
    t.join()


def test_async_bulkhead():
    async def main():
        bh = Bulkhead("llm", 1)
        async with bh:
            with pytest.raises(BulkheadFull):
                await bh.__aenter__()
        async with bh:
            return bh.active

    assert asyncio.run(main()) == 1


@pytest.mark.parametrize("make_store", [MemoryIdempotencyStore, lambda: SqliteIdempotencyStore(":memory:")])
def test_idempotency_guard(make_store):
    guard = IdempotencyGuard(make_store())
    with guard.once("job-1") as first:
        assert first
        with guard.once("job-1") as concurrent:
            assert not concurrent  # already being processed
    with guard.once("job-1") as again:
        assert not again  # finished work is remembered
    with pytest.raises(RuntimeError):
        with guard.once("job-2") as first:
            raise RuntimeError("worker failed")
    with guard.once("job-2") as retry:
        assert retry  # a failed run released its key so the retry can proceed


def test_idempotency_keys_expire():
    clock = Clock()
    guard = IdempotencyGuard(MemoryIdempotencyStore(clock=clock), done_ttl_s=10, in_progress_ttl_s=5)
    store = guard._store
    assert store.claim("crashed", 5)  # a worker claims, then dies without completing
    assert not store.claim("crashed", 5)
    clock.t = 6
    assert store.claim("crashed", 5)  # claim expired, another worker may take over
