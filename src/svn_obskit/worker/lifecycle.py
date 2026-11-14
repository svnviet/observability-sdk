import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from svn_obskit import runtime
from svn_obskit.core.context import current_context, use_context
from svn_obskit.core.correlation import new_trace_id
from svn_obskit.core.scope import ScopeBase
from svn_obskit.workflow.tracker import error_details


class WorkerState(StrEnum):
    REGISTERED = "REGISTERED"
    STARTING = "STARTING"
    IDLE = "IDLE"
    BUSY = "BUSY"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    UNHEALTHY = "UNHEALTHY"


class Worker:
    """One logical worker (a consumer loop, a render slot). Reports state and the job it is running.

    State changes are kept in memory and wake the heartbeat thread; they never block the caller.
    """

    def __init__(
        self,
        name: str,
        worker_type: str = "default",
        capacity: int = 1,
        metadata: dict[str, Any] | None = None,
        on_change: Callable[[], None] | None = None,
        on_job_update: Callable[[dict], None] | None = None,
    ) -> None:
        self.name = name
        self.worker_type = worker_type
        self.capacity = capacity
        self.metadata = metadata or {}
        self.platform_id: str | None = None  # set by the heartbeat thread once registered
        self._state = WorkerState.REGISTERED
        self._active_jobs: list[str] = []
        self._lock = threading.Lock()
        self._on_change = on_change or (lambda: None)
        self._on_job_update = on_job_update or (lambda update: None)

    @property
    def state(self) -> WorkerState:
        return self._state

    def snapshot(self) -> tuple[WorkerState, str | None]:
        with self._lock:
            return self._state, (self._active_jobs[-1] if self._active_jobs else None)

    def start(self) -> "Worker":
        self._set(WorkerState.STARTING)
        self._set(WorkerState.IDLE)
        return self

    def job(self, job_id: str, *, workflow_run_id: str | None = None, metadata: dict | None = None):
        """Context manager (sync or async) around one job. Failures are reported and re-raised."""
        return _JobScope(self, job_id, workflow_run_id, metadata or {})

    def drain(self) -> None:
        """Stop accepting new work: the current jobs finish, then the worker is idle but draining."""
        self._set(WorkerState.DRAINING)

    def stop(self) -> None:
        self._set(WorkerState.STOPPED)

    def mark_unhealthy(self) -> None:
        self._set(WorkerState.UNHEALTHY)

    # --- internals --------------------------------------------------------------------------

    def _set(self, state: WorkerState) -> None:
        with self._lock:
            if self._state is WorkerState.STOPPED:
                return  # terminal
            self._state = state
        self._on_change()

    def _job_started(self, job_id: str) -> None:
        with self._lock:
            self._active_jobs.append(job_id)
            if self._state in (WorkerState.IDLE, WorkerState.STARTING, WorkerState.REGISTERED):
                self._state = WorkerState.BUSY
        self._on_change()

    def _job_finished(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._active_jobs:
                self._active_jobs.remove(job_id)
            if not self._active_jobs and self._state is WorkerState.BUSY:
                self._state = WorkerState.IDLE
        self._on_change()


class _JobScope(ScopeBase):
    def __init__(self, worker: Worker, job_id: str, run_key: str | None, metadata: dict) -> None:
        super().__init__()
        self._worker, self._job_id, self._run_key, self._metadata = worker, job_id, run_key, metadata
        self._trace_id: str | None = None

    def _update(self, status: str, **extra: Any) -> None:
        update = {"external_job_id": self._job_id, "status": status, "worker_name": self._worker.name,
                  "metadata": self._metadata, **extra}  # fmt: skip
        if self._run_key:
            update["workflow_run_key"] = self._run_key
        if self._trace_id:
            update["trace_id"] = self._trace_id  # lets the platform link the job to its trace and logs
        self._worker._on_job_update(update)

    def _enter(self) -> str:
        self._started = time.perf_counter()
        tracing = getattr(runtime.get_active(), "tracing", None)
        if tracing is not None:
            self._stack.enter_context(
                tracing.span(
                    f"job {self._worker.worker_type}",
                    kind="consumer",
                    attributes={"obskit.job.id": self._job_id, "obskit.worker.name": self._worker.name,
                                "obskit.worker.type": self._worker.worker_type},
                )
            )  # fmt: skip
        # The span above (when tracing is on) or the caller's context supplies the trace; otherwise start one,
        # so the job's logs and its platform record always share an id.
        self._trace_id = current_context().trace_id or new_trace_id()
        self._stack.enter_context(
            use_context(worker_id=self._worker.name, job_id=self._job_id, trace_id=self._trace_id)
        )
        self._worker._job_started(self._job_id)
        self._update("RUNNING")
        return self._job_id

    def _exit(self, exc: BaseException | None) -> None:
        if exc is None:
            self._update("COMPLETED")
            result = "completed"
        elif isinstance(exc, Exception):
            self._update("FAILED", error=error_details(exc))
            result = "failed"
        else:
            self._update("CANCELLED")
            result = "cancelled"
        self._worker._job_finished(self._job_id)
        metrics = getattr(runtime.get_active(), "metrics", None)
        if metrics is not None:
            try:
                metrics.job_finished(self._worker.worker_type, result, time.perf_counter() - self._started)
                if isinstance(exc, Exception):
                    metrics.exception(exc, "worker")
            except Exception:
                pass  # metrics must never break a job
