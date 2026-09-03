"""Unit tests for the Jira reply helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ymir.api.jira_reply import post_comment


@pytest.mark.asyncio
async def test_post_comment_success(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_response = AsyncMock()
    mock_response.status = 201
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("ymir.api.jira_reply.aiohttp.ClientSession", return_value=mock_session):
        await post_comment("RHEL-12345", "Command failed: bad args")

    mock_session.post.assert_called_once_with(
        "https://issues.example.com/rest/api/2/issue/RHEL-12345/comment",
        json={"body": "Command failed: bad args"},
        headers={
            "Authorization": "Basic Ym90QGV4YW1wbGUuY29tOmZha2UtdG9rZW4=",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


@pytest.mark.asyncio
async def test_post_comment_no_jira_url(monkeypatch):
    monkeypatch.delenv("JIRA_URL", raising=False)

    with patch("ymir.api.jira_reply.aiohttp.ClientSession") as mock_cls:
        await post_comment("RHEL-12345", "should not call Jira")

    mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_post_comment_jira_error(monkeypatch):
    """HTTP errors from Jira are logged but do not raise."""
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_response = AsyncMock()
    mock_response.status = 403
    mock_response.text = AsyncMock(return_value="Forbidden")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("ymir.api.jira_reply.aiohttp.ClientSession", return_value=mock_session):
        await post_comment("RHEL-12345", "some error")


@pytest.mark.asyncio
async def test_post_comment_network_exception(monkeypatch):
    """Network exceptions are caught and logged, never raised."""
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_session = AsyncMock()
    mock_session.post = MagicMock(side_effect=OSError("connection refused"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("ymir.api.jira_reply.aiohttp.ClientSession", return_value=mock_session):
        await post_comment("RHEL-12345", "some error")


@pytest.mark.asyncio
async def test_post_comment_trailing_slash(monkeypatch):
    """JIRA_URL with trailing slash should not produce double slashes."""
    monkeypatch.setenv("JIRA_URL", "https://issues.example.com/")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token")  # pragma: allowlist secret

    mock_response = AsyncMock()
    mock_response.status = 201
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("ymir.api.jira_reply.aiohttp.ClientSession", return_value=mock_session):
        await post_comment("RHEL-12345", "msg")

    call_url = mock_session.post.call_args[0][0]
    assert "//" not in call_url.split("://")[1]
