import asyncio
import json
import logging
import os
import re
import time
import traceback
from typing import Any

from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow
from pydantic import Field
from specfile import Specfile

import ymir.agents.tasks as tasks
from ymir.agents.build_agent import create_build_agent
from ymir.agents.build_agent import get_prompt as get_build_prompt
from ymir.agents.constants import I_AM_YMIR, ZSTREAM_TARGET_LABEL, mr_description_footer
from ymir.agents.log_agent import create_log_agent
from ymir.agents.log_agent import get_prompt as get_log_prompt
from ymir.agents.observability import setup_observability
from ymir.agents.package_update_steps import PackageUpdateState
from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.tasks import complete_job, pick_next_job
from ymir.agents.utils import (
    _PROMPTS_DIR,
    _get_jinja2_env,
    check_subprocess,
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
)
from ymir.common.base_utils import is_cs_branch, redis_client, run_task_loop
from ymir.common.constants import JiraLabels
from ymir.common.logging_setup import configure_logging, current_jira_issue, get_trajectory_writeable
from ymir.common.mock_repos import get_mock_local_tool_env
from ymir.common.models import (
    BuildInputSchema,
    BuildOutputSchema,
    LogInputSchema,
    LogOutputSchema,
    MergeConsolidationJob,
    MRConsolidationInputSchema,
    MRConsolidationOutputSchema,
)
from ymir.common.utils import get_all_patches
from ymir.tools.unprivileged.commands import RunShellCommandTool
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
from ymir.tools.unprivileged.wicked_git import (
    BuildSrpmInput,
    BuildSrpmTool,
    GitLogSearchTool,
    GitPatchApplyFinishTool,
    GitPatchApplyTool,
    GitPatchCreationTool,
    RunPackagePrepInput,
    RunPackagePrepTool,
)

logger = logging.getLogger(__file__)
redis_logger = logging.getLogger("agent.redis")

_NFS_CACHE_WAIT = 60


async def create_consolidation_agent(
    mcp_tools_list: list[Tool],
    local_tool_options: dict[str, Any],
) -> ReasoningAgent:
    """Create the LLM agent that combines two MR changesets."""
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
        GetPackageInfoTool(options=local_tool_options),
        RunPackagePrepTool(options=local_tool_options),
        BuildSrpmTool(options=local_tool_options),
    ]

    base_tools.extend([t for t in mcp_tools_list if t.name == "get_maintainer_rules"])

    return ReasoningAgent(
        name="MRConsolidationAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=base_tools,
        memory=UnconstrainedMemory(),
        middlewares=[GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())],
        role="Red Hat Enterprise Linux developer",
        instructions=render_template("mr_consolidation/instructions.j2"),
    )


class ConsolidationState(PackageUpdateState):
    """Workflow state for the MR consolidation agent."""

    mr_branches: list[str] = Field(default_factory=list)
    mr_descriptions: list[str] = Field(default_factory=list)
    mr_titles: list[str] = Field(default_factory=list)
    mr_urls: list[str] = Field(default_factory=list)
    jira_issues_collected: list[str] = Field(default_factory=list)
    release_strategy: str = Field(default="per_commit")
    consolidation_result: MRConsolidationOutputSchema | None = Field(default=None)
    consolidation_log: list[str] = Field(default_factory=list)
    attempts_remaining: int = Field(default=3)
    patches_per_mr: dict[str, list[str]] = Field(default_factory=dict)
    all_open_mrs: list[dict] = Field(default_factory=list)
    current_mrs_count: int = Field(default=0)


def _extract_jira_issues_from_description(description: str) -> list[str]:
    """Extract RHEL-NNNNN Jira issue keys from structured lines in MR descriptions.

    Only extracts from:
    - ``Resolves: RHEL-NNNNN, ...`` lines (backport agent format)
    - Bullet items under ``### Resolved Jira Issues`` (consolidated MR format)

    Ignores RHEL-NNNNN references in triage detail prose, ``<details>``
    blocks, and other free-text context to prevent spurious issue collection.
    """
    issues: list[str] = []

    for line in description.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Resolves:", "Related:")):
            issues.extend(re.findall(r"RHEL-\d+", stripped))

    in_resolved_section = False
    for line in description.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,4}\s+Resolved Jira Issues", stripped):
            in_resolved_section = True
            continue
        if in_resolved_section:
            if stripped.startswith("#") or (stripped and not stripped.startswith("-")):
                in_resolved_section = False
                continue
            issues.extend(re.findall(r"RHEL-\d+", stripped))

    return list(set(issues))


async def _resolve_source_issues(
    state,
    project_path: str,
    issue_keys: list[str],
    gateway_tools: list,
    target_branch: str | None = None,
) -> str:
    """Resolve specific Jira issue keys to their GitLab MR branches.

    Searches for open MRs whose title or description contains the issue
    key.  Populates state with the matched MRs and returns the next
    workflow step.
    """
    matched_mrs = []
    for issue_key in issue_keys:
        kwargs: dict[str, Any] = {
            "project": project_path,
            "state": "opened",
            "labels": ["ymir_backport"],
            "order_by": "updated_at",
            "sort": "desc",
            "available_tools": gateway_tools,
        }
        if target_branch:
            kwargs["target_branch"] = target_branch
        result = await run_tool("list_project_merge_requests", **kwargs)
        mrs = json.loads(result) if isinstance(result, str) else result
        mrs = [m for m in mrs if JiraLabels.MR_CONSOLIDATED.value not in m.get("labels", [])]

        mr = next(
            (m for m in mrs if issue_key in m.get("title", "")),
            None,
        )
        if not mr:
            mr = next(
                (m for m in mrs if issue_key in m.get("description", "")),
                None,
            )

        if not mr:
            logger.error(
                "No open MR found for issue %s in %s",
                issue_key,
                project_path,
            )
            state.consolidation_result = MRConsolidationOutputSchema(
                success=False,
                status=f"Could not find an open MR for {issue_key}",
                error=f"No open MR matching {issue_key} in {project_path}",
            )
            return Workflow.END

        matched_mrs.append(mr)
        logger.info(
            "Resolved %s to MR '%s' (branch: %s)",
            issue_key,
            mr["title"],
            mr["source_branch"],
        )

    state.mr_urls = [mr["url"] for mr in matched_mrs]
    state.mr_branches = [mr["source_branch"] for mr in matched_mrs]
    state.mr_titles = [mr["title"] for mr in matched_mrs]
    state.mr_descriptions = [mr.get("description", "") for mr in matched_mrs]

    all_jira: list[str] = list(issue_keys)
    for mr in matched_mrs:
        all_jira.extend(_extract_jira_issues_from_description(mr.get("description", "")))
    state.jira_issues_collected = sorted(set(all_jira))
    state.jira_issue = state.jira_issues_collected[0] if state.jira_issues_collected else None

    logger.info(
        "Label-triggered consolidation: resolved %s to MRs %s",
        issue_keys,
        state.mr_urls,
    )
    return "fork_and_prepare_dist_git"


async def run_workflow(
    package: str,
    dist_git_branch: str,
    release_strategy: str = "per_commit",
    redis_conn=None,
    dry_run: bool = False,
    consolidation_agent_factory=None,
    max_build_attempts: int = 3,
    backport_branches: list[dict[str, str]] | None = None,
    source_issues: list[str] | None = None,
):
    """Run the MR consolidation workflow.

    Args:
        package: RPM package name.
        dist_git_branch: Dist-git target branch.
        release_strategy: 'merged' or 'per_commit'.
        redis_conn: Redis connection (for queue management).
        dry_run: If True, skip real Jira/GitLab writes.
        consolidation_agent_factory: Optional override for agent creation.
        max_build_attempts: Max SRPM build retry attempts.
        backport_branches: Pre-populated branch info to skip the GitLab MR
            lookup.  Each dict must contain ``branch``, and may contain
            ``title``, ``description``, and ``jira_issues``.
        source_issues: When set, resolve MRs for these specific Jira issue
            keys instead of picking the two oldest open MRs (label-triggered).
    """
    local_tool_options: dict[str, Any] = {"working_directory": None}
    working_id = f"consolidation-{package}-{dist_git_branch}"
    if mock_env := get_mock_local_tool_env(working_id):
        local_tool_options["env"] = mock_env

    async with mcp_tools(
        os.environ["MCP_GATEWAY_URL"],
        call_meta={"package": package, "branch": dist_git_branch},
    ) as gateway_tools:
        if consolidation_agent_factory:
            result = consolidation_agent_factory(gateway_tools, local_tool_options)
            consolidation_agent = await result if asyncio.iscoroutine(result) else result
        else:
            consolidation_agent = await create_consolidation_agent(gateway_tools, local_tool_options)
        log_agent = create_log_agent(gateway_tools, local_tool_options)

        workflow = Workflow(ConsolidationState, name="MRConsolidationWorkflow")

        async def list_open_mrs(state):
            """List open backport MRs for the package/branch and pick the two oldest."""
            if state.mr_branches:
                logger.info(
                    "Branch info pre-populated (%s), skipping GitLab MR lookup",
                    state.mr_branches,
                )
                return "fork_and_prepare_dist_git"

            namespace = "centos-stream" if is_cs_branch(dist_git_branch) else "rhel"
            project_path = f"redhat/{namespace}/rpms/{package}"

            if source_issues:
                return await _resolve_source_issues(
                    state,
                    project_path,
                    source_issues,
                    gateway_tools,
                    target_branch=dist_git_branch,
                )

            result = await run_tool(
                "list_project_merge_requests",
                project=project_path,
                state="opened",
                target_branch=dist_git_branch,
                labels=["ymir_backport"],
                order_by="created_at",
                sort="asc",
                available_tools=gateway_tools,
            )

            mrs = json.loads(result) if isinstance(result, str) else result
            mrs = [mr for mr in mrs if JiraLabels.MR_CONSOLIDATED.value not in mr.get("labels", [])]
            state.all_open_mrs = mrs

            if len(mrs) < 2:
                logger.info(
                    "Fewer than 2 open MRs for %s/%s, nothing to consolidate",
                    package,
                    dist_git_branch,
                )
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=True,
                    status="Fewer than 2 MRs to consolidate; nothing to do.",
                )
                return Workflow.END

            return "fork_and_prepare_dist_git"

        async def fork_and_prepare_dist_git(state):
            working_id = f"consolidation-{package}-{dist_git_branch}-{int(time.time())}"
            (
                state.local_clone,
                state.update_branch,
                state.fork_url,
                _,
            ) = await tasks.fork_and_prepare_dist_git(
                jira_issue=working_id,
                package=package,
                dist_git_branch=dist_git_branch,
                available_tools=gateway_tools,
            )
            local_tool_options["working_directory"] = state.local_clone

            await run_tool(
                "download_sources",
                dist_git_path=str(state.local_clone),
                package=package,
                dist_git_branch=dist_git_branch,
                available_tools=gateway_tools,
            )

            namespace = "centos-stream" if is_cs_branch(dist_git_branch) else "rhel"
            repo_url = f"https://gitlab.com/redhat/{namespace}/rpms/{package}"
            git_env = local_tool_options.get("env")

            fetch_urls = [repo_url]
            if state.fork_url and state.fork_url != repo_url:
                fetch_urls.append(state.fork_url)

            candidate_branches = (
                state.mr_branches if state.mr_branches else [mr["source_branch"] for mr in state.all_open_mrs]
            )

            any_fetched = False
            for branch_name in candidate_branches:
                if branch_name == state.update_branch:
                    logger.warning(
                        "Skipping branch %s — same name as the working branch",
                        branch_name,
                    )
                    continue
                exit_code, _, _ = await run_subprocess(
                    ["git", "rev-parse", "--verify", f"refs/heads/{branch_name}"],
                    cwd=state.local_clone,
                    env=git_env,
                )
                if exit_code == 0:
                    continue
                fetched = False
                for url in fetch_urls:
                    try:
                        await run_tool(
                            "fetch_branch",
                            repository=url,
                            branch=branch_name,
                            clone_path=str(state.local_clone),
                            available_tools=gateway_tools,
                        )
                        fetched = True
                        any_fetched = True
                        break
                    except Exception:
                        logger.info("Branch %s not found at %s", branch_name, url)
                if not fetched:
                    raise RuntimeError(f"Could not fetch branch {branch_name} from any remote")

            if any_fetched:
                # The fetch_branch MCP tool runs on the mcp-gateway pod
                # while subsequent git commands run on this (agent) pod.
                # Both share an NFS4 PVC whose default acdirmin=30s means
                # the agent's NFS client may serve stale directory
                # listings for up to 30s after the gateway writes new ref
                # files.  Sleep long enough for the cache to expire.
                logger.info(
                    "Waiting %ds for NFS attribute cache to expire after MCP fetch",
                    _NFS_CACHE_WAIT,
                )
                await asyncio.sleep(_NFS_CACHE_WAIT)

            if not state.mr_branches:
                all_mrs = state.all_open_mrs
                _, target_head, _ = await run_subprocess(
                    ["git", "rev-parse", dist_git_branch],
                    cwd=state.local_clone,
                    env=git_env,
                )
                target_head = (target_head or "").strip()

                current_mrs = []
                for mr in all_mrs:
                    branch_name = mr["source_branch"]
                    _, merge_base, _ = await run_subprocess(
                        ["git", "merge-base", dist_git_branch, branch_name],
                        cwd=state.local_clone,
                        env=git_env,
                    )
                    merge_base = (merge_base or "").strip()
                    if merge_base == target_head:
                        current_mrs.append(mr)
                    else:
                        logger.warning(
                            "Skipping stale MR '%s' (%s): based on %s, current %s HEAD is %s",
                            mr["title"],
                            mr["url"],
                            merge_base[:12] or "???",
                            dist_git_branch,
                            target_head[:12],
                        )

                if len(current_mrs) < 2:
                    logger.info(
                        "Fewer than 2 MRs share the current %s HEAD, nothing to consolidate",
                        dist_git_branch,
                    )
                    state.consolidation_result = MRConsolidationOutputSchema(
                        success=True,
                        status="Fewer than 2 MRs based on current HEAD; nothing to do.",
                    )
                    return Workflow.END

                oldest_two = current_mrs[:2]
                state.mr_urls = [mr["url"] for mr in oldest_two]
                state.mr_branches = [mr["source_branch"] for mr in oldest_two]
                state.mr_titles = [mr["title"] for mr in oldest_two]
                state.mr_descriptions = [mr.get("description", "") for mr in oldest_two]

                all_jira = []
                for mr in oldest_two:
                    all_jira.extend(_extract_jira_issues_from_description(mr.get("description", "")))
                state.jira_issues_collected = sorted(set(all_jira))
                state.jira_issue = state.jira_issues_collected[0] if state.jira_issues_collected else None
                if state.jira_issues_collected:
                    current_jira_issue.set(",".join(state.jira_issues_collected))

                state.current_mrs_count = len(current_mrs)

                logger.info(
                    "Selected MRs for consolidation (same HEAD): %s",
                    [mr["url"] for mr in oldest_two],
                )

            for branch_name in state.mr_branches:
                exit_code, diff_out, diff_err = await run_subprocess(
                    ["git", "diff", "--name-only", "--diff-filter=A", f"{dist_git_branch}...{branch_name}"],
                    cwd=state.local_clone,
                    env=git_env,
                )
                if exit_code != 0:
                    err_msg = (diff_err or "").strip()
                    logger.error(
                        "git diff failed for %s...%s (exit %d): %s",
                        dist_git_branch,
                        branch_name,
                        exit_code,
                        err_msg,
                    )
                    state.consolidation_result = MRConsolidationOutputSchema(
                        success=False,
                        status="Failed to diff branch",
                        error=f"git diff {dist_git_branch}...{branch_name} "
                        f"failed (exit {exit_code}): {err_msg}",
                    )
                    return "handle_failure"
                new_patches = [f for f in (diff_out or "").strip().splitlines() if f.endswith(".patch")]
                if new_patches:
                    state.patches_per_mr[branch_name] = new_patches
            logger.info("Per-MR patch mapping: %s", state.patches_per_mr)

            return "per_commit_flow" if release_strategy == "per_commit" else "run_consolidation_agent"

        async def run_consolidation_agent(state):
            prompt = render_template(
                "mr_consolidation/prompt.j2",
                MRConsolidationInputSchema(
                    local_clone=state.local_clone,
                    package=package,
                    dist_git_branch=dist_git_branch,
                    mr_branches=state.mr_branches,
                    mr_titles=state.mr_titles,
                    mr_descriptions=state.mr_descriptions,
                    jira_issues=state.jira_issues_collected,
                    release_strategy=release_strategy,
                    build_error=state.build_error,
                ),
            )
            try:
                result = await consolidation_agent.run(
                    prompt,
                    expected_output=MRConsolidationOutputSchema,
                    **get_agent_execution_config(),
                )
                state.consolidation_result = MRConsolidationOutputSchema.model_validate_json(
                    result.last_message.text
                )
            except FrameworkError as e:
                logger.error("Consolidation agent error: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="Agent error",
                    error=str(e),
                )
            except Exception as e:
                logger.error("Unexpected consolidation error: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="Unexpected error",
                    error=str(e),
                )

            if not state.consolidation_result.success:
                return "handle_failure"
            return "run_build_agent"

        async def run_build_agent(state):
            if not state.consolidation_result or not state.consolidation_result.srpm_path:
                logger.warning("No SRPM generated, skipping build verification")
                return "stage_changes"

            build_agent = create_build_agent(gateway_tools, local_tool_options)
            build_prompt = render_template(
                get_build_prompt(),
                BuildInputSchema(
                    srpm_path=state.consolidation_result.srpm_path,
                    dist_git_branch=dist_git_branch,
                    jira_issue=state.jira_issue,
                ),
            )
            try:
                build_result = await build_agent.run(
                    build_prompt,
                    expected_output=BuildOutputSchema,
                    **get_agent_execution_config(),
                )
                build_output = BuildOutputSchema.model_validate_json(build_result.last_message.text)
                if not build_output.success:
                    state.build_error = build_output.error
                    state.attempts_remaining -= 1
                    retry_step = (
                        "per_commit_flow" if release_strategy == "per_commit" else "run_consolidation_agent"
                    )
                    if state.attempts_remaining > 0:
                        return retry_step
                    return "handle_failure"
            except Exception as e:
                logger.error("Build verification error: %s", e)
                state.build_error = str(e)
                state.attempts_remaining -= 1
                retry_step = (
                    "per_commit_flow" if release_strategy == "per_commit" else "run_consolidation_agent"
                )
                if state.attempts_remaining > 0:
                    return retry_step
                return "handle_failure"

            if release_strategy == "per_commit":
                return "push_and_open_mr"
            return "update_release"

        async def _choose_base_branch(branches: list[str], clone, env=None) -> tuple[str, list[str]]:
            """Pick the branch with the larger total patch diff as the base.

            Returns (base_branch, [other_branches...]).
            """
            sizes: dict[str, int] = {}
            for branch in branches:
                _, diff, _ = await run_subprocess(
                    ["git", "diff", "--stat", f"{dist_git_branch}...{branch}", "--", "*.patch"],
                    cwd=clone,
                    env=env,
                )
                total = 0
                for line in (diff or "").strip().splitlines():
                    parts = line.split("|")
                    if len(parts) == 2:
                        nums = re.findall(r"\d+", parts[1])
                        total += sum(int(n) for n in nums)
                sizes[branch] = total

            base = max(branches, key=lambda b: sizes.get(b, 0))
            others = [b for b in branches if b != base]
            logger.info(
                "Base branch (largest diff): %s (%d lines), others: %s",
                base,
                sizes.get(base, 0),
                others,
            )
            return base, others

        async def _run_prep(clone) -> tuple[bool, str]:
            """Run package prep via RunPackagePrepTool and return (success, output)."""
            prep_tool = RunPackagePrepTool(options=local_tool_options)
            result = await prep_tool.run(
                input=RunPackagePrepInput(
                    dist_git_path=clone,
                    package=package,
                    dist_git_branch=dist_git_branch,
                ),
            )
            output = result.result
            if "FAILED" in output or "fuzz" in output.lower():
                return False, output
            return True, output

        async def per_commit_flow(state):
            """Cherry-pick base branch, then incrementally adapt commits from the other branch.

            1. Choose the branch with larger patches as the "base" — cherry-pick
               its commits as-is (they already contain patch, spec changes,
               Release bump, and changelog from the backport agent).
            2. For each commit from the "other" branch:
               a. Cherry-pick --no-commit to bring in spec + patch changes
               b. Strip the Release bump and changelog (we handle those ourselves)
               c. Run %prep to verify patches apply cleanly
               d. If %prep fails, invoke the LLM to adapt the patch
               e. Bump Release deterministically, write changelog, commit
            3. Build SRPM for Copr verification.
            """
            git_env = local_tool_options.get("env")
            spec_name = f"{package}.spec"

            try:
                base_branch, other_branches = await _choose_base_branch(
                    state.mr_branches,
                    state.local_clone,
                    env=git_env,
                )

                base_idx = state.mr_branches.index(base_branch)
                base_jira_issues = _extract_jira_issues_from_description(
                    state.mr_descriptions[base_idx] if base_idx < len(state.mr_descriptions) else "",
                )

                # --- Step 1: cherry-pick all commits from the base branch ---
                _, base_commits_raw, _ = await run_subprocess(
                    ["git", "rev-list", "--reverse", f"{dist_git_branch}..{base_branch}"],
                    cwd=state.local_clone,
                    env=git_env,
                )
                base_commits = [c for c in (base_commits_raw or "").strip().splitlines() if c]

                if base_commits:
                    logger.info(
                        "Cherry-picking %d commit(s) from base branch %s",
                        len(base_commits),
                        base_branch,
                    )
                    await check_subprocess(
                        ["git", "cherry-pick", *base_commits],
                        cwd=state.local_clone,
                        env=git_env,
                    )

                logger.info("Base branch commits applied")

                # The base branch may have added new source tarballs to the
                # lookaside cache (updated `sources` file).  Re-download so
                # subsequent prep / SRPM builds find them.
                await run_tool(
                    "download_sources",
                    dist_git_path=str(state.local_clone),
                    package=package,
                    dist_git_branch=dist_git_branch,
                    available_tools=gateway_tools,
                )
                logger.info(
                    "Waiting %ds for NFS attribute cache after source download",
                    _NFS_CACHE_WAIT,
                )
                await asyncio.sleep(_NFS_CACHE_WAIT)

                # --- Step 2: process each commit from other branches ---
                for other_branch in other_branches:
                    other_idx = state.mr_branches.index(other_branch)
                    other_jira = _extract_jira_issues_from_description(
                        state.mr_descriptions[other_idx] if other_idx < len(state.mr_descriptions) else "",
                    )
                    if not other_jira and state.jira_issues_collected:
                        remaining = [j for j in state.jira_issues_collected if j not in base_jira_issues]
                        other_jira = remaining or [state.jira_issues_collected[-1]]

                    _, other_commits_raw, _ = await run_subprocess(
                        ["git", "rev-list", "--reverse", f"{dist_git_branch}..{other_branch}"],
                        cwd=state.local_clone,
                        env=git_env,
                    )
                    other_commits = [c for c in (other_commits_raw or "").strip().splitlines() if c]

                    for ci, commit_sha in enumerate(other_commits):
                        logger.info(
                            "Processing commit %d/%d from %s: %s",
                            ci + 1,
                            len(other_commits),
                            other_branch,
                            commit_sha[:12],
                        )

                        # Grab the original commit message before cherry-pick
                        # so we can reuse the backport agent's formatting.
                        _, original_msg, _ = await run_subprocess(
                            ["git", "log", "-1", "--format=%B", commit_sha],
                            cwd=state.local_clone,
                            env=git_env,
                        )
                        original_msg = (original_msg or "").strip()

                        has_patches = bool(state.patches_per_mr.get(other_branch))

                        # Cherry-pick to get patch files into the working tree,
                        # then restore the spec so the LLM handles integration
                        # (without touching Release or changelog).
                        cp_exit, _, _ = await run_subprocess(
                            ["git", "cherry-pick", "--no-commit", commit_sha],
                            cwd=state.local_clone,
                            env=git_env,
                        )
                        await check_subprocess(
                            ["git", "checkout", "HEAD", "--", f"{package}.spec"],
                            cwd=state.local_clone,
                            env=git_env,
                        )
                        if cp_exit != 0:
                            await run_subprocess(
                                ["git", "cherry-pick", "--quit"],
                                cwd=state.local_clone,
                                env=git_env,
                            )
                            await run_subprocess(
                                ["git", "reset", "HEAD"],
                                cwd=state.local_clone,
                                env=git_env,
                            )

                        # Run a preliminary prep check to give the LLM useful
                        # context if the patch already conflicts.
                        prep_ok, prep_output = await _run_prep(state.local_clone)

                        j2_env = _get_jinja2_env(_PROMPTS_DIR)
                        cherry_pick_conflict = cp_exit != 0

                        if has_patches:
                            adapt_prompt = j2_env.get_template(
                                "mr_consolidation/adapt_patch.j2",
                            ).render(
                                local_clone=state.local_clone,
                                package=package,
                                base_branch=base_branch,
                                other_branch=other_branch,
                                commit_sha=commit_sha[:12],
                                prep_error=prep_output if not prep_ok else "",
                                build_error=state.build_error,
                                cherry_pick_conflict=cherry_pick_conflict,
                            )
                        else:
                            _, spec_diff, _ = await run_subprocess(
                                [
                                    "git",
                                    "diff",
                                    f"{commit_sha}^",
                                    commit_sha,
                                    "--",
                                    f"{package}.spec",
                                ],
                                cwd=state.local_clone,
                                env=git_env,
                            )
                            adapt_prompt = j2_env.get_template(
                                "mr_consolidation/adapt_spec.j2",
                            ).render(
                                local_clone=state.local_clone,
                                package=package,
                                dist_git_branch=dist_git_branch,
                                base_branch=base_branch,
                                other_branch=other_branch,
                                commit_sha=commit_sha[:12],
                                spec_diff=(spec_diff or "").strip(),
                                prep_error=prep_output if not prep_ok else "",
                                build_error=state.build_error,
                                cherry_pick_conflict=cherry_pick_conflict,
                            )
                        try:
                            await consolidation_agent.run(
                                adapt_prompt,
                                expected_output=MRConsolidationOutputSchema,
                                **get_agent_execution_config(),
                            )
                        except FrameworkError as e:
                            raise RuntimeError(f"LLM adaptation failed for {commit_sha[:12]}: {e}") from e

                        await tasks.update_release(
                            local_clone=state.local_clone,
                            package=package,
                            dist_git_branch=dist_git_branch,
                            rebase=False,
                        )

                        mr_patches = state.patches_per_mr.get(other_branch, [])
                        files_to_stage = [spec_name, *mr_patches]
                        await tasks.stage_changes(
                            local_clone=state.local_clone,
                            files_to_commit=files_to_stage,
                        )

                        # Use the original commit message for the changelog
                        # summary so the log agent produces a matching entry.
                        summary = original_msg.split("\n")[0]
                        log_prompt = render_template(
                            get_log_prompt(),
                            LogInputSchema(
                                jira_issue=other_jira[0] if other_jira else None,
                                changes_summary=summary,
                            ),
                        )
                        try:
                            await log_agent.run(
                                log_prompt,
                                expected_output=LogOutputSchema,
                                **get_agent_execution_config(),
                            )
                        except Exception as e:
                            logger.warning(
                                "Log agent error (non-fatal) for commit %s: %s",
                                commit_sha[:12],
                                e,
                            )

                        await tasks.stage_changes(
                            local_clone=state.local_clone,
                            files_to_commit=[spec_name],
                        )

                        # Reuse the original backport agent commit message
                        await check_subprocess(
                            ["git", "commit", "-m", original_msg],
                            cwd=state.local_clone,
                            env=git_env,
                        )
                        logger.info(
                            "per_commit: committed adapted %s: %s",
                            commit_sha[:12],
                            summary,
                        )

                # Build SRPM for Copr verification (mirrors backport agent pattern)
                srpm_tool = BuildSrpmTool(options=local_tool_options)
                srpm_result = await srpm_tool.run(
                    input=BuildSrpmInput(
                        dist_git_path=state.local_clone,
                        package=package,
                        dist_git_branch=dist_git_branch,
                    ),
                )
                output = srpm_result.result
                srpm_path = output.strip() if "FAILED" not in output else None
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=srpm_path is not None,
                    status="per_commit consolidation complete",
                    srpm_path=srpm_path,
                )
                if not srpm_path:
                    state.consolidation_result.error = output
                    return "handle_failure"

            except Exception as e:
                logger.error("per_commit flow error: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="per_commit flow failed",
                    error=str(e),
                )
                return "handle_failure"

            return "run_build_agent"

        async def update_release(state):
            try:
                await tasks.update_release(
                    local_clone=state.local_clone,
                    package=package,
                    dist_git_branch=dist_git_branch,
                    rebase=False,
                )
            except Exception as e:
                logger.error("Error updating release: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="Failed to update release",
                    error=str(e),
                )
                return "handle_failure"
            return "stage_changes"

        async def stage_changes(state):
            try:
                spec_path = state.local_clone / f"{package}.spec"
                with Specfile(spec_path) as spec:
                    patch_files = [p.expanded_location for p in get_all_patches(spec) if p.expanded_location]

                files_to_stage = [f"{package}.spec", *patch_files]
                logger.info("Staging files: %s", files_to_stage)
                await tasks.stage_changes(
                    local_clone=state.local_clone,
                    files_to_commit=files_to_stage,
                )
            except Exception as e:
                logger.error("Error staging changes: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="Failed to stage changes",
                    error=str(e),
                )
                return "handle_failure"
            if state.log_result:
                return "commit_push_and_open_mr"
            return "run_log_agent"

        async def run_log_agent(state):
            summary_parts = [
                f"Consolidated {len(state.mr_branches)} backport branches "
                f"for {package} on {dist_git_branch}.",
            ]
            if state.mr_titles:
                summary_parts.extend(f"  - {title}" for title in state.mr_titles)
            if state.consolidation_result and state.consolidation_result.status:
                summary_parts.append(f"Result: {state.consolidation_result.status}")
            changes_summary = "\n".join(summary_parts)

            log_prompt = render_template(
                get_log_prompt(),
                LogInputSchema(
                    jira_issue=state.jira_issue,
                    changes_summary=changes_summary,
                ),
            )
            try:
                log_result = await log_agent.run(
                    log_prompt,
                    expected_output=LogOutputSchema,
                    **get_agent_execution_config(),
                )
                state.log_result = LogOutputSchema.model_validate_json(log_result.last_message.text)
            except Exception as e:
                logger.warning("Log agent error (non-fatal): %s", e)
                state.log_result = LogOutputSchema(
                    title=f"Consolidate backport MRs for {package}",
                    description=f"Consolidated backport branches for {package} on {dist_git_branch}",
                )
            return "stage_changes"

        async def commit_push_and_open_mr(state):
            """Squash all changes into a single commit (merged strategy), then push."""
            if state.log_result:
                commit_message = f"{state.log_result.title}\n\n{state.log_result.description}"
            else:
                commit_message = f"Consolidate backport MRs for {package}"

            if state.jira_issues_collected:
                resolves = ", ".join(state.jira_issues_collected)
                commit_message += f"\n\nResolves: {resolves}"

            try:
                exit_code, _, _ = await run_subprocess(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=state.local_clone,
                )
                has_staged = exit_code != 0

                exit_code, commit_count_str, _ = await run_subprocess(
                    ["git", "rev-list", "--count", f"{dist_git_branch}..HEAD"],
                    cwd=state.local_clone,
                )
                commits_ahead = int((commit_count_str or "").strip()) if exit_code == 0 else 0

                if has_staged:
                    await check_subprocess(
                        ["git", "commit", "-m", commit_message],
                        cwd=state.local_clone,
                    )
                    commits_ahead += 1

                if commits_ahead > 1:
                    await check_subprocess(
                        ["git", "reset", "--soft", f"HEAD~{commits_ahead}"],
                        cwd=state.local_clone,
                    )
                    await check_subprocess(
                        ["git", "commit", "-m", commit_message],
                        cwd=state.local_clone,
                    )
                elif commits_ahead == 1:
                    await check_subprocess(
                        ["git", "commit", "--amend", "-m", commit_message],
                        cwd=state.local_clone,
                    )
                elif commits_ahead == 0:
                    raise RuntimeError("No changes produced by consolidation agent")
            except Exception as e:
                logger.error("Failed to finalize commit: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="Failed to create consolidated commit",
                    error=str(e),
                )
                return Workflow.END

            return "push_and_open_mr"

        async def push_and_open_mr(state):
            """Push commits and open the consolidated MR on GitLab."""
            combined_description = _build_consolidated_description(
                state.mr_titles,
                state.mr_descriptions,
                state.mr_urls,
                state.jira_issues_collected,
                package,
            )
            title = f"[Consolidated] Backport fixes for {package} ({dist_git_branch})"

            try:
                if dry_run:
                    logger.info("Dry run: skipping push and MR creation for %s", package)
                    return Workflow.END

                await run_tool(
                    "push_to_remote_repository",
                    repository=state.fork_url,
                    clone_path=str(state.local_clone),
                    branch=state.update_branch,
                    force=True,
                    available_tools=gateway_tools,
                )

                source_labels = {
                    label
                    for mr in state.all_open_mrs
                    if mr["url"] in state.mr_urls
                    for label in (mr.get("labels") or [])
                }
                labels = ["ymir_backport"]
                if ZSTREAM_TARGET_LABEL in source_labels:
                    labels.append(ZSTREAM_TARGET_LABEL)

                mr_result_raw = await run_tool(
                    "open_merge_request",
                    fork_url=state.fork_url,
                    title=title,
                    description=combined_description,
                    target=dist_git_branch,
                    source=state.update_branch,
                    labels=labels,
                    available_tools=gateway_tools,
                )
                mr_result = json.loads(mr_result_raw) if isinstance(mr_result_raw, str) else mr_result_raw
                state.merge_request_url = mr_result.get("url", mr_result_raw)
                state.merge_request_newly_created = mr_result.get("is_new_mr", True)
                logger.info("Consolidated MR created: %s", state.merge_request_url)

                if not state.merge_request_newly_created and state.merge_request_url and labels:
                    try:
                        await run_tool(
                            "add_merge_request_labels",
                            merge_request_url=state.merge_request_url,
                            labels=labels,
                            available_tools=gateway_tools,
                        )
                    except Exception as e:
                        logger.warning("Failed to label reused consolidated MR: %s", e)
            except Exception as e:
                logger.error("Failed to create consolidated MR: %s", e)
                state.consolidation_result = MRConsolidationOutputSchema(
                    success=False,
                    status="Failed to create consolidated MR",
                    error=str(e),
                )
                return Workflow.END

            return "mark_original_mrs"

        async def mark_original_mrs(state):
            """Label original MRs as consolidated so they are excluded from future runs."""
            if dry_run:
                logger.info(
                    "Dry run: would label MRs %s with %s",
                    state.mr_urls,
                    JiraLabels.MR_CONSOLIDATED.value,
                )
                return "update_jira_issues"

            for mr_url in state.mr_urls:
                try:
                    await run_tool(
                        "add_merge_request_labels",
                        merge_request_url=mr_url,
                        labels=[JiraLabels.MR_CONSOLIDATED.value],
                        available_tools=gateway_tools,
                    )
                    logger.info("Labeled original MR as consolidated: %s", mr_url)
                except Exception as e:
                    logger.warning("Failed to label MR %s: %s", mr_url, e)

            return "update_jira_issues"

        async def update_jira_issues(state):
            if not state.jira_issues_collected or not state.merge_request_url:
                return "requeue_if_needed"

            for issue_key in state.jira_issues_collected:
                comment = (
                    f"Your backport MR has been consolidated with other fixes "
                    f"into a single MR: {state.merge_request_url}"
                )
                if dry_run:
                    logger.info(
                        "Dry run: would post consolidation comment on %s",
                        issue_key,
                    )
                    continue
                try:
                    await run_tool(
                        "add_jira_comment",
                        issue_key=issue_key,
                        comment=comment,
                        private=True,
                        available_tools=gateway_tools,
                    )
                    logger.info("Posted consolidation update to %s", issue_key)
                except Exception as e:
                    logger.warning(
                        "Failed to post consolidation comment on %s: %s",
                        issue_key,
                        e,
                    )

            return "requeue_if_needed"

        async def requeue_if_needed(state):
            current_count = state.current_mrs_count
            remaining = current_count - 2
            if remaining < 1:
                return Workflow.END

            logger.info(
                "%d MR(s) remain after consolidation for %s/%s, submitting follow-up consolidation job",
                remaining,
                package,
                dist_git_branch,
            )
            if redis_conn:
                await tasks.submit_merge_job(redis_conn, package, dist_git_branch)
            else:
                logger.warning(
                    "No Redis connection, cannot requeue consolidation for %s/%s",
                    package,
                    dist_git_branch,
                )
            return Workflow.END

        async def handle_failure(state):
            logger.error(
                "MR consolidation failed for %s/%s: %s",
                package,
                dist_git_branch,
                state.consolidation_result.error if state.consolidation_result else "unknown",
            )
            return Workflow.END

        workflow.add_step("list_open_mrs", list_open_mrs)
        workflow.add_step("fork_and_prepare_dist_git", fork_and_prepare_dist_git)
        workflow.add_step("run_consolidation_agent", run_consolidation_agent)
        workflow.add_step("run_build_agent", run_build_agent)
        workflow.add_step("per_commit_flow", per_commit_flow)
        workflow.add_step("update_release", update_release)
        workflow.add_step("stage_changes", stage_changes)
        workflow.add_step("run_log_agent", run_log_agent)
        workflow.add_step("commit_push_and_open_mr", commit_push_and_open_mr)
        workflow.add_step("push_and_open_mr", push_and_open_mr)
        workflow.add_step("mark_original_mrs", mark_original_mrs)
        workflow.add_step("update_jira_issues", update_jira_issues)
        workflow.add_step("requeue_if_needed", requeue_if_needed)
        workflow.add_step("handle_failure", handle_failure)

        initial_state = ConsolidationState(
            package=package,
            dist_git_branch=dist_git_branch,
            jira_issue=None,
            release_strategy=release_strategy,
            attempts_remaining=max_build_attempts,
        )
        if backport_branches:
            initial_state.mr_branches = [b["branch"] for b in backport_branches]
            initial_state.mr_titles = [b.get("title", "") for b in backport_branches]
            initial_state.mr_descriptions = [b.get("description", "") for b in backport_branches]
            all_jira = []
            for b in backport_branches:
                all_jira.extend(b.get("jira_issues", []))
            initial_state.jira_issues_collected = sorted(set(all_jira))
            if initial_state.jira_issues_collected:
                initial_state.jira_issue = initial_state.jira_issues_collected[0]

        response = await workflow.run(initial_state)
        return response.state


_CONSOLIDATED_MARKER = "## Consolidated Backport MR"
_SOURCE_MR_HEADER = re.compile(r"^####\s+MR\s+\d+:\s+(.+)$", re.MULTILINE)


def _extract_sub_mrs(
    title: str,
    description: str,
    url: str | None,
) -> list[dict[str, str | None]]:
    """If a source MR is itself a consolidation, extract its sub-MRs.

    Returns a flat list of ``{title, description, url}`` dicts.
    Non-consolidated MRs are returned as a single-element list.
    """
    if _CONSOLIDATED_MARKER not in (description or ""):
        return [{"title": title, "description": description, "url": url}]

    sub_mrs: list[dict[str, str | None]] = []
    for header_match in _SOURCE_MR_HEADER.finditer(description):
        sub_title = header_match.group(1).strip()
        sub_url: str | None = None
        link_match = re.match(r"\[(.+?)]\((.+?)\)", sub_title)
        if link_match:
            sub_title = link_match.group(1)
            sub_url = link_match.group(2)

        sub_desc = None
        details_start = description.find("<details>", header_match.end())
        if details_start != -1:
            details_end = description.find("</details>", details_start)
            if details_end != -1:
                inner = description[details_start : details_end + len("</details>")]
                content_match = re.search(
                    r"<summary>.*?</summary>\s*(.+)",
                    inner,
                    re.DOTALL,
                )
                if content_match:
                    sub_desc = content_match.group(1).strip()

        sub_mrs.append({"title": sub_title, "description": sub_desc, "url": sub_url})

    return sub_mrs if sub_mrs else [{"title": title, "description": description, "url": url}]


def _build_consolidated_description(
    mr_titles: list[str],
    mr_descriptions: list[str],
    mr_urls: list[str],
    jira_issues: list[str],
    package: str,
) -> str:
    """Build the description for the consolidated MR.

    When a source MR is itself a consolidation MR, its sub-MRs are
    flattened into the top-level list so each original backport is
    listed separately.
    """
    all_sub_mrs: list[dict[str, str | None]] = []
    for i, title in enumerate(mr_titles):
        url = mr_urls[i] if i < len(mr_urls) else None
        desc = mr_descriptions[i] if i < len(mr_descriptions) else None
        all_sub_mrs.extend(_extract_sub_mrs(title, desc or "", url))

    parts = [
        "## Consolidated Backport MR\n",
        f"This MR consolidates multiple backport merge requests {I_AM_YMIR} "
        "Learn more about [MR consolidation and configuration of its behavior]"
        "(https://ymir.pages.redhat.com/docs/agents/mr-consolidation/).\n",
    ]

    if jira_issues:
        parts.append("### Resolved Jira Issues\n")
        parts.extend(f"- [{issue}](https://issues.redhat.com/browse/{issue})" for issue in jira_issues)
        parts.append("")

    parts.append("### Source Merge Requests\n")
    for i, sub in enumerate(all_sub_mrs, 1):
        if sub["url"]:
            parts.append(f"#### MR {i}: [{sub['title']}]({sub['url']})\n")
        else:
            parts.append(f"#### MR {i}: {sub['title']}\n")
        if sub["description"]:
            parts.append(
                f"<details><summary>Original description</summary>\n\n{sub['description']}\n</details>\n"
            )

    parts.append(mr_description_footer(package))
    return "\n".join(parts)


async def main() -> None:
    """Entry point for the MR consolidation agent."""
    init_sentry()

    configure_logging(level=logging.INFO, buffer_size=int(os.getenv("LOG_BUFFER_SIZE", 0)))
    resolve_chat_model_override("mr_consolidation")

    span_processor = setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    max_build_attempts = int(os.getenv("MAX_BUILD_ATTEMPTS", "3"))

    if (package := os.getenv("PACKAGE")) and (branch := os.getenv("BRANCH")):
        release_strategy = os.getenv("RELEASE_STRATEGY", "per_commit")
        logger.info("Running in direct mode for %s/%s", package, branch)
        with span_processor.start_transaction(None, workflow="mr_consolidation"):
            state = await run_workflow(
                package=package,
                dist_git_branch=branch,
                release_strategy=release_strategy,
                dry_run=dry_run,
                max_build_attempts=max_build_attempts,
            )
            logger.info(
                "Direct run completed: %s",
                state.consolidation_result.model_dump_json(indent=4)
                if state.consolidation_result
                else "no result",
            )
            return

    logger.info("Starting MR consolidation agent in queue mode")
    max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", 1))

    async with redis_client(os.environ["REDIS_URL"]) as redis_conn:

        async def poll_consolidation_queue() -> bytes | None:
            job = await pick_next_job(redis_conn)
            if job is None:
                return None
            redis_logger.info("Picked job for %s/%s", job.package, job.target_branch)
            return job.model_dump_json().encode()

        async def process_task(payload: bytes) -> None:
            job = MergeConsolidationJob.model_validate_json(payload)
            jira_key = ",".join(job.source_issues) if job.source_issues else None
            current_jira_issue.set(jira_key)
            try:
                with span_processor.start_transaction(
                    jira_key,
                    workflow="mr_consolidation",
                ):
                    job_strategy = job.release_strategy or os.getenv(
                        "RELEASE_STRATEGY",
                        "per_commit",
                    )
                    state = await run_workflow(
                        package=job.package,
                        dist_git_branch=job.target_branch,
                        redis_conn=redis_conn,
                        dry_run=dry_run,
                        max_build_attempts=max_build_attempts,
                        source_issues=job.source_issues,
                        release_strategy=job_strategy,
                    )
                    if state.consolidation_result and state.consolidation_result.success:
                        logger.info(
                            "Consolidation succeeded for %s/%s",
                            job.package,
                            job.target_branch,
                        )
                    else:
                        logger.warning(
                            "Consolidation failed for %s/%s: %s",
                            job.package,
                            job.target_branch,
                            state.consolidation_result.error if state.consolidation_result else "unknown",
                        )
            except Exception:
                logger.error(
                    "Unhandled error processing %s/%s:\n%s",
                    job.package,
                    job.target_branch,
                    traceback.format_exc(),
                )
            finally:
                await complete_job(redis_conn, job.package, job.target_branch)

        await run_task_loop(
            redis_conn,
            [],
            process_task,
            max_concurrent=max_concurrent_tasks,
            poll_fn=poll_consolidation_queue,
        )


if __name__ == "__main__":
    asyncio.run(main())
