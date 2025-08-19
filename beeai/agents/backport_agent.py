import asyncio
import logging
import os
from shutil import rmtree
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

import tasks
from constants import COMMIT_PREFIX
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.specfile import AddChangelogEntryTool, BumpReleaseTool
from tools.text import CreateTool, InsertTool, StrReplaceTool, ViewTool
from tools.wicked_git import GitLogSearchTool, GitPatchCreationTool
from triage_agent import BackportData, ErrorData
from utils import fix_await, check_subprocess, get_agent_execution_config, mcp_tools, redis_client, post_private_jira_comment

logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    local_clone: Path = Field(description="Path to the local clone of forked dist-git repository")
    unpacked_sources: Path = Field(description="Path to the unpacked (using `centpkg prep`) sources")
    package: str = Field(description="Package to update")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
    upstream_fix: str = Field(description="Link to an upstream fix for the issue")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    cve_id: str = Field(default="", description="CVE ID if the jira issue is a CVE")


class OutputSchema(BaseModel):
    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(description="Backport status")
    mr_url: str | None = Field(description="URL to the opened merge request")
    error: str | None = Field(description="Specific details about an error")


def render_prompt(input: InputSchema) -> str:
    template = (
        'Work inside the repository cloned in "{{ local_clone }}", it is your current working directory\n'
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
    )
    return PromptTemplate(PromptTemplateInput(schema=InputSchema, template=template)).render(input)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])
    cve_id = os.getenv("CVE_ID", "")

    async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
        backport_agent = RequirementAgent(
            llm=ChatModel.from_name(os.environ["CHAT_MODEL"]),
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

        class State(BaseModel):
            jira_issue: str
            package: str
            dist_git_branch: str
            upstream_fix: str
            cve_id: str
            local_clone: Path | None = Field(default=None)
            update_branch: str | None = Field(default=None)
            fork_url: str | None = Field(default=None)
            unpacked_sources: Path | None = Field(default=None)
            backport_result: OutputSchema | None = Field(default=None)
            merge_request_url: str | None = Field(default=None)

        workflow = Workflow(State)

        async def fork_and_prepare_dist_git(state):
            state.local_clone, state.update_branch, state.fork_url = await tasks.fork_and_prepare_dist_git(
                jira_issue=state.jira_issue,
                package=state.package,
                dist_git_branch=state.dist_git_branch,
                available_tools=gateway_tools,
            )
            await check_subprocess(["centpkg", "sources"], cwd=state.local_clone)
            await check_subprocess(["centpkg", "prep"], cwd=state.local_clone)
            unpacked_sources = list(state.local_clone.glob(f"*-build/*{state.package}*"))
            if len(unpacked_sources) != 1:
                raise ValueError(f"Expected exactly one unpacked source, got {unpacked_sources}")
            [state.unpacked_sources] = unpacked_sources
            return "run_backport_agent"

        async def run_backport_agent(state):
            cwd = Path.cwd()
            try:
                # make things easier for the LLM
                os.chdir(state.local_clone)
                response = await backport_agent.run(
                    prompt=render_prompt(
                        InputSchema(
                            local_clone=state.local_clone,
                            unpacked_sources=state.unpacked_sources,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            upstream_fix=state.upstream_fix,
                            jira_issue=state.jira_issue,
                            cve_id=state.cve_id,
                        ),
                    ),
                    expected_output=OutputSchema,
                    execution=get_agent_execution_config(),
                )
                state.backport_result = OutputSchema.model_validate_json(response.answer.text)
            finally:
                os.chdir(cwd)
            if state.backport_result.success:
                return "commit_push_and_open_mr"
            else:
                return Workflow.END

        async def commit_push_and_open_mr(state):
            state.merge_request_url = await tasks.commit_push_and_open_mr(
                local_clone=state.local_clone,
                files_to_commit=["*.spec", f"{state.jira_issue}.patch"],
                commit_message=f"{COMMIT_PREFIX} backport {state.jira_issue}",
                fork_url=state.fork_url,
                dist_git_branch=state.dist_git_branch,
                update_branch=state.update_branch,
                mr_title="{COMMIT_PREFIX} backport {state.jira_issue}",
                mr_description="TODO",
                available_tools=gateway_tools,
                commit_only=os.getenv("DRY_RUN", "False").lower() == "true",
            )
            return Workflow.END

        workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
        workflow.add_step("run_backport_agent", run_backport_agent)
        workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)

        async def run_workflow(package, dist_git_branch, upstream_fix, jira_issue, cve_id):
            response = await workflow.run(
                State(
                    package=package,
                    dist_git_branch=dist_git_branch,
                    upstream_fix=upstream_fix,
                    jira_issue=jira_issue,
                    cve_id=cve_id,
                ),
            )
            return response.state

        if (
            (package := os.getenv("PACKAGE", None))
            and (branch := os.getenv("BRANCH", None))
            and (upstream_fix := os.getenv("UPSTREAM_FIX", None))
            and (jira_issue := os.getenv("JIRA_ISSUE", None))
        ):
            logger.info("Running in direct mode with environment variables")
            state = await run_workflow(
                package=package,
                dist_git_branch=branch,
                upstream_fix=upstream_fix,
                jira_issue=jira_issue,
                cve_id=os.getenv("CVE_ID", ""),
            )
            logger.info(f"Direct run completed: {state.backport_result.model_dump_json(indent=4)}")
            return

        class Task(BaseModel):
            metadata: dict = Field(description="Task metadata")
            attempts: int = Field(default=0, description="Number of processing attempts")

        logger.info("Starting backport agent in queue mode")
        async with redis_client(os.environ["REDIS_URL"]) as redis:
            max_retries = int(os.getenv("MAX_RETRIES", 3))
            logger.info(f"Connected to Redis, max retries set to {max_retries}")

            while True:
                logger.info("Waiting for tasks from backport_queue (timeout: 30s)...")
                element = await fix_await(redis.brpop(["backport_queue"], timeout=30))
                if element is None:
                    logger.info("No tasks received, continuing to wait...")
                    continue

                _, payload = element
                logger.info("Received task from queue.")

                task = Task.model_validate_json(payload)
                triage_state = task.metadata
                backport_data = BackportData.model_validate(triage_state["triage_result"]["data"])
                dist_git_branch = triage_state["target_branch"]
                logger.info(
                    f"Processing backport for package: {backport_data.package}, "
                    f"JIRA: {backport_data.jira_issue}, branch: {dist_git_branch}, "
                    f"attempt: {task.attempts + 1}"
                )

                async def retry(task, error):
                    task.attempts += 1
                    if task.attempts < max_retries:
                        logger.warning(
                            f"Task failed (attempt {task.attempts}/{max_retries}), "
                            f"re-queuing for retry: {backport_data.jira_issue}"
                        )
                        await fix_await(redis.lpush("backport_queue", task.model_dump_json()))
                    else:
                        logger.error(
                            f"Task failed after {max_retries} attempts, "
                            f"moving to error list: {backport_data.jira_issue}"
                        )
                        await fix_await(redis.lpush("error_list", error))

                try:
                    logger.info(f"Starting backport processing for {backport_data.jira_issue}")
                    state = await run_workflow(
                        package=backport_data.package,
                        dist_git_branch=dist_git_branch,
                        upstream_fix=backport_data.patch_url,
                        jira_issue=backport_data.jira_issue,
                        cve_id=backport_data.cve_id,
                    )
                    logger.info(
                        f"Backport processing completed for {backport_data.jira_issue}, " f"success: {state.backport_result.success}"
                    )

                    agent_type = "Backport"
                    if state.backport_result.success:
                        logger.info(f"Updating JIRA {backport_data.jira_issue} with {state.backport_result.mr_url} ")

                        await post_private_jira_comment(gateway_tools, backport_data.jira_issue, agent_type, state.backport_result.mr_url)
                    else:
                        logger.info(f"Agent failed to perform a backport for {backport_data.jira_issue}.")
                        await post_private_jira_comment(gateway_tools, backport_data.jira_issue, agent_type,
                                                        "Agent failed to perform a backport: {state.backport_result.error}")



                except Exception as e:
                    error = "".join(traceback.format_exception(e))
                    logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                    await retry(task, ErrorData(details=error, jira_issue=backport_data.jira_issue).model_dump_json())
                    rmtree(local_clone)
                else:
                    rmtree(local_clone)
                    if state.backport_data.success:
                        logger.info(f"Backport successful for {backport_data.jira_issue}, " f"adding to completed list")
                        await redis.lpush("completed_backport_list", output.model_dump_json())
                    else:
                        logger.warning(f"Backport failed for {backport_data.jira_issue}: {state.backport_data.error}")
                        await retry(task, state.backport_data.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
