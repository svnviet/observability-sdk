import time

from obskit import runtime
from obskit.core.context import use_context
from obskit.core.correlation import CORRELATION_ID, extract_context

_DEFAULT_EXCLUDED = ("/health", "/metrics")


class ObservabilityMiddleware:
    """Pure ASGI middleware (works with FastAPI/Starlette without importing either).

    Restores the caller's trace/correlation/workflow context from request headers, or starts a new
    trace when there are none, echoes the correlation id, and (when enabled) records a server span
    and request metrics. `exclude_paths` are neither traced nor counted, so probes and scrapes stay quiet.
    """

    def __init__(
        self, app, service_name: str | None = None, exclude_paths: tuple[str, ...] = _DEFAULT_EXCLUDED
    ):
        self._app = app
        self._service_name = service_name
        self._exclude = exclude_paths

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        ctx = extract_context(headers, self._service_name)
        sink = runtime.get_active()
        tracing = getattr(sink, "tracing", None)
        metrics = getattr(sink, "metrics", None)
        observed = scope.get("path", "") not in self._exclude

        status = {"code": None}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append(
                    (CORRELATION_ID.encode(), (ctx.correlation_id or "").encode())
                )
            await send(message)

        method = scope.get("method", "GET")
        started = time.perf_counter()
        if observed and metrics is not None:
            metrics.http_started()
        try:
            with use_context(**vars(ctx)):
                if observed and tracing is not None:
                    with tracing.span(method, kind="server", headers=headers) as span:
                        try:
                            await self._app(scope, receive, send_wrapper)
                        finally:
                            self._finish_span(span, scope, method, status["code"])
                else:
                    await self._app(scope, receive, send_wrapper)
        except Exception as exc:
            status["code"] = status["code"] or 500
            if observed and metrics is not None:
                metrics.exception(exc, "http")
            raise
        finally:
            if observed and metrics is not None:
                metrics.http_finished(method, _route(scope), status["code"], time.perf_counter() - started)

    @staticmethod
    def _finish_span(span, scope, method: str, code: int | None) -> None:
        route = _route(scope)
        span.update_name(f"{method} {route}" if route else method)
        span.set_attribute("http.request.method", method)
        if route:
            span.set_attribute("http.route", route)
        if code is not None:
            span.set_attribute("http.response.status_code", code)
            if code >= 500:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, f"HTTP {code}"))


def _route(scope) -> str | None:
    """The matched route template (bounded), never the raw path (which contains ids)."""
    route = scope.get("route")
    return getattr(route, "path", None)
