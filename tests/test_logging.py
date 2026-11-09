import io
import json
import logging

from obskit.core.config import ObservabilityConfig
from obskit.core.context import use_context
from obskit.core.identity import ServiceIdentity
from obskit.logging.logger import configure_logging


def make_logger():
    stream = io.StringIO()
    identity = ServiceIdentity.from_config(ObservabilityConfig(service_name="video-generator"))
    configure_logging(identity, "INFO", stream=stream)
    return stream, logging.getLogger("test.logging")


def test_json_log_has_identity_and_context():
    stream, log = make_logger()
    with use_context(workflow_id="wf-1", workflow_run_id="run-1", trace_id="t" * 32):
        log.error("render failed", extra={"job_id": "job-7"})
    entry = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert entry["level"] == "ERROR" and entry["message"] == "render failed"
    assert entry["service_name"] == "video-generator" and entry["instance_id"]
    assert (entry["workflow_id"], entry["workflow_run_id"], entry["job_id"]) == ("wf-1", "run-1", "job-7")


def test_exception_logged_with_type_and_trace():
    stream, log = make_logger()
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("failed")
    entry = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert entry["exception_type"] == "ValueError" and "boom" in entry["stack_trace"]


def test_secrets_are_redacted():
    stream, log = make_logger()
    log.info(
        "calling with api_key=abc123 and Authorization: Bearer tok.en-1",
        extra={"password": "hunter2", "meta": {"token": "x", "ok": 1}},
    )
    out = stream.getvalue()
    for secret in ("abc123", "tok.en-1", "hunter2"):
        assert secret not in out
    entry = json.loads(out.strip().splitlines()[-1])
    assert entry["meta"] == {"token": "[REDACTED]", "ok": 1}
