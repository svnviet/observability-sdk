import asyncio

from obskit.core.context import current_context, use_context
from obskit.core.correlation import extract_context, inject_headers, parse_traceparent

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def test_traceparent_roundtrip_keeps_trace_and_changes_span():
    ctx = extract_context({"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01", "X-Correlation-ID": "corr-9"})
    assert ctx.trace_id == TRACE and ctx.correlation_id == "corr-9"
    with use_context(**vars(ctx)):
        headers = inject_headers(current_context())
    trace, span = parse_traceparent(headers["traceparent"])
    assert trace == TRACE and span != "00f067aa0ba902b7"
    assert headers["x-correlation-id"] == "corr-9"


def test_invalid_or_missing_traceparent_starts_new_trace():
    for bad in (None, "garbage", f"00-{'0' * 32}-00f067aa0ba902b7-01"):
        ctx = extract_context({} if bad is None else {"traceparent": bad})
        assert len(ctx.trace_id) == 32 and ctx.trace_id != "0" * 32


def test_workflow_headers_propagate():
    with use_context(workflow_id="wf", workflow_run_id="run", step_id="s", trace_id=TRACE):
        headers = inject_headers(current_context())
    ctx = extract_context(headers)
    assert (ctx.workflow_id, ctx.workflow_run_id, ctx.step_id) == ("wf", "run", "s")


def test_context_is_restored_after_block():
    with use_context(job_id="j1"):
        with use_context(job_id="j2"):
            assert current_context().job_id == "j2"
        assert current_context().job_id == "j1"
    assert current_context().job_id is None


def test_async_tasks_are_isolated():
    async def task(name):
        with use_context(job_id=name):
            await asyncio.sleep(0.01)
            return current_context().job_id

    async def main():
        return await asyncio.gather(*(task(f"j{i}") for i in range(20)))

    assert asyncio.run(main()) == [f"j{i}" for i in range(20)]
