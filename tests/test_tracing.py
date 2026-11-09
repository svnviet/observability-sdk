import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.trace import StatusCode

from obskit import use_context
from obskit.integrations import httpx as obs_httpx
from obskit.middleware.fastapi import ObservabilityMiddleware
from obskit.tracing.decorators import span as traced

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"


def by_name(obs) -> dict:
    return {s.name: s for s in obs.exporter.get_finished_spans()}


def test_workflow_and_steps_become_a_span_tree_with_matching_event_ids(obs):
    with obs.tracker.workflow("news-video-pipeline", workflow_id="wf-1") as wf:
        with wf.step("render"):
            pass
        with wf.step("qa"):
            pass
    spans = by_name(obs)
    root, render, qa = spans["workflow news-video-pipeline"], spans["step render"], spans["step qa"]
    assert root.parent is None
    assert render.parent.span_id == root.context.span_id == qa.parent.span_id
    assert len({s.context.trace_id for s in spans.values()}) == 1
    assert (
        root.attributes["obskit.workflow.id"] == "wf-1" and root.attributes["obskit.workflow.run_id"] == wf.id
    )
    assert render.attributes["obskit.step.attempt"] == 1
    # the platform's trace_id is the real trace, so a trace link from a workflow works
    trace_hex = f"{root.context.trace_id:032x}"
    assert {e["context"]["trace_id"] for e in obs.events()} == {trace_hex}
    assert root.resource.attributes["service.name"] == "svc-a"


def test_failure_marks_spans_as_errors_and_records_the_exception(obs):
    with pytest.raises(ValueError):
        with obs.tracker.workflow("p") as wf:
            with wf.step("render"):
                raise ValueError("bad frame")
    spans = by_name(obs)
    for name in ("step render", "workflow p"):
        assert spans[name].status.status_code is StatusCode.ERROR
    assert spans["step render"].events[0].name == "exception"


def test_each_retry_attempt_is_its_own_span(obs):
    run = obs.tracker.start_workflow("p")
    for attempt in (1, 2):
        try:
            with run.step("render", attempt=attempt, max_attempts=2):
                if attempt == 1:
                    raise TimeoutError
        except TimeoutError:
            pass
    steps = [s for s in obs.exporter.get_finished_spans() if s.name == "step render"]
    assert [s.attributes["obskit.step.attempt"] for s in steps] == [1, 2]
    assert [s.status.status_code for s in steps] == [StatusCode.ERROR, StatusCode.UNSET]


def make_server_app():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, service_name="svc-b")

    @app.get("/qa/{item}")
    def qa(item: str):
        run = None
        from obskit import WorkflowTracker

        run = WorkflowTracker().current()
        if run is not None:
            with run.step("video-qa"):
                pass
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


def test_incoming_traceparent_continues_the_callers_trace(obs):
    TestClient(make_server_app()).get("/qa/7", headers={"traceparent": f"00-{TRACE}-{PARENT}-01"})
    server = by_name(obs)["GET /qa/{item}"]
    assert f"{server.context.trace_id:032x}" == TRACE and f"{server.parent.span_id:016x}" == PARENT
    assert server.kind.name == "SERVER" and server.attributes["http.route"] == "/qa/{item}"
    assert server.attributes["http.response.status_code"] == 200


def test_health_checks_are_not_traced(obs):
    TestClient(make_server_app()).get("/health")
    assert obs.exporter.get_finished_spans() == ()


def test_caller_and_callee_spans_join_into_one_trace(obs):
    """Service A's client span is the parent of service B's server span and its step span."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200)

    client = obs_httpx.client(transport=httpx.MockTransport(handler))
    with obs.tracker.workflow("pipeline", workflow_id="wf-1") as wf:
        with wf.step("quality-check"):
            client.post("http://svc-b/qa/1")
    client_span = by_name(obs)["POST svc-b"]
    assert client_span.kind.name == "CLIENT" and client_span.attributes["server.address"] == "svc-b"
    assert client_span.parent.span_id == by_name(obs)["step quality-check"].context.span_id

    assert captured["traceparent"].split("-")[2] == f"{client_span.context.span_id:016x}"
    assert captured["x-obskit-workflow-name"] == "pipeline"

    obs.exporter.clear()
    TestClient(make_server_app()).get("/qa/1", headers=captured)  # service B receives the call
    spans = by_name(obs)
    server = spans["GET /qa/{item}"]
    assert server.parent.span_id == client_span.context.span_id
    assert spans["step video-qa"].parent.span_id == server.context.span_id
    assert f"{server.context.trace_id:032x}" == f"{client_span.context.trace_id:032x}"
    assert spans["step video-qa"].attributes["obskit.workflow.name"] == "pipeline"


def test_failed_outgoing_call_is_recorded_on_the_client_span(obs):
    def boom(request):
        raise httpx.ConnectError("refused")

    client = obs_httpx.client(transport=httpx.MockTransport(boom))
    with pytest.raises(httpx.ConnectError):
        client.get("http://svc-b/x")
    span = by_name(obs)["GET svc-b"]
    assert span.status.status_code is StatusCode.ERROR and span.events[0].name == "exception"


def test_5xx_response_marks_client_span_as_error(obs):
    client = obs_httpx.client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    client.get("http://svc-b/x")
    assert by_name(obs)["GET svc-b"].status.status_code is StatusCode.ERROR


def test_span_decorator_sync_and_async(obs):
    @traced("compute", kind_of_work="math")
    def compute():
        return 2

    @traced()
    async def fetch():
        return 3

    with obs.tracker.workflow("p"):
        assert compute() == 2 and asyncio.run(fetch()) == 3
    spans = by_name(obs)
    assert spans["compute"].attributes["kind_of_work"] == "math"
    assert "test_span_decorator_sync_and_async.<locals>.fetch" in spans


def test_worker_job_is_a_span_with_worker_attributes(obs):
    worker = obs.worker("render-1", worker_type="render")
    with use_context(), worker.job("job-9"):
        pass
    span = by_name(obs)["job render"]
    assert span.attributes["obskit.job.id"] == "job-9" and span.attributes["obskit.worker.type"] == "render"
    assert span.kind.name == "CONSUMER"


def test_tracing_is_off_without_an_endpoint_or_processor():
    from obskit import Observability

    o = Observability("svc-x", heartbeat_interval_s=3600).initialize()
    try:
        assert o.tracing is None
        with o.tracker.workflow("p") as wf, wf.step("a"):
            pass  # still works, ids are still propagated
    finally:
        o.shutdown()


def test_otlp_endpoint_enables_tracing_and_shutdown_does_not_hang():
    from obskit import Observability

    o = Observability("svc-x", otlp_endpoint="http://127.0.0.1:9", heartbeat_interval_s=3600).initialize()
    assert o.tracing is not None  # exports in the background; an unreachable collector must not break the app
    with o.tracker.workflow("p"):
        pass
    o.shutdown(timeout_s=1)
