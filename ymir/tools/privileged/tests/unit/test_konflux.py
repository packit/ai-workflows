import asyncio
import gzip
from pathlib import Path

import pytest
import requests
from beeai_framework.tools import ToolError
from flexmock import flexmock
from pydantic import ValidationError

from ymir.common.models import BuildResult
from ymir.tools.privileged import konflux as konflux_mod
from ymir.tools.privileged.konflux import (
    BuildPackageToolInput,
    KonfluxBuildTool,
    KonfluxClient,
    KonfluxConfig,
    KonfluxDownloadArtifactsTool,
    _git_auth_secret_body,
    _pipelinerun_body,
    _pipelinerun_status,
    _sanitize_tag,
)

BUILD_INPUT = {
    "git_url": "https://gitlab.cee.redhat.com/redhat/rhel/bot-branches/expat.git",
    "revision": "0123456789abcdef0123456789abcdef01234567",  # pragma: allowlist secret
    "package_name": "expat",
    "target_branch": "rhel-10.1",
    "jira_issue": "RHEL-12345",
}


_NS = "ymir-tenant"
_SA = "build-pipeline-ymir-scratch-build"
_IMG = "quay.io/redhat-user-workloads/ymir-tenant/ymir-scratch-build"


def _config() -> KonfluxConfig:
    return KonfluxConfig(
        api_url="https://konflux.example.com",
        kubearchive_url="https://kubearchive.example.com",
        pipeline_url="https://gitlab.example.com/rhel-on-konflux/rpmbuild-pipeline.git",
        token="tok-123",
        gitlab_token="glpat-xyz",
        namespace=_NS,
        service_account=_SA,
        container_image=_IMG,
    )


def _no_sleep():
    async def _sleep(*_):
        return

    flexmock(asyncio).should_receive("sleep").replace_with(_sleep)


def _mock_prelude(cfg, *, pipeline_revision="pipelinesha"):
    """Mock config load + pipeline revision resolution shared by build tests."""
    flexmock(konflux_mod).should_receive("_load_config").and_return(cfg)

    async def _rev(*_):
        return pipeline_revision

    flexmock(konflux_mod).should_receive("_resolve_pipeline_revision").replace_with(_rev)


# --------------------------------------------------------------------------- #
# Input schema reconciliation                                                 #
# --------------------------------------------------------------------------- #
def test_input_schema_requires_konflux_fields():
    with pytest.raises(ValidationError):
        BuildPackageToolInput.model_validate(
            {"srpm_path": "/x.src.rpm", "dist_git_branch": "rhel-10.1", "jira_issue": "RHEL-1"}
        )


def test_input_schema_tolerates_copr_only_fields():
    # build_agent dumps the whole shared BuildInputSchema (including the COPR-only
    # srpm_path/dist_git_branch) at every backend. The Konflux tool must accept
    # that payload rather than reject the extra keys.
    model = BuildPackageToolInput.model_validate(
        {**BUILD_INPUT, "srpm_path": "/x.src.rpm", "dist_git_branch": "rhel-10.1"}
    )
    assert model.package_name == "expat"
    assert model.git_url == BUILD_INPUT["git_url"]


def test_input_schema_advertises_additional_properties():
    # beeai's MCP client rebuilds the tool's input model from the advertised JSON
    # schema with extra="forbid" UNLESS additionalProperties is truthy. Without
    # this the COPR-only fields would fail client-side validation ("Tool input
    # validation error") before the build ever reaches the gateway.
    schema = BuildPackageToolInput.model_json_schema()
    assert schema.get("additionalProperties") is True


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def test_sanitize_tag_replaces_invalid_chars():
    assert _sanitize_tag("gtk+2.0") == "gtk-2.0"
    assert _sanitize_tag("valid_tag-1.2.3") == "valid_tag-1.2.3"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"status": {"conditions": [{"reason": "Succeeded"}]}}, "Succeeded"),
        ({"status": {"conditions": [{"reason": "Running"}]}}, "Running"),
        ({"status": {"conditions": []}}, "Unknown"),
        ({}, "Unknown"),
        (None, "Unknown"),
    ],
)
def test_pipelinerun_status(data, expected):
    assert _pipelinerun_status(data) == expected


def test_git_auth_secret_body_parses_host_and_repo():
    body = _git_auth_secret_body(BUILD_INPUT["git_url"], "glpat-xyz")
    assert body["type"] == "kubernetes.io/basic-auth"
    assert body["metadata"]["generateName"] == "gitlab-secret-"
    assert body["metadata"]["labels"]["appstudio.redhat.com/scm.host"] == "gitlab.cee.redhat.com"
    assert (
        body["metadata"]["annotations"]["appstudio.redhat.com/scm.repository"]
        == "https://gitlab.cee.redhat.com/redhat/rhel/bot-branches/expat"
    )
    assert body["stringData"]["username"] == "gitlab-ci-token"
    assert body["stringData"]["password"] == "glpat-xyz"  # pragma: allowlist secret


def test_pipelinerun_body_shape():
    cfg = _config()
    body = _pipelinerun_body(
        config=cfg,
        namespace=_NS,
        service_account=_SA,
        run_name="ymir-build-abc-1234",
        package_name="expat",
        git_url=BUILD_INPUT["git_url"],
        revision=BUILD_INPUT["revision"],
        target_branch="rhel-10.1",
        specfile=None,
        ocistorage=f"{_IMG}:expat-abc-1234",
        pipeline_revision="psha",
        secret_name="gitlab-secret-xyz",  # pragma: allowlist secret
    )
    params = {p["name"]: p["value"] for p in body["spec"]["params"]}
    assert params["package-name"] == "expat"
    assert params["git-url"] == BUILD_INPUT["git_url"]
    assert params["revision"] == BUILD_INPUT["revision"]
    assert params["target-branch"] == "rhel-10.1"
    assert params["koji-target"] == "DEFAULT"
    assert params["hermetic"] == "true"
    assert params["specfile"] == "null"
    assert params["build-architectures"] == ["x86_64"]
    assert params["build-platforms"] == ["linux-mxlarge/amd64"]
    assert body["spec"]["taskRunTemplate"]["serviceAccountName"] == _SA
    assert body["metadata"]["namespace"] == _NS
    body_secret = body["spec"]["workspaces"][0]["secret"]["secretName"]
    assert body_secret == "gitlab-secret-xyz"  # pragma: allowlist secret
    ref = {p["name"]: p["value"] for p in body["spec"]["pipelineRef"]["params"]}
    assert ref["revision"] == "psha"
    assert ref["pathInRepo"] == "pipeline/build-rpm-package.yaml"
    # Standalone run: no application/component labels, no pull_request annotation.
    assert "appstudio.openshift.io/application" not in body["metadata"]["labels"]
    assert "build.appstudio.redhat.com/pull_request_number" not in body["metadata"]["annotations"]


# --------------------------------------------------------------------------- #
# Config loading                                                              #
# --------------------------------------------------------------------------- #
def test_load_config_missing_env_raises(monkeypatch):
    for var in (
        "KONFLUX_API_URL",
        "KONFLUX_IMAGE_REPO",
        "KONFLUX_PIPELINE_URL",
        "GITLAB_TOKEN",
        "KONFLUX_TOKEN_FILE",
        "KONFLUX_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(konflux_mod.ToolErrorWithContext):
        konflux_mod._load_config()


def test_load_config_reads_token_file(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("  secret-token\n")
    monkeypatch.setenv("KONFLUX_API_URL", "https://api.example.com/")
    monkeypatch.setenv("KONFLUX_PIPELINE_URL", "https://g/p.git")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat")
    monkeypatch.setenv("KONFLUX_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("KUBEARCHIVE_API_URL", "https://ka.example.com/")
    for var in ("KONFLUX_NAMESPACE", "KONFLUX_SERVICE_ACCOUNT", "KONFLUX_IMAGE_REPO"):
        monkeypatch.delenv(var, raising=False)
    cfg = konflux_mod._load_config()
    assert cfg.token == "secret-token"
    assert cfg.api_url == "https://api.example.com"
    assert cfg.kubearchive_url == "https://ka.example.com"
    # Fixed build target defaults (overridable via env).
    assert cfg.namespace == konflux_mod.DEFAULT_NAMESPACE
    assert cfg.service_account == konflux_mod.DEFAULT_SERVICE_ACCOUNT
    assert cfg.container_image == konflux_mod.DEFAULT_IMAGE_REPO


def test_load_config_reads_inline_token(monkeypatch):
    """KONFLUX_TOKEN may supply the SA token directly, without a file."""
    monkeypatch.setenv("KONFLUX_API_URL", "https://api.example.com/")
    monkeypatch.setenv("KONFLUX_PIPELINE_URL", "https://g/p.git")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat")
    monkeypatch.delenv("KONFLUX_TOKEN_FILE", raising=False)
    monkeypatch.setenv("KONFLUX_TOKEN", "  inline-secret\n")
    cfg = konflux_mod._load_config()
    assert cfg.token == "inline-secret"


def test_load_config_token_file_takes_precedence(tmp_path, monkeypatch):
    """When both are set, the (rotatable) token file wins over inline KONFLUX_TOKEN."""
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    monkeypatch.setenv("KONFLUX_API_URL", "https://api.example.com/")
    monkeypatch.setenv("KONFLUX_PIPELINE_URL", "https://g/p.git")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat")
    monkeypatch.setenv("KONFLUX_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("KONFLUX_TOKEN", "inline-secret")
    cfg = konflux_mod._load_config()
    assert cfg.token == "file-token"


# --------------------------------------------------------------------------- #
# Build flow                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_build_success_submits_and_cleans_up():
    cfg = _config()
    _mock_prelude(cfg)
    _no_sleep()
    captured = {}

    # Variadic so the capture works whether or not flexmock passes ``self``.
    def _create_secret(*args):
        captured["secret"] = args[-1]
        return "gitlab-secret-abc"

    def _create_pr(*args):
        body = args[-1]
        captured["pr"] = body
        return body["metadata"]["name"]

    flexmock(KonfluxClient).should_receive("create_secret").replace_with(_create_secret).once()
    flexmock(KonfluxClient).should_receive("create_pipelinerun").replace_with(_create_pr).once()
    flexmock(KonfluxClient).should_receive("get_pipelinerun").and_return(
        {"status": {"conditions": [{"reason": "Running"}]}}
    ).and_return({"status": {"conditions": [{"reason": "Succeeded"}]}})
    flexmock(KonfluxClient).should_receive("delete_secret").with_args("gitlab-secret-abc").once()

    out = await KonfluxBuildTool().run(input=BUILD_INPUT)
    assert isinstance(out.result, BuildResult)
    assert out.result.success is True
    assert out.result.is_timeout is False

    params = {p["name"]: p["value"] for p in captured["pr"]["spec"]["params"]}
    assert params["package-name"] == "expat"
    assert params["git-url"] == BUILD_INPUT["git_url"]
    assert params["ociStorage"].startswith(f"{_IMG}:expat-0123456789ab-")
    ws_secret = captured["pr"]["spec"]["workspaces"][0]["secret"]["secretName"]
    assert ws_secret == "gitlab-secret-abc"  # pragma: allowlist secret
    assert captured["secret"]["type"] == "kubernetes.io/basic-auth"


@pytest.mark.asyncio
async def test_build_failure_collects_logs():
    cfg = _config()
    _mock_prelude(cfg)
    _no_sleep()
    flexmock(KonfluxClient).should_receive("create_secret").and_return("sec")
    flexmock(KonfluxClient).should_receive("create_pipelinerun").and_return("ymir-build-x")
    pipelinerun = {
        "status": {
            "conditions": [{"reason": "Failed", "message": "boom"}],
            "childReferences": [{"name": "tr-1"}],
        }
    }
    flexmock(KonfluxClient).should_receive("get_pipelinerun").and_return(pipelinerun)
    flexmock(KonfluxClient).should_receive("get_taskrun").with_args("tr-1").and_return(
        {
            "status": {
                "podName": "pod-1",
                "conditions": [{"reason": "Failed"}],
                "steps": [{"container": "step-build"}, {"container": "step-prep"}],
            }
        }
    )
    flexmock(KonfluxClient).should_receive("delete_secret").with_args("sec").once()

    out = await KonfluxBuildTool().run(input=BUILD_INPUT)
    assert out.result.success is False
    assert "Failed" in out.result.error_message
    assert "boom" in out.result.error_message
    urls = out.result.artifacts_urls
    assert any("pods/pod-1/log?container=step-build" in u for u in urls)
    assert all(u.startswith(cfg.kubearchive_url) for u in urls)


@pytest.mark.asyncio
async def test_build_timeout(monkeypatch):
    cfg = _config()
    _mock_prelude(cfg)
    _no_sleep()
    # Negative budget -> poll loop exits immediately into the timeout branch.
    monkeypatch.setattr(konflux_mod, "KONFLUX_BUILD_TIMEOUT", -100)
    monkeypatch.setattr(konflux_mod, "KONFLUX_TIMEOUT_GRACE_PERIOD", 0)
    flexmock(KonfluxClient).should_receive("create_secret").and_return("sec")
    flexmock(KonfluxClient).should_receive("create_pipelinerun").and_return("run")
    flexmock(KonfluxClient).should_receive("delete_secret").with_args("sec").once()

    out = await KonfluxBuildTool().run(input=BUILD_INPUT)
    assert out.result.success is False
    assert out.result.is_timeout is True


@pytest.mark.asyncio
async def test_secret_cleanup_on_submit_failure():
    cfg = _config()
    _mock_prelude(cfg)
    flexmock(KonfluxClient).should_receive("create_secret").and_return("sec")
    flexmock(KonfluxClient).should_receive("create_pipelinerun").and_raise(RuntimeError("submit boom"))
    flexmock(KonfluxClient).should_receive("delete_secret").with_args("sec").once()

    with pytest.raises(ToolError):
        await KonfluxBuildTool().run(input=BUILD_INPUT)


# --------------------------------------------------------------------------- #
# KonfluxClient Kubearchive fallback                                          #
# --------------------------------------------------------------------------- #
def test_get_pipelinerun_falls_back_to_kubearchive():
    client = KonfluxClient(_config(), "ymir-tenant")
    calls = []

    def _get(*args, **kwargs):
        url = args[-1]
        calls.append(url)
        if "kubearchive" in url:
            return flexmock(status_code=200, json=lambda: {"ok": True})
        return flexmock(status_code=404, json=dict)

    flexmock(requests.Session).should_receive("get").replace_with(_get)
    data = client.get_pipelinerun("run-1")
    assert data == {"ok": True}
    assert any("kubearchive" in u for u in calls)


# --------------------------------------------------------------------------- #
# Download artifacts                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_artifacts_sends_bearer_token():
    cfg = _config()
    flexmock(konflux_mod).should_receive("_load_config").and_return(cfg)
    url = f"{cfg.kubearchive_url}/api/v1/namespaces/ymir-tenant/pods/pod-1/log?container=step-build"
    captured = {}

    def _get(*args, **kwargs):
        captured["headers"] = kwargs.get("headers")
        captured["url"] = args[-1]
        return flexmock(status_code=200, reason="OK", content=b"log-bytes")

    flexmock(requests.Session).should_receive("get").replace_with(_get)
    out = await KonfluxDownloadArtifactsTool().run(input={"artifacts_urls": [url]})
    target = Path(out.result.target_path) / "pod-1__step-build.log"
    assert target.read_bytes() == b"log-bytes"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"


@pytest.mark.asyncio
async def test_download_artifacts_decompresses_gzip():
    cfg = _config()
    flexmock(konflux_mod).should_receive("_load_config").and_return(cfg)
    url = f"{cfg.kubearchive_url}/api/v1/namespaces/ymir-tenant/pods/pod-1/log?container=step-build"
    payload = gzip.compress(b"hello logs")

    def _get(*args, **kwargs):
        return flexmock(status_code=200, reason="OK", content=payload)

    flexmock(requests.Session).should_receive("get").replace_with(_get)
    out = await KonfluxDownloadArtifactsTool().run(input={"artifacts_urls": [url]})
    target = Path(out.result.target_path) / "pod-1__step-build.log"
    assert target.read_bytes() == b"hello logs"


@pytest.mark.asyncio
async def test_download_artifacts_raises_on_http_error():
    cfg = _config()
    flexmock(konflux_mod).should_receive("_load_config").and_return(cfg)
    url = f"{cfg.kubearchive_url}/api/v1/namespaces/ymir-tenant/pods/pod-1/log?container=step-build"

    def _get(*args, **kwargs):
        return flexmock(status_code=404, reason="Not Found", content=b"")

    flexmock(requests.Session).should_receive("get").replace_with(_get)
    with pytest.raises(ToolError):
        await KonfluxDownloadArtifactsTool().run(input={"artifacts_urls": [url]})
