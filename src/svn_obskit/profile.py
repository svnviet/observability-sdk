"""A service's self-description (what it does, what it exposes), shown on the service's detail page in OBS.

The profile lives in the service's `metadata["profile"]` on the platform, so no platform change is needed to
store it. `build_profile` validates and trims what an agent wrote; `publish_profile` sends it.
"""

import json
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata

from svn_obskit.core.config import ObservabilityConfig
from svn_obskit.core.exceptions import TransportError
from svn_obskit.worker.registration import PlatformClient

PROFILE_SCHEMA = 1
MAX_BYTES = 16_000  # keeps service metadata small; a summary is not documentation

_LIMITS = {"summary": 1000, "language": 50, "repo": 300, "commit": 64}
_LIST_FIELDS = {  # field -> (max items, kinds allowed or None)
    "capabilities": (30, None),
    "interfaces": (40, {"http", "grpc", "queue", "event", "cron", "cli", "other"}),
    "dependencies": (30, {"database", "cache", "queue", "storage", "service", "api", "other"}),
    "background": (30, {"worker", "job", "workflow", "scheduler", "other"}),
}
_ITEM_LIMITS = {"name": 120, "description": 400}


class ProfileError(ValueError):
    """The profile file is not usable; the message says what to fix."""


def _text(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{field} must be a non-empty string")
    return value.strip()[:limit]


def _items(data: dict, field: str, max_items: int, kinds: set[str] | None) -> list[dict]:
    raw = data.get(field, [])
    if not isinstance(raw, list):
        raise ProfileError(f"{field} must be a list")
    out = []
    for i, item in enumerate(raw[:max_items]):
        if not isinstance(item, dict):
            raise ProfileError(f"{field}[{i}] must be an object with name and description")
        entry = {
            "name": _text(item.get("name"), f"{field}[{i}].name", _ITEM_LIMITS["name"]),
            "description": _text(item.get("description", item.get("name")), f"{field}[{i}].description", _ITEM_LIMITS["description"]),
        }  # fmt: skip
        if kinds is not None:
            kind = str(item.get("kind", "other")).lower()
            entry["kind"] = kind if kind in kinds else "other"
        out.append(entry)
    return out


def build_profile(data: dict) -> dict:
    """Validate an agent-written profile and return the cleaned dict that is stored. Unknown keys are dropped."""
    if not isinstance(data, dict):
        raise ProfileError("the profile must be a JSON object")
    profile: dict = {
        "schema": PROFILE_SCHEMA,
        "summary": _text(data.get("summary"), "summary", _LIMITS["summary"]),
    }
    for field in ("language", "repo", "commit"):
        if data.get(field) is not None:
            profile[field] = _text(data[field], field, _LIMITS[field])
    for field in ("frameworks", "limitations"):
        raw = data.get(field, [])
        if not isinstance(raw, list):
            raise ProfileError(f"{field} must be a list of strings")
        profile[field] = [_text(v, field, 200) for v in raw[:20]]
    for field, (max_items, kinds) in _LIST_FIELDS.items():
        profile[field] = _items(data, field, max_items, kinds)
    if not profile["capabilities"]:
        raise ProfileError("capabilities needs at least one entry: what can this service do?")
    if len(json.dumps(profile).encode()) > MAX_BYTES:
        raise ProfileError(
            f"the profile is larger than {MAX_BYTES} bytes; shorten descriptions or drop minor items"
        )
    return profile


def _sdk_version() -> str:
    try:
        return importlib_metadata.version("svn-obskit")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def publish_profile(
    service_name: str, data: dict, *, config: ObservabilityConfig | None = None, transport=None
) -> dict:
    """Store the profile on the platform under the service's metadata. Raises ProfileError / TransportError."""
    cfg = config or ObservabilityConfig.from_env(service_name)
    if not cfg.platform_url:
        raise ProfileError("OBSKIT_PLATFORM_URL is not set")
    profile = build_profile(data)
    profile["generated_at"] = datetime.now(UTC).isoformat()
    profile["sdk_version"] = _sdk_version()
    client = PlatformClient(cfg.platform_url, cfg.api_key, cfg.delivery_timeout_s, transport=transport)
    try:
        service_id = client.find_service_id(service_name, cfg.environment)
        if service_id is None:
            raise ProfileError(
                f"service {service_name!r} ({cfg.environment}) is not registered on the platform yet: "
                "start the service once with OBSKIT_PLATFORM_URL set, then publish again"
            )
        client.update_service(service_id, metadata={"profile": profile})
    except TransportError as exc:
        raise ProfileError(str(exc)) from exc
    finally:
        client.close()
    return profile
