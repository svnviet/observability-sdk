import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol


class IdempotencyStore(Protocol):
    def claim(self, key: str, ttl_s: float) -> bool: ...
    def complete(self, key: str, ttl_s: float) -> None: ...
    def release(self, key: str) -> None: ...


class MemoryIdempotencyStore:
    """Per-process only: use SqliteIdempotencyStore (or a shared store) when several processes consume."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._items: dict[str, float] = {}  # key -> expires_at
        self._lock = threading.Lock()
        self._clock = clock

    def claim(self, key: str, ttl_s: float) -> bool:
        with self._lock:
            now = self._clock()
            if self._items.get(key, 0) > now:
                return False
            self._items[key] = now + ttl_s
            return True

    def complete(self, key: str, ttl_s: float) -> None:
        with self._lock:
            self._items[key] = self._clock() + ttl_s

    def release(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


class SqliteIdempotencyStore:
    """File-backed, so duplicates are caught across restarts and across processes on one host."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
        )
        self._lock = threading.Lock()
        self._clock = clock

    def claim(self, key: str, ttl_s: float) -> bool:
        now = self._clock()
        with self._lock:
            self._db.execute("DELETE FROM idempotency WHERE key = ? AND expires_at <= ?", (key, now))
            cur = self._db.execute(
                "INSERT OR IGNORE INTO idempotency (key, expires_at) VALUES (?, ?)", (key, now + ttl_s)
            )
            return cur.rowcount == 1

    def complete(self, key: str, ttl_s: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO idempotency (key, expires_at) VALUES (?, ?)",
                (key, self._clock() + ttl_s),
            )

    def release(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM idempotency WHERE key = ?", (key,))

    def close(self) -> None:
        self._db.close()


class IdempotencyGuard:
    """Prevents the same job key from being processed twice.

    A claim lasts `in_progress_ttl_s` (so a crashed worker's key frees itself); a finished key is
    remembered for `done_ttl_s`. A failed run releases its key so a retry can proceed.
    """

    def __init__(self, store: IdempotencyStore, done_ttl_s: float = 3600.0, in_progress_ttl_s: float = 300.0):
        self._store = store
        self._done_ttl = done_ttl_s
        self._in_progress_ttl = in_progress_ttl_s

    @contextmanager
    def once(self, key: str) -> Iterator[bool]:
        """Yields True if this caller should do the work, False if it is a duplicate."""
        if not self._store.claim(key, self._in_progress_ttl):
            yield False
            return
        try:
            yield True
        except BaseException:
            self._store.release(key)
            raise
        self._store.complete(key, self._done_ttl)
