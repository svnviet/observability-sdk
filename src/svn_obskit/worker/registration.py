from urllib.parse import quote

import httpx

from svn_obskit.core.exceptions import TransportError
from svn_obskit.core.identity import ServiceIdentity


class NotRegistered(TransportError):
    """The platform does not know the id we used (for example after its database was reset)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class PlatformClient:
    """Small synchronous client for the platform's registry and job endpoints."""

    def __init__(self, base_url: str, api_key: str | None, timeout_s: float, *, transport=None) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s, transport=transport
        )

    def register_service(self, identity: ServiceIdentity, metadata: dict | None = None) -> str:
        data = self._post(
            "/services/register",
            {
                "service_name": identity.service_name,
                "instance_id": identity.instance_id,
                "environment": identity.environment,
                "service_version": identity.service_version,
                "hostname": identity.hostname,
                "metadata": {"process_id": identity.process_id, **(metadata or {})},
            },
        )
        return data["service_id"]

    # --- edit and remove what this service registered -----------------------------------

    def update_service(
        self, service_id: str, *, version: str | None = None, metadata: dict | None = None
    ) -> dict:
        return self._send("PATCH", f"/services/{service_id}", _given(version=version, metadata=metadata))

    def find_service_id(self, service_name: str, environment: str) -> str | None:
        """Look a service up by its identity without registering an instance (reads need no key)."""
        for service in self._get("/services"):
            if service["name"] == service_name and service["environment"] == environment:
                return service["id"]
        return None

    def update_instance(
        self, service_id: str, instance_id: str, *, hostname: str | None = None, metadata: dict | None = None
    ) -> dict:
        path = f"/services/{service_id}/instances/{quote(instance_id, safe='')}"
        return self._send("PATCH", path, _given(hostname=hostname, metadata=metadata))

    def delete_instance(self, service_id: str, instance_id: str) -> dict:
        return self._send("DELETE", f"/services/{service_id}/instances/{quote(instance_id, safe='')}")

    def delete_service(self, service_id: str, *, delete_jobs: bool = False) -> dict:
        return self._send(
            "DELETE", f"/services/{service_id}", params={"delete_jobs": str(delete_jobs).lower()}
        )

    def update_worker(
        self,
        worker_id: str,
        *,
        worker_type: str | None = None,
        capacity: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        body = _given(worker_type=worker_type, capacity=capacity, metadata=metadata)
        return self._send("PATCH", f"/workers/{worker_id}", body)

    def delete_worker(self, worker_id: str) -> dict:
        return self._send("DELETE", f"/workers/{worker_id}")

    def delete_workflow_run(self, run_id: str, *, force: bool = False) -> dict:
        """`run_id` is the platform's UUID of the run (from GET /workflow-runs), not the SDK's run key."""
        return self._send("DELETE", f"/workflow-runs/{run_id}", params={"force": str(force).lower()})

    def service_heartbeat(self, service_id: str, instance_id: str, *, stopping: bool = False) -> None:
        self._post(f"/services/{service_id}/heartbeat", {"instance_id": instance_id, "stopping": stopping})

    def register_worker(
        self, identity: ServiceIdentity, name: str, worker_type: str, capacity: int, metadata: dict
    ) -> str:
        data = self._post(
            "/workers/register",
            {
                "service_name": identity.service_name,
                "instance_id": identity.instance_id,
                "environment": identity.environment,
                "service_version": identity.service_version,
                "worker_name": name,
                "worker_type": worker_type,
                "capacity": capacity,
                "metadata": metadata,
            },
        )
        return data["id"]

    def worker_heartbeat(self, worker_id: str, status: str, current_job_id: str | None) -> None:
        self._post(f"/workers/{worker_id}/heartbeat", {"status": status, "current_job_id": current_job_id})

    def upsert_job(self, identity: ServiceIdentity, update: dict) -> None:
        self._post(
            "/jobs",
            {"service_name": identity.service_name, "environment": identity.environment,
             "instance_id": identity.instance_id, **update},
        )  # fmt: skip

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str):
        return self._send("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._send("POST", path, body)

    def _send(self, method: str, path: str, body: dict | None = None, *, params: dict | None = None) -> dict:
        try:
            response = self._client.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise TransportError(f"platform unreachable: {exc}") from exc
        if response.status_code == 404:
            raise NotRegistered(f"{path} not found on the platform")
        if response.status_code >= 400:
            retryable = response.status_code >= 500 or response.status_code in (401, 403, 408, 429)
            raise TransportError(
                f"{method} {path} returned HTTP {response.status_code}{_detail(response)}",
                retryable=retryable,
            )
        return response.json()


def _given(**fields) -> dict:
    """Only the fields the caller set: the platform changes nothing it is not sent."""
    return {k: v for k, v in fields.items() if v is not None}


def _detail(response: httpx.Response) -> str:
    """The platform's own explanation (e.g. why a delete was refused), when it sent one."""
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001 - a non-JSON error body just has no detail
        return ""
    return f": {detail}" if isinstance(detail, str) else ""
