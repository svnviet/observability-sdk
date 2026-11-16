import collections
import logging
import threading
import time

from svn_obskit.core.exceptions import TransportError
from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.worker.lifecycle import Worker, WorkerState
from svn_obskit.worker.registration import NotRegistered, PlatformClient

log = logging.getLogger("svn_obskit.worker")


class HeartbeatService:
    """Registers this service instance and its workers, then heartbeats on its own thread.

    The thread is independent of job execution, so a worker stuck in a long job still heartbeats
    and a worker whose process dies stops heartbeating, which is what the platform detects.
    """

    def __init__(
        self,
        client: PlatformClient,
        identity: ServiceIdentity,
        interval_s: float,
        instance_metadata: dict | None = None,
    ) -> None:
        self._client = client
        self._identity = identity
        self._interval = interval_s
        self._instance_metadata = instance_metadata or {}
        self._service_id: str | None = None
        self._last_ok: float | None = None
        self._workers: list[Worker] = []
        self._jobs: collections.deque[dict] = collections.deque(maxlen=1000)  # bounded: drop oldest
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="obskit-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def age(self) -> float | None:
        """Seconds since the platform last accepted a service heartbeat; None before the first one."""
        return None if self._last_ok is None else time.monotonic() - self._last_ok

    @property
    def service_id(self) -> str | None:
        """The platform's id for this service; None until the first registration succeeds."""
        return self._service_id

    def ensure_registered(self) -> str:
        """Register now if the background thread has not done it yet. Raises TransportError on failure."""
        if self._service_id is None:
            self._service_id = self._register()
        return self._service_id

    def add_worker(self, worker: Worker) -> None:
        with self._lock:
            self._workers.append(worker)
        self.wake()

    def remove_worker(self, worker: Worker) -> None:
        """Stop heartbeating for a worker (so deleting it on the platform is not undone by the next tick)."""
        with self._lock:
            if worker in self._workers:
                self._workers.remove(worker)

    def queue_job_update(self, update: dict) -> None:
        self._jobs.append(update)
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout_s: float = 3.0) -> None:
        """Tell the platform this instance and its workers are stopping on purpose, then end the thread.

        Only the first call does anything, so a later shutdown cannot heartbeat an instance back into
        existence after it was deleted.
        """
        if self._stop.is_set():
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout_s)
        try:
            self._tick(stopping=True)
        except Exception:
            log.debug("final heartbeat failed", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # never let the loop die; the next tick retries
                log.debug("heartbeat tick failed", exc_info=True)
            self._wake.wait(timeout=self._interval)
            self._wake.clear()

    def _tick(self, *, stopping: bool = False) -> None:
        try:
            if self._service_id is None:
                self._service_id = self._register()
            else:
                self._client.service_heartbeat(
                    self._service_id, self._identity.instance_id, stopping=stopping
                )
        except NotRegistered:
            self._service_id = None
            raise
        self._last_ok = time.monotonic()
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            self._beat_worker(worker, stopping)
        self._flush_jobs()

    def _register(self) -> str:
        if self._instance_metadata:
            return self._client.register_service(self._identity, self._instance_metadata)
        return self._client.register_service(self._identity)  # keeps the one-argument call older fakes expect

    def _beat_worker(self, worker: Worker, stopping: bool) -> None:
        try:
            if worker.platform_id is None:
                worker.platform_id = self._client.register_worker(
                    self._identity, worker.name, worker.worker_type, worker.capacity, worker.metadata
                )
            state, job = worker.snapshot()
            if stopping and state is not WorkerState.STOPPED:
                state, job = WorkerState.STOPPED, None
            self._client.worker_heartbeat(worker.platform_id, state.value, job)
        except NotRegistered:
            worker.platform_id = None  # re-register on the next tick
        except TransportError as exc:
            log.debug("worker %s heartbeat failed: %s", worker.name, exc)

    def _flush_jobs(self) -> None:
        while self._jobs:
            update = self._jobs[0]
            try:
                self._client.upsert_job(self._identity, update)
            except TransportError as exc:
                if exc.retryable:
                    return  # keep it queued for the next tick
            self._jobs.popleft()
