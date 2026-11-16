import json

import httpx
import pytest

from svn_obskit import Observability
from svn_obskit.core.config import ObservabilityConfig
from svn_obskit.core.exceptions import TransportError
from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.registry import Registry
from svn_obskit.worker.heartbeat import HeartbeatService
from svn_obskit.worker.lifecycle import Worker, WorkerState
from svn_obskit.worker.registration import NotRegistered, PlatformClient


class FakePlatform:
    """Records registry calls; `refuse` makes the next edit/delete fail the way the platform would."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.refuse: Exception | None = None

    def _call(self, *call):
        if self.refuse:
            raise self.refuse
        self.calls.append(call)
        return {}

    def register_service(self, identity, metadata=None):
        self.calls.append(("register_service", identity.instance_id, metadata))
        return "svc-1"

    def service_heartbeat(self, service_id, instance_id, *, stopping=False):
        self.calls.append(("service_heartbeat", service_id, stopping))

    def register_worker(self, identity, name, worker_type, capacity, metadata):
        self.calls.append(("register_worker", name, worker_type, capacity, metadata))
        return f"w-{name}"

    def worker_heartbeat(self, worker_id, status, job):
        self.calls.append(("worker_heartbeat", worker_id, status))

    def upsert_job(self, identity, update):
        self.calls.append(("upsert_job", update["external_job_id"]))

    def update_service(self, service_id, **fields):
        return self._call("update_service", service_id, fields)

    def update_instance(self, service_id, instance_id, **fields):
        return self._call("update_instance", service_id, instance_id, fields)

    def delete_instance(self, service_id, instance_id):
        return self._call("delete_instance", service_id, instance_id)

    def update_worker(self, worker_id, **fields):
        return self._call("update_worker", worker_id, fields)

    def delete_worker(self, worker_id):
        return self._call("delete_worker", worker_id)

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("OBSKIT_INSTANCE_ID", "video-1")
    return ServiceIdentity.from_config(ObservabilityConfig(service_name="video"))


def make(identity, metadata=None):
    platform = FakePlatform()
    hb = HeartbeatService(platform, identity, interval_s=60, instance_metadata=metadata)
    return platform, hb, Registry(platform, hb, identity)


def test_registration_sends_the_health_url_and_edits_reach_this_instance(identity):
    platform, hb, registry = make(identity, {"health_url": "https://video.example/health"})
    hb._tick()
    assert platform.of("register_service") == [
        ("register_service", "video-1", {"health_url": "https://video.example/health"})
    ]
    assert registry.update_service(version="2.0", metadata={"team": "media"})
    assert registry.update_instance(hostname="host-b")
    assert registry.set_health_url("https://new.example/health")
    assert platform.of("update_service") == [
        ("update_service", "svc-1", {"version": "2.0", "metadata": {"team": "media"}})
    ]
    assert platform.of("update_instance")[-1] == (
        "update_instance", "svc-1", "video-1", {"hostname": None, "metadata": {"health_url": "https://new.example/health"}}
    )  # fmt: skip


def test_an_edit_before_the_first_heartbeat_registers_first(identity):
    platform, hb, registry = make(identity)
    assert registry.update_service(version="1.1")
    assert [c[0] for c in platform.calls] == ["register_service", "update_service"]


def test_failures_return_false_and_never_raise(identity):
    platform, hb, registry = make(identity)
    hb._tick()
    platform.refuse = TransportError(
        "DELETE /services/x returned HTTP 409: has running instances", retryable=False
    )
    assert registry.update_service(version="3") is False
    platform.refuse = RuntimeError("anything else")
    assert registry.update_instance(hostname="h") is False


def test_worker_edits_are_kept_locally_so_reregistration_does_not_undo_them(identity):
    platform, hb, registry = make(identity)
    worker = Worker("render", "default", 1, {"gpu": False, "tmp": 1}).start()
    assert registry.update_worker(worker, capacity=4) is False  # not registered yet
    hb.add_worker(worker)
    hb._tick()
    assert registry.update_worker(
        worker, capacity=4, worker_type="render", metadata={"gpu": True, "tmp": None}
    )
    assert (worker.capacity, worker.worker_type, worker.metadata) == (4, "render", {"gpu": True})
    assert platform.of("update_worker") == [
        ("update_worker", "w-render", {"worker_type": "render", "capacity": 4, "metadata": {"gpu": True, "tmp": None}})
    ]  # fmt: skip


def test_removed_worker_is_stopped_deleted_and_no_longer_heartbeats(identity):
    platform, hb, registry = make(identity)
    worker = Worker("render").start()
    hb.add_worker(worker)
    hb._tick()
    assert registry.remove_worker(worker)
    assert worker.state is WorkerState.STOPPED and worker.platform_id is None
    assert platform.of("delete_worker") == [("delete_worker", "w-render")]
    before = len(platform.of("worker_heartbeat"))
    hb._tick()
    assert len(platform.of("worker_heartbeat")) == before and len(platform.of("register_worker")) == 1


def test_remove_instance_does_nothing_when_never_registered(identity):
    platform, hb, registry = make(identity)
    assert registry.remove_instance() is True and platform.calls == []
    hb._tick()
    assert registry.remove_instance() is True
    assert platform.of("delete_instance") == [("delete_instance", "svc-1", "video-1")]


def test_stopping_the_heartbeat_twice_does_not_bring_a_deleted_instance_back(identity):
    platform, hb, registry = make(identity)
    hb.start()
    hb.stop()
    registry.remove_instance()
    calls = len(platform.calls)
    hb.stop()  # what shutdown() does after unregister()
    assert len(platform.calls) == calls


def test_observability_without_a_platform_has_no_registry_and_unregister_is_harmless():
    obs = Observability("video", ObservabilityConfig(service_name="video")).initialize()
    assert obs.registry is None
    assert obs.unregister() is False


# --- the HTTP client itself --------------------------------------------------------------------


def client(handler) -> PlatformClient:
    return PlatformClient("http://platform", "secret", 5.0, transport=httpx.MockTransport(handler))


def test_client_sends_only_the_fields_given_and_escapes_the_instance_id():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.raw_path.decode(),
                request.content.decode(),
                request.headers["x-api-key"],
            )
        )
        return httpx.Response(200, json={"deleted": "instance"})

    c = client(handler)
    c.update_service("s1", version="2.0")
    c.update_instance("s1", "pod/a 1", metadata={"zone": "a"})
    c.update_worker("w1", capacity=3)
    c.delete_instance("s1", "pod/a 1")
    c.delete_worker("w1")
    c.delete_service("s1", delete_jobs=True)
    c.delete_workflow_run("r1", force=True)
    assert [(m, p) for m, p, _, _ in seen] == [
        ("PATCH", "/services/s1"),
        ("PATCH", "/services/s1/instances/pod%2Fa%201"),
        ("PATCH", "/workers/w1"),
        ("DELETE", "/services/s1/instances/pod%2Fa%201"),
        ("DELETE", "/workers/w1"),
        ("DELETE", "/services/s1?delete_jobs=true"),
        ("DELETE", "/workflow-runs/r1?force=true"),
    ]
    assert json.loads(seen[0][2]) == {"version": "2.0"} and json.loads(seen[2][2]) == {"capacity": 3}
    assert {key for *_, key in seen} == {"secret"}


def test_client_reports_a_refused_delete_with_the_platforms_reason():
    c = client(lambda r: httpx.Response(409, json={"detail": "service still has running instances: video-1"}))
    with pytest.raises(TransportError) as err:
        c.delete_service("s1")
    assert "409" in str(err.value) and "running instances: video-1" in str(err.value)
    assert err.value.retryable is False


def test_client_maps_404_to_not_registered():
    with pytest.raises(NotRegistered):
        client(lambda r: httpx.Response(404, json={"detail": "worker not found"})).delete_worker("gone")
