import json
import re

import pytest

from ymir.tools.privileged.aiohttp_client_session_mock import aiohttpClientSessionMock


@pytest.fixture
def mock_session(monkeypatch, tmp_path):
    base_url = "https://jira.example.test"
    monkeypatch.setenv("JIRA_MOCK_FILES", str(tmp_path))

    regexes = {
        "issue_get_regex": rf"{re.escape(base_url)}/rest/api/3/issue/([A-Z0-9-]+)",
        "transitions_get_regex": rf"{re.escape(base_url)}/rest/api/3/issue/([A-Z0-9-]+)/transitions",
        "remote_link_get_regex": rf"{re.escape(base_url)}/rest/api/3/issue/([A-Z0-9-]+)/remotelink",
        "comment_post_regex": rf"{re.escape(base_url)}/rest/api/[2-3]/issue/([A-Z0-9-]+)/comment",
        "search_post_regex": rf"{re.escape(base_url)}/rest/api/3/search/jql",
        "user_get_regex": rf"{re.escape(base_url)}/rest/api/3/user",
    }
    for name, pattern in regexes.items():
        monkeypatch.setattr(aiohttpClientSessionMock, name, re.compile(pattern))

    return aiohttpClientSessionMock(), base_url, tmp_path


@pytest.mark.asyncio
async def test_get_issue_returns_namespace_response(mock_session):
    session, base_url, mock_dir = mock_session
    issue = {"key": "RHEL-12345", "fields": {"summary": "test", "labels": ["triage"]}}
    (mock_dir / "RHEL-12345").write_text(json.dumps(issue))

    async with session.get(
        f"{base_url}/rest/api/3/issue/RHEL-12345",
        params={"fields": "summary"},
    ) as response:
        assert response.status == 200
        response.raise_for_status()
        assert await response.json() == {"key": "RHEL-12345", "fields": {"summary": "test"}}


@pytest.mark.asyncio
async def test_put_issue_updates_mock_file(mock_session):
    session, base_url, mock_dir = mock_session
    issue_path = mock_dir / "RHEL-12345"
    issue_path.write_text(json.dumps({"key": "RHEL-12345", "fields": {"labels": []}}))

    async with session.put(
        f"{base_url}/rest/api/3/issue/RHEL-12345",
        json={"fields": {"summary": "updated"}},
    ) as response:
        response.raise_for_status()

    assert json.loads(issue_path.read_text())["fields"]["summary"] == "updated"


@pytest.mark.asyncio
async def test_post_comment_updates_mock_file(mock_session):
    session, base_url, mock_dir = mock_session
    issue_path = mock_dir / "RHEL-12345"
    issue_path.write_text(
        json.dumps(
            {
                "key": "RHEL-12345",
                "fields": {"comment": {"comments": [], "maxResults": 0, "total": 0}},
            }
        )
    )

    async with session.post(
        f"{base_url}/rest/api/3/issue/RHEL-12345/comment",
        json={"body": "comment"},
    ) as response:
        response.raise_for_status()

    comments = json.loads(issue_path.read_text())["fields"]["comment"]
    assert comments["total"] == 1
    assert comments["comments"][0]["body"] == "comment"
