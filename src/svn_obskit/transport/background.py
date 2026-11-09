import logging
import threading
import time

from svn_obskit.core.exceptions import TransportError
from svn_obskit.transport.interface import Event, EventBuffer, Transport

log = logging.getLogger("svn_obskit.transport")


class BackgroundPublisher:
    """Non-blocking publishing: `submit` only appends to a buffer; a worker thread delivers batches.

    A buffer entry is removed only after the transport accepts it, so a platform outage leaves
    events buffered (durably, if the buffer is an outbox) and delivery resumes on its own.
    """

    def __init__(
        self,
        buffer: EventBuffer,
        transport: Transport | None,
        batch_size: int,
        flush_interval_s: float,
        max_backoff_s: float,
    ) -> None:
        self._buffer = buffer
        self._transport = transport
        self._batch_size = batch_size
        self._interval = flush_interval_s
        self._max_backoff = max_backoff_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="obskit-publisher", daemon=True)
        self.delivered = 0
        self.dropped_permanent = 0

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def submit(self, event: Event) -> None:
        self._buffer.add(event)
        if len(self._buffer) >= self._batch_size:
            self._wake.set()

    def pending(self) -> int:
        return len(self._buffer)

    def flush(self, timeout_s: float = 5.0) -> bool:
        """Block until the buffer drains or the timeout passes. Returns True if drained."""
        if self._transport is None:  # nothing can ever drain the buffer, so waiting would only delay shutdown
            return len(self._buffer) == 0
        deadline = time.monotonic() + timeout_s
        self._wake.set()
        while len(self._buffer) > 0 and time.monotonic() < deadline:
            self._wake.set()
            time.sleep(0.02)
        return len(self._buffer) == 0

    def shutdown(self, timeout_s: float = 5.0) -> bool:
        drained = self.flush(timeout_s)
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if not drained:
            log.warning("shutdown with %d undelivered events still buffered", len(self._buffer))
        return drained

    def _run(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            self._wake.wait(timeout=backoff or self._interval)
            self._wake.clear()
            while len(self._buffer) > 0 and not self._stop.is_set():
                if not self._deliver_batch():
                    backoff = min(self._max_backoff, max(0.5, backoff * 2))
                    break
                backoff = 0.0
            else:
                backoff = 0.0

    def _deliver_batch(self) -> bool:
        """Returns False when delivery should back off."""
        if self._transport is None:  # no platform configured: nothing can ever be delivered
            return False
        batch = self._buffer.peek(self._batch_size)
        if not batch:
            return True
        keys = [key for key, _ in batch]
        try:
            self._transport.send([event for _, event in batch])
        except TransportError as exc:
            if exc.retryable:
                log.warning("event delivery failed, will retry: %s", exc)
                return False
            log.error("dropping %d events after permanent delivery failure: %s", len(batch), exc)
            self.dropped_permanent += len(batch)
            dead_letter = getattr(self._buffer, "dead_letter", None)
            if dead_letter is not None:  # durable buffers keep a copy for inspection or manual replay
                dead_letter(keys, str(exc))
            else:
                self._buffer.ack(keys)
            return True
        except Exception:  # a transport bug must never kill the worker thread
            log.exception("unexpected transport error, will retry")
            return False
        self._buffer.ack(keys)
        self.delivered += len(batch)
        return True
