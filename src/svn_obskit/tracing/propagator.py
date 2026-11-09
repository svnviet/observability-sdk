"""Header propagation lives in core.correlation (W3C traceparent + x-correlation-id + workflow ids).

With tracing enabled the `traceparent` sent downstream is built from the current span, so a child
service's spans attach to the caller's span in the trace view.
"""

from svn_obskit.core.correlation import extract_context, inject_headers, parse_traceparent

__all__ = ["extract_context", "inject_headers", "parse_traceparent"]
