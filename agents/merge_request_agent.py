import asyncio
import copy
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from textwrap import dedent
from typing import Any

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
from common.config import get_package_instructions
from common.models import (
    BuildInputSchema,
    BuildOutputSchema,
    MergeRequestInputSchema,
    MergeRequestOutputSchema,
)
from constants import I_AM_JOTNAR
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
from utils import get_agent_execution_config, get_chat_model, get_tool_call_checker_config, mcp_tools, render_prompt

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert on maintaining packages in RHEL ecosystem. Your job is to tweak existing merge requests
      and accomodate user feedback.

      To process and accomodate feedback given on a merge request, knowing the target package <PACKAGE>
      and dist-git branch <DIST_GIT_BRANCH>, do the following:

      1. Go through the comments, including replies if relevant, and follow the provided feedback.

      2. If you updated the spec file, use `rpmlint <PACKAGE>.spec` to validate your changes and fix any new issues.

      3. Verify any changes to patches by running `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> prep`.
         Repeat as necessary. Do not remove any patches unless all their hunks have been already applied
         to the upstream sources.

      4. If you removed any patch file references from the spec file (e.g. because they were already applied upstream),
         you must remove all the corresponding patch files from the repository as well.

      5. Generate a SRPM using `centpkg --name=<PACKAGE> --namespace=rpms --release=<DIST_GIT_BRANCH> srpm`.

      6. In your output, provide a "files_to_git_add" list containing all files that have been modified, added or removed.
         This typically includes the updated spec file and any new/modified/deleted patch files or other files you've changed
         or added/removed during processing the feedback. Make sure to include patch files that were also removed
         from the spec file.


      General instructions:

      - If necessary, you can run `git checkout -- <FILE>` to revert any changes done to <FILE>.
      - Never change anything in the spec file changelog.
      - Preserve existing formatting and style conventions in spec files and patch headers.
      - Prefer native tools, if available, the `run_shell_command` tool should be the last resort.
      - If there are package-specific instructions, incorporate them into your work.
    """


def get_prompt() -> str:
    return """
      Your working directory is {{local_clone}}, a clone of source repository of merge request {{merge_request_url}}
      opened against {{dist_git_branch}} dist-git branch of package {{package}}. The merge request is titled
      "{{merge_request_title}}" and its description is:

      {{merge_request_description}}

      The merge request contains the following comments with user feedback:

      {{comments}}

      You are working on Jira issue {{jira_issue}}.

      {{#fedora_clone}}
      Additionally, you have access to the corresponding Fedora repository (rawhide branch) at {{.}}.
      This can be used as a reference for comparing package versions, spec files, patches, and other packaging details when explicitly instructed to do so.
      {{/fedora_clone}}

      {{^build_error}}
      Make changes necessary to accomodate user feedback provided in the comments.
      {{#package_instructions}}

      **Package-specific instructions (these are important to follow, incorporate them into your workflow reasonably):**
      {{.}}
      {{/package_instructions}}
      {{/build_error}}
      {{#build_error}}
      This is a retry, after the previous attempt the generated SRPM failed to build:

      {{.}}

      Everything from the previous attempt has been reset. Start over, follow the instructions from the start
      and don't forget to fix the issue.
      {{/build_error}}
    """


def create_merge_request_agent(mcp_tools: list[Tool], local_tool_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(
        name="MergeRequestAgent",
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
        ] + [t for t in mcp_tools if t.name == "upload_sources"],
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


def extract_jira_issue(mr_description: str) -> str:
    if not (m := re.search(r"Resolves:\s+(RHEL-\d+)", mr_description)):
        raise RuntimeError("Failed to extract Jira issue from MR description")
    return m.group(1)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))

    local_tool_options = {"working_directory": None}

    class State(BaseModel):
        merge_request_url: str
        local_clone: Path | None = Field(default=None)
        package: str | None = Field(default=None)
        dist_git_branch: str | None = Field(default=None)
        update_branch: str | None = Field(default=None)
        fork_url: str | None = Field(default=None)
        jira_issue: str | None = Field(default=None)
        merge_request_title: str | None = Field(default=None)
        merge_request_description: str | None = Field(default=None)
        merge_request_comments: str | None = Field(default=None)
        build_error: str | None = Field(default=None)
        fedora_clone: Path | None = Field(default=None)
        mr_update_log: list[str] = Field(default=[])
        mr_update_result: MergeRequestOutputSchema | None = Field(default=None)
        attempts_remaining: int = Field(default=max_build_attempts)
        all_files_git_to_add: set[str] = Field(default_factory=set)

    async def run_workflow(merge_request_url):
        local_tool_options["working_directory"] = None

        async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
            merge_request_agent = create_merge_request_agent(gateway_tools, local_tool_options)
            build_agent = create_build_agent(gateway_tools, local_tool_options)

            workflow = Workflow(State, name="MergeRequestWorkflow")

            async def prepare_dist_git_from_mr(state):
                state.local_clone, mr_details, state.fedora_clone = await tasks.prepare_dist_git_from_merge_request(
                    merge_request_url=state.merge_request_url,
                    available_tools=gateway_tools,
                    with_fedora=True,
                )

                if not mr_details.comments:
                    logger.info("No user feedback provided, nothing to do")
                    return Workflow.END

                state.package = mr_details.target_repo_name
                state.dist_git_branch = mr_details.target_branch
                state.update_branch = mr_details.source_branch
                state.fork_url = mr_details.source_repo
                state.jira_issue = extract_jira_issue(mr_details.description)
                state.merge_request_title = mr_details.title
                state.merge_request_description = mr_details.description

                comments_schema = mr_details.comments.model_json_schema(mode="serialization")
                comments = mr_details.comments.model_dump_json(indent=4)
                state.merge_request_comments = dedent(
                    f"""
                    JSON schema of the comments:
                    ```json
                    {comments_schema}
                    ```

                    Comments:
                    ```json
                    {comments}
                    ```
                    """
                )

                local_tool_options["working_directory"] = state.local_clone
                return "run_merge_request_agent"

            async def run_merge_request_agent(state):
                response = await merge_request_agent.run(
                    render_prompt(
                        template=get_prompt(),
                        input=MergeRequestInputSchema(
                            local_clone=state.local_clone,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                            merge_request_url=state.merge_request_url,
                            merge_request_title=state.merge_request_title,
                            merge_request_description=state.merge_request_description,
                            comments=state.merge_request_comments,
                            fedora_clone=state.fedora_clone,
                            build_error=state.build_error,
                        ),
                    ),
                    expected_output=MergeRequestOutputSchema,
                    **get_agent_execution_config(),
                )
                state.mr_update_result = MergeRequestOutputSchema.model_validate_json(response.last_message.text)
                if state.mr_update_result.success:
                    state.mr_update_log.append(state.mr_update_result.status)
                    # Accumulate files from this iteration
                    if state.mr_update_result.files_to_git_add:
                        state.all_files_git_to_add.update(state.mr_update_result.files_to_git_add)
                    return "run_build_agent"
                return "comment_in_mr"

            async def run_build_agent(state):
                response = await build_agent.run(
                    render_prompt(
                        template=get_build_prompt(),
                        input=BuildInputSchema(
                            srpm_path=state.mr_update_result.srpm_path,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                        ),
                    ),
                    expected_output=BuildOutputSchema,
                    **get_agent_execution_config(),
                )
                build_result = BuildOutputSchema.model_validate_json(response.last_message.text)
                if build_result.success:
                    return "stage_changes"
                if build_result.is_timeout:
                    logger.info(f"Build timed out for {state.jira_issue}, proceeding")
                    return "stage_changes"
                state.attempts_remaining -= 1
                if state.attempts_remaining <= 0:
                    state.mr_update_result.success = False
                    state.mr_update_result.error = (
                        f"Unable to successfully build the package in {max_build_attempts} attempts"
                    )
                    return "comment_in_mr"
                state.build_error = build_result.error
                return "prepare_dist_git_from_merge_request"

            async def stage_changes(state):
                # Use accumulated files from all iterations, fallback to *.spec if none specified
                files_to_git_add = list(state.all_files_git_to_add) or [f"{state.package}.spec"]

                try:
                    await tasks.stage_changes(
                        local_clone=state.local_clone,
                        files_to_commit=files_to_git_add,
                    )
                except Exception as e:
                    logger.warning(f"Error staging changes: {e}")
                    state.mr_update_result.success = False
                    state.mr_update_result.error = f"Could not stage changes: {e}"
                    return "comment_in_mr"
                return "commit_and_push"

            async def commit_and_push(state):
                try:
                    await tasks.commit_and_push(
                        local_clone=state.local_clone,
                        commit_message=(
                            f"{state.mr_update_log[-1]}\n\n"
                            f"Related: {state.jira_issue}\n\n"
                            f"This commit was created {I_AM_JOTNAR}\n\n"
                            f"Assisted-by: Jotnar\n"
                        ),
                        fork_url=state.fork_url,
                        update_branch=state.update_branch,
                        available_tools=gateway_tools,
                        commit_only=dry_run,
                    )
                except Exception as e:
                    logger.warning(f"Error committing: {e}")
                    state.mr_update_result.success = False
                    state.mr_update_result.error = f"Could not commit: {e}"
                    return "comment_in_mr"
                return Workflow.END

            async def comment_in_mr(state):
                if dry_run:
                    return Workflow.END
                if not state.mr_update_result.success:
                    await tasks.comment_in_mr(
                        merge_request_url=state.merge_request_url,
                        comment_text=f"Agent failed to update MR: {state.mr_update_result.error}",
                        available_tools=gateway_tools,
                    )
                return Workflow.END

            workflow.add_step("prepare_dist_git_from_mr", prepare_dist_git_from_mr)
            workflow.add_step("run_merge_request_agent", run_merge_request_agent)
            workflow.add_step("run_build_agent", run_build_agent)
            workflow.add_step("stage_changes", stage_changes)
            workflow.add_step("commit_and_push", commit_and_push)
            workflow.add_step("comment_in_mr", comment_in_mr)

            response = await workflow.run(State(merge_request_url=merge_request_url))
            return response.state

    if merge_request_url := os.getenv("MERGE_REQUEST_URL", None):
        logger.info("Running in direct mode with environment variables")
        state = await run_workflow(merge_request_url=merge_request_url)
        logger.info(f"Direct run completed: {state.mr_update_result.model_dump_json(indent=4)}")


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
