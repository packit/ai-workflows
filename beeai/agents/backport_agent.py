import asyncio
import copy
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.prompts import RequirementAgentSystemPrompt
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
from beeai_framework.workflows import Workflow

import tasks
from agents.build_agent import create_build_agent, get_prompt as get_build_prompt
from agents.log_agent import create_log_agent, get_prompt as get_log_prompt
from common.constants import JiraLabels, RedisQueues
from common.models import (
    BackportInputSchema,
    BackportOutputSchema,
    BuildInputSchema,
    BuildOutputSchema,
    LogInputSchema,
    LogOutputSchema,
    Task,
)
from common.utils import redis_client, fix_await
from constants import I_AM_JOTNAR, CAREFULLY_REVIEW_CHANGES
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.specfile import BumpReleaseTool
from tools.text import CreateTool, InsertAfterSubstringTool, InsertTool, StrReplaceTool, ViewTool
from tools.wicked_git import GitLogSearchTool, GitPatchCreationTool, GitPreparePackageSources
from triage_agent import BackportData, ErrorData
from utils import check_subprocess, get_agent_execution_config, mcp_tools, render_prompt

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert on backporting upstream patches to packages in RHEL ecosystem.

      To backport upstream fix <UPSTREAM_FIX> to package <PACKAGE> in dist-git branch <DIST_GIT_BRANCH>, do the following:

      1. Knowing Jira issue <JIRA_ISSUE>, CVE ID <CVE_ID> or both, use the `git_log_search` tool to check
         whether the issue/CVE has already been resolved. If it has, end the process with `success=True`
         and `status="Backport already applied"`.

      2. Use the `git_prepare_package_sources` tool to prepare package sources in directory <UNPACKED_SOURCES>
         for application of the upstream fix.

      3. Backport the <UPSTREAM_FIX> patch:

         - Navigate to <UNPACKED_SOURCES> and use `git am --reject <UPSTREAM_FIX>` to apply the patch.
         - Resolve all conflicts and leave the repository in a dirty state. Under any circumstances
           do not run `git am --continue`.
         - Delete all *.rej files.

      4. Once there are no more conflicts, use the `git_patch_create` tool with <UPSTREAM_FIX>
         as an argument to update the patch file.

      5. Bump release in the spec file and add a new `Patch` tag pointing to the <UPSTREAM_FIX> patch file.
         Add the new `Patch` tag after all existing `Patch` tags and, if `Patch` tags are numbered,
         make sure it has the highest number.
         Use `rpmlint <PACKAGE>.spec` to validate your changes and fix any new issues.

      6. Run `centpkg --release <DIST_GIT_BRANCH> prep` to see if the new patch applies cleanly.

      7. Generate a SRPM using `centpkg --release <DIST_GIT_BRANCH> srpm`.


      General instructions:

      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Prefer native tools, if available, the `run_shell_command` tool should be the last resort.
    """


def get_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of dist-git repository of package {{package}}.
      {{dist_git_branch}} dist-git branch has been checked out. You are working on Jira issue {{jira_issue}}
      {{#cve_id}}(a.k.a. {{.}}){{/cve_id}}.
      {{^build_error}}
      Backport upstream fix {{jira_issue}}.patch. Unpacked upstream sources are in {{unpacked_sources}}.
      {{/build_error}}
      {{#build_error}}
      This is a repeated backport, after the previous attempt the generated SRPM failed to build:

      {{.}}

      Do your best to fix the issue and then generate a new SRPM.
      {{/build_error}}
    """


def create_backport_agent(_: list[Tool], local_tool_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(
        name="BackportAgent",
        llm=ChatModel.from_name(os.environ["CHAT_MODEL"]),
        tools=[
            ThinkTool(),
            DuckDuckGoSearchTool(),
            RunShellCommandTool(options=local_tool_options),
            CreateTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            InsertTool(options=local_tool_options),
            InsertAfterSubstringTool(options=local_tool_options),
            StrReplaceTool(options=local_tool_options),
            GitPatchCreationTool(options=local_tool_options),
            GitLogSearchTool(options=local_tool_options),
            BumpReleaseTool(options=local_tool_options),
            GitPreparePackageSources(options=local_tool_options),
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(ThinkTool, force_at_step=1, force_after=Tool, consecutive_allowed=False),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
        # role and instructions above set defaults for the system prompt input
        # but the `RequirementAgentSystemPrompt` instance is shared so the defaults
        # affect all requirement agents - use our own copy to prevent that
        templates={"system": copy.deepcopy(RequirementAgentSystemPrompt)},
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))

    local_tool_options = {"working_directory": None}

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
        attempts_remaining: int = Field(default=max_build_attempts)
        backport_log: list[str] = Field(default=[])
        build_error: str | None = Field(default=None)
        backport_result: BackportOutputSchema | None = Field(default=None)
        log_result: LogOutputSchema | None = Field(default=None)
        merge_request_url: str | None = Field(default=None)

    async def run_workflow(package, dist_git_branch, upstream_fix, jira_issue, cve_id):
        local_tool_options["working_directory"] = None

        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            backport_agent = create_backport_agent(gateway_tools, local_tool_options)
            build_agent = create_build_agent(gateway_tools, local_tool_options)
            log_agent = create_log_agent(gateway_tools, local_tool_options)

            workflow = Workflow(State, name="BackportWorkflow")

            async def change_jira_status(state):
                if not dry_run:
                    try:
                        await tasks.change_jira_status(
                            jira_issue=state.jira_issue,
                            status="In Progress",
                            available_tools=gateway_tools,
                        )
                    except Exception as status_error:
                        logger.warning(f"Failed to change status for {state.jira_issue}: {status_error}")
                else:
                    logger.info(f"Dry run: would change status of {state.jira_issue} to In Progress")
                return "fork_and_prepare_dist_git"

            async def fork_and_prepare_dist_git(state):
                state.local_clone, state.update_branch, state.fork_url, _ = await tasks.fork_and_prepare_dist_git(
                    jira_issue=state.jira_issue,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                )
                local_tool_options["working_directory"] = state.local_clone
                await check_subprocess(["centpkg", "sources"], cwd=state.local_clone)
                await check_subprocess(["centpkg", "prep"], cwd=state.local_clone)
                unpacked_sources = list(state.local_clone.glob(f"*-build/*{state.package}*"))
                if len(unpacked_sources) != 1:
                    raise ValueError(f"Expected exactly one unpacked source, got {unpacked_sources}")
                [state.unpacked_sources] = unpacked_sources
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(state.upstream_fix) as response:
                        if response.status < 400:
                            (state.local_clone / f"{state.jira_issue}.patch").write_text(await response.text())
                        else:
                            raise ValueError(f"Failed to fetch upstream fix: {response.status}")
                return "run_backport_agent"

            async def run_backport_agent(state):
                response = await backport_agent.run(
                    render_prompt(
                        template=get_prompt(),
                        input=BackportInputSchema(
                            local_clone=state.local_clone,
                            unpacked_sources=state.unpacked_sources,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                            cve_id=state.cve_id,
                            build_error=state.build_error,
                        ),
                    ),
                    expected_output=BackportOutputSchema,
                    **get_agent_execution_config(),
                )
                state.backport_result = BackportOutputSchema.model_validate_json(response.last_message.text)
                if state.backport_result.success:
                    state.backport_log.append(state.backport_result.status)
                    return "run_build_agent"
                else:
                    return "comment_in_jira"

            async def run_build_agent(state):
                response = await build_agent.run(
                    render_prompt(
                        template=get_build_prompt(),
                        input=BuildInputSchema(
                            srpm_path=state.backport_result.srpm_path,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                        ),
                    ),
                    expected_output=BuildOutputSchema,
                    **get_agent_execution_config(),
                )
                build_result = BuildOutputSchema.model_validate_json(response.last_message.text)
                if build_result.success:
                    return "run_log_agent"
                state.attempts_remaining -= 1
                if state.attempts_remaining <= 0:
                    state.backport_result.success = False
                    state.backport_result.error = (
                        f"Unable to successfully build the package in {max_build_attempts} attempts"
                    )
                    return "comment_in_jira"
                state.build_error = build_result.error
                return "run_backport_agent"

            async def run_log_agent(state):
                response = await log_agent.run(
                    render_prompt(
                        template=get_log_prompt(),
                        input=LogInputSchema(
                            jira_issue=state.jira_issue,
                            changes_summary="\n".join(state.backport_log),
                        ),
                    ),
                    expected_output=LogOutputSchema,
                    **get_agent_execution_config(),
                )
                state.log_result = LogOutputSchema.model_validate_json(response.last_message.text)
                return "commit_push_and_open_mr"

            async def commit_push_and_open_mr(state):
                try:
                    state.merge_request_url = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        files_to_commit=["*.spec", f"{state.jira_issue}.patch"],
                        commit_message=(
                            f"{state.log_result.title}\n\n"
                            f"{state.log_result.description}\n\n"
                            f"{f'CVE: {state.cve_id}\n' if state.cve_id else ''}"
                            f"{f'Upstream fix: {state.upstream_fix}\n'}"
                            f"Resolves: {state.jira_issue}\n\n"
                            f"This commit was backported {I_AM_JOTNAR}\n\n"
                            f"Assisted-by: Jotnar\n"
                        ),
                        fork_url=state.fork_url,
                        dist_git_branch=state.dist_git_branch,
                        update_branch=state.update_branch,
                        mr_title=state.log_result.title,
                        mr_description=(
                            f"This merge request was created {I_AM_JOTNAR}\n"
                            f"{CAREFULLY_REVIEW_CHANGES}\n\n"
                            f"{state.log_result.description}\n\n"
                            f"Upstream patch: {state.upstream_fix}\n\n"
                            f"Resolves: {state.jira_issue}\n\n"
                            "Backporting steps:\n\n"
                            f"{'\n'.join(state.backport_log)}"
                        ),
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                    )
                except Exception as e:
                    logger.warning(f"Error committing and opening MR: {e}")
                    state.merge_request_url = None
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not commit and open MR: {e}"
                return "comment_in_jira"

            async def comment_in_jira(state):
                if dry_run:
                    return Workflow.END
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Backport",
                    comment_text=(
                        state.merge_request_url
                        if state.backport_result.success
                        else f"Agent failed to perform a backport: {state.backport_result.error}"
                    ),
                    available_tools=gateway_tools,
                )
                return Workflow.END

            workflow.add_step("change_jira_status", change_jira_status)
            workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
            workflow.add_step("run_backport_agent", run_backport_agent)
            workflow.add_step("run_build_agent", run_build_agent)
            workflow.add_step("run_log_agent", run_log_agent)
            workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
            workflow.add_step("comment_in_jira", comment_in_jira)

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

    logger.info("Starting backport agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from backport_queue (timeout: 30s)...")
            element = await fix_await(redis.brpop([RedisQueues.BACKPORT_QUEUE.value], timeout=30))
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
                    await fix_await(redis.lpush(RedisQueues.BACKPORT_QUEUE.value, task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {backport_data.jira_issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_ERRORED.value],
                        labels_to_remove=[JiraLabels.BACKPORT_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

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

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                await retry(task, ErrorData(details=error, jira_issue=backport_data.jira_issue).model_dump_json())
            else:
                if state.backport_result.success:
                    logger.info(f"Backport successful for {backport_data.jira_issue}, " f"adding to completed list")
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORTED.value],
                        labels_to_remove=[JiraLabels.BACKPORT_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await redis.lpush(RedisQueues.COMPLETED_BACKPORT_LIST.value, state.backport_result.model_dump_json())
                else:
                    logger.warning(f"Backport failed for {backport_data.jira_issue}: {state.backport_result.error}")
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_FAILED.value],
                        labels_to_remove=[JiraLabels.BACKPORT_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await retry(task, state.backport_result.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
