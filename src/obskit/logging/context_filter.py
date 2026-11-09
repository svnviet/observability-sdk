import logging
from dataclasses import asdict

from obskit.core.context import current_context
from obskit.core.identity import ServiceIdentity


class ContextFilter(logging.Filter):
    """Attach service identity and the current execution context to every record."""

    def __init__(self, identity: ServiceIdentity) -> None:
        super().__init__()
        self._identity = identity

    def filter(self, record: logging.LogRecord) -> bool:
        record.obskit = {
            "service_name": self._identity.service_name,
            "instance_id": self._identity.instance_id,
            **{k: v for k, v in asdict(current_context()).items() if v and k != "service_name"},
        }
        return True
