import pytest
from fastmcp import FastMCP
from flexmock import flexmock
from mcp.types import TextContent
from ogr.services.gitlab import GitlabService

from gitlab_server import fork_repository, open_merge_request


@pytest.mark.asyncio
async def test_fork_repository():
    repository = "https://gitlab.com/redhat/centos-stream/rpms/bash"
    clone_url = "git@gitlab.com:ai-bot/bash.git"
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(get_fork=lambda create: flexmock(get_git_urls=lambda: {"ssh": clone_url}))
    )
    result = await fork_repository.run({"repository": repository})
    [content] = result.content
    assert isinstance(content, TextContent)
    assert content.text == clone_url


@pytest.mark.asyncio
async def test_open_merge_request():
    repository = "https://gitlab.com/redhat/centos-stream/rpms/bash"
    title = "Fix RHEL-12345"
    description = "Resolves RHEL-12345"
    target = "c10s"
    source = "automated-package-update-RHEL-12345"
    mr_url = "https://gitlab.com/redhat/centos-stream/rpms/bash/-/merge_requests/1"
    flexmock(GitlabService).should_receive("get_project_from_url").with_args(url=repository).and_return(
        flexmock(create_pr=lambda title, body, target, source: flexmock(url=mr_url))
    )
    result = await open_merge_request.run(
        {"repository": repository, "title": title, "description": description, "target": target, "source": source}
    )
    [content] = result.content
    assert isinstance(content, TextContent)
    assert content.text == mr_url
