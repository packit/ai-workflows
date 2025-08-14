import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
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
from beeai_framework.workflows import Workflow

from constants import COMMIT_PREFIX, BRANCH_PREFIX
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.specfile import AddChangelogEntryTool
from tools.text import CreateTool, InsertTool, StrReplaceTool, ViewTool
from triage_agent import RebaseData, ErrorData
from utils import get_agent_execution_config, mcp_tools, redis_client, run_tool, get_git_finalization_steps

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="dist-git branch to update")
    version: str = Field(description="Version to update to")
    jira_issue: str = Field(description="Jira issue to reference as resolved")


class OutputSchema(BaseModel):
    success: bool = Field(description="Whether the rebase was successfully completed")
    status: str = Field(description="Rebase status")
    mr_url: str | None = Field(description="URL to the opened merge request")
    error: str | None = Field(description="Specific details about an error")


def render_prompt(input: InputSchema) -> str:
    template = """
      You are an AI Agent tasked to rebase a package to a newer version following the exact workflow.

      A couple of rules that you must follow and useful information for you:
      * You can find packaging guidelines at https://docs.fedoraproject.org/en-US/packaging-guidelines/.
      * You can find the RPM packaging guide at https://rpm-packaging-guide.github.io/.
      * IMPORTANT: Do not run the `centpkg new-sources` command for now (testing purposes), just write down
        the commands you would run.

      Follow exactly these steps:

      1. You will find the cloned dist-git repository of the {{ package }} package in {{ local_clone }}.
         It is your current working directory, do not `cd` anywhere else.

      2. Check if the {{ package }} was not already updated to version {{ version }}. That means comparing
         the current version with the provided version.
          * The current version of the package can be found in the 'Version' field of the spec file.
          * If there is nothing to update, print a message and exit. Otherwise follow the instructions below.

      3. Update the {{ package }} to the newer version:
          * Update the local package by:
            * Updating the 'Version' and 'Release' fields (or corresponding macros) in the spec file as needed,
              following packaging documentation.
              * Make sure the format of the spec file remains the same.
            * Updating macros related to update (e.g., 'commit') if present and necessary; examine the file history
              to see how updates are typically done.
              * You might need to check some information in upstream repository, e.g. the commit SHA of the new version.
            * Creating a changelog entry, referencing the Jira issue as "Resolves: {{ jira_issue }}".
            * Downloading sources using `spectool -g -S {{ package }}.spec` (you might need to copy local sources,
              e.g. if the spec file loads some macros from them, to a directory where `spectool` expects them).
            * Uploading the new sources using `centpkg --release {{ dist_git_branch }} new-sources`.
            * IMPORTANT: Only performing changes relevant to the version update: Do not rename variables,
              comment out existing lines, or alter if-else branches in the spec file.

      4. Verify and adjust the changes:
          * Use `rpmlint` to validate your spec file changes and fix any new errors it identifies.
          * Generate the SRPM using `rpmbuild -bs` (ensure your spec file and source files are correctly
            copied to the build environment as required by the command).

      5. {{ rebase_git_steps }}

      Report the status of the rebase operation including:
      - Whether the package was already up to date
      - Any errors encountered during the process
      - The URL of the created merge request if successful
      - Any validation issues found with rpmlint
    """

    # Define template function that can be called from the template
    def rebase_git_steps(data: dict) -> str:
        input_data = InputSchema.model_validate(data)
        return get_git_finalization_steps(
            package=input_data.package,
            jira_issue=input_data.jira_issue,
            commit_title=f"{COMMIT_PREFIX} Update to version {input_data.version}",
            files_to_commit="*.spec",
            branch_name=f"{BRANCH_PREFIX}-{input_data.version}",
            dist_git_branch=input_data.dist_git_branch,
        )

    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=template, functions={"rebase_git_steps": rebase_git_steps})
    ).render(input)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))

    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        rebase_agent = RequirementAgent(
            llm=ChatModel.from_name(os.getenv("CHAT_MODEL")),
            tools=[
                ThinkTool(),
                RunShellCommandTool(),
                DuckDuckGoSearchTool(),
                CreateTool(),
                ViewTool(),
                InsertTool(),
                StrReplaceTool(),
                AddChangelogEntryTool(),
            ],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
            ],
            middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
            role="Red Hat Enterprise Linux developer",
            instructions=[
                "Use the `think` tool to reason through complex decisions and document your approach.",
                "Preserve existing formatting and style conventions in RPM spec files and patch headers.",
                "Use `rpmlint *.spec` to check for packaging issues and address any NEW errors",
                "Ignore pre-existing rpmlint warnings unless they're related to your changes",
                "Run `centpkg prep` to verify all patches apply cleanly during build preparation",
                "Generate an SRPM using `centpkg srpm` command to ensure complete build readiness",
                "* IMPORTANT: Only perform changes relevant to the rebase update",
            ],
        )

        class State(BaseModel):
            package: str
            dist_git_branch: str
            version: str
            jira_issue: str
            fork_url: str | None = Field(default=None)
            local_clone: Path | None = Field(default=None)
            update_branch: str | None = Field(default=None)
            rebase_result: OutputSchema | None = Field(default=None)
            merge_request_url: str | None = Field(default=None)

        workflow = Workflow(State)

        async def fork_and_prepare_dist_git(state):
            working_dir = Path(os.getenv("GIT_REPO_BASEPATH")) / state.jira_issue
            working_dir.mkdir(parents=True, exist_ok=True)
            namespace = "centos-stream" if re.match(r"^c\d+s$", state.dist_git_branch) else "rhel"
            state.fork_url = await run_tool(
                "fork_repository",
                repository=f"https://gitlab.com/redhat/{namespace}/rpms/{state.package}",
                available_tools=gateway_tools,
            )
            state.local_clone = working_dir / state.package
            shutil.rmtree(state.local_clone, ignore_errors=True)
            subprocess.check_call(
                ["git", "clone", "--single-branch", "--branch", state.dist_git_branch, state.fork_url],
                cwd=working_dir,
            )
            state.update_branch = f"automated-package-update-{state.jira_issue}"
            subprocess.check_call(["git", "checkout", "-B", state.update_branch], cwd=state.local_clone)
            return "run_rebase_agent"

        async def run_rebase_agent(state):
            cwd = Path.cwd()
            try:
                # make things easier for the LLM
                os.chdir(state.local_clone)
                response = await rebase_agent.run(
                    prompt=render_prompt(
                        InputSchema(
                            local_clone=state.local_clone,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            version=state.version,
                            jira_issue=state.jira_issue,
                        ),
                    ),
                    expected_output=OutputSchema,
                    execution=get_agent_execution_config(),
                )
                state.rebase_result = OutputSchema.model_validate_json(response.answer.text)
            finally:
                os.chdir(cwd)
            if state.rebase_result.success:
                return "push_and_open_mr"
            else:
                return Workflow.END

        async def push_and_open_mr(state):
            if os.getenv("DRY_RUN", "False").lower() == "true":
                return Workflow.END
            await run_tool(
                "push_to_remote_repository",
                repository=state.fork_url,
                clone_path=state.local_clone,
                branch=state.update_branch,
                force=True,
                available_tools=gateway_tools,
            )
            state.merge_request_url = await run_tool(
                "open_merge_request",
                fork_url=state.fork_url,
                title="TODO",
                description="TODO",
                target=state.dist_git_branch,
                source=state.update_branch,
                available_tools=gateway_tools,
            )
            return Workflow.END

        workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
        workflow.add_step("run_rebase_agent", run_rebase_agent)
        workflow.add_step("push_and_open_mr", push_and_open_mr)

        async def run_workflow(package, dist_git_branch, version, jira_issue):
            response = await workflow.run(
                State(
                    package=package,
                    dist_git_branch=dist_git_branch,
                    version=version,
                    jira_issue=jira_issue,
                ),
            )
            return response.state

        if (
            (package := os.getenv("PACKAGE", None))
            and (version := os.getenv("VERSION", None))
            and (jira_issue := os.getenv("JIRA_ISSUE", None))
            and (branch := os.getenv("BRANCH", None))
        ):
            logger.info("Running in direct mode with environment variables")
            state = await run_workflow(
                package=package,
                dist_git_branch=branch,
                version=version,
                jira_issue=jira_issue,
            )
            logger.info(f"Direct run completed: {state.rebase_result.model_dump_json(indent=4)}")
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
                logger.info(
                    f"Processing rebase for package: {rebase_data.package}, "
                    f"version: {rebase_data.version}, JIRA: {rebase_data.jira_issue}, "
                    f"attempt: {task.attempts + 1}"
                )

                async def retry(task, error):
                    task.attempts += 1
                    if task.attempts < max_retries:
                        logger.warning(
                            f"Task failed (attempt {task.attempts}/{max_retries}), "
                            f"re-queuing for retry: {rebase_data.jira_issue}"
                        )
                        await redis.lpush("rebase_queue", task.model_dump_json())
                    else:
                        logger.error(
                            f"Task failed after {max_retries} attempts, "
                            f"moving to error list: {rebase_data.jira_issue}"
                        )
                        await redis.lpush("error_list", error)

                try:
                    logger.info(f"Starting rebase processing for {rebase_data.jira_issue}")
                    state = await run_workflow(
                        package=rebase_data.package,
                        dist_git_branch=rebase_data.branch,
                        version=rebase_data.version,
                        jira_issue=rebase_data.jira_issue,
                    )
                    logger.info(
                        f"Rebase processing completed for {rebase_data.jira_issue}, " f"success: {state.rebase_result.success}"
                    )
                except Exception as e:
                    error = "".join(traceback.format_exception(e))
                    logger.error(f"Exception during rebase processing for {rebase_data.jira_issue}: {error}")
                    await retry(task, ErrorData(details=error, jira_issue=rebase_data.jira_issue).model_dump_json())
                else:
                    if state.rebase_result.success:
                        logger.info(f"Rebase successful for {rebase_data.jira_issue}, " f"adding to completed list")
                        await redis.lpush("completed_rebase_list", state.rebase_result.model_dump_json())
                    else:
                        logger.warning(f"Rebase failed for {rebase_data.jira_issue}: {state.rebase_result.error}")
                        await retry(task, state.rebase_result.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
