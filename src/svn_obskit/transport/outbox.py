import json
import sqlite3
import threading

from svn_obskit.transport.interface import Event


class SqliteOutbox:
    """Durable buffer: events survive process crashes and restarts until the platform acks them.

    Delivery is at-least-once; the platform deduplicates by event_id.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS dead_letters ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, payload TEXT NOT NULL, reason TEXT NOT NULL)"
        )

    def add(self, event: Event) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO outbox (event_id, payload) VALUES (?, ?)",
                (event["event_id"], json.dumps(event)),
            )

    def peek(self, limit: int) -> list[tuple[int, Event]]:
        with self._lock:
            rows = self._db.execute("SELECT id, payload FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(row_id, json.loads(payload)) for row_id, payload in rows]

    def ack(self, keys: list[int]) -> None:
        if not keys:
            return
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(k,) for k in keys])

    def dead_letter(self, keys: list[int], reason: str) -> None:
        """Move events the platform will never accept out of the live queue, keeping them for inspection."""
        if not keys:
            return
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for key in keys:
                    self._db.execute(
                        "INSERT INTO dead_letters (event_id, payload, reason) "
                        "SELECT event_id, payload, ? FROM outbox WHERE id = ?",
                        (reason, key),
                    )
                    self._db.execute("DELETE FROM outbox WHERE id = ?", (key,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def dead_letter_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
