import asyncio
import threading
from typing import Self

from obskit.core.exceptions import ObservabilityError


class BulkheadFull(ObservabilityError):
    def __init__(self, name: str, limit: int) -> None:
        super().__init__(f"bulkhead '{name}' is full ({limit} concurrent operations)")


class Bulkhead:
    """Caps concurrent calls to one dependency so it cannot exhaust the process.

    `max_wait_s=0` rejects immediately when full; a positive value waits that long first.
    Use `with bulkhead:` in threads or `async with bulkhead:` in asyncio code (not both on one instance).
    """

    def __init__(self, name: str, max_concurrent: int, max_wait_s: float = 0.0) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.name = name
        self.limit = max_concurrent
        self._wait = max_wait_s
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._asem: asyncio.Semaphore | None = None
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        return self._active

    def __enter__(self) -> Self:
        if not self._sem.acquire(timeout=self._wait if self._wait > 0 else None, blocking=self._wait > 0):
            raise BulkheadFull(self.name, self.limit)
        self._bump(1)
        return self

    def __exit__(self, *exc_info) -> None:
        self._bump(-1)
        self._sem.release()

    async def __aenter__(self) -> Self:
        if self._asem is None:
            self._asem = asyncio.Semaphore(self.limit)
        if self._wait <= 0:
            if self._asem.locked():  # single-threaded event loop: no gap between this check and acquire
                raise BulkheadFull(self.name, self.limit)
            await self._asem.acquire()
        else:
            try:
                await asyncio.wait_for(self._asem.acquire(), timeout=self._wait)
            except TimeoutError as exc:
                raise BulkheadFull(self.name, self.limit) from exc
        self._bump(1)
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._bump(-1)
        assert self._asem is not None
        self._asem.release()

    def _bump(self, delta: int) -> None:
        with self._lock:
            self._active += delta
