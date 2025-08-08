import asyncio
import logging
import os
from shutil import rmtree
from pathlib import Path
import subprocess
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
from tools.specfile import AddChangelogEntryTool, BumpReleaseTool
from tools.text import CreateTool, InsertTool, StrReplaceTool, ViewTool
from tools.wicked_git import GitPatchCreationTool
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
    unpacked_sources: str = Field(
        description="Path to the unpacked (using `centpkg prep`) sources",
        default="",
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
            tools=[
                ThinkTool(),
                RunShellCommandTool(),
                DuckDuckGoSearchTool(),
                CreateTool(),
                ViewTool(),
                InsertTool(),
                StrReplaceTool(),
                GitPatchCreationTool(),
                BumpReleaseTool(),
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
                "Increment the 'Release' field in the .spec file following RPM packaging conventions "
                "using the `bump_release` tool",
                "Add a new changelog entry to the .spec file using the `add_changelog_entry` tool using name "
                "\"RHEL Packaging Agent <jotnar@redhat.com>\"",
                "* IMPORTANT: Only perform changes relevant to the backport update"
            ]
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
        return (
            "Work inside the repository cloned at \"{{ git_repo_basepath }}/{{ package }}\"\n"
            "Download the upstream fix from {{ upstream_fix }}\n"
            "Store the patch file as \"{{ jira_issue }}.patch\" in the repository root\n"
            "Navigate to the directory {{ unpacked_sources }} and use `git am --reject` "
            "command to apply the patch {{ jira_issue }}.patch\n"
            "Resolve all conflicts inside {{ unpacked_sources }} directory and "
            "leave the repository in a dirty state\n"
            "Delete all *.rej files\n"
            "DO **NOT** RUN COMMAND `git am --continue`\n"
            "Once you resolve all conflicts, use tool git_patch_create to create a patch file\n"
            "{{ backport_git_steps }}"
        )

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


def prepare_package(package: str, jira_issue: str, dist_git_branch: str,
                    input_schema: InputSchema) -> tuple[Path, Path]:
    """
    Prepare the package for backporting by cloning the dist-git repository, switching to the appropriate branch,
    and downloading the sources.
    Returns the path to the unpacked sources.
    """
    git_repo = Path(input_schema.git_repo_basepath)
    git_repo.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "centpkg",
            "clone",
            "--anonymous",
            "--branch",
            dist_git_branch,
            package,
        ],
        cwd=git_repo,
    )
    local_clone = git_repo / package
    subprocess.check_call(
        [
            "git",
            "switch",
            "-c",
            f"automated-package-update-{jira_issue}",
            dist_git_branch,
        ],
        cwd=local_clone,
    )
    subprocess.check_call(["centpkg", "sources"], cwd=local_clone)
    subprocess.check_call(["centpkg", "prep"], cwd=local_clone)
    unpacked_sources = list(local_clone.glob(f"*-build/*{package}*"))
    if len(unpacked_sources) != 1:
        raise ValueError(
            f"Expected exactly one unpacked source, got {unpacked_sources}"
        )
    return unpacked_sources[0], local_clone

async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    agent = BackportAgent()
    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

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
        unpacked_sources, local_clone = prepare_package(package, jira_issue, branch, input)
        input.unpacked_sources = str(unpacked_sources)
        try:
            output = await agent.run_with_schema(input)
        finally:
            if not dry_run:
                logger.info(f"Removing {local_clone}")
                rmtree(local_clone)
            else:
                logger.info(f"DRY RUN: Not removing {local_clone}")
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
            input.unpacked_sources, local_clone = prepare_package(backport_data.package,
                backport_data.jira_issue, backport_data.branch, input)

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
                rmtree(local_clone)
            else:
                rmtree(local_clone)
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
