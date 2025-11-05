from contextlib import asynccontextmanager
from urllib.parse import urljoin
from functools import partial
import datetime
import aiofiles
import json
import re
import os

from fastmcp.exceptions import ToolError
from flexmock import flexmock

async def _get_transitions():
    return {"transitions": [{"to": {"name": "In Progress"}, "id": 1}, {"to": {"name": "Closed"}, "id": 2}]}


async def _get_verified_user():
    return {"groups": {"items": [{"name": "Red Hat Employee"}]}}


async def _get_unverified_user():
    return {"groups": {"items": []}}


async def _read_jira_mock(issue_key: str, remote_link = False) -> dict:
    try:
        async with aiofiles.open(f"{os.environ['JIRA_MOCK_FILES']}/{issue_key}", "r") as jira_file:
            if remote_link:
                return json.loads(await jira_file.read())["remote_links"]
            return json.loads(await jira_file.read())
    except (FileNotFoundError, json.JSONDecodeError, IOError) as e:
        raise ToolError(f"Error while reading mock up Jira issue {e}") from e


async def _write_jira_mock(issue_key: str, data: dict):
    try:
        async with aiofiles.open(f"{os.environ['JIRA_MOCK_FILES']}/{issue_key}", "w") as jira_file:
            await jira_file.write(json.dumps(data, indent=2))
    except IOError as e:
        raise ToolError(f"Error while writing mock up Jira issue {e}") from e


class aiohttpClientSessionMock:
    # mocking endpoint providing information about issue
    issue_get_regex       = re.compile(
        re.escape(urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue"))+"/([A-Z0-9-]+)")
    # mocking endpoint providing available transitions
    transitions_get_regex = re.compile(
        re.escape(urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue"))+"/([A-Z0-9-]+)/transitions")
    # mocking endpoint providing remote links present in issues
    remote_link_get_regex = re.compile(
        re.escape(urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue"))+"/([A-Z0-9-]+)/remotelink")
    # mocking endpoint for posting comments
    comment_post_regex    = re.compile(
        re.escape(urljoin(os.getenv("JIRA_URL"), f"rest/api/2/issue"))+"/([A-Z0-9-]+)/comment")
    # mocking endpoint for retrieval of information about users
    user_get_regex        = re.compile(
        re.escape(urljoin(os.getenv("JIRA_URL"), f"rest/api/2/user")))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    @asynccontextmanager
    async def get(self, *args, **kwargs):
        if match_data := self.issue_get_regex.fullmatch(args[0]):
            yield flexmock(raise_for_status=lambda: None,
                           json=partial(_read_jira_mock,
                                        issue_key=match_data.group(1),
                                        remote_link = False))
        elif match_data:= self.remote_link_get_regex.fullmatch(args[0]):
            yield flexmock(raise_for_status=lambda: None,
                           json=partial(_read_jira_mock,
                                        issue_key=match_data.group(1)),
                                        remote_link=True)
        elif match_data:= self.transitions_get_regex.fullmatch(args[0]):
            yield flexmock(raise_for_status=lambda: None,
                           json=_get_transitions)
        elif match_data:= self.user_get_regex.fullmatch(args[0]):
            if (kwargs["params"].get("key") == "verified_user" or
                    kwargs["params"].get("accountId") == "verified_user"):
                yield flexmock(raise_for_status=lambda: None,
                               json=_get_verified_user)
            yield flexmock(raise_for_status=lambda: None,
                           json=_get_unverified_user)
        else:
            raise NotImplementedError()

    @asynccontextmanager
    async def put(self, *args, **kwargs):
        if match_data := self.issue_get_regex.fullmatch(args[0]):
            issue_data = await _read_jira_mock(match_data.group(1), remote_link=False)
            if "fields" in kwargs["json"]:
                issue_data["fields"].update(kwargs["json"]["fields"])
            elif "update" in kwargs["json"]:
                current_labels = set(issue_data["fields"]["labels"])
                labels_to_add = [action_dict["add"] for action_dict
                            in kwargs["json"]["update"]["labels"]
                            if "add" in action_dict]
                labels_to_remove = [action_dict["remove"] for action_dict
                            in kwargs["json"]["update"]["labels"]
                            if "remove" in action_dict]
                if labels_to_remove:
                    current_labels.difference_update(labels_to_remove)
                if labels_to_add:
                    current_labels.update(labels_to_add)
                issue_data["fields"]["labels"] = list(current_labels)
            else:
                raise NotImplementedError()
            await _write_jira_mock(match_data.group(1), issue_data)
            yield flexmock(raise_for_status=lambda: None)
        else:
            raise NotImplementedError()

    @asynccontextmanager
    async def post(self, *args, **kwargs):
        if match_data := self.comment_post_regex.fullmatch(args[0]):
            current_issue = await _read_jira_mock(match_data.group(1))
            comment_dict = kwargs["json"]
            comment_dict["created"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            comment_dict["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            comment_dict["author"] = {"name": "jotnar-project",
                                    "key": "JIRAUSER288184",
                                    "displayName": "Jotnar Project"}
            current_issue["fields"]["comment"]["comments"].append(comment_dict)
            current_issue["fields"]["comment"]["maxResults"] += 1
            current_issue["fields"]["comment"]["total"] += 1
            await _write_jira_mock(match_data.group(1), current_issue)
            yield flexmock(raise_for_status=lambda: None)
        elif match_data := self.transitions_get_regex.fullmatch(args[0]):
            jira_data = await _read_jira_mock(match_data.group(1))
            if kwargs["json"]["transition"]["id"] == 1:
                jira_data["fields"]["status"] = {"name": "In Progress"}
                jira_data["fields"]["status"]["description"] = "Work has started"
            elif kwargs["json"]["transition"]["id"] == 2:
                jira_data["fields"]["status"] = {"name": "Closed"}
                jira_data["fields"]["status"]["description"] = "The issue is closed. See the" \
                    "resolution for context regarding why" \
                    "(for example Done, Abandoned, Duplicate, etc)"
            else:
                raise NotImplementedError()
            await _write_jira_mock(match_data.group(1), jira_data)
            yield flexmock(raise_for_status=lambda: None)
        else:
            raise NotImplementedError()
