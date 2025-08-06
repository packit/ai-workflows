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
from triage_agent import RebaseData, ErrorData
from utils import mcp_tools, redis_client, get_git_finalization_steps

logger = logging.getLogger(__name__)

class InputSchema(BaseModel):
    package: str = Field(description="Package to update")
    version: str = Field(description="Version to update to")
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
    success: bool = Field(description="Whether the rebase was successfully completed")
    status: str = Field(description="Rebase status")
    mr_url: Optional[str] = Field(description="URL to the opened merge request")
    error: Optional[str] = Field(description="Specific details about an error")


class RebaseAgent(BaseAgent):
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

    def _render_prompt(self, input: TInputSchema) -> str:
        # Define template function that can be called from the template
        def rebase_git_steps(data: dict) -> str:
            input_data = self.input_schema.model_validate(data)
            return get_git_finalization_steps(
                package=input_data.package,
                jira_issue=input_data.jira_issue,
                commit_title=f"{COMMIT_PREFIX} Update to version {input_data.version}",
                files_to_commit="*.spec",
                branch_name=f"{BRANCH_PREFIX}-{input_data.version}",
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
                    "rebase_git_steps": rebase_git_steps
                }
            )
        )
        return template.render(input)

    @property
    def prompt(self) -> str:
        return """
          You are an AI Agent tasked to rebase a CentOS package to a newer version following the exact workflow.

          A couple of rules that you must follow and useful information for you:
          * All packages are in separate Git repositories under the Gitlab project {{ git_url }}
          * You can find the package at {{ git_url }}/{{ package }}
          * The Git user name is {{ git_user }}
          * The Git user's email address is {{ git_email }}
          * Use {{ gitlab_user }} as the GitLab user.
          * Work only in a temporary directory that you can create with the mktemp tool.
          * You can find packaging guidelines at https://docs.fedoraproject.org/en-US/packaging-guidelines/
          * You can find the RPM packaging guide at https://rpm-packaging-guide.github.io/.
          * Do not run the `centpkg new-sources` command for now (testing purposes), just write down the commands you would run.

          IMPORTANT GUIDELINES:
          - **Tool Usage**: You have run_shell_command tool available - use it directly!
          - **Command Execution Rules**:
            - Use run_shell_command tool for ALL command execution
            - If a command shows "no output" or empty STDOUT, that is a VALID result - do not retry
            - Commands that succeed with no output are normal - report success
          - **Git Configuration**: Always configure git user name and email before any git operations

          Follow exactly these steps:

          1. Find the location of the {{ package }} package at {{ git_url }}.  Always use the {{ dist_git_branch }} branch.

          2. Check if the {{ package }} was not already updated to version {{ version }}.  That means comparing
             the current version and provided version.
              * The current version of the package can be found in the 'Version' field of the RPM .spec file.
              * If there is nothing to update, print a message and exit. Otherwise follow the instructions below.
              * Do not clone any repository for detecting the version in .spec file.

          3. Create a local Git repository by following these steps:
              * Create a fork of the {{ package }} package using the `fork_repository` tool.
              * Clone the fork using git and HTTPS into a temporary directory under {{ git_repo_basepath }}.

          4. Update the {{ package }} to the newer version:
              * Create a new Git branch named `automated-package-update-{{ version }}`.
              * Update the local package by:
                * Updating the 'Version' and 'Release' fields in the .spec file as needed (or corresponding macros),
                  following packaging documentation.
                  * Make sure the format of the .spec file remains the same.
                * Updating macros related to update (e.g., 'commit') if present and necessary; examine the file's history
                  to see how updates are typically done.
                  * You might need to check some information in upstream repository, e.g. the commit SHA of the new version.
                * Creating a changelog entry, referencing the Jira issue as "Resolves: {{ jira_issue }}".
                * Downloading sources using `spectool -g -S {{ package }}.spec` (you might need to copy local sources,
                  e.g. if the .spec file loads some macros from them, to a directory where spectool expects them).
                * Uploading the new sources using `centpkg --release {{ dist_git_branch }} new-sources`.
                * IMPORTANT: Only performing changes relevant to the version update: Do not rename variables,
                  comment out existing lines, or alter if-else branches in the .spec file.

          5. Verify and adjust the changes:
              * Use `rpmlint` to validate your .spec file changes and fix any new errors it identifies.
              * Generate the SRPM using `rpmbuild -bs` (ensure your .spec file and source files are correctly
                copied to the build environment as required by the command).
              * Take the path to the SRPM file and build the RPM in Copr using `build_package` tool, confirm it succeeds.
                Use the following parameters:
                * project: {{ jira_issue }}
                * chroots: {{ dist_git_branch }} (verify how to map dist_git_branch into a Copr chroot, you can assume architecture is x86_64)
                * srpm_path: path to the SRPM file

          6. {{ rebase_git_steps }}

          Report the status of the rebase operation including:
          - Whether the package was already up to date
          - Any errors encountered during the process
          - The URL of the created merge request if successful
          - Any validation issues found with rpmlint
        """

    async def run_with_schema(self, input: TInputSchema) -> TOutputSchema:
        async with mcp_tools(
            os.getenv("MCP_GATEWAY_URL"),
            filter=lambda t: t
            in ("fork_repository", "open_merge_request", "push_to_remote_repository", "build_package"),
        ) as gateway_tools:
            tools = self._tools.copy()
            try:
                self._tools.extend(gateway_tools)
                return await self._run_with_schema(input)
            finally:
                self._tools = tools
                # disassociate removed tools from requirements
                for requirement in self._requirements:
                    if requirement._source_tool in gateway_tools:
                        requirement._source_tool = None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    agent = RebaseAgent()

    if (
        (package := os.getenv("PACKAGE", None))
        and (version := os.getenv("VERSION", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (branch := os.getenv("BRANCH", None))
    ):
        logger.info("Running in direct mode with environment variables")
        input = InputSchema(
            package=package,
            version=version,
            jira_issue=jira_issue,
            dist_git_branch=branch,
        )
        output = await agent.run_with_schema(input)
        logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
        return

    class Task(BaseModel):
        metadata: dict = Field(description="Task metadata")
        attempts: int = Field(default=0, description="Number of processing attempts")

    logger.info("Starting rebase agent in queue mode")
    async with redis_client(os.getenv("REDIS_URL")) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from rebase_queue (timeout: 30s)...")
            element = await redis.brpop("rebase_queue", timeout=30)
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info(f"Received task from queue.")

            task = Task.model_validate_json(payload)
            rebase_data = RebaseData.model_validate(task.metadata)
            logger.info(f"Processing rebase for package: {rebase_data.package}, "
                       f"version: {rebase_data.version}, JIRA: {rebase_data.jira_issue}, "
                       f"attempt: {task.attempts + 1}")

            input = InputSchema(
                package=rebase_data.package,
                version=rebase_data.version,
                jira_issue=rebase_data.jira_issue,
                dist_git_branch=rebase_data.branch,
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(f"Task failed (attempt {task.attempts}/{max_retries}), "
                                 f"re-queuing for retry: {rebase_data.jira_issue}")
                    await redis.lpush("rebase_queue", task.model_dump_json())
                else:
                    logger.error(f"Task failed after {max_retries} attempts, "
                               f"moving to error list: {rebase_data.jira_issue}")
                    await redis.lpush("error_list", error)

            try:
                logger.info(f"Starting rebase processing for {rebase_data.jira_issue}")
                output = await agent.run_with_schema(input)
                logger.info(f"Rebase processing completed for {rebase_data.jira_issue}, "
                          f"success: {output.success}")
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during rebase processing for {rebase_data.jira_issue}: {error}")
                await retry(
                    task, ErrorData(details=error, jira_issue=input.jira_issue).model_dump_json()
                )
            else:
                if output.success:
                    logger.info(f"Rebase successful for {rebase_data.jira_issue}, "
                              f"adding to completed list")
                    await redis.lpush("completed_rebase_list", output.model_dump_json())
                else:
                    logger.warning(f"Rebase failed for {rebase_data.jira_issue}: {output.error}")
                    await retry(task, output.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
