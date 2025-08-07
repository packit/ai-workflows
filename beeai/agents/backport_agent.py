import asyncio
import logging
import os
import sys
import traceback
from typing import Optional

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.backend import ChatModel
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from base_agent import BaseAgent, TInputSchema, TOutputSchema
from constants import COMMIT_PREFIX, BRANCH_PREFIX
from observability import setup_observability
from tools.commands import RunShellCommandTool
from triage_agent import BackportData, ErrorData
from utils import mcp_tools, redis_client, get_git_finalization_steps

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    package: str = Field(description="Package to update")
    upstream_fix: str = Field(description="Link to an upstream fix for the issue")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
    gitlab_user: str = Field(
        description="Name of the GitLab user",
        default=os.getenv("GITLAB_USER", "rhel-packaging-agent"),
    )
    git_url: str = Field(
        description="URL of the git repository",
        default="https://gitlab.com/redhat/centos-stream/rpms",
    )
    git_user: str = Field(description="Name of the git user", default="RHEL Packaging Agent")
    git_email: str = Field(
        description="E-mail address of the git user", default="rhel-packaging-agent@redhat.com"
    )
    git_repo_basepath: str = Field(
        description="Base path for cloned git repos",
        default=os.getenv("GIT_REPO_BASEPATH"),
    )


class OutputSchema(BaseModel):
    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(description="Backport status")
    mr_url: Optional[str] = Field(description="URL to the opened merge request")
    error: Optional[str] = Field(description="Specific details about an error")


class BackportAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            llm=ChatModel.from_name(os.getenv("CHAT_MODEL")),
            tools=[ThinkTool(), RunShellCommandTool(), DuckDuckGoSearchTool()],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
            ],
            middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        )

    @property
    def input_schema(self) -> type[TInputSchema]:
        return InputSchema

    @property
    def output_schema(self) -> type[TOutputSchema]:
        return OutputSchema

    def _render_prompt(self, input: TInputSchema) -> str:
        # Define template function that can be called from the template
        def backport_git_steps(data: dict) -> str:
            input_data = self.input_schema.model_validate(data)
            return get_git_finalization_steps(
                package=input_data.package,
                jira_issue=input_data.jira_issue,
                commit_title=f"{COMMIT_PREFIX} backport {input_data.jira_issue}",
                files_to_commit=f"*.spec and {input_data.jira_issue}.patch",
                branch_name=f"{BRANCH_PREFIX}-{input_data.jira_issue}",
                git_user=input_data.git_user,
                git_email=input_data.git_email,
                git_url=input_data.git_url,
                dist_git_branch=input_data.dist_git_branch,
            )

        template = PromptTemplate(
            PromptTemplateInput(
                schema=self.input_schema,
                template=self.prompt,
                functions={
                    "backport_git_steps": backport_git_steps
                }
            )
        )
        return template.render(input)

    @property
    def prompt(self) -> str:
        return """
          You are an agent for backporting a fix for a CentOS Stream package. You will prepare the content
          of the update and then create a commit with the changes. Create a temporary directory and always work
          inside it. Follow exactly these steps:

          1. Find the location of the {{ package }} package at {{ git_url }}. Always use the {{ dist_git_branch }} branch.

          2. Check if the package {{ package }} already has the fix {{ jira_issue }} applied.

          3. Create a local Git repository by following these steps:
            * Create a fork of the {{ package }} package using the `fork_repository` tool.
            * Clone the fork using git and HTTPS into a temporary directory under {{ git_repo_basepath }}.
            * Run command `centpkg sources` in the cloned repository which downloads all sources defined in the RPM specfile.
            * Create a new Git branch named `automated-package-update-{{ jira_issue }}`.

          4. Update the {{ package }} with the fix:
            * Updating the 'Release' field in the .spec file as needed (or corresponding macros), following packaging
              documentation.
              * Make sure the format of the .spec file remains the same.
            * Fetch the upstream fix {{ upstream_fix }} locally and store it in the git repo as "{{ jira_issue }}.patch".
              * Add a new "Patch:" entry in the spec file for patch "{{ jira_issue }}.patch".
              * Verify that the patch is being applied in the "%prep" section.
            * Creating a changelog entry, referencing the Jira issue as "Resolves: <jira_issue>" for the issue {{ jira_issue }}.
              The changelog entry has to use the current date.
            * IMPORTANT: Only performing changes relevant to the backport update: Do not rename variables,
              comment out existing lines, or alter if-else branches in the .spec file.

          5. Verify and adjust the changes:
            * Use `rpmlint` to validate your .spec file changes and fix any new errors it identifies.
            * Generate the SRPM using `rpmbuild -bs` (ensure your .spec file and source files are correctly copied
              to the build environment as required by the command).
            * Verify the newly added patch applies cleanly using the command `centpkg prep`.

          6. {{ backport_git_steps }}
        """

    async def run_with_schema(self, input: TInputSchema, capture_raw_response: bool = False) -> TOutputSchema:
        async with mcp_tools(
            os.getenv("MCP_GATEWAY_URL"),
            filter=lambda t: t
            in ("fork_repository", "open_merge_request", "push_to_remote_repository"),
        ) as gateway_tools:
            tools = self._tools.copy()
            try:
                self._tools.extend(gateway_tools)
                return await self._run_with_schema(input, capture_raw_response=capture_raw_response)
            finally:
                self._tools = tools
                # disassociate removed tools from requirements
                for requirement in self._requirements:
                    if requirement._source_tool in gateway_tools:
                        requirement._source_tool = None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    agent = BackportAgent()

    if (
        (package := os.getenv("PACKAGE", None))
        and (upstream_fix := os.getenv("UPSTREAM_FIX", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (branch := os.getenv("BRANCH", None))
    ):
        logger.info("Running in direct mode with environment variables")
        input = InputSchema(
            package=package,
            upstream_fix=upstream_fix,
            jira_issue=jira_issue,
            dist_git_branch=branch,
        )
        output = await agent.run_with_schema(input)
        logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
        return

    class Task(BaseModel):
        metadata: dict = Field(description="Task metadata")
        attempts: int = Field(default=0, description="Number of processing attempts")

    logger.info("Starting backport agent in queue mode")
    async with redis_client(os.getenv("REDIS_URL")) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from backport_queue (timeout: 30s)...")
            element = await redis.brpop("backport_queue", timeout=30)
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info(f"Received task from queue.")

            task = Task.model_validate_json(payload)
            backport_data = BackportData.model_validate(task.metadata)
            logger.info(f"Processing backport for package: {backport_data.package}, "
                       f"JIRA: {backport_data.jira_issue}, attempt: {task.attempts + 1}")

            input = InputSchema(
                package=backport_data.package,
                upstream_fix=backport_data.patch_url,
                jira_issue=backport_data.jira_issue,
                dist_git_branch=backport_data.branch,
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(f"Task failed (attempt {task.attempts}/{max_retries}), "
                                 f"re-queuing for retry: {backport_data.jira_issue}")
                    await redis.lpush("backport_queue", task.model_dump_json())
                else:
                    logger.error(f"Task failed after {max_retries} attempts, "
                               f"moving to error list: {backport_data.jira_issue}")
                    await redis.lpush("error_list", error)

            try:
                logger.info(f"Starting backport processing for {backport_data.jira_issue}")
                output = await agent.run_with_schema(input)
                logger.info(f"Backport processing completed for {backport_data.jira_issue}, "
                          f"success: {output.success}")
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                await retry(
                    task, ErrorData(details=error, jira_issue=input.jira_issue).model_dump_json()
                )
            else:
                if output.success:
                    logger.info(f"Backport successful for {backport_data.jira_issue}, "
                              f"adding to completed list")
                    await redis.lpush("completed_backport_list", output.model_dump_json())
                else:
                    logger.warning(f"Backport failed for {backport_data.jira_issue}: {output.error}")
                    await retry(task, output.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
