import re
import uuid
from collections.abc import Mapping

from obskit.core.context import ExecutionContext

TRACEPARENT = "traceparent"
CORRELATION_ID = "x-correlation-id"
WORKFLOW_ID = "x-obskit-workflow-id"
WORKFLOW_NAME = "x-obskit-workflow-name"
WORKFLOW_RUN_ID = "x-obskit-workflow-run-id"
STEP_ID = "x-obskit-step-id"

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def new_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex[:16]}"


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    """W3C Trace Context `traceparent` -> (trace_id, parent_span_id), or None if absent/invalid."""
    match = _TRACEPARENT_RE.match((value or "").strip().lower())
    if not match or set(match.group(1)) == {"0"} or set(match.group(2)) == {"0"}:
        return None
    return match.group(1), match.group(2)


def inject_headers(ctx: ExecutionContext) -> dict[str, str]:
    """Headers that carry `ctx` to another service. Each call mints a fresh child span id."""
    headers: dict[str, str] = {}
    if ctx.trace_id:
        # The current span is the parent of whatever the receiver does; without spans a fresh id stands in.
        headers[TRACEPARENT] = f"00-{ctx.trace_id}-{ctx.span_id or new_span_id()}-01"
    for header, value in (
        (CORRELATION_ID, ctx.correlation_id),
        (WORKFLOW_ID, ctx.workflow_id),
        (WORKFLOW_NAME, ctx.workflow_name),
        (WORKFLOW_RUN_ID, ctx.workflow_run_id),
        (STEP_ID, ctx.step_id),
    ):
        if value:
            headers[header] = value
    return headers


def extract_context(headers: Mapping[str, str], service_name: str | None = None) -> ExecutionContext:
    """Rebuild a context from incoming headers (keys compared case-insensitively).

    Always returns a usable context: a request with no headers starts a new trace.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    parsed = parse_traceparent(lowered.get(TRACEPARENT))
    return ExecutionContext(
        trace_id=parsed[0] if parsed else new_trace_id(),
        span_id=new_span_id(),
        correlation_id=lowered.get(CORRELATION_ID) or new_correlation_id(),
        workflow_id=lowered.get(WORKFLOW_ID),
        workflow_name=lowered.get(WORKFLOW_NAME),
        workflow_run_id=lowered.get(WORKFLOW_RUN_ID),
        step_id=lowered.get(STEP_ID),
        service_name=service_name,
    )
