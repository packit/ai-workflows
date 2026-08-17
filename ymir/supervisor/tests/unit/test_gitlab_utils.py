"""Unit tests for ymir.supervisor.gitlab_utils."""

import pytest

from ymir.supervisor import gitlab_utils
from ymir.supervisor.gitlab_utils import ALLOWED_GITLAB_HOSTS, gitlab_api_get


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    gitlab_utils.gitlab_headers.cache_clear()
    yield
    gitlab_utils.gitlab_headers.cache_clear()


@pytest.mark.parametrize(
    "gitlab_url",
    [
        "https://gitlab.attacker.com",
        "https://gitlab.com.attacker.com",
        "https://evil.example.com",
        "https://GITLAB.COM.attacker.com",
    ],
)
def test_gitlab_api_get_rejects_untrusted_host(gitlab_url, monkeypatch):
    """The Bearer token must never be attached to an untrusted host."""

    def _fail(*_args, **_kwargs):
        raise AssertionError("network request must not be made for untrusted host")

    monkeypatch.setattr(gitlab_utils, "requests_session", _fail)

    with pytest.raises(ValueError, match="untrusted host"):
        gitlab_api_get("projects/1/merge_requests/1", gitlab_url=gitlab_url)


@pytest.mark.parametrize("host", sorted(ALLOWED_GITLAB_HOSTS))
def test_gitlab_api_get_allows_trusted_hosts(host, monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"state": "opened"}

    class _Session:
        def get(self, url, headers=None, params=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(gitlab_utils, "requests_session", lambda: _Session())

    result = gitlab_api_get("projects/1/merge_requests/1", gitlab_url=f"https://{host}")

    assert result == {"state": "opened"}
    assert captured["url"] == f"https://{host}/api/v4/projects/1/merge_requests/1"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
