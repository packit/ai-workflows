import logging
import os
from urllib.parse import quote

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.base import CloneableTool as Tool
from ymir.tools.base import tool_error_context
from ymir.tools.constants import AIOHTTP_TIMEOUT, GITLAB_API_URL, RULES_NAMESPACE, YMIR_USER_AGENT
from ymir.tools.http import aiohttp_get_with_retries

logger = logging.getLogger(__name__)


class MaintainerRulesInput(BaseModel):
    package: str = Field(description="Name of the CentOS Stream package to fetch maintainer rules for")
    file_path: str = Field(
        default="AGENTS.md",
        description="Path to the file to fetch from the rules repository (default: AGENTS.md)",
    )


class MaintainerRulesTool(Tool[MaintainerRulesInput, ToolRunOptions, StringToolOutput]):
    name = "get_maintainer_rules"
    timeout = 120
    description = (
        "Fetch maintainer-defined rules and guidelines for a package from its rules repository. "
        "Returns the content of the requested file (default: AGENTS.md) from "
        "gitlab.com/redhat/centos-stream/rules/<package>. "
        "If no rules repository or file exists for the package, returns a 'not found' message. "
        "Use this after identifying the package name to get maintainer context before investigation."
    )
    input_schema = MaintainerRulesInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "rules", self.name],
            creator=self,
        )

    async def _run(
        self,
        tool_input: MaintainerRulesInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        project_path = quote(f"{RULES_NAMESPACE}/{tool_input.package}", safe="")
        file_path = quote(tool_input.file_path, safe="")
        url = f"{GITLAB_API_URL}/projects/{project_path}/repository/files/{file_path}/raw?ref=main"

        headers: dict[str, str] = {"User-Agent": YMIR_USER_AGENT}
        if token := os.getenv("GITLAB_TOKEN"):
            headers["PRIVATE-TOKEN"] = token

        with tool_error_context(
            f"Failed to fetch maintainer rules for {tool_input.package}", file_path=tool_input.file_path
        ):
            try:
                async with (
                    aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                    aiohttp_get_with_retries(session, url, headers=headers) as response,
                ):
                    if response.status == 200:
                        return StringToolOutput(result=await response.text())
                    if response.status == 404:
                        return StringToolOutput(
                            result=f"No maintainer rules found for package '{tool_input.package}' "
                            f"(file '{tool_input.file_path}' not found in rules repository)."
                        )
                    text = await response.text()
                    return StringToolOutput(
                        result=f"Failed to fetch maintainer rules (HTTP {response.status}): {text}"
                    )
            except TimeoutError as e:
                raise ToolError(f"Timeout while fetching maintainer rules for {tool_input.package}") from e
