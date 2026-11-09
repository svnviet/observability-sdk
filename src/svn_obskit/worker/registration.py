import httpx

from svn_obskit.core.exceptions import TransportError
from svn_obskit.core.identity import ServiceIdentity


class NotRegistered(TransportError):
    """The platform does not know the id we used (for example after its database was reset)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class PlatformClient:
    """Small synchronous client for the platform's registry and job endpoints."""

    def __init__(self, base_url: str, api_key: str | None, timeout_s: float) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)

    def register_service(self, identity: ServiceIdentity) -> str:
        data = self._post(
            "/services/register",
            {
                "service_name": identity.service_name,
                "instance_id": identity.instance_id,
                "environment": identity.environment,
                "service_version": identity.service_version,
                "hostname": identity.hostname,
                "metadata": {"process_id": identity.process_id},
            },
        )
        return data["service_id"]

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

    def _post(self, path: str, body: dict) -> dict:
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise TransportError(f"platform unreachable: {exc}") from exc
        if response.status_code == 404:
            raise NotRegistered(f"{path} not found on the platform")
        if response.status_code >= 400:
            retryable = response.status_code >= 500 or response.status_code in (401, 403, 408, 429)
            raise TransportError(f"{path} returned HTTP {response.status_code}", retryable=retryable)
        return response.json()
