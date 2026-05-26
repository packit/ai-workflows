import logging
import os
from urllib.parse import quote

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT

logger = logging.getLogger(__name__)

GITLAB_API_URL = "https://gitlab.com/api/v4"
RULES_NAMESPACE = "redhat/centos-stream/rules"
# use for testing:
# RULES_NAMESPACE = "ymir-rules-test"


class MaintainerRulesInput(BaseModel):
    package: str = Field(description="Name of the CentOS Stream package to fetch maintainer rules for")
    file_path: str = Field(
        default="AGENTS.md",
        description="Path to the file to fetch from the rules repository (default: AGENTS.md)",
    )


class MaintainerRulesTool(Tool[MaintainerRulesInput, ToolRunOptions, StringToolOutput]):
    name = "get_maintainer_rules"
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

        try:
            async with (
                aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
                session.get(url, headers=headers) as response,
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
            raise ToolError("Timeout while fetching maintainer rules") from e
        except Exception as e:
            raise ToolError(f"Error fetching maintainer rules: {e}") from e
