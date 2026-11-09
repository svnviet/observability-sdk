import asyncio
import concurrent.futures
import contextvars
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from svn_obskit.core.exceptions import ObservabilityError

T = TypeVar("T")


class OperationTimeout(ObservabilityError, TimeoutError):
    """The operation did not finish within its time limit."""


async def with_timeout(awaitable: Awaitable[T], seconds: float, *, what: str = "operation") -> T:
    """Await with a limit; the awaitable is cancelled on timeout."""
    try:
        async with asyncio.timeout(seconds):
            return await awaitable
    except TimeoutError as exc:
        raise OperationTimeout(f"{what} exceeded {seconds}s") from exc


def run_with_timeout(
    fn: Callable[..., T], seconds: float, *args: Any, what: str = "operation", **kwargs: Any
) -> T:
    """Run sync code with a limit.

    Python cannot kill a running thread: on timeout the caller gets OperationTimeout immediately,
    but `fn` keeps running in its worker thread until it returns. Prefer library-level timeouts
    (httpx, DB drivers) where they exist, and use this as the outer bound.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="obskit-timeout")
    ctx = contextvars.copy_context()  # keep trace/workflow context visible inside fn
    future = executor.submit(ctx.run, functools.partial(fn, *args, **kwargs))
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError as exc:
        raise OperationTimeout(f"{what} exceeded {seconds}s") from exc
    finally:
        executor.shutdown(wait=False)


def timeout(seconds: float) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator; works for sync and async functions."""

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any):
                return await with_timeout(fn(*args, **kwargs), seconds, what=fn.__name__)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return run_with_timeout(fn, seconds, *args, what=fn.__name__, **kwargs)

        return wrapper

    return decorate
