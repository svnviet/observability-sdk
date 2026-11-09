import uuid
from datetime import UTC, datetime
from typing import Any

from svn_obskit.core.context import current_context
from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.transport.interface import Event

EVENT_VERSION = 1

WORKFLOW_STARTED = "WORKFLOW_STARTED"
WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
WORKFLOW_FAILED = "WORKFLOW_FAILED"
WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"
STEP_STARTED = "STEP_STARTED"
STEP_COMPLETED = "STEP_COMPLETED"
STEP_FAILED = "STEP_FAILED"


def attempt_id(run_id: str, step_id: str, attempt: int) -> str:
    # Deterministic, so every service touching the same attempt agrees on its identity.
    return f"{run_id}:{step_id}:{attempt}"


def build_event(
    identity: ServiceIdentity,
    event_type: str,
    *,
    workflow_id: str,
    run_id: str,
    trace_id: str | None,
    correlation_id: str | None,
    step_id: str | None = None,
    attempt: int | None = None,
    data: dict[str, Any] | None = None,
) -> Event:
    """Create an event matching the platform's workflow event contract (version 1)."""
    ctx = current_context()
    context: dict[str, Any] = {
        "workflow_id": workflow_id,
        "workflow_run_id": run_id,
        "trace_id": trace_id,
        "correlation_id": correlation_id,
    }
    if step_id is not None:
        context["step_id"] = step_id
        if attempt is not None:
            context["step_attempt_id"] = attempt_id(run_id, step_id, attempt)
    payload = dict(data or {})
    if attempt is not None:
        payload["attempt_number"] = attempt
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "event_version": EVENT_VERSION,
        "occurred_at": datetime.now(UTC).isoformat(),
        "source": {
            "service_name": identity.service_name,
            "service_version": identity.service_version,
            "environment": identity.environment,
            "instance_id": identity.instance_id,
            "worker_id": ctx.worker_id,
        },
        "context": {k: v for k, v in context.items() if v is not None},
        "data": payload,
    }
