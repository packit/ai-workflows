import asyncio
import itertools
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

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
from pydantic import Field
from specfile import Specfile

import ymir.agents.tasks as tasks
from ymir.agents.build_agent import create_build_agent
from ymir.agents.build_agent import get_prompt as get_build_prompt
from ymir.agents.constants import I_AM_YMIR, mr_description_footer
from ymir.agents.log_agent import create_log_agent
from ymir.agents.log_agent import get_prompt as get_log_prompt
from ymir.agents.observability import setup_observability
from ymir.agents.package_update_steps import PackageUpdateState
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import (
    check_subprocess,
    format_mr_triage_details,
    get_agent_execution_config,
    get_chat_model,
    get_tool_call_checker_config,
    init_sentry,
    is_reasoning_enabled,
    mcp_tools,
    render_template,
    resolve_chat_model_override,
    run_tool,
    wrap_details,
)
from ymir.common.base_utils import fix_await, redis_client, run_task_loop
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    BackportData,
    BackportInputSchema,
    BackportOutputSchema,
    BuildInputSchema,
    BuildOutputSchema,
    ErrorData,
    LogInputSchema,
    LogOutputSchema,
    Task,
)
from ymir.common.utils import get_all_patches
from ymir.common.version_utils import is_older_zstream
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.distgit_detector import DistgitDetectorTool
from ymir.tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir.tools.unprivileged.specfile import GetPackageInfoTool
from ymir.tools.unprivileged.text import (
    CreateTool,
    InsertAfterSubstringTool,
    InsertTool,
    SearchTextTool,
    StrReplaceTool,
    ViewTool,
)
from ymir.tools.unprivileged.upstream_tools import (
    ApplyDownstreamPatchesTool,
    CherryPickCommitTool,
    CherryPickContinueTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
)
from ymir.tools.unprivileged.wicked_git import (
    BuildSrpmTool,
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
    GitPreparePackageSources,
    RunPackagePrepTool,
)

logger = logging.getLogger(__file__)
redis_logger = logging.getLogger("agent.redis")


async def get_instructions(fix_version: str | None = None) -> str:
    if fix_version and await is_older_zstream(fix_version):
        return render_template("backport/instructions_zstream.j2")
    return render_template("backport/instructions.j2")


def get_prompt() -> str:
    return "backport/prompt.j2"


async def get_fix_build_error_prompt(fix_version: str | None = None) -> str:
    return "backport/prompt_fix_build_error.j2"


async def create_backport_agent(
    mcp_tools: list[Tool],
    local_tool_options: dict[str, Any],
    include_build_tools: bool = False,
    fix_version: str | None = None,
) -> ReasoningAgent:
    """
    Create a backport agent.

    Args:
        mcp_tools: List of MCP gateway tools
        local_tool_options: Options for local tools
        include_build_tools: If True, include build_package and download_artifacts tools
                           for iterative build testing during error fixing
        fix_version: Fix version string for z-stream instruction selection
    """
    base_tools = [
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
        DistgitDetectorTool(options=local_tool_options),
        # Upstream cherry-pick workflow tools
        GetPackageInfoTool(options=local_tool_options),
        ExtractUpstreamRepositoryTool(options=local_tool_options),
        CloneUpstreamRepositoryTool(options=local_tool_options),
        FindBaseCommitTool(options=local_tool_options),
        ApplyDownstreamPatchesTool(options=local_tool_options),
        CherryPickCommitTool(options=local_tool_options),
        CherryPickContinueTool(options=local_tool_options),
        RunPackagePrepTool(options=local_tool_options),
        BuildSrpmTool(options=local_tool_options),
    ]

    base_tools.extend([t for t in mcp_tools if t.name == "get_maintainer_rules"])

    # Add clone_repository from MCP gateway (needed for dist-git workflow with auth)
    if fix_version and await is_older_zstream(fix_version):
        base_tools.extend([t for t in mcp_tools if t.name == "clone_repository"])

    # Add build tools if requested (for iterative build error fixing)
    if include_build_tools:
        base_tools.extend(
            [
                t
                for t in mcp_tools
                if t.name in ["build_package", "download_artifacts", "extract_log_snippets"]
            ]
        )

    return ReasoningAgent(
        name="BackportAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=base_tools,
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions=await get_instructions(fix_version),
    )


def _move_build_logs(source_dir: Path, target_dir: Path) -> None:
    """Move build log files from source_dir into target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for log_file in itertools.chain(
        source_dir.glob("*.log"),
        source_dir.glob("*.log.gz"),
    ):
        log_file.rename(target_dir / log_file.name)


def _update_fix_attempts_log(log_dir: Path, attempt_num: int, build_error: str) -> None:
    """Create or append to fix-attempts.md with the current build error."""
    attempts_log = log_dir / "fix-attempts.md"
    if not attempts_log.exists():
        attempts_log.write_text(
            f"# Fix Attempts Log\n\n"
            f"## Initial build failure\n\n```\n{build_error}\n```\n\n"
            f"## Attempt {attempt_num}\n\n"
            f"**Build error to fix:**\n```\n{build_error}\n```\n\n"
        )
    else:
        with attempts_log.open("a") as f:
            f.write(f"\n## Attempt {attempt_num}\n\n**Build error to fix:**\n```\n{build_error}\n```\n\n")


def _extract_commit_hash(url: str) -> str | None:
    """Extract a commit hash from a dist-git commit URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    match = re.search(r"(?:commit(?:s)?|c)/([a-f0-9]{7,40})", parsed.path)
    if match:
        return match.group(1)
    query_match = re.search(r"(?:id|h)=([a-f0-9]{7,40})", parsed.query or "")
    if query_match:
        return query_match.group(1)
    return None


async def extract_source_changelog(
    local_clone: Path, upstream_patches: list[str], package: str
) -> str | None:
    """Extract changelog messages from source dist-git commits.

    Iterates all upstream patch URLs, extracts the newest changelog entry
    from each commit's spec file, and combines the lines (deduplicating
    across commits). The content is passed through as-is; the LogAgent
    handles replacing Jira references.
    """
    upstream_clone = Path(f"{local_clone}-upstream")
    if not upstream_clone.exists():
        return None

    collected_lines: list[str] = []
    seen: set[str] = set()

    for url in upstream_patches:
        commit_hash = _extract_commit_hash(url)
        if not commit_hash:
            continue

        try:
            stdout, _ = await check_subprocess(
                ["git", "-C", str(upstream_clone), "show", f"{commit_hash}:{package}.spec"],
            )
        except Exception:
            logger.debug(f"Could not read spec from {commit_hash} in {upstream_clone}")
            continue

        try:
            spec = Specfile(content=stdout, sourcedir=upstream_clone)
            with spec.changelog() as changelog:
                if not changelog:
                    continue
                entry = changelog[-1]
        except Exception:
            logger.debug(f"Could not parse spec from {commit_hash}")
            continue

        for line in entry.content:
            if line not in seen:
                seen.add(line)
                collected_lines.append(line)

    if not collected_lines:
        return None

    return "\n".join(collected_lines)


class BackportState(PackageUpdateState):
    upstream_patches: list[str]
    cve_id: str | None
    justification: str | None = Field(default=None)
    triage_summary: str | None = Field(default=None)
    unpacked_sources: Path | None = Field(default=None)
    backport_log: list[str] = Field(default_factory=list)
    backport_result: BackportOutputSchema | None = Field(default=None)
    attempts_remaining: int = Field(default=10)
    used_cherry_pick_workflow: bool = Field(default=False)
    incremental_fix_attempts: int = Field(default=0)
    fix_version: str | None = Field(default=None)
    abandon_autorelease: bool = Field(default=False)


async def run_workflow(
    package,
    dist_git_branch,
    upstream_patches,
    jira_issue,
    cve_id,
    justification=None,
    triage_summary=None,
    fix_version=None,
    redis_conn=None,
    dry_run=False,
    backport_agent_factory=None,
    max_build_attempts=10,
    max_incremental_fix_attempts=None,
    user_triggered=False,
):
    if max_incremental_fix_attempts is None:
        max_incremental_fix_attempts = max_build_attempts

    local_tool_options: dict[str, Any] = {"working_directory": None}
    if mock_env := get_mock_local_tool_env(jira_issue):
        local_tool_options["env"] = mock_env

    async with mcp_tools(
        os.environ["MCP_GATEWAY_URL"], call_meta={"jira_issue": jira_issue}
    ) as gateway_tools:
        if backport_agent_factory:
            result = backport_agent_factory(gateway_tools, local_tool_options)
            backport_agent = await result if asyncio.iscoroutine(result) else result
        else:
            backport_agent = await create_backport_agent(
                gateway_tools, local_tool_options, fix_version=fix_version
            )
        log_agent = create_log_agent(gateway_tools, local_tool_options)

        workflow = Workflow(BackportState, name="BackportWorkflow")

        async def change_jira_status(state):
            if dry_run:
                logger.info(f"Dry run: skipping Jira status change of {state.jira_issue} to In Progress")
                return "fork_and_prepare_dist_git"
            # tasks.change_jira_status further gates the write on
            # JIRA_ALLOW_STATUS_CHANGES; nothing else to check here.
            try:
                await tasks.change_jira_status(
                    jira_issue=state.jira_issue,
                    status="In Progress",
                    available_tools=gateway_tools,
                )
            except Exception as status_error:
                logger.warning(f"Failed to change status for {state.jira_issue}: {status_error}")
            return "fork_and_prepare_dist_git"

        async def fork_and_prepare_dist_git(state):
            state.used_cherry_pick_workflow = False
            state.incremental_fix_attempts = 0

            (
                state.local_clone,
                state.update_branch,
                state.fork_url,
                _,
            ) = await tasks.fork_and_prepare_dist_git(
                jira_issue=state.jira_issue,
                package=state.package,
                dist_git_branch=state.dist_git_branch,
                available_tools=gateway_tools,
            )
            local_tool_options["working_directory"] = state.local_clone
            await run_tool(
                "download_sources",
                dist_git_path=str(state.local_clone),
                package=state.package,
                dist_git_branch=state.dist_git_branch,
                available_tools=gateway_tools,
            )
            await run_tool(
                RunPackagePrepTool(options=local_tool_options),
                dist_git_path=str(state.local_clone),
                package=state.package,
                dist_git_branch=state.dist_git_branch,
            )
            state.unpacked_sources = tasks.get_unpacked_sources(state.local_clone, state.package)
            for idx, upstream_patch in enumerate(state.upstream_patches):
                patch_name = f"{state.jira_issue}-{idx}.patch"
                content = await run_tool(
                    "get_patch_from_url",
                    available_tools=gateway_tools,
                    patch_url=upstream_patch,
                )
                (state.local_clone / patch_name).write_text(content)
            return "run_backport_agent"

        async def run_backport_agent(state):
            response = await backport_agent.run(
                render_template(
                    get_prompt(),
                    BackportInputSchema(
                        local_clone=state.local_clone,
                        unpacked_sources=state.unpacked_sources,
                        package=state.package,
                        dist_git_branch=state.dist_git_branch,
                        jira_issue=state.jira_issue,
                        cve_id=state.cve_id,
                        upstream_patches=state.upstream_patches,
                        build_error=state.build_error,
                        triage_summary=state.triage_summary,
                    ),
                ),
                expected_output=BackportOutputSchema,
                **get_agent_execution_config(),
            )
            state.backport_result = BackportOutputSchema.model_validate_json(response.last_message.text)
            if state.backport_result.abandon_autorelease:
                state.abandon_autorelease = True
            if state.backport_result.success:
                state.backport_log.append(state.backport_result.status)

                upstream_repo = Path(f"{state.local_clone}-upstream")
                if upstream_repo.exists():
                    try:
                        stdout, _ = await check_subprocess(
                            [
                                "git",
                                "-C",
                                str(upstream_repo),
                                "rev-list",
                                "--count",
                                "HEAD",
                            ]
                        )
                        commit_count = int(stdout.strip())
                        if commit_count > 1:
                            state.used_cherry_pick_workflow = True
                            logger.info(
                                f"Cherry-pick workflow detected: {commit_count} commits in upstream repo"
                            )
                        else:
                            state.used_cherry_pick_workflow = False
                            logger.info("Git am workflow detected: no commits in upstream repo")
                    except Exception as e:
                        logger.warning(f"Could not determine workflow type: {e}")
                        state.used_cherry_pick_workflow = False
                else:
                    state.used_cherry_pick_workflow = False
                    logger.info("Git am workflow detected: no upstream repo exists")

                return "run_build_agent"
            return "comment_in_jira"

        async def fix_build_error(state):
            """Try to fix build errors by finding and cherry-picking prerequisite commits."""
            logger.info(
                f"Attempting incremental fix for cherry-pick workflow "
                f"(attempt {state.incremental_fix_attempts}/{max_incremental_fix_attempts})"
            )

            try:
                upstream_repo = Path(f"{state.local_clone}-upstream")
                if not upstream_repo.exists():
                    logger.error(
                        f"Upstream repo {upstream_repo} missing, cannot do incremental fix — "
                        "falling back to full reset"
                    )
                    return "fork_and_prepare_dist_git"

                log_dir = upstream_repo / "build-logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                attempt_num = state.incremental_fix_attempts + 1

                if state.incremental_fix_attempts > 0:
                    _move_build_logs(
                        state.local_clone,
                        log_dir / f"attempt-{state.incremental_fix_attempts}",
                    )
                _update_fix_attempts_log(log_dir, attempt_num, state.build_error)

                fix_agent = await create_backport_agent(
                    gateway_tools,
                    local_tool_options,
                    include_build_tools=True,
                    fix_version=state.fix_version,
                )

                response = await fix_agent.run(
                    render_template(
                        await get_fix_build_error_prompt(fix_version=state.fix_version),
                        BackportInputSchema(
                            local_clone=state.local_clone,
                            unpacked_sources=state.unpacked_sources,
                            package=state.package,
                            dist_git_branch=state.dist_git_branch,
                            jira_issue=state.jira_issue,
                            cve_id=state.cve_id,
                            upstream_patches=state.upstream_patches,
                            build_error=state.build_error,
                            triage_summary=state.triage_summary,
                        ),
                    ),
                    expected_output=BackportOutputSchema,
                    **get_agent_execution_config(),
                )

                fix_result = BackportOutputSchema.model_validate_json(response.last_message.text)

                if fix_result.success:
                    state.backport_result = fix_result
                    state.backport_log.append(fix_result.status)
                    logger.info("Incremental fix succeeded with passing build")
                    state.incremental_fix_attempts = 0
                    return "update_release"

                logger.info(f"Build still failing after fix attempt: {fix_result.error}")
                state.build_error = fix_result.error
                state.backport_result = fix_result

                state.incremental_fix_attempts += 1
                if state.incremental_fix_attempts < max_incremental_fix_attempts:
                    logger.info(
                        f"Will retry incremental fix "
                        f"(attempt {state.incremental_fix_attempts + 1}/{max_incremental_fix_attempts})"
                    )
                    return "fix_build_error"
                logger.error(
                    f"Exhausted all {max_incremental_fix_attempts} incremental fix attempts, giving up"
                )
                state.backport_result.success = False
                state.backport_result.error = (
                    f"Unable to fix build errors after "
                    f"{max_incremental_fix_attempts} incremental fix attempts. "
                    f"Last error: {fix_result.error}"
                )
                return "comment_in_jira"

            except Exception as e:
                logger.error(f"Exception during incremental fix: {e}", exc_info=True)
                state.backport_result.success = False
                state.backport_result.error = f"Exception during incremental fix: {e!s}"
                return "comment_in_jira"

        async def run_build_agent(state):
            if not state.backport_result or not state.backport_result.srpm_path:
                logger.error("Cannot run build agent: no valid backport result or SRPM path")
                state.backport_result = state.backport_result or BackportOutputSchema(
                    success=False,
                    srpm_path=None,
                    status="",
                    error="No SRPM generated by backport agent",
                )
                return "comment_in_jira"

            fresh_build_agent = create_build_agent(gateway_tools, local_tool_options)
            response = await fresh_build_agent.run(
                render_template(
                    get_build_prompt(),
                    BuildInputSchema(
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
                state.incremental_fix_attempts = 0
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
            if state.used_cherry_pick_workflow:
                upstream_repo = Path(f"{state.local_clone}-upstream")
                if upstream_repo.exists():
                    _move_build_logs(
                        state.local_clone,
                        upstream_repo / "build-logs" / "attempt-0",
                    )
                logger.info("Cherry-pick workflow was used - starting incremental fix")
                return "fix_build_error"
            logger.info("Git am workflow was used - resetting for retry")
            return "fork_and_prepare_dist_git"

        async def update_release(state):
            try:
                await tasks.update_release(
                    local_clone=state.local_clone,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    rebase=False,
                    abandon_autorelease=state.abandon_autorelease,
                )
            except Exception as e:
                logger.warning(f"Error updating release: {e}")
                state.backport_result.success = False
                state.backport_result.error = f"Could not update release: {e}"
                return "comment_in_jira"
            return "stage_changes"

        async def stage_changes(state):
            try:
                spec_path = state.local_clone / f"{state.package}.spec"
                with Specfile(spec_path) as spec:
                    patch_files = [p.expanded_location for p in get_all_patches(spec) if p.expanded_location]

                if not patch_files:
                    raise RuntimeError(f"Backport completed but no Patch tags found in {spec_path}")

                files_to_git_add = [f"{state.package}.spec", *patch_files]
                logger.info(f"Staging files: {files_to_git_add}")

                await tasks.stage_changes(
                    local_clone=state.local_clone,
                    files_to_commit=files_to_git_add,
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
            source_changelog = await extract_source_changelog(
                state.local_clone, state.upstream_patches, state.package
            )
            if source_changelog:
                logger.info(f"Extracted source changelog for reuse: {source_changelog}")

            response = await log_agent.run(
                render_template(
                    get_log_prompt(),
                    LogInputSchema(
                        jira_issue=state.jira_issue,
                        changes_summary=state.backport_log[-1],
                        source_changelog=source_changelog,
                    ),
                ),
                expected_output=LogOutputSchema,
                **get_agent_execution_config(),
            )
            log_output = LogOutputSchema.model_validate_json(response.last_message.text)

            if redis_conn and not dry_run:
                log_output = await tasks.cache_mr_metadata(
                    redis_conn,
                    log_output=log_output,
                    operation_type="backport",
                    package=state.package,
                    details=str(state.upstream_patches),
                )
            state.log_result = log_output

            return "stage_changes"

        async def commit_push_and_open_mr(state):
            try:
                formatted_patches = "\n".join(f" - {p}" for p in state.upstream_patches)
                triage_details_text = format_mr_triage_details(state.justification, state.triage_summary)
                (
                    state.merge_request_url,
                    state.merge_request_newly_created,
                ) = await tasks.commit_push_and_open_mr(
                    local_clone=state.local_clone,
                    commit_message=(
                        f"{state.log_result.title}\n\n"
                        f"{state.log_result.description}\n\n"
                        + (f"CVE: {state.cve_id}\n" if state.cve_id else "")
                        + "Upstream patches:\n"
                        + formatted_patches
                        + "\n"
                        + f"Resolves: {state.jira_issue}\n\n"
                        f"This commit was backported {I_AM_YMIR}\n\n"
                        "Assisted-by: Ymir\n"
                    ),
                    fork_url=state.fork_url,
                    dist_git_branch=state.dist_git_branch,
                    update_branch=state.update_branch,
                    mr_title=state.log_result.title,
                    mr_description=(
                        f"{state.log_result.description}\n\n"
                        f"Upstream patches:\n{formatted_patches}\n\n"
                        f"{triage_details_text}"
                        f"Resolves: {state.jira_issue}\n\n"
                        f"{wrap_details('Backporting steps', state.backport_log[-1])}"
                        f"\n\n{mr_description_footer(state.package)}"
                    ),
                    available_tools=gateway_tools,
                    commit_only=dry_run,
                    labels=["ymir_backport"],
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
            if state.backport_result.success:
                comment_text = (
                    state.merge_request_url if state.merge_request_url else state.backport_result.status
                )
                is_error = False
            else:
                comment_text = f"Agent failed to perform a backport: {state.backport_result.error}"
                is_error = True
            logger.info(f"Result to be put in Jira comment: {comment_text}")
            await tasks.comment_in_jira(
                jira_issue=state.jira_issue,
                agent_type="Backport",
                comment_text=comment_text,
                is_error=is_error,
                available_tools=gateway_tools,
                user_triggered=user_triggered,
            )
            return Workflow.END

        workflow.add_step("change_jira_status", change_jira_status)
        workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
        workflow.add_step("run_backport_agent", run_backport_agent)
        workflow.add_step("fix_build_error", fix_build_error)
        workflow.add_step("run_build_agent", run_build_agent)
        workflow.add_step("update_release", update_release)
        workflow.add_step("stage_changes", stage_changes)
        workflow.add_step("run_log_agent", run_log_agent)
        workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
        workflow.add_step("comment_in_jira", comment_in_jira)

        response = await workflow.run(
            BackportState(
                package=package,
                dist_git_branch=dist_git_branch,
                upstream_patches=upstream_patches,
                jira_issue=jira_issue,
                cve_id=cve_id,
                justification=justification,
                triage_summary=triage_summary,
                fix_version=fix_version,
                attempts_remaining=max_build_attempts,
            ),
        )
        return response.state


async def main() -> None:
    init_sentry()

    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("backport")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "10"))
    max_incremental_fix_attempts = int(os.getenv("MAX_INCREMENTAL_FIX_ATTEMPTS", str(max_build_attempts)))

    if (
        (package := os.getenv("PACKAGE", None))
        and (branch := os.getenv("BRANCH", None))
        and (upstream_patches_raw := os.getenv("UPSTREAM_PATCHES", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
    ):
        upstream_patches = upstream_patches_raw.split(",")
        logger.info("Running in direct mode with environment variables")
        with span_processor.start_transaction(jira_issue, workflow="backport"):
            state = await run_workflow(
                package=package,
                dist_git_branch=branch,
                upstream_patches=upstream_patches,
                jira_issue=jira_issue,
                cve_id=os.getenv("CVE_ID", None),
                justification=os.getenv("JUSTIFICATION", None),
                triage_summary=os.getenv("TRIAGE_SUMMARY", None),
                fix_version=branch,
                redis_conn=None,
                dry_run=dry_run,
                max_build_attempts=max_build_attempts,
                max_incremental_fix_attempts=max_incremental_fix_attempts,
            )
            logger.info(f"Direct run completed: {state.backport_result.model_dump_json(indent=4)}")
            return

    logger.info("Starting backport agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        # Determine which backport queue to listen to based on container version
        container_version = os.getenv("CONTAINER_VERSION", "c10s")
        backport_queue = (
            RedisQueues.BACKPORT_QUEUE_C9S.value
            if container_version == "c9s"
            else RedisQueues.BACKPORT_QUEUE_C10S.value
        )
        # Priority twin: ymir_todo-triggered tasks are served before normal ones.
        backport_queue_todo = RedisQueues.priority_twin(backport_queue)
        redis_logger.info(
            f"Connected to Redis, max retries set to {max_retries}, "
            f"listening to queues: [{backport_queue_todo}, {backport_queue}]"
        )

        async def process_task(payload):
            task = Task.model_validate_json(payload)
            triage_state = task.metadata
            backport_data = BackportData.model_validate(triage_state["triage_result"]["data"])
            current_jira_issue.set(backport_data.jira_issue)
            dist_git_branch = triage_state["target_branch"]
            user_triggered = task.user_triggered
            logger.info(
                f"Processing backport for package: {backport_data.package}, "
                f"JIRA: {backport_data.jira_issue}, branch: {dist_git_branch}, "
                f"attempt: {task.attempts + 1}"
                + (" (user-triggered via ymir_todo)" if user_triggered else "")
            )

            async def retry(
                task, error, comment_text=None, backport_data=backport_data, user_triggered=user_triggered
            ):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {backport_data.jira_issue}"
                    )
                    retry_queue = backport_queue_todo if task.user_triggered else backport_queue
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
                else:
                    # Final attempt exhausted — mark errored and stop retrying.
                    logger.error(
                        f"Task failed after {max_retries} attempts, "
                        f"moving to error list: {backport_data.jira_issue}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_ERRORED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_BACKPORT.value],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                    )
                    # Post failure feedback to Jira once, here on the final attempt
                    # only — never for intermediate retries. Restricted to
                    # user-triggered (ymir_todo) runs: a maintainer who didn't ask
                    # for processing shouldn't be notified, so skip the gateway
                    # connection entirely otherwise.
                    if user_triggered and comment_text and not dry_run:
                        try:
                            async with mcp_tools(
                                os.environ["MCP_GATEWAY_URL"],
                                call_meta={"jira_issue": backport_data.jira_issue},
                            ) as gateway_tools:
                                await tasks.comment_in_jira(
                                    jira_issue=backport_data.jira_issue,
                                    agent_type="Backport",
                                    comment_text=comment_text,
                                    available_tools=gateway_tools,
                                    is_error=True,
                                    user_triggered=user_triggered,
                                )
                        except Exception as comment_error:
                            logger.warning(
                                f"Failed to post final backport failure comment for "
                                f"{backport_data.jira_issue}: {comment_error}"
                            )
                    await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, error))

            try:
                logger.info(f"Starting backport processing for {backport_data.jira_issue}")
                with span_processor.start_transaction(backport_data.jira_issue, workflow="backport"):
                    state = await run_workflow(
                        package=backport_data.package,
                        dist_git_branch=dist_git_branch,
                        upstream_patches=backport_data.patch_urls,
                        jira_issue=backport_data.jira_issue,
                        cve_id=backport_data.cve_id,
                        justification=backport_data.justification,
                        triage_summary=backport_data.triage_summary,
                        fix_version=backport_data.fix_version,
                        redis_conn=redis,
                        dry_run=dry_run,
                        max_build_attempts=max_build_attempts,
                        max_incremental_fix_attempts=max_incremental_fix_attempts,
                        user_triggered=user_triggered,
                    )
                    logger.info(
                        f"Backport processing completed for {backport_data.jira_issue}, "
                        f"success: {state.backport_result.success}"
                    )

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                reason = e.explain() if isinstance(e, FrameworkError) else e
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=backport_data.jira_issue).model_dump_json(),
                    comment_text=f"Agent failed to perform a backport: {reason}",
                )
            else:
                if state.backport_result.success:
                    logger.info(
                        f"Backport successful for {backport_data.jira_issue}, adding to completed list"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORTED.value],
                        labels_to_remove=[
                            JiraLabels.TRIAGED_BACKPORT.value,
                            JiraLabels.BACKPORT_ERRORED.value,
                            JiraLabels.BACKPORT_FAILED.value,
                        ],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                    )
                    await fix_await(
                        redis.lpush(
                            RedisQueues.COMPLETED_BACKPORT_LIST.value,
                            state.backport_result.model_dump_json(),
                        )
                    )
                else:
                    logger.warning(
                        f"Backport failed for {backport_data.jira_issue}: {state.backport_result.error}"
                    )
                    await tasks.set_jira_labels(
                        jira_issue=backport_data.jira_issue,
                        labels_to_add=[JiraLabels.BACKPORT_FAILED.value],
                        labels_to_remove=[JiraLabels.TRIAGED_BACKPORT.value],
                        dry_run=dry_run,
                        user_triggered=user_triggered,
                    )
                    # No comment_text here: the in-workflow comment_in_jira step has
                    # already posted the failure feedback for this graceful path.
                    # Only the crash path (which never reaches that step) passes
                    # comment_text, so we never double-comment.
                    await retry(
                        task,
                        ErrorData(
                            details=getattr(state.backport_result, "error", None) or "Unknown backport error",
                            jira_issue=backport_data.jira_issue,
                        ).model_dump_json(),
                    )

        await run_task_loop(
            redis,
            [backport_queue_todo, backport_queue],
            process_task,
            max_concurrent=max_concurrent_tasks,
        )


if __name__ == "__main__":
    try:
        # uncomment for debugging
        # from utils import set_litellm_debug
        # set_litellm_debug()
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
