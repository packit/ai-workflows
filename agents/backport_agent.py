import asyncio
import copy
import logging
import os
import sys
import re
import traceback
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.prompts import RequirementAgentSystemPrompt
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
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
from agents.package_update_steps import PackageUpdateStep, PackageUpdateState
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
from tools.filesystem import GetCWDTool, RemoveTool
from tools.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    StrReplaceTool,
    ViewTool,
    SearchTextTool,
)
from tools.wicked_git import (
    GitLogSearchTool,
    GitPatchApplyTool,
    GitPatchApplyFinishTool,
    GitPatchCreationTool,
    GitPreparePackageSources,
)
from triage_agent import BackportData, ErrorData
from utils import (
    check_subprocess,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    mcp_tools,
    render_prompt,
)
from specfile import Specfile

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert on backporting upstream patches to packages in RHEL ecosystem.

      To backport upstream fix <UPSTREAM_FIX> to package <PACKAGE> in dist-git branch <DIST_GIT_BRANCH>, do the following:

      1. Knowing Jira issue <JIRA_ISSUE>, CVE ID <CVE_ID> or both, use the `git_log_search` tool to check
         in the dist-git repository whether the issue/CVE has already been resolved. If it has,
         end the process with `success=True` and `status="Backport already applied"`.

      2. Use the `git_prepare_package_sources` tool to prepare package sources in directory <UNPACKED_SOURCES>
         for application of the upstream fix.

      3. Backport the <UPSTREAM_FIX> patch:

         - Use the `git_patch_apply` tool, with an absolute path to the patch, to apply the patch.
         - Resolve all conflicts and leave the repository in a dirty state. Delete all *.rej files.
         - Use the `git_apply_finish` tool to finish the patch application.

      4. Once there are no more conflicts, use the `git_patch_create` tool with <UPSTREAM_FIX>
         as an argument to update the patch file.

      5. Update the spec file. Add a new `Patch` tag pointing to the <UPSTREAM_FIX> patch file.
         Add the new `Patch` tag after all existing `Patch` tags and, if `Patch` tags are numbered,
         make sure it has the highest number. Make sure the patch is applied in the "%prep" section
         and the `-p` argument is correct.

      6. Use `rpmlint <PACKAGE>.spec` to validate your changes and fix any new issues.

      7. Run `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep` to see if the new patch
         applies cleanly. When `prep` command finishes with "exit 0", it's a success. Ignore errors from
         libtoolize that warn about newer files: "use '--force' to overwrite".

      8. Generate a SRPM using `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm`.


      General instructions:

      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Prefer native tools, if available, the `run_shell_command` tool should be the last resort.
      - Ignore all changes that cause conflicts in the following kinds of files: .github/ workflows, .gitignore, news, changes, and internal documentation.
      - Apply all changes that modify the core library of the package, and all binaries, manpages, and user-facing documentation.
      - For more information how the package is being built, inspect the RPM spec file and read sections `%prep` and `%build`.
      - If there is a complex conflict, you are required to properly resolve it by applying the core functionality of the proposed patch.
      - Never apply the patches yourself, always use the `git_patch_apply` tool.
      - Never run `git am --skip`, always use the `git_apply_finish` tool instead.
      - Never abort the existing git am session.
    """


def get_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of dist-git repository of package {{package}}.
      {{dist_git_branch}} dist-git branch has been checked out. You are working on Jira issue {{jira_issue}}
      {{#cve_id}}(a.k.a. {{.}}){{/cve_id}}.
      {{^build_error}}
      Backport upstream fix {{local_clone}}/{{jira_issue}}.patch.
      Unpacked upstream sources are in {{unpacked_sources}}.
      {{/build_error}}
      {{#build_error}}
      This is a repeated backport, after the previous attempt the generated SRPM failed to build:

      {{.}}

      Everything from the previous attempt has been reset. Start over, follow the instructions from the start
      and don't forget to fix the issue.
      {{/build_error}}
    """


def create_backport_agent(_: list[Tool], local_tool_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(
        name="BackportAgent",
        llm=get_chat_model(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            DuckDuckGoSearchTool(),
            RunShellCommandTool(options=local_tool_options),
            CreateTool(options=local_tool_options),
            ViewTool(options=local_tool_options),
            InsertTool(options=local_tool_options),
            InsertAfterSubstringTool(options=local_tool_options),
            StrReplaceTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            GetCWDTool(options=local_tool_options),
            RemoveTool(options=local_tool_options),
            GitPatchCreationTool(options=local_tool_options),
            GitPatchApplyTool(options=local_tool_options),
            GitPatchApplyFinishTool(options=local_tool_options),
            GitLogSearchTool(options=local_tool_options),
            GitPreparePackageSources(options=local_tool_options),
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat Enterprise Linux developer",
        instructions=get_instructions(),
        # role and instructions above set defaults for the system prompt input
        # but the `RequirementAgentSystemPrompt` instance is shared so the defaults
        # affect all requirement agents - use our own copy to prevent that
        templates={"system": copy.deepcopy(RequirementAgentSystemPrompt)},
    )


def get_unpacked_sources(local_clone: Path, package: str) -> Path:
    """
    Get a path to the root of extracted archive directory tree (referenced as TLD
    in RPM documentation) for a given package.

    That's the place where we'll initiate the backporting process.
    """
    with Specfile(local_clone / f"{package}.spec") as spec:
        buildsubdir = spec.expand("%{buildsubdir}")
    if "/" in buildsubdir:
        # Sooner or later we'll run into a package where this will break. Sorry.
        # More details: https://github.com/packit/jotnar/issues/217
        buildsubdir = buildsubdir.split("/")[0]
    sources_dir = local_clone / buildsubdir

    if not sources_dir.exists():
        raise ValueError(f"Unpacked source directory does not exist: {sources_dir}")

    return sources_dir


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))

    local_tool_options = {"working_directory": None}

    class State(PackageUpdateState):
        upstream_fix: str
        cve_id: str | None
        unpacked_sources: Path | None = Field(default=None)
        backport_log: list[str] = Field(default=[])
        backport_result: BackportOutputSchema | None = Field(default=None)
        attempts_remaining: int = Field(default=max_build_attempts)

    async def run_workflow(
        package, dist_git_branch, upstream_fix, jira_issue, cve_id, redis_conn=None
    ):
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
                centpkg_cmd = ["centpkg", f"--name={state.package}", "--namespace=rpms", f"--release={state.dist_git_branch}"]
                await check_subprocess(centpkg_cmd + ["sources"], cwd=state.local_clone)
                await check_subprocess(centpkg_cmd + ["prep"], cwd=state.local_clone)
                state.unpacked_sources = get_unpacked_sources(state.local_clone, state.package)
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
                    return "update_release"
                if build_result.is_timeout:
                    logger.info(f"Build timed out for {state.jira_issue}, proceeding")
                    return "update_release"
                state.attempts_remaining -= 1
                if state.attempts_remaining <= 0:
                    state.backport_result.success = False
                    state.backport_result.error = (
                        f"Unable to successfully build the package in {max_build_attempts} attempts"
                    )
                    return "comment_in_jira"
                state.build_error = build_result.error
                return "fork_and_prepare_dist_git"

            async def update_release(state):
                try:
                    await tasks.update_release(
                        local_clone=state.local_clone,
                        package=state.package,
                        dist_git_branch=state.dist_git_branch,
                        rebase=False,
                    )
                except Exception as e:
                    logger.warning(f"Error updating release: {e}")
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not update release: {e}"
                    return "comment_in_jira"
                return "stage_changes"

            async def stage_changes(state):
                try:
                    await tasks.stage_changes(
                        local_clone=state.local_clone,
                        files_to_commit=["*.spec", f"{state.jira_issue}.patch"],
                    )
                except Exception as e:
                    logger.warning(f"Error staging changes: {e}")
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not stage changes: {e}"
                    return "comment_in_jira"
                if state.log_result:
                    return "commit_push_and_open_mr"
                return "run_log_agent"

            async def run_log_agent(state):
                response = await log_agent.run(
                    render_prompt(
                        template=get_log_prompt(),
                        input=LogInputSchema(
                            jira_issue=state.jira_issue,
                            changes_summary=state.backport_log[-1],
                        ),
                    ),
                    expected_output=LogOutputSchema,
                    **get_agent_execution_config(),
                )
                log_output = LogOutputSchema.model_validate_json(response.last_message.text)

                if redis_conn and not dry_run:
                    # Cache MR metadata for sharing MR titles
                    # for the same upstream fix across different streams if redis
                    # is available.
                    # Do not modify the cache during a dry run.
                    log_output = await tasks.cache_mr_metadata(
                        redis_conn,
                        log_output=log_output,
                        operation_type="backport",
                        package=state.package,
                        details=state.upstream_fix,
                    )
                state.log_result = log_output

                return "stage_changes"

            async def commit_push_and_open_mr(state):
                try:
                    state.merge_request_url = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        commit_message=(
                            f"{state.log_result.title}\n\n"
                            f"{state.log_result.description}\n\n"
                            + (f"CVE: {state.cve_id}\n" if state.cve_id else "")
                            + f"Upstream fix: {state.upstream_fix}\n"
                            + f"Resolves: {state.jira_issue}\n\n"
                            f"This commit was backported {I_AM_JOTNAR}\n\n"
                            "Assisted-by: Jotnar\n"
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
                            f"Backporting steps:\n\n{state.backport_log[-1]}"
                        ),
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                    )
                except Exception as e:
                    logger.warning(f"Error committing and opening MR: {e}")
                    state.merge_request_url = None
                    state.backport_result.success = False
                    state.backport_result.error = f"Could not commit and open MR: {e}"
                return "add_fusa_label"

            async def add_fusa_label(state):
                return await PackageUpdateStep.add_fusa_label(state, "comment_in_jira", dry_run=dry_run, gateway_tools=gateway_tools)

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
            workflow.add_step("update_release", update_release)
            workflow.add_step("stage_changes", stage_changes)
            workflow.add_step("run_log_agent", run_log_agent)
            workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
            workflow.add_step("add_fusa_label", add_fusa_label)
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
            cve_id=os.getenv("CVE_ID", None),
            redis_conn=None,
        )
        logger.info(f"Direct run completed: {state.backport_result.model_dump_json(indent=4)}")
        return

    logger.info("Starting backport agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        # Determine which backport queue to listen to based on container version
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        backport_queue = RedisQueues.BACKPORT_QUEUE_C9S.value if container_version == "c9s" else RedisQueues.BACKPORT_QUEUE_C10S.value
        logger.info(f"Connected to Redis, max retries set to {max_retries}, listening to queue: {backport_queue}")

        while True:
            logger.info(f"Waiting for tasks from {backport_queue} (timeout: 30s)...")
            element = await fix_await(redis.brpop([backport_queue], timeout=30))
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
                    await fix_await(redis.lpush(backport_queue, task.model_dump_json()))
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
                    redis_conn=redis,
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
                        labels_to_remove=[
                            JiraLabels.BACKPORT_IN_PROGRESS.value,
                            JiraLabels.BACKPORT_ERRORED.value,
                            JiraLabels.BACKPORT_FAILED.value,
                        ],
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
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
