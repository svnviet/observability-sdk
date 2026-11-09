import threading
from collections import OrderedDict

from svn_obskit.transport.interface import Event


class MemoryBuffer:
    """Bounded in-memory buffer. When full, the oldest event is dropped (and counted)."""

    def __init__(self, max_size: int) -> None:
        self._max = max_size
        self._items: OrderedDict[int, Event] = OrderedDict()
        self._next_key = 0
        self._lock = threading.Lock()
        self.dropped = 0

    def add(self, event: Event) -> None:
        with self._lock:
            if len(self._items) >= self._max:
                self._items.popitem(last=False)
                self.dropped += 1
            self._items[self._next_key] = event
            self._next_key += 1

    def peek(self, limit: int) -> list[tuple[int, Event]]:
        with self._lock:
            return list(self._items.items())[:limit]

    def ack(self, keys: list[int]) -> None:
        with self._lock:
            for key in keys:
                self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)
