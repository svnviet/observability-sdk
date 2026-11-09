import time

import httpx

from svn_obskit import runtime
from svn_obskit.core.context import current_context
from svn_obskit.core.correlation import TRACEPARENT, inject_headers
from svn_obskit.tracing.provider import end_client_span


def _begin(request: httpx.Request):
    """Add context headers; start a client span and its traceparent when tracing is on."""
    sink = runtime.get_active()
    tracing, metrics = getattr(sink, "tracing", None), getattr(sink, "metrics", None)
    headers = inject_headers(current_context())
    span = None
    if tracing is not None:
        span, carrier = tracing.start_client_span(
            f"{request.method} {request.url.host}",
            {"http.request.method": request.method, "server.address": request.url.host or ""},
        )
        headers.update(carrier)  # the client span is the parent of the callee's server span
    for name, value in headers.items():
        if name == TRACEPARENT or name not in request.headers:
            request.headers[name] = value
    return span, metrics, time.perf_counter()


def _end(request: httpx.Request, span, metrics, started: float, status: int | None, error=None) -> None:
    if span is not None:
        end_client_span(span, status_code=status, error=error)
    if metrics is not None:
        metrics.client_call(request.url.host, request.method, status, time.perf_counter() - started)


class TracedClient(httpx.Client):
    """httpx.Client that forwards the current execution context and records spans and metrics."""

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        span, metrics, started = _begin(request)
        try:
            response = super().send(request, **kwargs)
        except BaseException as exc:
            _end(request, span, metrics, started, None, exc if isinstance(exc, Exception) else None)
            raise
        _end(request, span, metrics, started, response.status_code)
        return response


class TracedAsyncClient(httpx.AsyncClient):
    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        span, metrics, started = _begin(request)
        try:
            response = await super().send(request, **kwargs)
        except BaseException as exc:
            _end(request, span, metrics, started, None, exc if isinstance(exc, Exception) else None)
            raise
        _end(request, span, metrics, started, response.status_code)
        return response


def client(**kwargs) -> httpx.Client:
    """An httpx.Client that forwards the current execution context to the services it calls."""
    return TracedClient(**kwargs)


def async_client(**kwargs) -> httpx.AsyncClient:
    return TracedAsyncClient(**kwargs)
