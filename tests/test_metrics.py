from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from svn_obskit.integrations import httpx as obs_httpx
from svn_obskit.metrics.registry import BoundedLabels
from svn_obskit.middleware.fastapi import ObservabilityMiddleware


def sample(obs, name, **labels):
    return obs.metrics.registry.get_sample_value(name, labels)


def test_workflow_and_step_metrics(obs):
    run = obs.tracker.start_workflow("news-video-pipeline", workflow_id="wf-1")
    for attempt in (1, 2):
        try:
            with run.step("render", attempt=attempt, max_attempts=2):
                if attempt == 1:
                    raise TimeoutError("slow")
        except TimeoutError:
            pass
    obs.tracker.complete_workflow(run)
    wf = "news-video-pipeline"
    assert sample(obs, "obskit_workflows_started_total", workflow=wf) == 1
    assert sample(obs, "obskit_workflows_finished_total", workflow=wf, status="completed") == 1
    assert sample(obs, "obskit_workflow_duration_seconds_count", workflow=wf, status="completed") == 1
    assert sample(obs, "obskit_workflow_step_retries_total", workflow=wf, step="render") == 1
    assert (
        sample(
            obs, "obskit_workflow_step_duration_seconds_count", workflow=wf, step="render", status="retrying"
        )
        == 1
    )
    assert (
        sample(
            obs, "obskit_workflow_step_duration_seconds_count", workflow=wf, step="render", status="completed"
        )
        == 1
    )
    assert sample(obs, "obskit_exceptions_total", exception_type="TimeoutError", source="workflow") == 1


def test_failed_and_cancelled_workflows_are_counted_separately(obs):
    with pytest.raises(ValueError):
        with obs.tracker.workflow("p"):
            raise ValueError("x")
    for status in ("failed",):
        assert sample(obs, "obskit_workflows_finished_total", workflow="p", status=status) == 1
    assert sample(obs, "obskit_workflows_finished_total", workflow="p", status="completed") is None


def test_queue_wait_time(obs):
    obs.tracker.start_workflow("p", queued_at=datetime.now(UTC) - timedelta(seconds=5))
    assert sample(obs, "obskit_workflow_queue_wait_seconds_count", workflow="p") == 1
    assert sample(obs, "obskit_workflow_queue_wait_seconds_sum", workflow="p") >= 5


def test_no_ids_or_messages_become_label_values(obs):
    with (
        obs.tracker.workflow("p", workflow_id="wf-secret-123") as wf,
        wf.step("render", step_id="step-abc-999"),
    ):
        pass
    text = obs.metrics.render().decode()
    assert "wf-secret-123" not in text and wf.id not in text and "step-abc-999" not in text


def test_bounded_labels_collapse_unexpected_values():
    labels = BoundedLabels(max_values=3)
    assert [labels.clamp("route", f"/r{i}") for i in range(5)] == ["/r0", "/r1", "/r2", "other", "other"]
    assert labels.clamp("route", "/r1") == "/r1"  # values already seen keep working
    assert labels.clamp("other_label", "x") == "x"  # limits are per label


def test_runaway_workflow_names_cannot_blow_up_the_series_count(obs):
    for i in range(300):
        obs.metrics.workflow_started(f"dynamic-{i}")
    series = [
        line
        for line in obs.metrics.render().decode().splitlines()
        if line.startswith("obskit_workflows_started_total{")
    ]
    assert len(series) == 101  # 100 distinct names + "other"


def make_app(obs):
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, service_name="svc-a")

    @app.get("/items/{item_id}")
    def item(item_id: str):
        if item_id == "boom":
            raise RuntimeError("db down")
        return {"id": item_id}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    assert obs.mount_metrics(app)
    return app


def test_http_metrics_use_route_templates_not_raw_paths(obs):
    client = TestClient(make_app(obs), raise_server_exceptions=False)
    for i in range(3):
        assert client.get(f"/items/{i}").status_code == 200
    assert client.get("/items/boom").status_code == 500
    client.get("/nope")
    client.get("/health")  # excluded: probes are not traffic

    assert (
        sample(obs, "obskit_http_requests_total", method="GET", route="/items/{item_id}", status_class="2xx")
        == 3
    )
    assert (
        sample(obs, "obskit_http_requests_total", method="GET", route="/items/{item_id}", status_class="5xx")
        == 1
    )
    assert sample(obs, "obskit_http_requests_total", method="GET", route="unmatched", status_class="4xx") == 1
    assert (
        sample(obs, "obskit_http_request_duration_seconds_count", method="GET", route="/items/{item_id}") == 4
    )
    assert sample(obs, "obskit_exceptions_total", exception_type="RuntimeError", source="http") == 1
    assert sample(obs, "obskit_http_requests_in_progress") == 0
    text = obs.metrics.render().decode()
    assert "/items/1" not in text and "/health" not in text


def test_metrics_endpoint_serves_prometheus_text(obs):
    client = TestClient(make_app(obs))
    client.get("/items/1")
    response = client.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    assert (
        'obskit_http_requests_total{method="GET",route="/items/{item_id}",status_class="2xx"} 1.0'
        in response.text
    )
    assert 'obskit_build_info{environment="test",service_name="svc-a"' in response.text


def test_outgoing_call_metrics_by_host_and_status(obs):
    client = obs_httpx.client(transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    client.get("http://payments.internal/charge")
    assert (
        sample(
            obs,
            "obskit_http_client_request_duration_seconds_count",
            host="payments.internal",
            method="GET",
            status_class="5xx",
        )
        == 1
    )

    def refuse(request):
        raise httpx.ConnectError("no route")

    broken = obs_httpx.client(transport=httpx.MockTransport(refuse))
    with pytest.raises(httpx.ConnectError):
        broken.get("http://payments.internal/charge")
    assert (
        sample(
            obs,
            "obskit_http_client_request_duration_seconds_count",
            host="payments.internal",
            method="GET",
            status_class="error",
        )
        == 1
    )


def test_worker_and_job_metrics(obs):
    worker = obs.worker("render-1", worker_type="render", capacity=2)
    assert sample(obs, "obskit_workers", worker_type="render", state="IDLE") == 1
    assert sample(obs, "obskit_workers_available", worker_type="render") == 1
    with worker.job("job-1"):
        assert sample(obs, "obskit_workers", worker_type="render", state="BUSY") == 1
        assert sample(obs, "obskit_workers", worker_type="render", state="IDLE") is None
    with pytest.raises(RuntimeError):
        with worker.job("job-2"):
            raise RuntimeError("oom")
    assert sample(obs, "obskit_worker_jobs_total", worker_type="render", result="completed") == 1
    assert sample(obs, "obskit_worker_jobs_total", worker_type="render", result="failed") == 1
    assert sample(obs, "obskit_worker_job_duration_seconds_count", worker_type="render") == 2
    assert sample(obs, "obskit_exceptions_total", exception_type="RuntimeError", source="worker") == 1
    worker.drain()
    assert sample(obs, "obskit_workers_available", worker_type="render") == 0


def test_process_and_heartbeat_metrics(obs):
    assert sample(obs, "obskit_process_uptime_seconds") > 0
    assert sample(obs, "obskit_process_resident_memory_bytes") > 1_000_000
    assert sample(obs, "obskit_process_threads") >= 1
    assert sample(obs, "obskit_process_cpu_seconds_total") >= 0
    assert sample(obs, "obskit_process_open_fds") > 0
    assert sample(obs, "obskit_worker_heartbeat_age_seconds") is None  # no platform, no heartbeat yet


def test_database_operations_are_timed_and_spanned_without_leaking_sql(obs):
    from sqlalchemy import create_engine, text

    from svn_obskit.integrations.sqlalchemy import instrument

    engine = create_engine("sqlite://")
    instrument(engine)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (secret TEXT)"))
        conn.execute(text("INSERT INTO t VALUES ('hunter2')"))
        conn.execute(text("SELECT secret FROM t")).all()
    for op in ("CREATE", "INSERT", "SELECT"):
        assert sample(obs, "obskit_db_operation_duration_seconds_count", operation=op) == 1
    names = {s.name for s in obs.exporter.get_finished_spans()}
    assert {"db SELECT", "db INSERT"} <= names
    assert "hunter2" not in obs.metrics.render().decode()
    assert all("hunter2" not in str(dict(s.attributes)) for s in obs.exporter.get_finished_spans())


def test_metrics_can_be_switched_off():
    from svn_obskit import Observability

    o = Observability("svc-x", metrics_enabled=False, heartbeat_interval_s=3600).initialize()
    try:
        app = FastAPI()
        assert o.metrics is None and o.mount_metrics(app) is True
        assert TestClient(app).get("/metrics").status_code == 503
        with o.tracker.workflow("p"):
            pass
    finally:
        o.shutdown()
