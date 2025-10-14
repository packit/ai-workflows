import os
import asyncio
import aiohttp
import logging
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions, ToolError

logger = logging.getLogger(__name__)


class UpstreamSearchResult(Enum):
    FOUND        = "found"
    NOT_FOUND    = "not_found"
    NOT_POSSIBLE = "not_possible"


class UpstreamSearchToolInput(BaseModel):
    project:     str = Field(
        description="name of the upstream project which should be searched through")
    description: str = Field(
        description="description of issue for which fixing commit will be looked for")
    date: str | None = Field(
        description="date in iso format after which the commit was created")


class UpstreamSearchToolResult(BaseModel):
    result: UpstreamSearchResult      = Field(
        description="result of the tool invocation")
    repository_url: str | None        = Field(
        description="url of repository where commits reside")
    related_commits: list[str] | None = Field(
        description="commits related to given description")


class UpstreamSearchToolOutput(JSONToolOutput[UpstreamSearchToolResult]):
    pass


class UpstreamSearchTool(Tool[UpstreamSearchToolInput, ToolRunOptions, UpstreamSearchToolOutput]):
    name = "upstream_search"
    description = """
        Search through upstream project's git repository and finds commits related to
        provided description and optionally allows to filter commits made after provided date.

        If the tool was successful, 'result' is set to 'found' which means that commits
        'related_commits'in repository 'repository_url' are the ones which should be related to
        provided description.

        If the tool was unsuccessful to find commits for this particular query, 'result'
        is set to 'not_found'.

        If the tool can not be used for this particular project, 'result' is set to 'not_possible'.
    """
    input_schema = UpstreamSearchToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "commands", self.name],
            creator=self,
        )

    async def _run(
        self, tool_input: UpstreamSearchToolInput,
        options: ToolRunOptions | None, context: RunContext) -> UpstreamSearchToolOutput:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            repos = []
            commits = []
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{os.environ['UPSTREAM_SEARCH_API_URL']}/find_repository",
                                       params={"name": tool_input.project}) as response:
                    if response.status != 200:
                        logger.debug("Searching did not yield repo. status %d response %s",
                                      response.status, await response.text())
                        return UpstreamSearchToolOutput(UpstreamSearchToolResult(
                            result=UpstreamSearchResult.NOT_POSSIBLE,
                            repository_url=None,
                            related_commits=None
                        ))
                    repos = await response.json()

                # until we have solid reference to upstream repository through for example VCS
                # spec file tag, this is the best we can do
                post_params = {"url": repos[0], "text": tool_input.description}
                if tool_input.date is not None:
                    post_params["date"] = tool_input.date
                async with session.post(f"{os.environ['UPSTREAM_SEARCH_API_URL']}/find_commit",
                                        json=post_params, timeout=240) as response:
                    if response.status != 200:
                        logger.debug("Searching did not yield commits. status %d response %s",
                                      response.status, await response.text())
                        return UpstreamSearchToolOutput(UpstreamSearchToolResult(
                            result=UpstreamSearchResult.NOT_FOUND,
                            repository_url=None,
                            related_commits=None
                        ))
                    commits = await response.json()
        except asyncio.TimeoutError:
            raise ToolError("Timeout occured while contacting upstream-search backend")
        except Exception as e:
            raise ToolError(f"Unexpected internal error occured while contacting backend {e}")

        def get_patch_url(commit):
            parsed_url = urlparse(repos[0])
            if not parsed_url.path.endswith(".git"):
                return commit
            if parsed_url.hostname.startswith("gitlab"):
                prefix = "/-"
            elif parsed_url.hostname.startswith("github"):
                prefix = ""
            else:
                return commit
            path = f"{prefix}/commit/{commit}.patch"
            return parsed_url._replace(path=parsed_url.path.replace(".git", path)).geturl()
        commits = [get_patch_url(commit) for commit in commits]

        return UpstreamSearchToolOutput(UpstreamSearchToolResult(
            result=UpstreamSearchResult.FOUND,
            repository_url=repos[0],
            related_commits=commits
        ))
