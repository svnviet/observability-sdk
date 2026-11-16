import logging

from svn_obskit.core.exceptions import TransportError
from svn_obskit.core.identity import ServiceIdentity
from svn_obskit.worker.heartbeat import HeartbeatService
from svn_obskit.worker.lifecycle import Worker
from svn_obskit.worker.registration import PlatformClient

log = logging.getLogger("svn_obskit.registry")


class Registry:
    """Edit or remove what this process registered on the platform (its service, instance and workers).

    Registration itself is automatic (`Observability.initialize()` and `obs.worker(...)`). Every method here
    returns True on success and False on failure, logging the reason; none raises into business code.
    Scope is deliberately "this service only": the platform's key is shared, so the SDK offers no way to
    touch another service's records.
    """

    def __init__(
        self, client: PlatformClient, heartbeat: HeartbeatService, identity: ServiceIdentity
    ) -> None:
        self._client = client
        self._heartbeat = heartbeat
        self._identity = identity

    def update_service(self, *, version: str | None = None, metadata: dict | None = None) -> bool:
        """Change the service's version or metadata. Name and environment are its identity and cannot change."""
        return self._do(
            "update service",
            lambda sid: self._client.update_service(sid, version=version, metadata=metadata),
        )

    def update_instance(self, *, hostname: str | None = None, metadata: dict | None = None) -> bool:
        """Change this instance's hostname or metadata (merged; a key set to None is removed)."""
        return self._do(
            "update instance",
            lambda sid: self._client.update_instance(
                sid, self._identity.instance_id, hostname=hostname, metadata=metadata
            ),
        )

    def set_health_url(self, url: str) -> bool:
        """Tell the platform where to probe this instance. The URL must be reachable from the platform."""
        return self.update_instance(metadata={"health_url": url})

    def update_worker(
        self,
        worker: Worker,
        *,
        worker_type: str | None = None,
        capacity: int | None = None,
        metadata: dict | None = None,
    ) -> bool:
        if worker.platform_id is None:
            log.warning("update worker %s skipped: it is not registered yet", worker.name)
            return False
        ok = self._do(
            f"update worker {worker.name}",
            lambda _sid: self._client.update_worker(
                worker.platform_id, worker_type=worker_type, capacity=capacity, metadata=metadata
            ),
        )
        if ok:  # keep the local copy in step, or the next re-registration would undo the edit
            worker.worker_type = worker_type or worker.worker_type
            worker.capacity = capacity or worker.capacity
            if metadata:
                merged = {**worker.metadata, **metadata}
                worker.metadata = {k: v for k, v in merged.items() if v is not None}
        return ok

    def remove_worker(self, worker: Worker) -> bool:
        """Stop a worker for good and delete its record. Its jobs keep their history on the platform."""
        worker.stop()
        self._heartbeat.remove_worker(worker)
        if worker.platform_id is None:
            return True  # never reached the platform: nothing to delete
        worker_id, worker.platform_id = worker.platform_id, None
        return self._do(f"remove worker {worker.name}", lambda _sid: self._client.delete_worker(worker_id))

    def remove_instance(self) -> bool:
        """Delete this instance's record (with its workers). Used by `Observability.unregister()`."""
        if self._heartbeat.service_id is None:
            return True  # never registered: there is no record to delete
        return self._do(
            "remove instance",
            lambda sid: self._client.delete_instance(sid, self._identity.instance_id),
        )

    def _do(self, what: str, call) -> bool:
        try:
            call(self._heartbeat.ensure_registered())
            return True
        except TransportError as exc:
            log.warning("%s failed: %s", what, exc)
        except Exception:  # noqa: BLE001 - the registry must never break business code
            log.warning("%s failed", what, exc_info=True)
        return False
