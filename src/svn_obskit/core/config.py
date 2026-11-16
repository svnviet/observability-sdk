import os
from dataclasses import dataclass

from svn_obskit.core.exceptions import ConfigurationError


@dataclass(frozen=True)
class ObservabilityConfig:
    service_name: str
    service_version: str = "0.0.0"
    environment: str = "development"
    # Where events are delivered. None disables delivery (events are still logged/buffered locally).
    platform_url: str | None = None
    api_key: str | None = None
    # Path of the durable SQLite outbox. None keeps events in a bounded in-memory buffer only.
    outbox_path: str | None = None
    batch_size: int = 100
    flush_interval_s: float = 1.0
    buffer_size: int = 10_000  # in-memory buffer bound; oldest events are dropped beyond it
    delivery_timeout_s: float = 5.0
    max_backoff_s: float = 30.0
    log_level: str = "INFO"
    # Tracing: OTLP/HTTP endpoint of an OpenTelemetry Collector, e.g. http://otel-collector:4318.
    # None disables span export (trace ids are still propagated).
    otlp_endpoint: str | None = None
    trace_sample_ratio: float = 1.0
    metrics_enabled: bool = True  # only has an effect when prometheus-client is installed
    metrics_port: int | None = None  # serve /metrics on its own port (for processes without a web server)
    heartbeat_interval_s: float = 10.0  # keep well below the platform's heartbeat timeout (30s default)
    # URL of this instance's health route as the platform can reach it; sent at registration so the platform
    # probes it. None leaves active probing off.
    health_url: str | None = None

    def __post_init__(self) -> None:
        if not self.service_name:
            raise ConfigurationError("service_name is required")
        if self.batch_size < 1 or self.buffer_size < 1:
            raise ConfigurationError("batch_size and buffer_size must be >= 1")

    @classmethod
    def from_env(cls, service_name: str, **overrides) -> "ObservabilityConfig":
        """Read OBSKIT_* variables; explicit overrides win."""
        env = os.environ.get
        values = {
            "service_version": env("OBSKIT_SERVICE_VERSION", "0.0.0"),
            "environment": env("OBSKIT_ENVIRONMENT", "development"),
            "platform_url": env("OBSKIT_PLATFORM_URL"),
            "api_key": env("OBSKIT_API_KEY"),
            "outbox_path": env("OBSKIT_OUTBOX_PATH"),
            "log_level": env("OBSKIT_LOG_LEVEL", "INFO"),
            "flush_interval_s": float(env("OBSKIT_FLUSH_INTERVAL_S", "1.0")),
            "max_backoff_s": float(env("OBSKIT_MAX_BACKOFF_S", "30.0")),
            "otlp_endpoint": env("OBSKIT_OTLP_ENDPOINT"),
            "trace_sample_ratio": float(env("OBSKIT_TRACE_SAMPLE_RATIO", "1.0")),
            "metrics_enabled": env("OBSKIT_METRICS_ENABLED", "true").lower() != "false",
            "metrics_port": int(env("OBSKIT_METRICS_PORT")) if env("OBSKIT_METRICS_PORT") else None,
            "heartbeat_interval_s": float(env("OBSKIT_HEARTBEAT_INTERVAL_S", "10.0")),
            "health_url": env("OBSKIT_HEALTH_URL"),
        }
        values.update(overrides)
        return cls(service_name=service_name, **values)
