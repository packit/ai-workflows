import os
from typing import Annotated

from fastmcp import FastMCP
from ogr.factory import get_project
from pydantic import Field


mcp = FastMCP(name="GitLab")


@mcp.tool
def fork_repository(
    repository: Annotated[str, Field(description="Repository URL")],
) -> str | None:
    """
    Creates a new fork of the specified repository if it doesn't exist yet,
    otherwise gets the existing fork. Returns a clonable git URL of the fork.
    """
    project = get_project(url=repository, token=os.getenv("GITLAB_TOKEN"))
    if not project:
        return None
    fork = project.get_fork(create=True)
    if not fork:
        return None
    return fork.get_git_urls()["ssh"]


@mcp.tool
def open_merge_request(
    repository: Annotated[str, Field(description="Repository URL")],
    title: Annotated[str, Field(description="MR title")],
    description: Annotated[str, Field(description="MR description")],
    target: Annotated[str, Field(description="Target branch")],
    source: Annotated[str, Field(description="Source branch")],
) -> str | None:
    """
    Opens a new merge request against the specified repository. Returns URL
    of the opened merge request.
    """
    project = get_project(url=repository, token=os.getenv("GITLAB_TOKEN"))
    if not project:
        return None
    pr = project.create_pr(title, description, target, source)
    if not pr:
        return None
    return pr.url


mcp.run(transport="sse", host="0.0.0.0", port=os.getenv("SSE_PORT"))
