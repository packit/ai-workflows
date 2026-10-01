"""Unit tests for the Jira reply helper."""

import pytest
from flexmock import flexmock

from ymir.api import jira_reply
from ymir.api.jira_reply import post_comment


class _AsyncContextManager:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_post_comment_success(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_response = flexmock(status=201)

    mock_session = flexmock()
    mock_session.should_receive("post").with_args(
        "https://issues.example.com/rest/api/2/issue/RHEL-12345/comment",
        json={"body": "Command failed: bad args"},
        headers={
            "Authorization": "Basic Ym90QGV4YW1wbGUuY29tOmZha2UtdG9rZW4=",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    ).and_return(_AsyncContextManager(mock_response)).once()

    flexmock(jira_reply.aiohttp).should_receive("ClientSession").replace_with(
        lambda: _AsyncContextManager(mock_session)
    )

    await post_comment("RHEL-12345", "Command failed: bad args")


@pytest.mark.asyncio
async def test_post_comment_no_jira_url(monkeypatch):
    monkeypatch.delenv("JIRA_URL", raising=False)

    flexmock(jira_reply.aiohttp).should_receive("ClientSession").never()

    await post_comment("RHEL-12345", "should not call Jira")


@pytest.mark.asyncio
async def test_post_comment_jira_error(monkeypatch):
    """HTTP errors from Jira are logged but do not raise."""
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    async def _mock_response_text(*_args, **_kwargs):
        return "Forbidden"

    mock_response = flexmock(status=403)
    mock_response.should_receive("text").replace_with(_mock_response_text)

    mock_session = flexmock()
    mock_session.should_receive("post").and_return(_AsyncContextManager(mock_response))

    flexmock(jira_reply.aiohttp).should_receive("ClientSession").replace_with(
        lambda: _AsyncContextManager(mock_session)
    )

    await post_comment("RHEL-12345", "some error")


@pytest.mark.asyncio
async def test_post_comment_network_exception(monkeypatch):
    """Network exceptions are caught and logged, never raised."""
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_session = flexmock()
    mock_session.should_receive("post").and_raise(OSError("connection refused"))

    flexmock(jira_reply.aiohttp).should_receive("ClientSession").replace_with(
        lambda: _AsyncContextManager(mock_session)
    )

    await post_comment("RHEL-12345", "some error")


@pytest.mark.asyncio
async def test_post_comment_trailing_slash(monkeypatch):
    """JIRA_URL with trailing slash should not produce double slashes."""
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com/")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_response = flexmock(status=201)

    call_url = "http://should/be//overwritten//by/mock_post"

    def _mock_post(*_args, **_kwargs):
        nonlocal call_url
        call_url = _args[0]
        return _AsyncContextManager(mock_response)

    mock_session = flexmock()
    mock_session.should_receive("post").replace_with(_mock_post)

    flexmock(jira_reply.aiohttp).should_receive("ClientSession").replace_with(
        lambda: _AsyncContextManager(mock_session)
    )

    await post_comment("RHEL-12345", "msg")

    assert "//" not in call_url.split("://")[1]
