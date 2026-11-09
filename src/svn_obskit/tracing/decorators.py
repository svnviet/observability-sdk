import asyncio
import functools
from collections.abc import Callable
from typing import Any, TypeVar

from svn_obskit import runtime

T = TypeVar("T")


def span(name: str | None = None, **attributes: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Wrap a function in a span (sync or async). A no-op when tracing is not enabled."""

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        span_name = name or fn.__qualname__

        def tracing():
            sink = runtime.get_active()
            return getattr(sink, "tracing", None)

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any):
                t = tracing()
                if t is None:
                    return await fn(*args, **kwargs)
                with t.span(span_name, attributes=attributes):
                    return await fn(*args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            t = tracing()
            if t is None:
                return fn(*args, **kwargs)
            with t.span(span_name, attributes=attributes):
                return fn(*args, **kwargs)

        return wrapper

    return decorate
