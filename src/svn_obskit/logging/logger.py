import logging
import sys
from typing import IO

from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.logging.context_filter import ContextFilter
from svn_obskit.logging.formatter import JsonFormatter

_HANDLER_MARK = "_obskit_handler"


def configure_logging(
    identity: ServiceIdentity,
    level: str = "INFO",
    stream: IO[str] | None = None,
    file_path: str | None = None,
) -> None:
    """Install JSON handlers on the root logger. Safe to call again: it replaces its own handlers."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if file_path:
        handlers.append(logging.FileHandler(file_path))
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        handler.addFilter(ContextFilter(identity))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(level.upper())
    # The SDK's own heartbeats and event batches go through httpx; logging each request at INFO
    # would bury the application's logs in the SDK's own traffic.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
