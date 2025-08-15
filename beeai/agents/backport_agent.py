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

from tools.specfile import AddChangelogEntryTool, BumpReleaseTool
from tools.text import CreateTool, InsertTool, StrReplaceTool, ViewTool
from tools.wicked_git import GitLogSearchTool, GitPatchCreationTool
from constants import COMMIT_PREFIX, BRANCH_PREFIX
from observability import setup_observability
from tools.commands import RunShellCommandTool
from triage_agent import BackportData, ErrorData
from utils import fixAwait, get_agent_execution_config, mcp_tools, redis_client, get_git_finalization_steps

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    package: str = Field(description="Package to update")
    upstream_fix: str = Field(description="Link to an upstream fix for the issue")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    cve_id: str = Field(default="", description="CVE ID if the jira issue is a CVE")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
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


def render_prompt(input: InputSchema) -> str:
    template = (
        'Work inside the repository cloned at "{{ git_repo_basepath }}/{{ package }}"\n'
        "Use the `git_log_search` tool to check if the jira issue ({{ jira_issue }}) or CVE ({{ cve_id }}) is already resolved.\n"
        "If the issue or the cve are already resolved, exit the backporting process with success=True and status=\"Backport already applied\"\n"
        "Download the upstream fix from {{ upstream_fix }}\n"
        'Store the patch file as "{{ jira_issue }}.patch" in the repository root\n'
        "If directory {{ unpacked_sources }} is not a git repository, run `git init` in it "
        "and create an initial commit\n"
        "Navigate to the directory {{ unpacked_sources }} and use `git am --reject` "
        "command to apply the patch {{ jira_issue }}.patch\n"
        "Resolve all conflicts inside {{ unpacked_sources }} directory and "
        "leave the repository in a dirty state\n"
        "Delete all *.rej files\n"
        "DO **NOT** RUN COMMAND `git am --continue`\n"
        "Once you resolve all conflicts, use tool git_patch_create to create a patch file\n"
        "{{ backport_git_steps }}"
    )

    # Define template function that can be called from the template
    def backport_git_steps(data):
        input_data = InputSchema.model_validate(data)
        return get_git_finalization_steps(
            package=input_data.package,
            jira_issue=input_data.jira_issue,
            commit_title=f"{COMMIT_PREFIX} backport {input_data.jira_issue}",
            files_to_commit=f"*.spec and {input_data.jira_issue}.patch",
            branch_name=f"{BRANCH_PREFIX}-{input_data.jira_issue}",
            dist_git_branch=input_data.dist_git_branch,
        )

    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=template, functions={"backport_git_steps": backport_git_steps})
    ).render(input)


def prepare_package(
    package: str, jira_issue: str, dist_git_branch: str, input_schema: InputSchema
) -> tuple[Path, Path]:
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
        raise ValueError(f"Expected exactly one unpacked source, got {unpacked_sources}")
    return unpacked_sources[0], local_clone


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    cve_id = os.getenv("CVE_ID", "")

    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        agent = RequirementAgent(
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
                GitLogSearchTool(),
                BumpReleaseTool(),
                AddChangelogEntryTool(),
            ]
            + [
                t
                for t in gateway_tools
                if t.name in ("fork_repository", "open_merge_request", "push_to_remote_repository")
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
                '"RHEL Packaging Agent <jotnar@redhat.com>"',
                "* IMPORTANT: Only perform changes relevant to the backport update",
            ],
        )

        dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

        async def run(input):
            response = await agent.run(
                prompt=render_prompt(input),
                expected_output=OutputSchema,
                execution=get_agent_execution_config(),
            )
            return OutputSchema.model_validate_json(response.answer.text)

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
                cve_id=cve_id,
            )
            unpacked_sources, local_clone = prepare_package(package, jira_issue, branch, input)
            input.unpacked_sources = str(unpacked_sources)
            try:
                output = await run(input)
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
                element = await fixAwait(redis.brpop(["backport_queue"], timeout=30))
                if element is None:
                    logger.info("No tasks received, continuing to wait...")
                    continue

                _, payload = element
                logger.info(f"Received task from queue.")

                task = Task.model_validate_json(payload)
                backport_data = BackportData.model_validate(task.metadata)
                logger.info(
                    f"Processing backport for package: {backport_data.package}, "
                    f"JIRA: {backport_data.jira_issue}, attempt: {task.attempts + 1}"
                )

                input = InputSchema(
                    package=backport_data.package,
                    upstream_fix=backport_data.patch_url,
                    jira_issue=backport_data.jira_issue,
                    dist_git_branch=backport_data.branch,
                    cve_id=backport_data.cve_id,
                )
                unpacked_sources, local_clone = prepare_package(
                    backport_data.package, backport_data.jira_issue, backport_data.branch, input
                )
                input.unpacked_sources = str(unpacked_sources)

                async def retry(task, error):
                    task.attempts += 1
                    if task.attempts < max_retries:
                        logger.warning(
                            f"Task failed (attempt {task.attempts}/{max_retries}), "
                            f"re-queuing for retry: {backport_data.jira_issue}"
                        )
                        await fixAwait(redis.lpush("backport_queue", task.model_dump_json()))
                    else:
                        logger.error(
                            f"Task failed after {max_retries} attempts, "
                            f"moving to error list: {backport_data.jira_issue}"
                        )
                        await fixAwait(redis.lpush("error_list", error))

                try:
                    logger.info(f"Starting backport processing for {backport_data.jira_issue}")
                    output = await run(input)
                    logger.info(
                        f"Backport processing completed for {backport_data.jira_issue}, " f"success: {output.success}"
                    )
                except Exception as e:
                    error = "".join(traceback.format_exception(e))
                    logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                    await retry(task, ErrorData(details=error, jira_issue=input.jira_issue).model_dump_json())
                    rmtree(local_clone)
                else:
                    rmtree(local_clone)
                    if output.success:
                        logger.info(f"Backport successful for {backport_data.jira_issue}, " f"adding to completed list")
                        await fixAwait(redis.lpush("completed_backport_list", output.model_dump_json()))
                    else:
                        logger.warning(f"Backport failed for {backport_data.jira_issue}: {output.error}")
                        await retry(task, output.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
