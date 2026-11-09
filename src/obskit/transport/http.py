import httpx

from obskit.core.exceptions import TransportError
from obskit.transport.interface import Event


class HttpTransport:
    """Posts event batches to the platform's `POST /events/batch`."""

    def __init__(self, base_url: str, api_key: str | None, timeout_s: float) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)

    def send(self, events: list[Event]) -> None:
        try:
            response = self._client.post("/events/batch", json={"events": events})
        except httpx.HTTPError as exc:  # connect/read timeout, DNS, connection reset
            raise TransportError(f"platform unreachable: {exc}", retryable=True) from exc
        status = response.status_code
        if status == 200:
            return  # per-event REJECTED results are permanent; resending cannot fix them
        # 5xx, 429 and 408 are transient. 401/403 are retryable too: a rotated key or a
        # misconfigured deploy gets fixed, and dropping durable events for it would lose history.
        retryable = status >= 500 or status in (401, 403, 408, 429)
        raise TransportError(f"platform returned HTTP {status}", retryable=retryable)

    def close(self) -> None:
        self._client.close()
