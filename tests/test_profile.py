import json

import httpx
import pytest

from svn_obskit import cli
from svn_obskit.core.config import ObservabilityConfig
from svn_obskit.profile import ProfileError, build_profile, publish_profile

GOOD = {
    "summary": "Renders videos from scripts.",
    "language": "python",
    "frameworks": ["fastapi"],
    "capabilities": [{"name": "Render video", "description": "Makes an mp4."}],
    "interfaces": [{"kind": "HTTP", "name": "POST /videos"}],
    "dependencies": [{"kind": "weird", "name": "PostgreSQL", "description": "store"}],
    "secret_field": "dropped",
}
CFG = ObservabilityConfig(service_name="video", environment="prod", platform_url="http://obs", api_key="k")
TOO_BIG = {
    **GOOD,
    "capabilities": [{"name": "a", "description": "x" * 400}] * 30,
    "limitations": ["y" * 200] * 20,
    "background": [{"name": "w", "description": "z" * 400}] * 30,
    "dependencies": [{"name": "d", "description": "z" * 400}] * 30,
    "interfaces": [{"name": "i", "description": "z" * 400}] * 40,
}


def test_build_profile_cleans_and_drops_unknown_keys():
    p = build_profile(GOOD)
    assert p["schema"] == 1 and "secret_field" not in p
    assert p["interfaces"][0] == {"name": "POST /videos", "description": "POST /videos", "kind": "http"}
    assert p["dependencies"][0]["kind"] == "other"


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "summary": ""},
        {**GOOD, "capabilities": []},
        {**GOOD, "capabilities": "nope"},
        {**GOOD, "interfaces": ["x"]},
        TOO_BIG,
        [],
    ],
)
def test_build_profile_rejects_unusable_input(bad):
    with pytest.raises(ProfileError):
        build_profile(bad)


def _transport(services, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(200, json=services)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def test_publish_patches_service_metadata_without_registering():
    seen = []
    svc = [{"id": "sid-1", "name": "video", "environment": "prod"}]
    out = publish_profile("video", GOOD, config=CFG, transport=_transport(svc, seen))
    assert [m for m, _, _ in seen] == ["GET", "PATCH"]
    assert seen[1][1] == "/services/sid-1"
    assert json.loads(seen[1][2])["metadata"]["profile"]["summary"] == out["summary"]
    assert out["generated_at"] and out["sdk_version"]


def test_publish_explains_an_unregistered_service():
    with pytest.raises(ProfileError, match="not registered"):
        publish_profile("video", GOOD, config=CFG, transport=_transport([], []))


def test_cli_dry_run_validates_without_sending(tmp_path, capsys):
    f = tmp_path / "p.json"
    f.write_text(json.dumps(GOOD), encoding="utf-8")
    assert cli.main(["profile", "publish", str(f), "--service", "video", "--dry-run"]) == 0
    f.write_text("{}", encoding="utf-8")
    assert cli.main(["profile", "publish", str(f), "--service", "video", "--dry-run"]) == 1
    assert "summary" in capsys.readouterr().err
