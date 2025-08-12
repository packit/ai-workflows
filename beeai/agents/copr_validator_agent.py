import asyncio
import logging
import os
import sys
import traceback
from typing import Optional

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.backend import ChatModel
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from base_agent import BaseAgent, TInputSchema, TOutputSchema
from observability import setup_observability
from tools.commands import RunShellCommandTool
from triage_agent import ErrorData
from utils import mcp_tools, redis_client

logger = logging.getLogger(__name__)

class ValidationData(BaseModel):
    package: str = Field(description="Package name")
    version: str = Field(description="Target version")
    branch: str = Field(description="Target branch")
    jira_issue: str = Field(description="Jira issue identifier")
    srpm_path: str = Field(description="Path to the SRPM file to validate")
    mr_url: Optional[str] = Field(description="URL to the merge request", default=None)


class InputSchema(BaseModel):
    package: str = Field(description="Package name")
    version: str = Field(description="Version being validated")
    jira_issue: str = Field(description="Jira issue to reference")
    dist_git_branch: str = Field(description="Git branch in dist-git")
    srpm_path: str = Field(description="Path to the SRPM file to validate")
    mr_url: Optional[str] = Field(description="URL to the merge request", default=None)


class OutputSchema(BaseModel):
    success: bool = Field(description="Whether the validation was successful")
    status: str = Field(description="Validation status")
    build_url: Optional[str] = Field(description="URL to the Copr build")
    error: Optional[str] = Field(description="Specific details about an error")


class CoprValidatorAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            llm=ChatModel.from_name(os.getenv("CHAT_MODEL")),
            tools=[ThinkTool(), RunShellCommandTool(), DuckDuckGoSearchTool()],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
                ConditionalRequirement("build_package", min_invocations=1),
            ],
            middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        )

    @property
    def input_schema(self) -> type[TInputSchema]:
        return InputSchema

    @property
    def output_schema(self) -> type[TOutputSchema]:
        return OutputSchema

    @property
    def prompt(self) -> str:
        return """
          You are an AI Agent tasked to validate a package patch by building it in Copr.

          Your primary responsibilities:
          * Validate that the SRPM file exists and is accessible at {{ srpm_path }} otherwise terminate with an error
          * Build the package in Copr using the provided SRPM
          * Monitor the build process and report results
          * If build fails, analyze the logs to understand and report the failure

          IMPORTANT GUIDELINES:
          - **Tool Usage**: You have run_shell_command and build_package tools available
          - **Build Validation**: Use the `build_package` tool to validate the SRPM in Copr
          - **Error Analysis**: If the build fails, analyze the provided URLs to find the root cause

          Follow exactly these steps:

          1. Verify SRPM file:
              * Check that the SRPM file exists at {{ srpm_path }}
              * Verify the file is a valid SRPM (ends with .src.rpm)

          2. Determine build parameters:
              * Determine the appropriate Copr chroot based on {{ dist_git_branch }}
                * if dist_git_branch is cNs, the Copr chroot is rhel-N.dev-x86_64
              * Use {{ jira_issue }} as the project name

          3. Build the package in Copr:
              * Use the `build_package` tool with the following parameters:
                * project: {{ jira_issue }}
                * chroots: [the chroot you determined based on the dist_git_branch]
                * srpm_path: {{ srpm_path }}
              * Monitor the build progress and wait for completion

          4. Handle build results:
              * If build succeeds: Report success with build URLs
              * If build fails due to kerberos ticket issue: Retry up to 3 times with 10 second delays
              * If build fails due to project already exists: Retry with project name {{ jira_issue }}-N (where N is a random number of 3 digits)
              * If build fails with other errors: Analyze the build logs at the provided URLs
                * Look specifically at "builder-live.log.gz" for build errors
                * Extract the relevant error information and include in the report

          5. Report validation results:
              * Success status and any build URLs
              * Detailed error analysis if the build failed
              * Recommendations for fixing any identified issues

          Remember: Your role is specifically to validate that the patch builds correctly in Copr.
          You are not responsible for creating or modifying the package - only validating it.
        """

    async def run_with_schema(self, input: TInputSchema) -> TOutputSchema:
        # Debug: Log available tools
        tool_names = [getattr(tool, 'name', str(type(tool).__name__)) for tool in self._tools]
        logger.info(f"CoprValidatorAgent available tools: {tool_names}")
        
        # Try to run with whatever tools are available first
        # This handles the case where we're running as HandoffTool and tools are inherited
        try:
            logger.info("CoprValidatorAgent attempting to run with current tools")
            return await self._run_with_schema(input)
        except ValueError as e:
            if "Source tool build_package was not found" in str(e):
                # We need to get the build_package tool from MCP gateway
                logger.info("build_package tool not found - connecting to MCP gateway")
                
                mcp_gateway_url = os.getenv("MCP_GATEWAY_URL")
                if not mcp_gateway_url:
                    logger.error("MCP_GATEWAY_URL not set - cannot connect to MCP gateway")
                    raise ValueError("build_package tool not available and MCP_GATEWAY_URL not configured")
                
                async with mcp_tools(
                    mcp_gateway_url,
                    filter=lambda t: t in ("build_package",),
                ) as gateway_tools:
                    tools = self._tools.copy()
                    try:
                        self._tools.extend(gateway_tools)
                        logger.info("Added MCP tools, retrying execution")
                        return await self._run_with_schema(input)
                    finally:
                        self._tools = tools
                        # disassociate removed tools from requirements
                        for requirement in self._requirements:
                            if requirement._source_tool in gateway_tools:
                                requirement._source_tool = None
            else:
                # Some other error - re-raise it
                raise


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    agent = CoprValidatorAgent()

    if (
        (package := os.getenv("PACKAGE", None))
        and (version := os.getenv("VERSION", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (branch := os.getenv("BRANCH", None))
        and (srpm_path := os.getenv("SRPM_PATH", None))
    ):
        logger.info("Running in direct mode with environment variables")
        input = InputSchema(
            package=package,
            version=version,
            jira_issue=jira_issue,
            dist_git_branch=branch,
            srpm_path=srpm_path,
            mr_url=os.getenv("MR_URL", None),
        )
        output = await agent.run_with_schema(input)
        logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
        return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())