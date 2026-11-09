import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

from obskit import runtime

T = TypeVar("T")


def timed(operation: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Record a function's duration as a database-style operation metric (sync or async).

    `operation` must be a short fixed name ("render", "fetch"), never an id.
    """

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        def record(started: float) -> None:
            metrics = getattr(runtime.get_active(), "metrics", None)
            if metrics is not None:
                metrics.db_operation(operation, time.perf_counter() - started)

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any):
                started = time.perf_counter()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    record(started)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                record(started)

        return wrapper

    return decorate
