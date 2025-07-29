import asyncio
import logging
import os
import subprocess
import sys
import time
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
from tools.shell_command import ShellCommandTool
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
            tools=[ThinkTool(), ShellCommandTool(), DuckDuckGoSearchTool()],
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
          of the update and then create a commit with the changes.
          The repository is cloned at "{{ git_repo_basepath }}/{{ package }}", you should work in this directory.
          Follow exactly these steps:

          1. Validate and prepare the upstream fix for integration:

             **Download and Examine the Upstream Fix:**
             * Download the upstream fix from {{ upstream_fix }}
             * Examine its format (unified diff, git patch, etc.)
             * Store the patch file as "{{ jira_issue }}.patch" in the repository root
             * Ensure the patch has proper headers including description and author information

             **Test Patch Integration via Spec File:**
             * Temporarily add the patch to the spec file to test compatibility:
               - Add `Patch: {{ jira_issue }}.patch` entry in the appropriate location
               - Ensure the patch is applied in the "%prep" section (e.g., `%patch -P <number>`)
               - Update the Release field temporarily for testing
             * Test the patch application during build preparation:
               - Run `centpkg prep` to verify the patch applies cleanly during RPM build preparation
               - If `prep` command fails, follow these steps to resolve the issue:
                 - Navigate to directory cups-filters-2.0.0-build/cups-filters-2.0.0 where the sources are unpacked
                 - Using the output from previous `centpkg prep` command, manually backport the patch
                 - Resolve all conflict the from previous patch application
                 - Once all conflicts are resolved, update the patch {{ jira_issue }}.patch file with the new changes from cups-filters-2.0.0-build/cups-filters-2.0.0
             * If this test passes, the patch is ready for final spec file integration in step 3

             **Validate the Prepared Patch:**
             * Test spec file correctness:
               - Use `rpmlint *.spec` to check for packaging issues and address any NEW errors
               - Ignore pre-existing rpmlint warnings unless they're related to your changes
             * Test build preparation and SRPM generation:
               - Run `centpkg prep` to verify all patches apply cleanly during build preparation
               - Generate the SRPM using `rpmbuild -bs *.spec` to ensure complete build readiness
               - Ensure all source files are available and the SRPM builds successfully
             * Document validation results for step 5 integration

             **Handle Preparation Failures:**
             * If advanced preparation methods also fail:
               - Document the specific conflicts and version differences encountered
               - Mark the patch as requiring manual intervention before integration
               - Provide detailed analysis of why automatic preparation failed
               - Include recommendations for manual preparation steps

          2. Integrate the validated patch into the spec file and create changelog:
            * Update the 'Release' field in the .spec file following RPM packaging conventions
            * Add the prepared patch to the spec file in the appropriate location (using {{ jira_issue }}.patch)
            * Ensure the patch is applied in the "%prep" section
            * Create a changelog entry with current date and "Resolves: {{ jira_issue }}"
            * IMPORTANT: Only perform changes relevant to the backport update

          3. {{ backport_git_steps }}

          Throughout the process:
          * Use the `think` tool to reason through complex decisions and document your approach
          * If you encounter errors, try alternative approaches before giving up
          * Always validate patch integration through the RPM build process (centpkg prep, rpmbuild) rather than direct file patching
          * Remember that dist-git repos contain spec files and patches, while source code is downloaded separately via centpkg sources
          * Document any assumptions or potential issues for human review
          * Be systematic - try spec file integration first, then escalate to advanced patch modification techniques
          * Preserve existing formatting and style conventions in spec files and patch headers
        """

    async def run_with_schema(self, input: TInputSchema) -> TOutputSchema:
        async with mcp_tools(os.getenv("MCP_GITLAB_URL")) as gitlab_tools:
            tools = self._tools.copy()
            try:
                self._tools.extend(gitlab_tools)
                return await self._run_with_schema(input)
            finally:
                self._tools = tools
                # disassociate removed tools from requirements
                for requirement in self._requirements:
                    if requirement._source_tool in gitlab_tools:
                        requirement._source_tool = None


def prepare_package(package: str, jira_issue: str, dist_git_branch: str, input_schema: InputSchema) -> None:
    """
    Prepare a package for backporting by cloning the dist-git repository, switching to the appropriate branch,
    and downloading the sources.
    """
    os.makedirs(input_schema.git_repo_basepath, exist_ok=True)
    subprocess.check_call(["centpkg", "clone", "--anonymous", "--branch", dist_git_branch, package], cwd=input_schema.git_repo_basepath)
    local_clone = os.path.join(input_schema.git_repo_basepath, package)
    subprocess.check_call(["centpkg", "sources"], cwd=local_clone)
    subprocess.check_call(["git", "switch", "-c", f"automated-package-update-{jira_issue}", dist_git_branch], cwd=local_clone)
    subprocess.check_call(["centpkg", "prep"], cwd=local_clone)


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
        input_schema = InputSchema(
            package=package,
            upstream_fix=upstream_fix,
            jira_issue=jira_issue,
            dist_git_branch=branch,
        )
        prepare_package(package, jira_issue, branch, input_schema)
        try:
            output = await agent.run_with_schema(input_schema)
            logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
        finally:
            logger.info("Direct run completed, keeping the container running for debugging")
            # keep the container running for debugging
            time.sleep(999999)
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

            input_schema = InputSchema(
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
                output = await agent.run_with_schema(input_schema)
                logger.info(f"Backport processing completed for {backport_data.jira_issue}, "
                          f"success: {output.success}")
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                await retry(
                    task, ErrorData(details=error, jira_issue=input_schema.jira_issue).model_dump_json()
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
