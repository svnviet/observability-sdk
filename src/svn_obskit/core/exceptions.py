class ObservabilityError(Exception):
    """Base class for SDK errors."""


class ConfigurationError(ObservabilityError):
    pass


class TransportError(ObservabilityError):
    """Event delivery failed. `retryable` says whether resending the same events can succeed."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
