import asyncio
import copy
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

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
from common.config import get_package_instructions
from common.models import BuildInputSchema, BuildOutputSchema, RebaseInputSchema, RebaseOutputSchema, Task
from common.utils import redis_client, fix_await
from constants import I_AM_JOTNAR, CAREFULLY_REVIEW_CHANGES, JiraLabels
from observability import setup_observability
from tools.commands import RunShellCommandTool
from tools.specfile import AddChangelogEntryTool
from tools.text import CreateTool, InsertTool, StrReplaceTool, ViewTool
from triage_agent import RebaseData, ErrorData
from utils import get_agent_execution_config, mcp_tools, render_prompt, run_tool

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert on rebasing packages in RHEL ecosystem.

      To rebase package <PACKAGE> to version <VERSION> in dist-git branch <DIST_GIT_BRANCH>, do the following:

      1. Check if the current version is older than <VERSION>. To get the current version,
         you can use `rpmspec -q --queryformat "%{VERSION}\n" --srpm <PACKAGE>.spec`.
         To compare versions, use `rpmdev-vercmp`. If the current version is not older than <VERSION>,
         rebasing doesn't make sense, so end the process with an error.

      2. Try to find past rebases in git history to see how this particular package does rebases.
         Keep in mind what parts of the spec file are usually changed. At the minimum a rebase should
         change `Version` and `Release` tags (or corresponding macros) and add a new changelog entry,
         but sometimes other things are changed - if that's the case, try to understand the logic behind it.

      3. Update the spec file. Set <VERSION>, reset release and do any other usual changes. You may need
         to get some information from the upstream repository, for example commit hashes.
         Add a new changelog entry using the `add_changelog_entry` tool. Use `rpmlint <PACKAGE>.spec`
         to validate your changes and fix any new issues.

      4. Download upstream sources using `spectool -g -S <PACKAGE>.spec`. Run `centpkg --release <DIST_GIT_BRANCH> prep`
         to see if everything is in order. It is possible that some *.patch files will fail to apply now
         that the spec file has been updated. Don't jump to conclusions - if one patch fails to apply, it doesn't mean
         all other patches fail to apply as well. Go through the errors one by one, fix them and verify the changes
         by running `centpkg --release <DIST_GIT_BRANCH> prep` again. Repeat as necessary. Do not remove any patches
         unless all their hunks have been already applied to the upstream sources.

      5. Upload new upstream sources (files that the `spectool` command downloaded in the previous step)
         to lookaside cache using the `upload_sources` tool.

      6. Generate a SRPM using `centpkg --release <DIST_GIT_BRANCH> srpm`.

      7. In your output, provide a "files_to_git_add" list containing all files that should be git added for this rebase.
         This typically includes the updated .spec file and any new/modified/deleted patch files or other files you've changed
         or added/removed during the rebase. Do not include files that were automatically generated or downloaded by spectool.


      General instructions:

      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog, you are only allowed to add a single changelog entry.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Prefer native tools, if available, the `run_shell_command` tool should be the last resort.
      - If there are package-specific instructions, incorporate them into your work.
    """


def get_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of dist-git repository of package {{package}}.
      {{dist_git_branch}} dist-git branch has been checked out. You are working on Jira issue {{jira_issue}}
      {{#cve_id}}(a.k.a. {{.}}){{/cve_id}}.

      {{#fedora_clone}}
      Additionally, you have access to the corresponding Fedora repository (rawhide branch) at {{.}}.
      This can be used as a reference for comparing package versions, spec files, patches, and other packaging details when explicitly instructed to do so.
      {{/fedora_clone}}

      {{^build_error}}
      Rebase the package to version {{version}}. Use "- resolves: {{jira_issue}}" as changelog entry.
      {{#package_instructions}}

      **Package-specific instructions (these are important to follow, incorporate them into your workflow reasonably):**
      {{.}}
      {{/package_instructions}}
      {{/build_error}}
      {{#build_error}}
      This is a repeated rebase, after the previous attempt the generated SRPM failed to build:

      {{.}}

      Do your best to fix the issue and then generate a new SRPM.
      {{/build_error}}
    """


def create_rebase_agent(mcp_tools: list[Tool], run_shell_command_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(
        name="RebaseAgent",
        llm=ChatModel.from_name(os.environ["CHAT_MODEL"]),
        tools=[
            ThinkTool(),
            RunShellCommandTool(options=run_shell_command_options),
            DuckDuckGoSearchTool(),
            CreateTool(),
            ViewTool(),
            InsertTool(),
            StrReplaceTool(),
            AddChangelogEntryTool(),
        ] + [t for t in mcp_tools if t.name == "upload_sources"],
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

    run_shell_command_options = {"working_directory": None}

    class State(BaseModel):
        jira_issue: str
        package: str
        dist_git_branch: str
        version: str
        local_clone: Path | None = Field(default=None)
        fedora_clone: Path | None = Field(default=None)
        update_branch: str | None = Field(default=None)
        fork_url: str | None = Field(default=None)
        attempts_remaining: int = Field(default=max_build_attempts)
        build_error: str | None = Field(default=None)
        rebase_result: RebaseOutputSchema | None = Field(default=None)
        merge_request_url: str | None = Field(default=None)

    async def run_workflow(package, dist_git_branch, version, jira_issue):
        run_shell_command_options["working_directory"] = None

        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            rebase_agent = create_rebase_agent(gateway_tools, run_shell_command_options)
            build_agent = create_build_agent(gateway_tools, run_shell_command_options)

            workflow = Workflow(State, name="RebaseWorkflow")

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
                state.local_clone, state.update_branch, state.fork_url, state.fedora_clone = await tasks.fork_and_prepare_dist_git(
                    jira_issue=state.jira_issue,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                    with_fedora=True,
                )
                run_shell_command_options["working_directory"] = state.local_clone
                return "run_rebase_agent"

            async def run_rebase_agent(state):
                package_instructions = await get_package_instructions(state.package, "rebase")
                response = await rebase_agent.run(
                    prompt=render_prompt(
                        template=get_prompt(),
                        input=RebaseInputSchema(
                            local_clone=state.local_clone,
                            fedora_clone=state.fedora_clone,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            version=state.version,
                            jira_issue=state.jira_issue,
                            build_error=state.build_error,
                            package_instructions=package_instructions,
                        ),
                    ),
                    expected_output=RebaseOutputSchema,
                    execution=get_agent_execution_config(),
                )
                state.rebase_result = RebaseOutputSchema.model_validate_json(response.answer.text)
                if state.rebase_result.success:
                    return "run_build_agent"
                return "comment_in_jira"

            async def run_build_agent(state):
                response = await build_agent.run(
                    prompt=render_prompt(
                        template=get_build_prompt(),
                        input=BuildInputSchema(
                            srpm_path=state.rebase_result.srpm_path,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                        ),
                    ),
                    expected_output=BuildOutputSchema,
                    execution=get_agent_execution_config(),
                )
                build_result = BuildOutputSchema.model_validate_json(response.answer.text)
                if build_result.success:
                    return "commit_push_and_open_mr"
                state.attempts_remaining -= 1
                if state.attempts_remaining <= 0:
                    state.rebase_result.success = False
                    state.rebase_result.error = (
                        f"Unable to successfully build the package in {max_build_attempts} attempts"
                    )
                    return "comment_in_jira"
                state.build_error = build_result.error
                return "run_rebase_agent"

            async def commit_push_and_open_mr(state):
                # Use files specified by rebase agent, fallback to *.spec if none specified
                files_to_git_add = state.rebase_result.files_to_git_add or ["*.spec"]

                try:
                    state.merge_request_url = await tasks.commit_push_and_open_mr(
                        local_clone=state.local_clone,
                        files_to_commit=files_to_git_add,
                        commit_message=(
                            f"Rebase to version {state.version}\n\n"
                            f"Resolves: {state.jira_issue}\n\n"
                            f"This commit was created {I_AM_JOTNAR}\n\n"
                            f"Assisted-by: Jotnar\n"
                        ),
                        fork_url=state.fork_url,
                        dist_git_branch=state.dist_git_branch,
                        update_branch=state.update_branch,
                        mr_title=f"Update to version {state.version}",
                        mr_description=(
                            f"This merge request was created {I_AM_JOTNAR}\n"
                            f"{CAREFULLY_REVIEW_CHANGES}\n\n"
                            f"Resolves: {state.jira_issue}\n\n"
                            "Status of the rebase:\n\n"
                            f"{state.rebase_result.status}"
                        ),
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                    )
                except Exception as e:
                    logger.warning(f"Error committing and opening MR: {e}")
                    state.merge_request_url = None
                    state.rebase_result.success = False
                    state.rebase_result.error = f"Could not commit and open MR: {e}"
                return "comment_in_jira"

            async def comment_in_jira(state):
                if dry_run:
                    return Workflow.END
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Rebase",
                    comment_text=(
                        state.merge_request_url
                        if state.rebase_result.success
                        else f"Agent failed to perform a rebase: {state.rebase_result.error}"
                    ),
                    available_tools=gateway_tools,
                )
                return Workflow.END

            workflow.add_step("change_jira_status", change_jira_status)
            workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
            workflow.add_step("run_rebase_agent", run_rebase_agent)
            workflow.add_step("run_build_agent", run_build_agent)
            workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
            workflow.add_step("comment_in_jira", comment_in_jira)

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

    logger.info("Starting rebase agent in queue mode")
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        logger.info(f"Connected to Redis, max retries set to {max_retries}")

        while True:
            logger.info("Waiting for tasks from rebase_queue (timeout: 30s)...")
            element = await fix_await(redis.brpop(["rebase_queue"], timeout=30))
            if element is None:
                logger.info("No tasks received, continuing to wait...")
                continue

            _, payload = element
            logger.info("Received task from queue.")

            task = Task.model_validate_json(payload)
            triage_state = task.metadata
            rebase_data = RebaseData.model_validate(triage_state["triage_result"]["data"])
            dist_git_branch = triage_state["target_branch"]
            logger.info(
                f"Processing rebase for package: {rebase_data.package}, "
                f"version: {rebase_data.version}, JIRA: {rebase_data.jira_issue}, "
                f"branch: {dist_git_branch}, attempt: {task.attempts + 1}"
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {rebase_data.jira_issue}"
                    )
                    await fix_await(redis.lpush("rebase_queue", task.model_dump_json()))
                else:
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {rebase_data.jira_issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=rebase_data.jira_issue,
                        labels_to_add=[JiraLabels.REBASE_ERRORED.value],
                        labels_to_remove=[JiraLabels.REBASE_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await fix_await(redis.lpush("error_list", error))

            try:
                logger.info(f"Starting rebase processing for {rebase_data.jira_issue}")
                state = await run_workflow(
                    package=rebase_data.package,
                    dist_git_branch=dist_git_branch,
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
                    await tasks.set_jira_labels(
                        jira_issue=rebase_data.jira_issue,
                        labels_to_add=[JiraLabels.REBASED.value],
                        labels_to_remove=[JiraLabels.REBASE_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await fix_await(redis.lpush("completed_rebase_list", state.rebase_result.model_dump_json()))
                else:
                    logger.warning(f"Rebase failed for {rebase_data.jira_issue}: {state.rebase_result.error}")
                    await tasks.set_jira_labels(
                        jira_issue=rebase_data.jira_issue,
                        labels_to_add=[JiraLabels.REBASE_FAILED.value],
                        labels_to_remove=[JiraLabels.REBASE_IN_PROGRESS.value],
                        dry_run=dry_run
                    )
                    await retry(task, state.rebase_result.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
