from fastapi import FastAPI
from fastapi.testclient import TestClient

from obskit import WorkflowTracker, current_context
from obskit.core.correlation import parse_traceparent
from obskit.integrations import httpx as obs_httpx
from obskit.middleware.fastapi import ObservabilityMiddleware

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def make_app():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, service_name="svc-b")

    @app.get("/ctx")
    def ctx():
        c = current_context()
        return {"trace_id": c.trace_id, "correlation_id": c.correlation_id, "run": c.workflow_run_id}

    @app.get("/step")
    def step():
        run = WorkflowTracker().current()
        if run is None:
            return {"joined": False}
        with run.step("qa"):
            pass
        return {"joined": True}

    return app


def test_incoming_context_is_restored_and_correlation_echoed():
    client = TestClient(make_app())
    r = client.get(
        "/ctx", headers={"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01", "x-correlation-id": "corr-5"}
    )
    assert r.json()["trace_id"] == TRACE and r.json()["correlation_id"] == "corr-5"
    assert r.headers["x-correlation-id"] == "corr-5"


def test_request_without_headers_starts_new_trace():
    r = TestClient(make_app()).get("/ctx")
    assert len(r.json()["trace_id"]) == 32 and r.headers["x-correlation-id"].startswith("corr-")


def test_downstream_service_joins_upstream_workflow(sink):
    client = TestClient(make_app())
    headers = {
        "x-obskit-workflow-id": "wf-1",
        "x-obskit-workflow-run-id": "run-1",
        "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01",
    }
    assert client.get("/step", headers=headers).json() == {"joined": True}
    assert sink.types() == ["STEP_STARTED", "STEP_COMPLETED"]
    assert sink.events[0]["context"]["trace_id"] == TRACE
    assert client.get("/step").json() == {"joined": False}


def test_httpx_client_injects_current_context():
    from obskit import use_context

    seen = {}

    def handler(request):
        seen.update(request.headers)
        return __import__("httpx").Response(200)

    c = obs_httpx.client(transport=__import__("httpx").MockTransport(handler))
    with use_context(trace_id=TRACE, correlation_id="corr-1", workflow_run_id="run-1"):
        c.get("http://svc-b/x")
    assert parse_traceparent(seen["traceparent"])[0] == TRACE
    assert seen["x-correlation-id"] == "corr-1" and seen["x-obskit-workflow-run-id"] == "run-1"
