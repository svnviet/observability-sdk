from typing import Protocol

from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.transport.interface import Event


class EventSink(Protocol):
    identity: ServiceIdentity

    def emit(self, event: Event) -> None: ...


_active: EventSink | None = None


def set_active(sink: EventSink | None) -> None:
    global _active
    _active = sink


def get_active() -> EventSink | None:
    return _active
