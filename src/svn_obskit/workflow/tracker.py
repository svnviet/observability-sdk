import logging
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

from svn_obskit import runtime
from svn_obskit.core.context import current_context, use_context
from svn_obskit.core.correlation import new_correlation_id, new_span_id, new_trace_id
from svn_obskit.core.scope import ScopeBase
from svn_obskit.logging.formatter import redact_text
from svn_obskit.workflow import events as ev
from svn_obskit.workflow.step import StepHandle

log = logging.getLogger("svn_obskit.workflow")


def error_details(exc: BaseException) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": redact_text(str(exc))[:500],
        "stack_trace": redact_text("".join(traceback.format_exception(exc)))[-4000:],
    }


class WorkflowRun:
    """A started (or attached) workflow run. Serializable via `to_dict` for cross-process use."""

    def __init__(
        self,
        tracker: "WorkflowTracker",
        name: str | None,
        workflow_id: str,
        run_id: str,
        trace_id: str | None,
        correlation_id: str | None,
    ) -> None:
        self._tracker = tracker
        self.started_at = time.time()
        self.name = name
        self.workflow_id = workflow_id
        self.id = run_id
        self.trace_id = trace_id
        self.correlation_id = correlation_id

    def step(self, name: str, *, step_id: str | None = None, attempt: int = 1, max_attempts: int = 1):
        """Context manager (sync or async) around one execution attempt of a logical step.

        `step_id` defaults to `name`, so retries of "render" share one step. Pass explicit ids when the
        same name runs more than once in a run. A failure with `attempt < max_attempts` is reported
        as "will retry" and leaves the step in RETRYING instead of FAILED.
        """
        return _StepScope(self._tracker, self, name, step_id or name, attempt, max_attempts)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "workflow_id": self.workflow_id,
            "run_id": self.id,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
        }


def _obs(sink: object, name: str):
    """The sink's tracing/metrics helper, or None (also for sinks that have neither)."""
    return getattr(sink, name, None)


class WorkflowTracker:
    """Records workflow and step lifecycle events, spans and metrics. Never raises into business code."""

    def __init__(self, observability: runtime.EventSink | None = None) -> None:
        self._explicit = observability

    # --- context-manager API --------------------------------------------------------------

    def workflow(self, name: str, *, workflow_id: str | None = None, run_id: str | None = None):
        return _WorkflowScope(self, name, workflow_id, run_id)

    # --- explicit lifecycle API (start and finish may happen in different processes) --------

    def start_workflow(
        self,
        name: str,
        *,
        workflow_id: str | None = None,
        run_id: str | None = None,
        input_metadata: dict | None = None,
        queued_at: datetime | None = None,
    ) -> WorkflowRun:
        ctx = current_context()
        run = WorkflowRun(
            self,
            name,
            workflow_id or f"wf-{uuid.uuid4().hex[:12]}",
            run_id or f"run-{uuid.uuid4().hex[:12]}",
            ctx.trace_id or new_trace_id(),
            ctx.correlation_id or new_correlation_id(),
        )
        data: dict[str, Any] = {"workflow_name": name}
        if input_metadata:
            data["input_metadata"] = input_metadata
        self._emit(ev.WORKFLOW_STARTED, run, data=data)
        self._metric("workflow_started", name)
        if queued_at is not None:
            self._metric(
                "workflow_queue_wait", name, (datetime.now(queued_at.tzinfo) - queued_at).total_seconds()
            )
        return run

    def complete_workflow(self, run: WorkflowRun, output_metadata: dict | None = None) -> None:
        self._emit(ev.WORKFLOW_COMPLETED, run, data={"output_metadata": output_metadata or {}})
        self._metric("workflow_finished", run.name, "completed", time.time() - run.started_at)

    def fail_workflow(self, run: WorkflowRun, error: BaseException | dict) -> None:
        self._emit(ev.WORKFLOW_FAILED, run, data={"error": _as_error(error)})
        self._metric("workflow_finished", run.name, "failed", time.time() - run.started_at)
        if isinstance(error, BaseException):
            self._metric("exception", error, "workflow")

    def cancel_workflow(self, run: WorkflowRun) -> None:
        self._emit(ev.WORKFLOW_CANCELLED, run)
        self._metric("workflow_finished", run.name, "cancelled", time.time() - run.started_at)

    def attach(
        self, workflow_id: str, run_id: str, *, trace_id: str | None = None, correlation_id: str | None = None
    ) -> WorkflowRun:
        """Join a run started elsewhere (no start event is emitted)."""
        ctx = current_context()
        return WorkflowRun(
            self,
            ctx.workflow_name,
            workflow_id,
            run_id,
            trace_id or ctx.trace_id,
            correlation_id or ctx.correlation_id,
        )

    def current(self) -> WorkflowRun | None:
        """The run carried by the current context (set by the FastAPI middleware from request headers)."""
        ctx = current_context()
        if not ctx.workflow_id or not ctx.workflow_run_id:
            return None
        return self.attach(ctx.workflow_id, ctx.workflow_run_id)

    def start_step(
        self, run: WorkflowRun, name: str, *, step_id: str | None = None, attempt_number: int = 1
    ) -> StepHandle:
        step = StepHandle(
            step_id=step_id or name,
            name=name,
            workflow_id=run.workflow_id,
            run_id=run.id,
            attempt_number=attempt_number,
            # Prefer the trace this code is running in (the step's span), else the run's own.
            trace_id=current_context().trace_id or run.trace_id,
            correlation_id=run.correlation_id,
            workflow_name=run.name,
        )
        self._emit_step(ev.STEP_STARTED, step, data={"step_name": name})
        if attempt_number > 1:
            self._metric("step_retry", run.name, step.name)
        return step

    def complete_step(self, step: StepHandle, output_metadata: dict | None = None) -> None:
        data = {"step_name": step.name}
        if output_metadata:
            data["output_metadata"] = output_metadata
        self._emit_step(ev.STEP_COMPLETED, step, data=data)
        self._metric(
            "step_finished", step.workflow_name, step.name, "completed", time.time() - step.started_at
        )

    def fail_step(self, step: StepHandle, error: BaseException | dict, *, will_retry: bool = False) -> None:
        self._emit_step(
            ev.STEP_FAILED,
            step,
            data={"step_name": step.name, "error": _as_error(error), "will_retry": will_retry},
        )
        self._metric(
            "step_finished", step.workflow_name, step.name, "retrying" if will_retry else "failed",
            time.time() - step.started_at,
        )  # fmt: skip
        if isinstance(error, BaseException):
            self._metric("exception", error, "workflow")

    # --- internals -------------------------------------------------------------------------

    def _sink(self) -> runtime.EventSink | None:
        return self._explicit or runtime.get_active()

    def _metric(self, method: str, *args: Any) -> None:
        metrics = _obs(self._sink(), "metrics")
        if metrics is None:
            return
        try:
            getattr(metrics, method)(*args)
        except Exception:  # a metrics problem must never reach business code
            log.debug("metric %s failed", method, exc_info=True)

    def _emit(self, event_type: str, run: WorkflowRun, *, data: dict | None = None) -> None:
        self._send(
            event_type,
            workflow_id=run.workflow_id,
            run_id=run.id,
            trace_id=run.trace_id,
            correlation_id=run.correlation_id,
            data=data,
        )

    def _emit_step(self, event_type: str, step: StepHandle, *, data: dict | None = None) -> None:
        self._send(
            event_type,
            workflow_id=step.workflow_id,
            run_id=step.run_id,
            trace_id=step.trace_id,
            correlation_id=step.correlation_id,
            step_id=step.step_id,
            attempt=step.attempt_number,
            data=data,
        )

    def _send(self, event_type: str, **kwargs: Any) -> None:
        sink = self._sink()
        if sink is None:
            log.debug("observability not initialized; dropping %s", event_type)
            return
        try:
            sink.emit(ev.build_event(sink.identity, event_type, **kwargs))
        except Exception:  # observability must never break business execution
            log.exception("failed to record %s", event_type)


def _as_error(error: BaseException | dict) -> dict:
    return error if isinstance(error, dict) else error_details(error)


class _WorkflowScope(ScopeBase):
    def __init__(self, tracker: WorkflowTracker, name: str, workflow_id: str | None, run_id: str | None):
        super().__init__()
        self._tracker, self._name, self._workflow_id, self._run_id = tracker, name, workflow_id, run_id
        self.run: WorkflowRun | None = None

    def _enter(self) -> WorkflowRun:
        tracing = _obs(self._tracker._sink(), "tracing")
        span = None
        if tracing is not None:
            span = self._stack.enter_context(
                tracing.span(f"workflow {self._name}", attributes={"obskit.workflow.name": self._name})
            )
        ctx = current_context()
        trace_id = ctx.trace_id or new_trace_id()
        correlation_id = ctx.correlation_id or new_correlation_id()
        self._stack.enter_context(use_context(trace_id=trace_id, correlation_id=correlation_id))
        self.run = self._tracker.start_workflow(
            self._name, workflow_id=self._workflow_id, run_id=self._run_id
        )
        self._stack.enter_context(
            use_context(
                workflow_id=self.run.workflow_id, workflow_run_id=self.run.id, workflow_name=self._name
            )
        )
        if span is not None:
            span.set_attribute("obskit.workflow.id", self.run.workflow_id)
            span.set_attribute("obskit.workflow.run_id", self.run.id)
        return self.run

    def _exit(self, exc: BaseException | None) -> None:
        assert self.run is not None
        if exc is None:
            self._tracker.complete_workflow(self.run)
        elif isinstance(exc, Exception):
            self._tracker.fail_workflow(self.run, exc)
        else:  # CancelledError, KeyboardInterrupt, SystemExit: the run did not fail, it was stopped
            self._tracker.cancel_workflow(self.run)


class _StepScope(ScopeBase):
    def __init__(
        self,
        tracker: WorkflowTracker,
        run: WorkflowRun,
        name: str,
        step_id: str,
        attempt: int,
        max_attempts: int,
    ) -> None:
        super().__init__()
        self._tracker, self._run, self._name = tracker, run, name
        self._step_id, self._attempt, self._max_attempts = step_id, attempt, max_attempts
        self.handle: StepHandle | None = None

    def _enter(self) -> StepHandle:
        tracing = _obs(self._tracker._sink(), "tracing")
        if tracing is not None:
            self._stack.enter_context(
                tracing.span(
                    f"step {self._name}",
                    attributes={
                        "obskit.workflow.name": self._run.name or "",
                        "obskit.workflow.id": self._run.workflow_id,
                        "obskit.workflow.run_id": self._run.id,
                        "obskit.step.id": self._step_id,
                        "obskit.step.attempt": self._attempt,
                    },
                )
            )
        self.handle = self._tracker.start_step(
            self._run, self._name, step_id=self._step_id, attempt_number=self._attempt
        )
        fields = {
            "correlation_id": self._run.correlation_id,
            "workflow_id": self._run.workflow_id,
            "workflow_run_id": self._run.id,
            "workflow_name": self._run.name,
            "step_id": self._step_id,
            "step_attempt_id": f"{self._run.id}:{self._step_id}:{self._attempt}",
        }
        if tracing is None:  # with tracing on, trace and span ids already come from the step's span
            fields.update(trace_id=self._run.trace_id, span_id=new_span_id())
        self._stack.enter_context(use_context(**fields))
        return self.handle

    def _exit(self, exc: BaseException | None) -> None:
        assert self.handle is not None
        if exc is None:
            self._tracker.complete_step(self.handle)
        else:
            will_retry = isinstance(exc, Exception) and self._attempt < self._max_attempts
            self._tracker.fail_step(self.handle, exc, will_retry=will_retry)


__all__ = ["WorkflowRun", "WorkflowTracker"]
