import asyncio
import itertools
import logging
import os
import re
import sys
import traceback
from enum import StrEnum
from pathlib import Path
from typing import Any

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field
from specfile import Specfile

import ymir.agents.tasks as tasks
from ymir.agents.build_agent import create_build_agent
from ymir.agents.build_agent import get_prompt as get_build_prompt
from ymir.agents.constants import (
    I_AM_YMIR,
    ZSTREAM_TARGET_LABEL,
    format_jira_links_for_mr,
    mr_description_footer,
)
from ymir.agents.log_agent import create_log_agent
from ymir.agents.log_agent import get_prompt as get_log_prompt
from ymir.agents.observability import setup_observability
from ymir.agents.package_update_steps import PackageUpdateState
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.tasks import InvalidConsolidationConfigError
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
    run_subprocess,
    run_tool,
    wrap_details,
)
from ymir.agents.ystream_inherit import (
    AlreadyInheritedError,
    BrewSource,
    ImmutablePatchError,
    InheritCandidateError,
    InheritedPatchApplyError,
    IntegratedChange,
    apply_zstream_change,
    ensure_single_ymir_attribution,
    find_zstream_fix_commit,
    reset_inherit_attempt,
    resolve_brew_source,
    rewrite_commit_message,
    same_major_candidate,
    spec_matches_brew_version,
    validate_inherited_adaptation,
    verify_inherited_patches,
)
from ymir.common.base_utils import (
    fix_await,
    install_shutdown_handler,
    is_cs_branch,
    redis_client,
    run_task_loop,
)
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.issue_lock import issue_lock
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    BackportData,
    BackportInputSchema,
    BackportOutputSchema,
    BuildInputSchema,
    BuildOutputSchema,
    ErrorData,
    ErrorListEntry,
    InheritAdaptationInputSchema,
    InheritAdaptationOutputSchema,
    LogInputSchema,
    LogOutputSchema,
    ShippedZStreamCandidate,
    Task,
)
from ymir.common.utils import get_all_patches
from ymir.common.version_utils import is_older_zstream, parse_rhel_version
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.distgit_detector import DistgitDetectorTool
from ymir.tools.unprivileged.filesystem import GetCWDTool, RemoveTool
from ymir.tools.unprivileged.specfile import AddChangelogEntryTool, GetPackageInfoTool
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

_INHERITED_PUBLICATION_CHECKPOINT = "inherited_publication_checkpoint"
_YSTREAM_INHERITANCE_DISABLED = "ystream_inheritance_disabled"


class BackportRetryMode(StrEnum):
    FULL = "full"
    RESUME_INHERITED_MR = "resume_inherited_mr"
    NONE = "none"


class InheritedPublicationCheckpoint(BaseModel):
    fork_url: str
    update_branch: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    local_commit: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    mr_title: str
    mr_description: str
    result_status: str
    source_issue_key: str
    source_nvr: str
    source_commit: str = Field(pattern=r"^[0-9a-fA-F]{40}$")


def get_inherit_adaptation_instructions() -> str:
    return render_template("backport/instructions_inherit.j2")


def get_inherit_adaptation_prompt() -> str:
    return "backport/prompt_inherit.j2"


def create_inherit_adaptation_agent(local_tool_options: dict[str, Any]) -> ReasoningAgent:
    """Create the spec-only editor used after deterministic source validation."""
    return ReasoningAgent(
        name="YStreamInheritAdaptationAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            ViewTool(options=local_tool_options),
            InsertTool(options=local_tool_options),
            InsertAfterSubstringTool(options=local_tool_options),
            StrReplaceTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            GetCWDTool(options=local_tool_options),
        ],
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
        instructions=get_inherit_adaptation_instructions(),
    )


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

    base_tools.extend([t for t in mcp_tools if t.name in ["get_maintainer_rules", "get_shared_rules"]])

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
    shipped_zstream_candidates: list[ShippedZStreamCandidate] = Field(default_factory=list)
    inherit_cleanup_retried: bool = Field(default=False)
    inherit_candidate: ShippedZStreamCandidate | None = Field(default=None)
    inherit_source: BrewSource | None = Field(default=None)
    inherit_change: IntegratedChange | None = Field(default=None)
    inherit_saved_head: str | None = Field(default=None)
    inherit_introduced_files: list[str] = Field(default_factory=list)
    inherit_build_attempts: int = Field(default=0)
    inherit_commit_message: str | None = Field(default=None)
    inherit_mr_description: str | None = Field(default=None)
    inherit_local_commit: str | None = Field(default=None)
    inherit_pushed: bool = Field(default=False)
    inherited_publication_checkpoint: InheritedPublicationCheckpoint | None = Field(default=None)
    retry_mode: BackportRetryMode = Field(default=BackportRetryMode.FULL)
    inheritance_disabled: bool = Field(default=False)


def _schedule_inherit_cleanup_retry(state: BackportState) -> bool:
    """Allow one fresh-clone retry when inheritance cleanup cannot be proven."""
    if state.inherit_cleanup_retried:
        return False
    state.inherit_cleanup_retried = True
    return True


def _remote_branch_matches_commit(remote_head: Any, local_commit: str | None) -> bool:
    return bool(isinstance(remote_head, str) and local_commit and remote_head.lower() == local_commit.lower())


def _disable_ystream_inheritance(
    state: BackportState,
    task_metadata: dict[str, Any] | None,
) -> None:
    """Make normal backport fallback durable across clone and queue retries."""
    state.inheritance_disabled = True
    if task_metadata is not None:
        task_metadata[_YSTREAM_INHERITANCE_DISABLED] = True


def _inherit_prep_error(
    prep_result: str,
    change: IntegratedChange,
) -> InheritCandidateError | None:
    if "prep failed" not in prep_result.lower() and not re.search(
        r"\bfuzz(?:y|ing)?\b",
        prep_result,
        re.IGNORECASE,
    ):
        return None
    error = f"Inherited package prep was not clean: {prep_result}"
    if change.patch_files:
        return InheritedPatchApplyError(error)
    return InheritCandidateError(error)


def _validate_inherited_staged_files(staged: str, expected_files: list[str]) -> None:
    """Require the index to contain exactly the validated inheritance files."""
    staged_files = {path for path in staged.splitlines() if path}
    expected = set(expected_files)
    if staged_files != expected:
        raise InheritCandidateError(
            f"Inherited staging contains unexpected files: {sorted(staged_files ^ expected)}"
        )


def _build_inherited_publication_checkpoint(state: BackportState) -> InheritedPublicationCheckpoint:
    required = {
        "fork URL": state.fork_url,
        "update branch": state.update_branch,
        "local commit": state.inherit_local_commit,
        "log result": state.log_result,
        "MR description": state.inherit_mr_description,
        "backport result": state.backport_result,
        "candidate": state.inherit_candidate,
        "source": state.inherit_source,
        "change": state.inherit_change,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise RuntimeError(f"Cannot checkpoint inherited publication without {', '.join(missing)}")

    return InheritedPublicationCheckpoint(
        fork_url=state.fork_url,
        update_branch=state.update_branch,
        local_commit=state.inherit_local_commit,
        mr_title=state.log_result.title,
        mr_description=state.inherit_mr_description,
        result_status=state.backport_result.status,
        source_issue_key=state.inherit_candidate.issue_key,
        source_nvr=state.inherit_source.nvr,
        source_commit=state.inherit_change.commit_sha,
    )


def _configure_task_retry(task: Task, state: BackportState) -> bool:
    """Persist retry state and return whether queue-level retry is allowed."""
    if state.retry_mode == BackportRetryMode.NONE:
        return False
    if state.retry_mode == BackportRetryMode.RESUME_INHERITED_MR:
        checkpoint = state.inherited_publication_checkpoint
        if checkpoint is None:
            return False
        _persist_inherited_publication_checkpoint(task.metadata, checkpoint)
    return True


def _persist_inherited_publication_checkpoint(
    metadata: dict[str, Any],
    checkpoint: InheritedPublicationCheckpoint,
) -> None:
    metadata[_INHERITED_PUBLICATION_CHECKPOINT] = checkpoint.model_dump(mode="json")


def _restore_inherited_publication(state: BackportState) -> InheritedPublicationCheckpoint:
    checkpoint = state.inherited_publication_checkpoint
    if checkpoint is None:
        raise RuntimeError("No inherited publication checkpoint to resume")
    state.retry_mode = BackportRetryMode.RESUME_INHERITED_MR
    state.fork_url = checkpoint.fork_url
    state.update_branch = checkpoint.update_branch
    state.inherit_local_commit = checkpoint.local_commit
    state.inherit_mr_description = checkpoint.mr_description
    state.log_result = LogOutputSchema(
        title=checkpoint.mr_title,
        description=checkpoint.result_status,
    )
    state.backport_result = BackportOutputSchema(
        success=True,
        status=checkpoint.result_status,
        srpm_path=None,
        error=None,
    )
    return checkpoint


def _get_shipped_zstream_candidates(
    triage_state: dict[str, Any],
) -> list[ShippedZStreamCandidate]:
    eligibility = triage_state.get("cve_eligibility_result")
    if not isinstance(eligibility, dict):
        return []
    return [
        ShippedZStreamCandidate.model_validate(candidate)
        for candidate in eligibility.get("shipped_zstream_candidates") or []
    ]


def _can_attempt_ystream_inheritance(state: BackportState) -> bool:
    parsed_fix_version = parse_rhel_version(state.fix_version or "")
    return bool(
        not state.inheritance_disabled
        and state.shipped_zstream_candidates
        and is_cs_branch(state.dist_git_branch)
        and parsed_fix_version
        and not parsed_fix_version[2]
    )


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
    dist_git_namespace=None,
    shipped_zstream_candidates=None,
    inherited_publication_checkpoint=None,
    inheritance_disabled=False,
    task_metadata=None,
    inherit_agent_factory=None,
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
        inherit_agent = None

        async def get_inherit_agent():
            nonlocal inherit_agent
            if inherit_agent is None:
                if inherit_agent_factory:
                    result = inherit_agent_factory(local_tool_options)
                    inherit_agent = await result if asyncio.iscoroutine(result) else result
                else:
                    inherit_agent = create_inherit_adaptation_agent(local_tool_options)
            return inherit_agent

        log_agent = create_log_agent(gateway_tools, local_tool_options)

        workflow = Workflow(BackportState, name="BackportWorkflow")

        async def change_jira_status(state):
            if state.inherited_publication_checkpoint:
                return "resume_inherited_publication"
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

        async def resume_inherited_publication(state):
            checkpoint = _restore_inherited_publication(state)

            try:
                remote_head = await run_tool(
                    "get_remote_branch_head",
                    repository=checkpoint.fork_url,
                    branch=checkpoint.update_branch,
                    available_tools=gateway_tools,
                )
                if not _remote_branch_matches_commit(remote_head, checkpoint.local_commit):
                    raise RuntimeError(
                        f"source branch points at {remote_head}, expected {checkpoint.local_commit}"
                    )
            except Exception as error:
                state.backport_result.success = False
                state.backport_result.error = (
                    "Could not confirm the checkpointed inherited branch before resuming MR creation: "
                    f"{error}"
                )
                return "comment_in_jira"

            state.inherit_pushed = True
            return "open_inherited_mr"

        async def fork_and_prepare_dist_git(state):
            state.used_cherry_pick_workflow = False
            state.incremental_fix_attempts = 0
            state.inherit_candidate = None
            state.inherit_source = None
            state.inherit_change = None
            state.inherit_introduced_files = []
            state.inherit_build_attempts = 0
            state.inherit_commit_message = None
            state.inherit_mr_description = None
            state.inherit_local_commit = None
            state.inherit_pushed = False
            state.backport_result = None
            state.log_result = None

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
                agent_type="Backport",
                dist_git_namespace=state.dist_git_namespace,
            )
            local_tool_options["working_directory"] = state.local_clone
            state.inherit_saved_head, _ = await check_subprocess(
                ["git", "rev-parse", "HEAD"],
                cwd=state.local_clone,
            )
            state.inherit_saved_head = state.inherit_saved_head.strip()
            if not state.inheritance_disabled and _can_attempt_ystream_inheritance(state):
                state.inherit_candidate = same_major_candidate(
                    state.shipped_zstream_candidates,
                    state.fix_version,
                )
                if state.inherit_candidate:
                    return "evaluate_inherit_source"
            return "prepare_normal_backport"

        async def prepare_normal_backport(state):
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

        async def get_untracked_files(state) -> list[str]:
            paths: set[str] = set()
            for extra_arguments in ([], ["--ignored"]):
                output, _ = await check_subprocess(
                    [
                        "git",
                        "ls-files",
                        "--others",
                        *extra_arguments,
                        "--exclude-standard",
                        "-z",
                    ],
                    cwd=state.local_clone,
                )
                paths.update(path for path in (output or "").split("\0") if path)
            return sorted(paths)

        async def cleanup_inherit_attempt(state) -> bool:
            if not state.inherit_saved_head:
                return False
            introduced = set(state.inherit_introduced_files)
            introduced.update(await get_untracked_files(state))
            try:
                await reset_inherit_attempt(
                    state.local_clone,
                    state.inherit_saved_head,
                    sorted(introduced),
                )
            except Exception as cleanup_error:
                logger.error("Could not prove inheritance cleanup: %s", cleanup_error)
                return False
            state.inherit_source = None
            state.inherit_change = None
            state.inherit_candidate = None
            state.inherit_introduced_files = []
            state.inherit_build_attempts = 0
            state.inherit_commit_message = None
            state.inherit_mr_description = None
            state.inherit_local_commit = None
            state.inherit_pushed = False
            state.backport_result = None
            state.log_result = None
            return True

        def handle_inherit_cleanup_failure(state) -> str:
            if not state.inheritance_disabled and _schedule_inherit_cleanup_retry(state):
                logger.warning(
                    "Recreating the clone before retrying leading Z-stream source %s",
                    state.inherit_candidate.issue_key if state.inherit_candidate else "unknown",
                )
                return "fork_and_prepare_dist_git"

            logger.warning(
                "Could not prove inheritance cleanup for %s; recreating the clone for normal backport",
                state.inherit_candidate.issue_key if state.inherit_candidate else "unknown",
            )
            _disable_ystream_inheritance(state, task_metadata)
            return "fork_and_prepare_dist_git"

        async def wait_for_fetched_commit(state, commit_sha: str) -> None:
            ref = f"refs/ymir/zstream/{commit_sha}"
            for _ in range(36):
                exit_code, _, _ = await run_subprocess(
                    ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                    cwd=state.local_clone,
                )
                if exit_code == 0:
                    return
                await asyncio.sleep(1)
            raise InheritCandidateError(f"Fetched commit {commit_sha} is not visible in the clone")

        async def evaluate_inherit_source(state):
            candidate = state.inherit_candidate
            if candidate is None:
                return "prepare_normal_backport"
            state.inherit_introduced_files = []
            logger.info(
                "Trying shipped Z-stream fix %s from %s",
                candidate.issue_key,
                candidate.fixed_in_build,
            )
            try:
                state.inherit_source = await resolve_brew_source(
                    candidate.fixed_in_build,
                    state.package,
                )
                if not spec_matches_brew_version(
                    state.local_clone / f"{state.package}.spec",
                    state.inherit_source,
                ):
                    raise InheritCandidateError(
                        f"{candidate.fixed_in_build} does not share the target Epoch:Version"
                    )
                await run_tool(
                    "fetch_commit",
                    repository=state.inherit_source.repository_url,
                    commit_sha=state.inherit_source.commit_sha,
                    clone_path=str(state.local_clone),
                    available_tools=gateway_tools,
                )
                await wait_for_fetched_commit(state, state.inherit_source.commit_sha)
                fix_commit = await find_zstream_fix_commit(
                    state.local_clone,
                    state.inherit_saved_head,
                    state.inherit_source.commit_sha,
                    candidate.issue_key,
                )
                state.inherit_change = await apply_zstream_change(
                    state.local_clone,
                    state.package,
                    fix_commit,
                )
                state.inherit_introduced_files = state.inherit_change.changed_files

                adaptation_agent = await get_inherit_agent()
                response = await adaptation_agent.run(
                    render_template(
                        get_inherit_adaptation_prompt(),
                        InheritAdaptationInputSchema(
                            local_clone=state.local_clone,
                            package=state.package,
                            target_spec=f"{state.package}.spec",
                            source_issue_key=candidate.issue_key,
                            target_issue_key=state.jira_issue,
                            source_commit=fix_commit,
                            source_commit_message=state.inherit_change.commit_message,
                            source_spec_diff=state.inherit_change.source_spec_diff,
                            patch_files=state.inherit_change.patch_files,
                        ),
                    ),
                    expected_output=InheritAdaptationOutputSchema,
                    **get_agent_execution_config(),
                )
                adaptation = InheritAdaptationOutputSchema.model_validate_json(response.last_message.text)
                if not adaptation.success or adaptation.strategy == "unsupported":
                    error = adaptation.error or adaptation.status or "Inheritance adaptation failed"
                    if state.inherit_change.patch_files:
                        raise InheritedPatchApplyError(error)
                    raise InheritCandidateError(error)
                if state.inherit_change.patch_files and adaptation.strategy == "spec_only":
                    raise InheritCandidateError(
                        "Inheritance adaptation reported spec_only for a patch-bearing commit"
                    )
                if not state.inherit_change.patch_files and adaptation.strategy in {
                    "patch",
                    "mixed",
                }:
                    raise InheritCandidateError(
                        f"Inheritance adaptation reported {adaptation.strategy} without patch files"
                    )
                await validate_inherited_adaptation(
                    state.local_clone,
                    state.package,
                    state.inherit_saved_head,
                    state.inherit_change,
                )

                await tasks.update_release(
                    local_clone=state.local_clone,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    rebase=False,
                    available_tools=gateway_tools,
                )
                title = state.inherit_change.commit_message.splitlines()[0]
                await run_tool(
                    AddChangelogEntryTool(options=local_tool_options),
                    spec=f"{state.package}.spec",
                    content=[f"- {title} ({state.jira_issue})"],
                )
                await run_tool(
                    "download_sources",
                    dist_git_path=str(state.local_clone),
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    available_tools=gateway_tools,
                )
                prep_result = await run_tool(
                    RunPackagePrepTool(options=local_tool_options),
                    dist_git_path=str(state.local_clone),
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                )
                if prep_error := _inherit_prep_error(prep_result, state.inherit_change):
                    raise prep_error
                srpm_path = await run_tool(
                    BuildSrpmTool(options=local_tool_options),
                    dist_git_path=str(state.local_clone),
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                )
                if "srpm build failed" in srpm_path.lower() or not Path(srpm_path).is_absolute():
                    raise InheritCandidateError(f"Inherited SRPM build failed: {srpm_path}")

                state.inherit_introduced_files = await get_untracked_files(state)
                state.inherit_introduced_files.extend(state.inherit_change.changed_files)
                origin = (
                    f"Inherited from {candidate.issue_key} ({state.inherit_source.nvr}, commit {fix_commit})."
                )
                state.backport_log.append(origin)
                state.log_result = LogOutputSchema(title=title, description=origin)
                state.inherit_commit_message = ensure_single_ymir_attribution(
                    rewrite_commit_message(
                        state.inherit_change.commit_message,
                        candidate.issue_key,
                        state.jira_issue,
                    )
                )
                triage_details_text = format_mr_triage_details(
                    state.justification,
                    state.triage_summary,
                )
                state.inherit_mr_description = (
                    f"{origin}\n\n"
                    f"{triage_details_text}"
                    f"{format_jira_links_for_mr(state.jira_issue)}\n"
                    f"{wrap_details('Backporting steps', state.backport_log[-1])}"
                    f"\n\n{mr_description_footer(state.package)}"
                )
                state.backport_result = BackportOutputSchema(
                    success=True,
                    status=origin,
                    srpm_path=Path(srpm_path),
                    error=None,
                )
                state.inherit_build_attempts = max_build_attempts
                return "run_inherit_build_agent"
            except AlreadyInheritedError as error:
                logger.error("Y-stream inheritance invariant failed: %s", error)
                state.retry_mode = BackportRetryMode.NONE
                state.backport_result = BackportOutputSchema(
                    success=False,
                    status="",
                    srpm_path=None,
                    error=str(error),
                )
                return "comment_in_jira"
            except (ImmutablePatchError, InheritedPatchApplyError) as error:
                logger.info("Abandoning inheritance and starting normal backport: %s", error)
                _disable_ystream_inheritance(state, task_metadata)
                if not await cleanup_inherit_attempt(state):
                    return handle_inherit_cleanup_failure(state)
                return "prepare_normal_backport"
            except Exception as error:
                logger.info("Cannot inherit %s: %s", candidate.issue_key, error)
                if not await cleanup_inherit_attempt(state):
                    return handle_inherit_cleanup_failure(state)
                _disable_ystream_inheritance(state, task_metadata)
                return "prepare_normal_backport"

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
                            has_extract_log_snippets=any(
                                t.name == "extract_log_snippets" for t in gateway_tools
                            ),
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
            if build_result.is_infra_error:
                logger.error(f"Copr infrastructure error for {state.jira_issue}: {build_result.error}")
                state.backport_result.success = False
                state.backport_result.error = build_result.error or "Copr API infrastructure error"
                return "comment_in_jira"
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

        async def run_inherit_build_agent(state):
            """Require a successful Copr validation before publishing inheritance."""
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
                return "stage_changes"

            state.inherit_build_attempts -= 1
            if state.inherit_build_attempts > 0:
                logger.warning(
                    "Inherited Copr validation failed; retrying (%d attempts left): %s",
                    state.inherit_build_attempts,
                    build_result.error,
                )
                return "run_inherit_build_agent"

            logger.info("Inherited Copr validation did not pass: %s", build_result.error)
            if not await cleanup_inherit_attempt(state):
                return handle_inherit_cleanup_failure(state)
            _disable_ystream_inheritance(state, task_metadata)
            return "prepare_normal_backport"

        async def update_release(state):
            try:
                await tasks.update_release(
                    local_clone=state.local_clone,
                    package=state.package,
                    dist_git_branch=state.dist_git_branch,
                    rebase=False,
                    available_tools=gateway_tools,
                )
            except Exception as e:
                logger.warning(f"Error updating release: {e}")
                state.backport_result.success = False
                state.backport_result.error = f"Could not update release: {e}"
                return "comment_in_jira"
            return "stage_changes"

        async def stage_changes(state):
            try:
                if state.inherit_change:
                    await verify_inherited_patches(state.local_clone, state.inherit_change)
                    files_to_git_add = state.inherit_change.changed_files
                else:
                    spec_path = state.local_clone / f"{state.package}.spec"
                    with Specfile(spec_path) as spec:
                        patch_files = [p.location for p in get_all_patches(spec) if p.location]

                    if not patch_files:
                        raise RuntimeError(f"Backport completed but no Patch tags found in {spec_path}")

                    files_to_git_add = [f"{state.package}.spec", *patch_files]
                logger.info(f"Staging files: {files_to_git_add}")

                await tasks.stage_changes(
                    local_clone=state.local_clone,
                    files_to_commit=files_to_git_add,
                )
                if state.inherit_change:
                    staged, _ = await check_subprocess(
                        ["git", "diff", "--cached", "--name-only"],
                        cwd=state.local_clone,
                    )
                    _validate_inherited_staged_files(staged, state.inherit_change.changed_files)
            except InheritCandidateError as e:
                logger.info(
                    "Inherited change failed validation before commit; starting normal backport: %s",
                    e,
                )
                _disable_ystream_inheritance(state, task_metadata)
                if not await cleanup_inherit_attempt(state):
                    return handle_inherit_cleanup_failure(state)
                return "prepare_normal_backport"
            except Exception as e:
                logger.warning(f"Error staging changes: {e}")
                state.backport_result.success = False
                state.backport_result.error = f"Could not stage changes: {e}"
                return "comment_in_jira"
            if state.inherit_change:
                return "commit_inherited_change"
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

        async def commit_inherited_change(state):
            try:
                state.inherit_local_commit = await tasks.commit_changes(
                    state.local_clone,
                    state.inherit_commit_message,
                )
                state.inherited_publication_checkpoint = _build_inherited_publication_checkpoint(state)
            except Exception as error:
                logger.warning("Could not create inherited commit: %s", error)
                if not await cleanup_inherit_attempt(state):
                    return handle_inherit_cleanup_failure(state)
                _disable_ystream_inheritance(state, task_metadata)
                return "prepare_normal_backport"
            if dry_run:
                return "submit_consolidation_job"
            return "push_inherited_change"

        async def push_inherited_change(state):
            state.retry_mode = BackportRetryMode.RESUME_INHERITED_MR
            if task_metadata is not None:
                _persist_inherited_publication_checkpoint(
                    task_metadata,
                    state.inherited_publication_checkpoint,
                )
            try:
                await tasks.push_changes(
                    state.local_clone,
                    state.fork_url,
                    state.update_branch,
                    gateway_tools,
                )
                state.inherit_pushed = True
            except Exception as push_error:
                logger.warning("Inherited push returned an error; reconciling remote: %s", push_error)
                try:
                    remote_head = await run_tool(
                        "get_remote_branch_head",
                        repository=state.fork_url,
                        branch=state.update_branch,
                        available_tools=gateway_tools,
                    )
                    if not _remote_branch_matches_commit(remote_head, state.inherit_local_commit):
                        raise RuntimeError(
                            f"source branch points at {remote_head}, expected {state.inherit_local_commit}"
                        )
                    state.inherit_pushed = True
                except Exception as reconcile_error:
                    state.backport_result.success = False
                    state.backport_result.error = (
                        "Could not confirm whether the validated inherited commit was pushed: "
                        f"{reconcile_error}"
                    )
                    return "comment_in_jira"
            return "open_inherited_mr"

        async def open_inherited_mr(state):
            try:
                labels = ["ymir_backport"]
                if await tasks.needs_zstream_target_label(
                    state.dist_git_branch,
                    state.fix_version,
                ):
                    labels.append(ZSTREAM_TARGET_LABEL)
                (
                    state.merge_request_url,
                    state.merge_request_newly_created,
                ) = await tasks.open_update_merge_request(
                    fork_url=state.fork_url,
                    dist_git_branch=state.dist_git_branch,
                    update_branch=state.update_branch,
                    mr_title=state.log_result.title,
                    mr_description=state.inherit_mr_description,
                    available_tools=gateway_tools,
                    labels=labels,
                    package=state.package,
                )
            except Exception as error:
                logger.warning("Inherited commit was pushed but MR creation failed: %s", error)
                state.retry_mode = BackportRetryMode.RESUME_INHERITED_MR
                state.backport_result.success = False
                state.backport_result.error = (
                    f"Validated inherited commit {state.inherit_local_commit} was pushed, "
                    f"but the merge request could not be opened: {error}"
                )
            return "submit_consolidation_job"

        async def commit_push_and_open_mr(state):
            try:
                formatted_patches = "\n".join(f" - {p}" for p in state.upstream_patches)
                triage_details_text = format_mr_triage_details(state.justification, state.triage_summary)
                commit_message = (
                    f"{state.log_result.title}\n\n"
                    f"{state.log_result.description}\n\n"
                    + (f"CVE: {state.cve_id}\n" if state.cve_id else "")
                    + "Upstream patches:\n"
                    + formatted_patches
                    + "\n"
                    + f"Resolves: {state.jira_issue}\n\n"
                    f"This commit was backported {I_AM_YMIR}\n\n"
                    "Assisted-by: Ymir\n"
                )
                mr_description = (
                    f"{state.log_result.description}\n\n"
                    f"Upstream patches:\n{formatted_patches}\n\n"
                    f"{triage_details_text}"
                    f"{format_jira_links_for_mr(state.jira_issue)}\n"
                    f"{wrap_details('Backporting steps', state.backport_log[-1])}"
                    f"\n\n{mr_description_footer(state.package)}"
                )
                (
                    state.merge_request_url,
                    state.merge_request_newly_created,
                ) = await tasks.commit_push_and_open_mr(
                    local_clone=state.local_clone,
                    commit_message=commit_message,
                    fork_url=state.fork_url,
                    dist_git_branch=state.dist_git_branch,
                    update_branch=state.update_branch,
                    mr_title=state.log_result.title,
                    mr_description=mr_description,
                    available_tools=gateway_tools,
                    commit_only=dry_run,
                    labels=["ymir_backport"]
                    + (
                        [ZSTREAM_TARGET_LABEL]
                        if await tasks.needs_zstream_target_label(state.dist_git_branch, state.fix_version)
                        else []
                    ),
                    package=state.package,
                )
            except Exception as e:
                logger.warning(f"Error committing and opening MR: {e}")
                state.merge_request_url = None
                state.backport_result.success = False
                state.backport_result.error = f"Could not commit and open MR: {e}"
            return "submit_consolidation_job"

        async def submit_consolidation_job(state):
            if (
                not state.merge_request_url
                or not state.backport_result
                or not state.backport_result.success
                or not state.merge_request_newly_created
            ):
                return "comment_in_jira"

            try:
                await tasks.try_submit_consolidation_job(
                    state.package,
                    state.dist_git_branch,
                    gateway_tools,
                    redis_conn,
                )
            except InvalidConsolidationConfigError as e:
                logger.warning("Invalid consolidation config for %s: %s", state.package, e)
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Backport",
                    comment_text=(
                        f"ymir.yaml for {state.package} has a malformed consolidation "
                        f"section: {e}\n\nMR consolidation was skipped. Please fix "
                        f"the config file in the rules repository."
                    ),
                    is_error=True,
                    available_tools=gateway_tools,
                    user_triggered=user_triggered,
                )
            except Exception as e:
                logger.warning("Failed to submit consolidation job: %s", e)

            return "comment_in_jira"

        async def comment_in_jira(state):
            if dry_run:
                return Workflow.END
            if state.backport_result.success:
                if checkpoint := state.inherited_publication_checkpoint:
                    comment_text = (
                        f"Inherited and validated the fix from "
                        f"{checkpoint.source_issue_key} "
                        f"({checkpoint.source_nvr}, commit "
                        f"{checkpoint.source_commit}): "
                        f"{state.merge_request_url or state.backport_result.status}"
                    )
                elif state.inherit_candidate and state.inherit_source and state.inherit_change:
                    comment_text = (
                        f"Inherited and validated the fix from "
                        f"{state.inherit_candidate.issue_key} "
                        f"({state.inherit_source.nvr}, commit "
                        f"{state.inherit_change.commit_sha}): "
                        f"{state.merge_request_url or state.backport_result.status}"
                    )
                else:
                    comment_text = (
                        state.merge_request_url if state.merge_request_url else state.backport_result.status
                    )
                is_error = False
            else:
                comment_text = f"Agent failed to perform a backport: {state.backport_result.error}"
                is_error = True
            logger.info(f"Result to be put in Jira comment: {comment_text}")
            try:
                await tasks.comment_in_jira(
                    jira_issue=state.jira_issue,
                    agent_type="Backport",
                    comment_text=comment_text,
                    is_error=is_error,
                    available_tools=gateway_tools,
                    user_triggered=user_triggered,
                )
            except Exception:
                if not state.inherit_pushed and state.retry_mode != BackportRetryMode.NONE:
                    raise
                logger.warning(
                    "Jira comment failed for terminal/publication-only result; not restarting backport",
                    exc_info=True,
                )
            return Workflow.END

        workflow.add_step("change_jira_status", change_jira_status)
        workflow.add_step("resume_inherited_publication", resume_inherited_publication)
        workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
        workflow.add_step("prepare_normal_backport", prepare_normal_backport)
        workflow.add_step("evaluate_inherit_source", evaluate_inherit_source)
        workflow.add_step("run_backport_agent", run_backport_agent)
        workflow.add_step("fix_build_error", fix_build_error)
        workflow.add_step("run_build_agent", run_build_agent)
        workflow.add_step("run_inherit_build_agent", run_inherit_build_agent)
        workflow.add_step("update_release", update_release)
        workflow.add_step("stage_changes", stage_changes)
        workflow.add_step("run_log_agent", run_log_agent)
        workflow.add_step("commit_inherited_change", commit_inherited_change)
        workflow.add_step("push_inherited_change", push_inherited_change)
        workflow.add_step("open_inherited_mr", open_inherited_mr)
        workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
        workflow.add_step("submit_consolidation_job", submit_consolidation_job)
        workflow.add_step("comment_in_jira", comment_in_jira)

        response = await workflow.run(
            BackportState(
                package=package,
                dist_git_branch=dist_git_branch,
                dist_git_namespace=dist_git_namespace,
                upstream_patches=upstream_patches,
                jira_issue=jira_issue,
                cve_id=cve_id,
                justification=justification,
                triage_summary=triage_summary,
                fix_version=fix_version,
                attempts_remaining=max_build_attempts,
                shipped_zstream_candidates=shipped_zstream_candidates or [],
                inherited_publication_checkpoint=inherited_publication_checkpoint,
                inheritance_disabled=inheritance_disabled,
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
        with span_processor.start_transaction(jira_issue, workflow="BackportWorkflow"):
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
            async with issue_lock(redis, backport_data.jira_issue, prefix="lock:backport:") as lock_token:
                if lock_token is None:
                    logger.info(
                        "Issue %s is already locked by another worker; dropping duplicate",
                        backport_data.jira_issue,
                    )
                    return
                await _process_backport_locked(task, triage_state, backport_data)

        async def _process_backport_locked(task, triage_state, backport_data):
            dist_git_branch = triage_state["target_branch"]
            dist_git_namespace = triage_state.get("dist_git_namespace")
            user_triggered = task.user_triggered
            logger.info(
                f"Processing backport for package: {backport_data.package}, "
                f"JIRA: {backport_data.jira_issue}, branch: {dist_git_branch}, "
                f"attempt: {task.attempts + 1}"
                + (" (user-triggered via ymir_todo)" if user_triggered else "")
            )

            async def finalize_failure(error: ErrorData, retry_queue: str, task, comment_text=None):
                logger.error("Moving failed task to error list: %s", backport_data.jira_issue)
                await tasks.set_jira_labels(
                    jira_issue=backport_data.jira_issue,
                    labels_to_add=[JiraLabels.BACKPORT_ERRORED.value],
                    labels_to_remove=[JiraLabels.TRIAGED_BACKPORT.value],
                    dry_run=dry_run,
                    user_triggered=user_triggered,
                )
                # Crash paths have not reached the workflow's Jira-comment step.
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
                            "Failed to post final backport failure comment for %s: %s",
                            backport_data.jira_issue,
                            comment_error,
                        )
                error_id = await fix_await(redis.incr(RedisQueues.ERROR_ID_COUNTER.value))
                entry = ErrorListEntry(error_id=error_id, queue=retry_queue, task=task, error=error)
                await fix_await(redis.lpush(RedisQueues.ERROR_LIST.value, entry.model_dump_json()))

            async def retry(
                task,
                error: ErrorData,
                comment_text=None,
                backport_data=backport_data,
                user_triggered=user_triggered,
            ):
                task.attempts += 1
                retry_queue = backport_queue_todo if task.user_triggered else backport_queue
                if task.attempts < max_retries:
                    logger.warning(
                        f"Task failed (attempt {task.attempts}/{max_retries}), "
                        f"re-queuing for retry: {backport_data.jira_issue}"
                    )
                    await fix_await(redis.lpush(retry_queue, task.model_dump_json()))
                    return

                logger.error(f"Task failed after {max_retries} attempts: {backport_data.jira_issue}")
                await finalize_failure(error, retry_queue, task, comment_text)

            try:
                logger.info(f"Starting backport processing for {backport_data.jira_issue}")
                with span_processor.start_transaction(backport_data.jira_issue, workflow="BackportWorkflow"):
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
                        dist_git_namespace=dist_git_namespace,
                        shipped_zstream_candidates=_get_shipped_zstream_candidates(triage_state),
                        inherited_publication_checkpoint=triage_state.get(_INHERITED_PUBLICATION_CHECKPOINT),
                        inheritance_disabled=bool(triage_state.get(_YSTREAM_INHERITANCE_DISABLED, False)),
                        task_metadata=triage_state,
                    )
                    logger.info(
                        f"Backport processing completed for {backport_data.jira_issue}, "
                        f"success: {state.backport_result.success}"
                    )

            except tasks.ZStreamBranchStaleError as e:
                await tasks.handle_zstream_branch_stale_error(
                    e,
                    jira_issues=[backport_data.jira_issue],
                    primary_jira_issue=backport_data.jira_issue,
                    agent_type="Backport",
                    errored_label=JiraLabels.BACKPORT_ERRORED.value,
                    triaged_label=JiraLabels.TRIAGED_BACKPORT.value,
                    dry_run=dry_run,
                    user_triggered=user_triggered,
                    redis_conn=redis,
                    task=task,
                    queue=backport_queue_todo if user_triggered else backport_queue,
                )
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during backport processing for {backport_data.jira_issue}: {error}")
                reason = e.explain() if isinstance(e, FrameworkError) else e
                await retry(
                    task,
                    ErrorData(details=error, jira_issue=backport_data.jira_issue),
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
                    failure = ErrorData(
                        details=getattr(state.backport_result, "error", None) or "Unknown backport error",
                        jira_issue=backport_data.jira_issue,
                    )
                    if not _configure_task_retry(task, state):
                        retry_queue = backport_queue_todo if task.user_triggered else backport_queue
                        await finalize_failure(failure, retry_queue, task)
                        return
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
                        failure,
                    )

        shutdown_event = asyncio.Event()
        install_shutdown_handler(asyncio.get_running_loop(), shutdown_event)
        await run_task_loop(
            redis,
            [backport_queue_todo, backport_queue],
            process_task,
            max_concurrent=max_concurrent_tasks,
            shutdown_event=shutdown_event,
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
