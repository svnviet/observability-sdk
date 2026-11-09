import asyncio

import pytest

from svn_obskit import WorkflowTracker
from svn_obskit.core.context import current_context
from svn_obskit.workflow.step import StepHandle


def test_sync_workflow_emits_lifecycle_in_contract_shape(sink):
    tracker = WorkflowTracker()
    with tracker.workflow("news-video-pipeline", workflow_id="wf-123") as wf:
        with wf.step("video-render"):
            assert current_context().step_id == "video-render"
        with wf.step("video-qa"):
            pass
    assert sink.types() == [
        "WORKFLOW_STARTED",
        "STEP_STARTED",
        "STEP_COMPLETED",
        "STEP_STARTED",
        "STEP_COMPLETED",
        "WORKFLOW_COMPLETED",
    ]
    first, step_started = sink.events[0], sink.events[1]
    assert first["event_version"] == 1 and first["data"]["workflow_name"] == "news-video-pipeline"
    assert first["source"]["service_name"] == "svc-a"
    ctx = step_started["context"]
    assert ctx["workflow_id"] == "wf-123" and ctx["workflow_run_id"] == wf.id
    assert ctx["step_id"] == "video-render" and ctx["step_attempt_id"] == f"{wf.id}:video-render:1"
    assert ctx["trace_id"] == first["context"]["trace_id"]
    assert len({e["event_id"] for e in sink.events}) == 6
    assert current_context().workflow_id is None  # context restored


def test_async_workflow(sink):
    async def main():
        async with WorkflowTracker().workflow("p", workflow_id="wf-1") as wf:
            async with wf.step("a"):
                await asyncio.sleep(0)

    asyncio.run(main())
    assert sink.types() == ["WORKFLOW_STARTED", "STEP_STARTED", "STEP_COMPLETED", "WORKFLOW_COMPLETED"]


def test_failure_reports_error_and_reraises(sink):
    with pytest.raises(RuntimeError):
        with WorkflowTracker().workflow("p") as wf:
            with wf.step("render"):
                raise RuntimeError("gpu out of memory, password=hunter2")
    assert sink.types() == ["WORKFLOW_STARTED", "STEP_STARTED", "STEP_FAILED", "WORKFLOW_FAILED"]
    err = sink.events[2]["data"]["error"]
    assert err["type"] == "RuntimeError" and "hunter2" not in err["message"]
    assert sink.events[2]["data"]["will_retry"] is False


def test_retry_attempts_share_step_and_flag_will_retry(sink):
    tracker = WorkflowTracker()
    run = tracker.start_workflow("p", workflow_id="wf-1")
    for attempt in (1, 2, 3):
        try:
            with run.step("render", attempt=attempt, max_attempts=3):
                if attempt < 3:
                    raise TimeoutError("slow")
        except TimeoutError:
            pass
    failed = [e for e in sink.events if e["event_type"] == "STEP_FAILED"]
    assert [e["data"]["will_retry"] for e in failed] == [True, True]
    attempts = [e["context"]["step_attempt_id"] for e in sink.events if e["event_type"] == "STEP_STARTED"]
    assert attempts == [f"{run.id}:render:{n}" for n in (1, 2, 3)]
    assert {e["context"]["step_id"] for e in sink.events if "step_id" in e["context"]} == {"render"}


def test_cancellation_is_not_a_failure(sink):
    async def main():
        async with WorkflowTracker().workflow("p"):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main())
    assert sink.types() == ["WORKFLOW_STARTED", "WORKFLOW_CANCELLED"]


def test_explicit_api_across_processes(sink):
    tracker = WorkflowTracker()
    run = tracker.start_workflow("p", workflow_id="wf-1")
    step = tracker.start_step(run, "render")
    wire = step.to_dict()  # e.g. stored in a job payload and finished by another process
    tracker.complete_step(StepHandle.from_dict(wire))
    tracker.complete_workflow(run)
    assert sink.types() == ["WORKFLOW_STARTED", "STEP_STARTED", "STEP_COMPLETED", "WORKFLOW_COMPLETED"]
    assert sink.events[2]["context"]["step_attempt_id"] == sink.events[1]["context"]["step_attempt_id"]


def test_attach_joins_run_without_start_event(sink):
    run = WorkflowTracker().attach("wf-1", "run-1", trace_id="t" * 32)
    with run.step("qa"):
        pass
    assert sink.types() == ["STEP_STARTED", "STEP_COMPLETED"]
    assert sink.events[0]["context"]["workflow_run_id"] == "run-1"


def test_uninitialized_sdk_is_a_noop():
    with WorkflowTracker().workflow("p") as wf:  # no active sink
        with wf.step("a"):
            pass


def test_sink_failure_never_reaches_business_code(sink):
    def boom(event):
        raise OSError("disk full")

    sink.emit = boom
    with WorkflowTracker().workflow("p") as wf:
        with wf.step("a"):
            result = 42
    assert result == 42
