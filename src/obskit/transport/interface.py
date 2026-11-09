from typing import Any, Protocol

Event = dict[str, Any]


class Transport(Protocol):
    def send(self, events: list[Event]) -> None:
        """Deliver events. Raise TransportError(retryable=...) on failure."""
        ...


class EventBuffer(Protocol):
    """Holds events until delivery is acknowledged."""

    def add(self, event: Event) -> None: ...

    def peek(self, limit: int) -> list[tuple[int, Event]]:
        """Oldest events first as (key, event); they stay buffered until `ack`."""
        ...

    def ack(self, keys: list[int]) -> None: ...

    def __len__(self) -> int: ...
