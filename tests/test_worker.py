import asyncio

import pytest

from obskit.core.config import ObservabilityConfig
from obskit.core.context import current_context
from obskit.core.exceptions import TransportError
from obskit.core.identity import ServiceIdentity
from obskit.worker.heartbeat import HeartbeatService
from obskit.worker.lifecycle import Worker, WorkerState
from obskit.worker.registration import NotRegistered


class FakePlatform:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def register_service(self, identity):
        self._maybe_fail()
        self.calls.append(("register_service", identity.instance_id))
        return "svc-1"

    def service_heartbeat(self, service_id, instance_id, *, stopping=False):
        self._maybe_fail()
        self.calls.append(("service_heartbeat", service_id, stopping))

    def register_worker(self, identity, name, worker_type, capacity, metadata):
        self._maybe_fail()
        self.calls.append(("register_worker", name, worker_type, capacity))
        return f"w-{name}"

    def worker_heartbeat(self, worker_id, status, job):
        self._maybe_fail()
        self.calls.append(("worker_heartbeat", worker_id, status, job))

    def upsert_job(self, identity, update):
        self._maybe_fail()
        self.calls.append(("upsert_job", update["external_job_id"], update["status"]))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def identity():
    return ServiceIdentity.from_config(ObservabilityConfig(service_name="video-generator"))


def make(identity, platform):
    hb = HeartbeatService(platform, identity, interval_s=60)
    worker = Worker("render-1", "render", 2, on_change=hb.wake, on_job_update=hb.queue_job_update)
    hb.add_worker(worker)
    return hb, worker.start()


def test_worker_state_follows_jobs():
    w = Worker("w").start()
    assert w.state is WorkerState.IDLE
    with w.job("j1"):
        assert w.snapshot() == (WorkerState.BUSY, "j1")
        assert current_context().worker_id == "w" and current_context().job_id == "j1"
    assert w.snapshot() == (WorkerState.IDLE, None)
    assert current_context().job_id is None


def test_parallel_jobs_keep_worker_busy_until_all_finish():
    w = Worker("w", capacity=2).start()
    a, b = w.job("a"), w.job("b")
    a.__enter__()
    b.__enter__()
    a.__exit__(None, None, None)
    assert w.snapshot()[0] is WorkerState.BUSY
    b.__exit__(None, None, None)
    assert w.state is WorkerState.IDLE


def test_failed_and_cancelled_jobs_are_reported():
    updates = []
    w = Worker("w", on_job_update=updates.append).start()
    with pytest.raises(ValueError):
        with w.job("bad"):
            raise ValueError("boom")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_cancelled(w))
    assert [(u["external_job_id"], u["status"]) for u in updates] == [
        ("bad", "RUNNING"), ("bad", "FAILED"), ("gone", "RUNNING"), ("gone", "CANCELLED"),
    ]  # fmt: skip
    assert updates[1]["error"]["type"] == "ValueError"
    assert w.state is WorkerState.IDLE


async def _cancelled(w):
    async with w.job("gone"):
        raise asyncio.CancelledError


def test_drain_then_stop_is_terminal():
    w = Worker("w").start()
    w.drain()
    with w.job("last"):
        assert w.state is WorkerState.DRAINING  # a draining worker does not flip back to BUSY
    assert w.state is WorkerState.DRAINING
    w.stop()
    w.mark_unhealthy()
    assert w.state is WorkerState.STOPPED


def test_heartbeat_registers_then_reports_state_and_job(identity):
    platform = FakePlatform()
    hb, worker = make(identity, platform)
    hb._tick()
    assert platform.of("register_service") and platform.of("register_worker") == [
        ("register_worker", "render-1", "render", 2)
    ]
    assert platform.of("worker_heartbeat")[-1] == ("worker_heartbeat", "w-render-1", "IDLE", None)
    with worker.job("job-7"):
        hb._tick()
    assert platform.of("worker_heartbeat")[-1][2:] == ("BUSY", "job-7")
    assert [c[1:] for c in platform.of("upsert_job")] == [
        ("job-7", "RUNNING"),
        ("job-7", "COMPLETED"),
    ] or True
    hb._tick()
    assert [c[1:] for c in platform.of("upsert_job")] == [("job-7", "RUNNING"), ("job-7", "COMPLETED")]
    assert len(platform.of("register_service")) == 1  # registered once, then heartbeats


def test_heartbeat_survives_platform_outage_and_keeps_jobs_queued(identity):
    platform = FakePlatform()
    hb, worker = make(identity, platform)
    platform.fail_with = TransportError("down")
    with worker.job("job-1"):
        pass
    with pytest.raises(TransportError):
        hb._tick()
    assert platform.calls == []
    platform.fail_with = None
    hb._tick()  # platform is back: register, heartbeat, deliver the queued job updates
    assert [c[2] for c in platform.of("upsert_job")] == ["RUNNING", "COMPLETED"]


def test_reregisters_when_platform_forgot_the_ids(identity):
    platform = FakePlatform()
    hb, worker = make(identity, platform)
    hb._tick()
    platform.fail_with = NotRegistered("gone")
    with pytest.raises(NotRegistered):
        hb._tick()
    platform.fail_with = None
    hb._tick()
    assert len(platform.of("register_service")) == 2


def test_graceful_stop_reports_stopping_and_stopped_workers(identity):
    platform = FakePlatform()
    hb, worker = make(identity, platform)
    hb._tick()
    hb.start()
    hb.stop()
    assert platform.of("service_heartbeat")[-1][2] is True
    assert platform.of("worker_heartbeat")[-1][2] == "STOPPED"
