import time

import httpx
import pytest

from obskit.core.exceptions import TransportError
from obskit.transport.background import BackgroundPublisher
from obskit.transport.http import HttpTransport
from obskit.transport.memory import MemoryBuffer
from obskit.transport.outbox import SqliteOutbox


def ev(n):
    return {"event_id": f"e{n}", "n": n}


class ScriptedTransport:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent: list[dict] = []

    def send(self, events):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.extend(events)


def publisher(buffer, transport):
    p = BackgroundPublisher(buffer, transport, batch_size=10, flush_interval_s=0.02, max_backoff_s=0.05)
    p.start()
    return p


def test_memory_buffer_is_bounded_and_counts_drops():
    buf = MemoryBuffer(3)
    for n in range(5):
        buf.add(ev(n))
    assert [e["n"] for _, e in buf.peek(10)] == [2, 3, 4] and buf.dropped == 2


def test_outbox_survives_reopen_and_dedupes(tmp_path):
    path = str(tmp_path / "outbox.db")
    box = SqliteOutbox(path)
    box.add(ev(1))
    box.add(ev(1))  # same event_id
    box.add(ev(2))
    box.close()
    reopened = SqliteOutbox(path)
    items = reopened.peek(10)
    assert [e["n"] for _, e in items] == [1, 2]
    reopened.ack([items[0][0]])
    assert len(reopened) == 1


def test_delivery_retries_until_platform_recovers():
    transport = ScriptedTransport([TransportError("down"), TransportError("down")])
    buf = MemoryBuffer(100)
    pub = publisher(buf, transport)
    for n in range(5):
        pub.submit(ev(n))
    assert pub.flush(3.0)
    assert [e["n"] for e in transport.sent] == [0, 1, 2, 3, 4]
    pub.shutdown()


def test_events_stay_in_outbox_while_platform_is_down_and_flow_after(tmp_path):
    box = SqliteOutbox(str(tmp_path / "o.db"))
    down = ScriptedTransport([TransportError("down")] * 1000)
    pub = publisher(box, down)
    pub.submit(ev(1))
    assert not pub.flush(0.3) and len(box) == 1  # not lost
    pub.shutdown(0.1)

    up = ScriptedTransport()  # "restart" with the platform reachable
    pub2 = publisher(box, up)
    assert pub2.flush(3.0) and up.sent == [ev(1)]
    pub2.shutdown()


def test_permanent_failure_drops_batch_instead_of_looping_forever():
    transport = ScriptedTransport([TransportError("bad request", retryable=False)])
    pub = publisher(MemoryBuffer(10), transport)
    pub.submit(ev(1))
    assert pub.flush(2.0) and pub.dropped_permanent == 1
    pub.shutdown()


def test_submit_never_blocks_or_raises_when_no_transport():
    pub = publisher(MemoryBuffer(5), None)
    for n in range(50):
        pub.submit(ev(n))
    assert pub.pending() == 5
    pub.shutdown(0.1)


@pytest.mark.parametrize(
    ("status", "retryable"), [(500, True), (503, True), (429, True), (401, True), (400, False), (422, False)]
)
def test_http_status_classification(status, retryable):
    t = HttpTransport("http://platform", "key", 1.0)
    t._client = httpx.Client(
        base_url="http://platform", transport=httpx.MockTransport(lambda r: httpx.Response(status))
    )
    with pytest.raises(TransportError) as err:
        t.send([ev(1)])
    assert err.value.retryable is retryable


def test_http_success_and_key_header():
    seen = {}

    def handler(request):
        seen["key"], seen["path"] = request.headers["x-api-key"], request.url.path
        return httpx.Response(200, json={})

    t = HttpTransport("http://platform", "secret", 1.0)
    t._client = httpx.Client(
        base_url="http://platform", headers={"X-API-Key": "secret"}, transport=httpx.MockTransport(handler)
    )
    t.send([ev(1)])
    assert seen == {"key": "secret", "path": "/events/batch"}


def test_network_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("refused")

    t = HttpTransport("http://platform", None, 1.0)
    t._client = httpx.Client(base_url="http://platform", transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError) as err:
        t.send([ev(1)])
    assert err.value.retryable


def test_permanent_failure_moves_events_to_dead_letters_in_the_outbox(tmp_path):
    box = SqliteOutbox(str(tmp_path / "o.db"))
    pub = publisher(box, ScriptedTransport([TransportError("rejected", retryable=False)]))
    pub.submit(ev(1))
    assert pub.flush(2.0)
    assert len(box) == 0 and box.dead_letter_count() == 1  # kept for inspection, not retried
    pub.shutdown()


def test_shutdown_does_not_wait_when_there_is_no_transport():
    pub = publisher(MemoryBuffer(5), None)
    pub.submit(ev(1))
    started = time.monotonic()
    assert pub.shutdown(timeout_s=5.0) is False  # reports that an event is still buffered...
    assert time.monotonic() - started < 1.0  # ...without making the service wait for it
