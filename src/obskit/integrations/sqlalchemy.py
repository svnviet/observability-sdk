import time

from obskit import runtime


def instrument(engine) -> None:
    """Record every statement's duration (metric) and a span, using SQLAlchemy's event hooks.

    The statement text is never attached: only the operation keyword (SELECT, INSERT, ...) is, so
    no parameters or data can leak into spans or labels.
    """
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        sink = runtime.get_active()
        tracing = getattr(sink, "tracing", None)
        span_cm = tracing.span(f"db {_operation(statement)}", kind="client",
                               attributes={"db.system": engine.dialect.name, "db.operation": _operation(statement)}) if tracing else None  # fmt: skip
        if span_cm is not None:
            span_cm.__enter__()
        conn.info.setdefault("obskit_queries", []).append((time.perf_counter(), span_cm))

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        _finish(conn, statement, None)

    @event.listens_for(engine, "handle_error")
    def _error(exception_context):
        conn = exception_context.connection
        if conn is not None:
            _finish(conn, exception_context.statement or "", exception_context.original_exception)


def _finish(conn, statement: str, error: BaseException | None) -> None:
    stack = conn.info.get("obskit_queries")
    if not stack:
        return
    started, span_cm = stack.pop()
    metrics = getattr(runtime.get_active(), "metrics", None)
    if metrics is not None:
        metrics.db_operation(_operation(statement), time.perf_counter() - started)
    if span_cm is not None:
        if error is not None:
            span_cm.__exit__(type(error), error, error.__traceback__)
        else:
            span_cm.__exit__(None, None, None)


def _operation(statement: str) -> str:
    parts = statement.lstrip().split(None, 1)
    return parts[0].upper() if parts else "UNKNOWN"
