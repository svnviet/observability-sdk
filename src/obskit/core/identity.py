import os
import socket
import uuid
from dataclasses import asdict, dataclass

from obskit.core.config import ObservabilityConfig


@dataclass(frozen=True)
class ServiceIdentity:
    service_name: str
    service_version: str
    environment: str
    instance_id: str
    hostname: str
    process_id: int

    @classmethod
    def from_config(cls, config: ObservabilityConfig) -> "ServiceIdentity":
        hostname = socket.gethostname()
        # pid + random suffix keeps instances distinct across restarts and forked workers.
        instance_id = os.environ.get("OBSKIT_INSTANCE_ID") or (
            f"{config.service_name}-{hostname}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        )
        return cls(
            service_name=config.service_name,
            service_version=config.service_version,
            environment=config.environment,
            instance_id=instance_id,
            hostname=hostname,
            process_id=os.getpid(),
        )

    def as_dict(self) -> dict:
        return asdict(self)
