from contextlib import asynccontextmanager

import pytest

from ymir.common.constants import ALLOWED_GITLAB_HOSTS
from ymir.common.github_api import github_headers
from ymir.common.gitlab_api import (
    gitlab_api_get,
    gitlab_api_url,
    gitlab_headers,
    search_gitlab_project_mrs,
)
from ymir.common.jira_api import (
    decode_issue,
    get_current_issues,
    jira_api_get,
    jira_api_url,
    update_issue_label,
)
from ymir.common.models import FullIssue, Issue


def test_gitlab_api_url_validates_and_encodes_hosts():
    assert (
        gitlab_api_url("projects/group%2Frepo/merge_requests/1", gitlab_url="https://gitlab.gnome.org")
        == "https://gitlab.gnome.org/api/v4/projects/group%2Frepo/merge_requests/1"
    )
    assert "gitlab.com" in ALLOWED_GITLAB_HOSTS
    with pytest.raises(ValueError, match="untrusted host"):
        gitlab_api_url("projects/1", gitlab_url="https://gitlab.attacker.example")


def test_gitlab_headers_only_send_token_to_private_projects(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret")

    assert gitlab_headers("https://gitlab.com/redhat/project")["PRIVATE-TOKEN"] == "secret"
    assert "PRIVATE-TOKEN" not in gitlab_headers("https://gitlab.com/public/project")
    assert gitlab_headers("https://gitlab.cee.redhat.com/group/project")["PRIVATE-TOKEN"] == "secret"


def test_github_headers_can_omit_the_read_only_token(monkeypatch):
    monkeypatch.setenv("GITHUB_READONLY_TOKEN", "secret")
    assert github_headers()["Authorization"] == "Bearer secret"
    assert "Authorization" not in github_headers(allow_token=False)


@pytest.mark.asyncio
async def test_gitlab_search_uses_common_request_and_response_mapping(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def raise_for_status(self):
            pass

        async def json(self):
            return [
                {
                    "iid": 4,
                    "web_url": "https://gitlab.com/redhat/group/repo/-/merge_requests/4",
                    "title": "Fix issue",
                    "state": "locked",
                    "merged_at": None,
                }
            ]

    class Session:
        @asynccontextmanager
        async def get(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            yield Response()

    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    result = await search_gitlab_project_mrs("redhat/group/repo", "RHEL-1", state="opened", session=Session())

    assert captured["url"] == "https://gitlab.com/api/v4/projects/redhat%2Fgroup%2Frepo/merge_requests"
    assert captured["params"] == {"search": "RHEL-1", "state": "opened"}
    assert captured["headers"]["PRIVATE-TOKEN"] == "secret"
    assert result == [
        {
            "project": "redhat/group/repo",
            "iid": 4,
            "url": "https://gitlab.com/redhat/group/repo/-/merge_requests/4",
            "title": "Fix issue",
            "description": "",
            "state": "locked",
            "merged_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_gitlab_get_rejects_untrusted_host_before_request():
    class Session:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("untrusted host must be rejected before a request")

    with pytest.raises(ValueError, match="untrusted host"):
        await gitlab_api_get("projects/1", gitlab_url="https://gitlab.attacker.example", session=Session())


def test_jira_api_url_and_issue_decoder_use_common_models():
    assert jira_api_url("issue/RHEL-1", api_version="3", base_url="https://jira.example/") == (
        "https://jira.example/rest/api/3/issue/RHEL-1"
    )

    custom_fields = {
        "AssignedTeam": "team",
        "Errata Link": "errata",
        "Fixed in Build": "build",
        "Test Coverage": "coverage",
        "Preliminary Testing": "testing",
    }
    data = {
        "key": "RHEL-1",
        "fields": {
            "summary": "Summary",
            "status": {"name": "In Progress"},
            "components": [{"name": "kernel"}],
            "labels": ["label"],
            "fixVersions": [{"name": "rhel-9.8.0"}],
            "team": {"value": "Kernel"},
            "errata": "RHSA-1",
            "build": "kernel-1-1",
            "coverage": [{"value": "Automated"}],
            "testing": {"value": "Pass"},
            "description": "Details",
            "comment": {
                "comments": [
                    {
                        "author": {"displayName": "Author", "emailAddress": "author@example.com"},
                        "created": "2025-01-01T00:00:00+00:00",
                        "body": "Comment",
                        "id": "2",
                    }
                ]
            },
        },
    }

    issue = decode_issue(data, custom_fields)
    full_issue = decode_issue(data, custom_fields, full=True)

    assert isinstance(issue, Issue)
    assert issue.assigned_team == "Kernel"
    assert issue.fixed_in_build == "kernel-1-1"
    assert isinstance(full_issue, FullIssue)
    assert full_issue.comments[0].body == "Comment"


@pytest.mark.asyncio
async def test_jira_api_and_label_update_use_shared_async_requests(monkeypatch):
    requests = []
    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "secret")

    class Response:
        status = 200

        def raise_for_status(self):
            pass

        async def json(self):
            return {"key": "RHEL-1"}

    class Session:
        @asynccontextmanager
        async def get(self, url, **kwargs):
            requests.append(("get", url, kwargs))
            yield Response()

        @asynccontextmanager
        async def put(self, url, **kwargs):
            requests.append(("put", url, kwargs))
            yield Response()

    session = Session()
    data = await jira_api_get(
        "issue/RHEL-1",
        api_version="3",
        base_url="https://jira.example",
        headers={"Authorization": "Basic test"},
        session=session,
    )
    await update_issue_label("RHEL-1", "sweep-label", add=False, comment="Resolved", session=session)

    assert data == {"key": "RHEL-1"}
    assert requests[0][1] == "https://jira.example/rest/api/3/issue/RHEL-1"
    assert requests[1][1] == "https://jira.example/rest/api/2/issue/RHEL-1"
    assert requests[1][2]["json"] == {
        "update": {
            "labels": [{"remove": "sweep-label"}],
            "comment": [{"add": {"body": "Resolved"}}],
        }
    }


@pytest.mark.asyncio
async def test_get_current_issues_paginates_and_decodes_full_issues(monkeypatch):
    from ymir.common import jira_api

    custom_fields = {
        "AssignedTeam": "team",
        "Errata Link": "errata",
        "Fixed in Build": "build",
        "Test Coverage": "coverage",
        "Preliminary Testing": "testing",
    }
    request_bodies = []

    async def get_custom_fields():
        return custom_fields

    pages = iter(
        [
            {"issues": [{"key": "RHEL-1"}], "nextPageToken": "page-2", "isLast": False},
            {"issues": [{"key": "RHEL-2"}], "isLast": True},
        ]
    )

    async def post(path, **kwargs):
        assert path == "search/jql"
        request_bodies.append(kwargs["json"])
        return next(pages)

    async def get(path, **kwargs):
        key = path.split("/", 2)[1].split("?", 1)[0]
        return {
            "key": key,
            "fields": {
                "summary": f"Summary {key}",
                "status": {"name": "In Progress"},
                "components": [{"name": "kernel"}],
                "labels": [],
                "fixVersions": [{"name": "rhel-9.8.0"}],
                "team": None,
                "errata": None,
                "build": None,
                "coverage": None,
                "testing": None,
                "description": "Details",
                "comment": {"comments": []},
            },
        }

    monkeypatch.setattr(jira_api, "get_custom_fields", get_custom_fields)
    monkeypatch.setattr(jira_api, "jira_api_post", post)
    monkeypatch.setattr(jira_api, "jira_api_get", get)

    issues = await get_current_issues("project = RHEL", full=True)

    assert [issue.key for issue in issues] == ["RHEL-1", "RHEL-2"]
    assert all(isinstance(issue, FullIssue) for issue in issues)
    assert request_bodies[0]["fields"] == []
    assert request_bodies[1]["nextPageToken"] == "page-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post", "put"])
async def test_jira_requests_retry_rate_limits(method, monkeypatch):
    from ymir.common import jira_api

    calls = []

    class Response:
        def __init__(self, status):
            self.status = status

        def raise_for_status(self):
            assert self.status == 200

        async def json(self):
            return {"ok": True}

    class RequestContext:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            return None

    class Session:
        def get(self, url, **kwargs):
            return self._response("get")

        def post(self, url, **kwargs):
            return self._response("post")

        def put(self, url, **kwargs):
            return self._response("put")

        def _response(self, request_method):
            calls.append(request_method)
            return RequestContext(Response(429 if len(calls) == 1 else 200))

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr("backoff._async.asyncio.sleep", no_sleep)
    session = Session()
    headers = {"Authorization": "Basic test"}
    if method == "get":
        result = await jira_api.jira_api_get("issue/RHEL-1", session=session, headers=headers)
    elif method == "post":
        result = await jira_api.jira_api_post(
            "search/jql", json={}, session=session, headers=headers, decode_response=True
        )
    else:
        result = await jira_api.jira_api_put(
            "issue/RHEL-1", json={}, session=session, headers=headers, decode_response=True
        )

    assert calls == [method, method]
    assert result == {"ok": True}
