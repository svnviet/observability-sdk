from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ExecutionContext:
    trace_id: str | None = None
    span_id: str | None = None
    correlation_id: str | None = None
    workflow_id: str | None = None
    workflow_name: str | None = None
    workflow_run_id: str | None = None
    step_id: str | None = None
    step_attempt_id: str | None = None
    job_id: str | None = None
    worker_id: str | None = None
    service_name: str | None = None


_current: ContextVar[ExecutionContext | None] = ContextVar("obskit_execution_context", default=None)


def current_context() -> ExecutionContext:
    return _current.get() or ExecutionContext()


@contextmanager
def use_context(**changes: str | None) -> Iterator[ExecutionContext]:
    """Overlay fields on the current context for the duration of the block.

    ContextVars follow asyncio tasks and `contextvars.copy_context()`, but not threads or
    processes: pass the context explicitly (headers, message headers) when crossing those.
    """
    updated = replace(current_context(), **changes)
    token = _current.set(updated)
    try:
        yield updated
    finally:
        _current.reset(token)
