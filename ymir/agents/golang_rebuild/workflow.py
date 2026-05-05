"""
Workflow Orchestrator for Golang CVE Rebuilds (async)

Coordinates Jira ticket processing, repository operations, spec file
modifications, build execution, and status updates.

Supports Brew build workflow (RHEL 8/9) and GitLab MR workflow (RHEL 10+).

For Jira write operations, uses agents/tasks.py MCP gateway helpers.
For Jira read operations, uses jira_queries.py (direct JIRA library).
"""

import asyncio
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path

from ymir.agents.golang_rebuild.brew_client import BrewClient
from ymir.agents.golang_rebuild.constants import (
    AGENT_EMAIL,
    AGENT_NAME,
    GOLANG_COMPONENTS,
    GOLANG_CVE_FIXED_STATUSES,
    RebuildStatus,
)
from ymir.agents.golang_rebuild.git_client import GitClient
from ymir.agents.golang_rebuild.jira_queries import GolangJiraQueries
from ymir.agents.golang_rebuild.models import (
    ComponentRebuildInfo,
    GolangCVEInfo,
    GolangRebuildData,
    RebuildSummary,
)
from ymir.agents.golang_rebuild.specfile import SpecFile, bump_spec_for_golang_rebuild
from ymir.agents.golang_rebuild.utils import (
    get_rhel_version_config,
    get_workspace_path,
    load_golang_config,
)
from ymir.common.constants import (
    GOLANG_REBUILD_QUEUE_LABEL,
    JiraLabels,
    RedisQueues,
)
from ymir.common.version_utils import parse_rhel_version


def _import_tasks():
    """Lazy import of ymir.agents.tasks to avoid pulling in beeai_framework at module level."""
    import ymir.agents.tasks as tasks

    return tasks


def _import_queue_deps():
    """Lazy import of queue/Redis dependencies."""
    from ymir.common.base_utils import fix_await, redis_client
    from ymir.common.models import ErrorData, Task

    return Task, ErrorData, redis_client, fix_await


logger = logging.getLogger(__name__)


class GolangRebuildWorkflow:
    """
    Main workflow orchestrator for golang CVE rebuilds (async).

    Uses MCP gateway for Jira writes, direct JIRA library for reads.
    """

    def __init__(
        self,
        config_path: str | None = None,
        dry_run: bool = False,
        gateway_tools: list | None = None,
    ):
        self.config = load_golang_config(config_path)
        self.dry_run = dry_run
        self.gateway_tools = gateway_tools

        # Initialize clients
        self.jira_queries = GolangJiraQueries(config=self.config)
        self.git_client = GitClient(config=self.config)
        self.brew_client = BrewClient(config=self.config)

        # User info
        # Agent identity for changelog/commit (not user-specific)
        self.agent_name = AGENT_NAME
        self.agent_email = AGENT_EMAIL

        logger.info(f"Initialized workflow (dry_run={dry_run})")

    async def process_golang_cve_ticket(self, ticket_key: str) -> RebuildSummary:
        """Process a single Golang CVE ticket and rebuild all dependent components."""
        logger.info(f"Processing Golang CVE ticket: {ticket_key}")

        summary = RebuildSummary(
            golang_ticket=ticket_key,
            started_at=datetime.utcnow(),
        )

        try:
            issue = self.jira_queries.get_issue(ticket_key)
            cve_info = self.jira_queries.extract_golang_cve_info(issue)
            if not cve_info:
                logger.error(f"Failed to extract CVE info from {ticket_key}")
                summary.components_skipped += 1
                return summary

            # Check if Golang CVE is fixed
            if cve_info.status not in GOLANG_CVE_FIXED_STATUSES:
                if not self.dry_run:
                    logger.warning(f"Golang CVE {ticket_key} not fixed yet (status: {cve_info.status})")
                    summary.components_skipped += 1
                    return summary
                logger.warning(
                    f"DRY RUN: Golang CVE {ticket_key} status is '{cve_info.status}', proceeding for testing"
                )
            else:
                logger.info(f"Golang CVE {ticket_key} is fixed (status: {cve_info.status})")

            # Validate z-stream
            parsed = parse_rhel_version(cve_info.rhel_version)
            if not parsed or not parsed[2]:
                logger.warning(f"Skipping non-z-stream ticket: {ticket_key} ({cve_info.rhel_version})")
                summary.components_skipped += 1
                return summary

            logger.info(f"Processing {ticket_key}: {len(cve_info.cve_ids)} CVEs for {cve_info.rhel_version}")

            # Find dependent component tickets
            component_tickets = self._find_all_dependent_tickets(cve_info)
            logger.info(f"Found {len(component_tickets)} dependent component tickets")

            for component_issue in component_tickets:
                component_key = component_issue.get("key")
                component_name = self._extract_component_name(component_issue)

                if not component_name:
                    logger.warning(f"Could not determine component name for {component_key}")
                    summary.components_skipped += 1
                    continue

                if not self._is_component_allowed(component_name):
                    logger.info(f"Skipping {component_name} ({component_key}) - not in allowed list")
                    summary.components_skipped += 1
                    continue

                logger.info(f"Processing component: {component_name} ({component_key})")

                rebuild_info = ComponentRebuildInfo(
                    component=component_name,
                    ticket_key=component_key,
                    rhel_version=cve_info.rhel_version,
                    cve_ids=cve_info.cve_ids,
                    golang_version=cve_info.golang_version,
                )

                success = await self.process_component_rebuild(rebuild_info, cve_info.ticket_key)

                summary.components_processed += 1
                summary.add_result(
                    component=component_name,
                    success=success,
                    message=rebuild_info.error_message or "Success",
                    scratch_task_id=rebuild_info.scratch_task_id,
                    scratch_nvr=rebuild_info.scratch_nvr,
                )

        except Exception as e:
            logger.exception(f"Error processing {ticket_key}")
            summary.add_result(component="workflow", success=False, message=str(e))

        summary.completed_at = datetime.utcnow()
        return summary

    async def process_component_rebuild(
        self, rebuild_info: ComponentRebuildInfo, golang_ticket: str | None = None
    ) -> bool:
        """Process rebuild for a single component."""
        logger.info(f"Processing rebuild for {rebuild_info.component}")

        try:
            version_config = get_rhel_version_config(self.config, rebuild_info.rhel_version)
            rebuild_info.branch = version_config.get("branch")
            rebuild_info.build_target = version_config.get("build_target")

            # Only RHEL 9.x and 10.x z-streams supported (RHEL 8 dropped)
            parsed = parse_rhel_version(rebuild_info.rhel_version)
            if parsed and int(parsed[0]) < 9:
                logger.warning(f"RHEL {parsed[0]}.x not supported, skipping {rebuild_info.ticket_key}")
                rebuild_info.error_message = f"RHEL {parsed[0]}.x not supported (only 9.x and 10.x)"
                return False

            tasks = _import_tasks()

            # Mark ticket as in progress via MCP
            if not self.dry_run:
                await tasks.set_jira_labels(
                    jira_issue=rebuild_info.ticket_key,
                    labels_to_add=[JiraLabels.GOLANG_REBUILD_IN_PROGRESS.value],
                    labels_to_remove=[JiraLabels.GOLANG_REBUILD_TRIAGED.value],
                )

            success = await self._process_rebuild(rebuild_info)

            # Update labels based on result
            if not self.dry_run:
                if success:
                    await tasks.set_jira_labels(
                        jira_issue=rebuild_info.ticket_key,
                        labels_to_add=[JiraLabels.GOLANG_REBUILD_COMPLETED.value],
                        labels_to_remove=[
                            JiraLabels.GOLANG_REBUILD_IN_PROGRESS.value,
                            GOLANG_REBUILD_QUEUE_LABEL,
                        ],
                    )
                else:
                    await tasks.set_jira_labels(
                        jira_issue=rebuild_info.ticket_key,
                        labels_to_add=[JiraLabels.GOLANG_REBUILD_FAILED.value],
                        labels_to_remove=[JiraLabels.GOLANG_REBUILD_IN_PROGRESS.value],
                    )

            return success

        except Exception as e:
            logger.exception(f"Error processing component {rebuild_info.component}")
            rebuild_info.status = RebuildStatus.ERRORED
            rebuild_info.error_message = str(e)
            if not self.dry_run:
                await tasks.set_jira_labels(
                    jira_issue=rebuild_info.ticket_key,
                    labels_to_add=[JiraLabels.GOLANG_REBUILD_ERRORED.value],
                    labels_to_remove=[JiraLabels.GOLANG_REBUILD_IN_PROGRESS.value],
                )
            return False

    async def _process_rebuild(self, rebuild_info: ComponentRebuildInfo) -> bool:
        """
        Unified rebuild workflow for RHEL 9.x and 10.x z-streams.

        Steps:
        1. Read Jira comment for build instructions (side-tag, commit, extra jiras, message)
        2. Fork dist-git repo via MCP (GitLab fork, same as jotnar-se)
        3. Bump spec file (release + changelog with all jiras and custom message)
        4. If commit hash: update %global commit0, spectool -g, rhpkg new-sources
        5. Scratch build (rhpkg scratch-build --srpm, with side-tag if provided)
        6. STOP — post scratch result to Jira, wait for golang-rebuild-approved label
        7. On approval: commit, push to fork, open GitLab MR for review
        8. Official build happens when MR is merged (via GitLab pipeline)
        """
        logger.info(f"Starting rebuild workflow for {rebuild_info.component} ({rebuild_info.rhel_version})")
        tasks = _import_tasks()
        from ymir.agents.golang_rebuild.comment_parser import parse_recent_comments
        from ymir.agents.golang_rebuild.utils import format_cve_list, format_jira_list

        try:
            if not self.gateway_tools:
                raise ValueError(
                    "Rebuild workflow requires MCP gateway tools. Set MCP_GATEWAY_URL environment variable."
                )

            # Step 1: Read build instructions from Jira comments
            comments = self.jira_queries.get_issue_comments(rebuild_info.ticket_key)
            instructions = parse_recent_comments(comments)

            # Determine build target and release flag (for side-tag)
            build_target = rebuild_info.build_target
            release_flag = None
            if instructions and instructions.has_side_tag:
                build_target = instructions.side_tag
                release_flag = instructions.release
                logger.info(f"Using side-tag: {build_target} (release: {release_flag})")

            # Collect all Jira IDs for changelog/commit
            all_jiras = [rebuild_info.ticket_key]
            if instructions and instructions.additional_jiras:
                for jira_id in instructions.additional_jiras:
                    if jira_id not in all_jiras:
                        all_jiras.append(jira_id)
                logger.info(f"Jira IDs for changelog: {all_jiras}")

            custom_message = instructions.custom_message if instructions else None
            commit_hash = instructions.commit if instructions else None

            # Step 2: Fork and prepare dist-git via MCP (GitLab fork)
            local_clone, update_branch, fork_url, _ = await tasks.fork_and_prepare_dist_git(
                jira_issue=rebuild_info.ticket_key,
                package=rebuild_info.component,
                dist_git_branch=rebuild_info.branch,
                available_tools=self.gateway_tools,
            )
            rebuild_info.repo_path = str(local_clone)
            rebuild_info.fork_url = fork_url

            # Step 3: Find and bump spec file
            spec_file = SpecFile.find_spec_file(local_clone)
            if not spec_file:
                raise FileNotFoundError(f"No .spec file found in {local_clone}")

            old_release, new_release = bump_spec_for_golang_rebuild(
                spec_path=spec_file,
                golang_version=rebuild_info.golang_version,
                cves=rebuild_info.cve_ids,
                jiras=all_jiras,
                author_name=self.agent_name,
                author_email=self.agent_email,
                commit_hash=commit_hash,
                custom_message=custom_message,
            )
            logger.info(f"Bumped release: {old_release} -> {new_release}")

            # Step 4: If commit hash provided, download and upload new sources
            if commit_hash:
                logger.info(f"Updating sources for commit {commit_hash[:12]}...")
                await self.git_client.update_sources_for_commit(local_clone, spec_file.name)

            # Step 5: Scratch build from local changes
            rebuild_info.status = RebuildStatus.SCRATCH_BUILD
            scratch_result = await self.brew_client.build_and_wait(
                repo_path=local_clone,
                target=build_target,
                scratch=True,
                release=release_flag,
            )
            rebuild_info.scratch_task_id = scratch_result.task_id
            rebuild_info.scratch_nvr = scratch_result.nvr

            if not scratch_result.success:
                raise ValueError(f"Scratch build failed: {scratch_result.error_message}")

            logger.info(f"Scratch build succeeded: {scratch_result.nvr}")

            # Step 6: Post scratch result and STOP — wait for approval
            rebuild_info.status = RebuildStatus.SCRATCH_COMPLETE
            await self._post_scratch_result_and_wait_approval(rebuild_info, build_target, release_flag)

            # Step 7: Approved — stage, commit, push to fork, open GitLab MR
            files_to_stage = [spec_file.name]
            if commit_hash:
                files_to_stage.append("sources")
            await tasks.stage_changes(local_clone, files_to_stage)

            msg_description = custom_message or f"Rebuilding with new golang {rebuild_info.golang_version}"
            commit_msg = (
                f"{msg_description}\n"
                f"Fixes: {format_cve_list(rebuild_info.cve_ids)}\n"
                f"Resolves: {format_jira_list(all_jiras)}\n\n"
                f"Signed-off-by: {self.agent_name} <{self.agent_email}>"
            )
            mr_title = f"Rebuild {rebuild_info.component} for golang CVE fix"
            brew_url = (
                f"https://brewweb.engineering.redhat.com/brew/taskinfo?taskID={rebuild_info.scratch_task_id}"
            )
            mr_description = (
                f"{msg_description}\n\n"
                f"Scratch build: {rebuild_info.scratch_nvr} ([Brew]({brew_url}))\n\n"
                f"CVEs: {format_cve_list(rebuild_info.cve_ids)}\n"
                f"Resolves: {format_jira_list(all_jiras)}\n"
            )

            mr_url, _newly_created = await tasks.commit_push_and_open_mr(
                local_clone=local_clone,
                commit_message=commit_msg,
                fork_url=fork_url,
                dist_git_branch=rebuild_info.branch,
                update_branch=update_branch,
                mr_title=mr_title,
                mr_description=mr_description,
                available_tools=self.gateway_tools,
                commit_only=self.dry_run,
            )

            rebuild_info.mr_url = mr_url
            rebuild_info.status = RebuildStatus.COMPLETED

            # Comment in Jira with MR link
            if mr_url and not self.dry_run:
                await tasks.comment_in_jira(
                    jira_issue=rebuild_info.ticket_key,
                    agent_type="Golang Rebuild",
                    comment_text=(
                        f"Merge request created for review:\n\n"
                        f"MR: {mr_url}\n"
                        f"Scratch Build: {rebuild_info.scratch_nvr}\n\n"
                        f"Official build will be triggered when MR is merged."
                    ),
                    available_tools=self.gateway_tools,
                )

            logger.info(f"MR created: {mr_url}")
            return True

        except Exception as e:
            logger.exception(f"Rebuild workflow failed for {rebuild_info.component}")
            rebuild_info.status = RebuildStatus.FAILED
            rebuild_info.error_message = str(e)
            return False

    # ==========================================
    # Helper Methods
    # ==========================================

    def _is_component_allowed(self, component_name: str) -> bool:
        """Check if a component is allowed based on config filter."""
        component_filter = self.config.get("component_filter", {})
        if not component_filter.get("enabled", False):
            return True
        allowed = component_filter.get("allowed_components", [])
        if not allowed:
            return True
        return component_name.lower() in [c.lower() for c in allowed]

    async def _prepare_repository(self, rebuild_info: ComponentRebuildInfo) -> Path:
        """Clone or update repository for component (RHEL 8/9 Brew workflow)."""
        workspace = get_workspace_path(self.config, rebuild_info.component, rebuild_info.rhel_version)
        repo_path = await self.git_client.clone_repository(
            component=rebuild_info.component,
            target_dir=workspace.parent,
            branch=rebuild_info.branch,
        )
        is_clean, message = await self.git_client.verify_clean_state(repo_path)
        if not is_clean:
            logger.warning(f"Repository not clean: {message}")
        is_correct, message = await self.git_client.verify_branch(repo_path, rebuild_info.branch)
        if not is_correct:
            logger.warning(f"Branch mismatch: {message}")
            await self.git_client.checkout_branch(repo_path, rebuild_info.branch)
        return repo_path

    async def _post_scratch_result_and_wait_approval(
        self,
        rebuild_info: ComponentRebuildInfo,
        build_target: str,
        release_flag: str | None,
    ):
        """
        Post scratch build result to Jira and wait for golang-rebuild-approved label.

        Polls the ticket every 60 seconds for the approval label.
        """
        tasks = _import_tasks()

        brew_url = (
            f"https://brewweb.engineering.redhat.com/brew/taskinfo?taskID={rebuild_info.scratch_task_id}"
        )
        target_info = f"Target: {build_target}"
        if release_flag:
            target_info += f" (release: {release_flag})"

        approval_comment = (
            f"Scratch build completed successfully.\n\n"
            f"Task ID: {rebuild_info.scratch_task_id}\n"
            f"NVR: {rebuild_info.scratch_nvr}\n"
            f"{target_info}\n"
            f"Brew URL: {brew_url}\n\n"
            f"Changes are ready locally but NOT pushed.\n"
            f"To proceed with official build, add label: golang-rebuild-approved"
        )

        if self.gateway_tools:
            try:
                await tasks.comment_in_jira(
                    jira_issue=rebuild_info.ticket_key,
                    agent_type="Golang Rebuild",
                    comment_text=approval_comment,
                    available_tools=self.gateway_tools,
                )
            except Exception as e:
                logger.warning(f"Failed to post scratch result comment: {e}")

        if self.dry_run:
            logger.info("DRY RUN: Skipping approval wait, would stop here.")
            return

        # Poll for approval label
        logger.info(f"Waiting for golang-rebuild-approved label on {rebuild_info.ticket_key}...")
        approval_label = JiraLabels.GOLANG_REBUILD_APPROVED.value
        poll_interval = 60  # seconds

        while True:
            if self.jira_queries.check_label_exists(rebuild_info.ticket_key, approval_label):
                logger.info(f"Approval label found on {rebuild_info.ticket_key}. Proceeding.")
                return
            logger.debug(f"No approval yet for {rebuild_info.ticket_key}, checking again in {poll_interval}s")
            await asyncio.sleep(poll_interval)

    def _find_all_dependent_tickets(self, cve_info: GolangCVEInfo) -> list[dict]:
        """Find all dependent component tickets for a Golang CVE."""
        all_tickets = []
        for cve_id in cve_info.cve_ids:
            tickets = self.jira_queries.find_dependent_tickets(
                cve_id=cve_id, rhel_version=cve_info.rhel_version
            )
            all_tickets.extend(tickets)

        seen_keys = set()
        unique_tickets = []
        for ticket in all_tickets:
            key = ticket.get("key")
            if key and key not in seen_keys:
                seen_keys.add(key)
                unique_tickets.append(ticket)
        return unique_tickets

    def _extract_component_name(self, issue: dict) -> str | None:
        """Extract component name from Jira issue."""
        fields = issue.get("fields", {})
        components = fields.get("components", [])
        if components:
            return components[0].get("name")

        summary = fields.get("summary", "")
        for component in GOLANG_COMPONENTS:
            if component.lower() in summary.lower():
                return component
        return None

    async def _add_build_comment(self, rebuild_info: ComponentRebuildInfo, scratch_only: bool):
        """Add build info comment to Jira via MCP."""
        tasks = _import_tasks()
        if not self.gateway_tools:
            return

        from ymir.agents.golang_rebuild.utils import format_cve_list

        parts = [
            "Rebuild completed for golang CVE fix.",
            "",
            f"Component: {rebuild_info.component}",
            f"RHEL Version: {rebuild_info.rhel_version}",
            f"Golang Version: {rebuild_info.golang_version}",
            f"CVE(s): {format_cve_list(rebuild_info.cve_ids)}",
            "",
        ]
        if rebuild_info.scratch_task_id:
            parts.append(f"Scratch Build: {rebuild_info.scratch_task_id} ({rebuild_info.scratch_nvr})")
        if not scratch_only and rebuild_info.final_task_id:
            parts.append(f"Final Build: {rebuild_info.final_task_id} ({rebuild_info.final_nvr})")
            parts.append("")
            parts.append(
                f"Brew URL: https://brewweb.engineering.redhat.com/brew/taskinfo?taskID={rebuild_info.final_task_id}"
            )

        try:
            await tasks.comment_in_jira(
                jira_issue=rebuild_info.ticket_key,
                agent_type="Golang Rebuild",
                comment_text="\n".join(parts),
                available_tools=self.gateway_tools,
            )
        except Exception as e:
            logger.warning(f"Failed to add build comment to {rebuild_info.ticket_key}: {e}")

    async def process_queue(self, max_tickets: int | None = None) -> list[RebuildSummary]:
        """Process all Golang CVE tickets in the Jira queue."""
        logger.info("Processing Golang CVE queue")
        tickets = self.jira_queries.find_golang_cve_tickets()
        if max_tickets:
            tickets = tickets[:max_tickets]

        logger.info(f"Found {len(tickets)} tickets to process")
        summaries = []
        for issue in tickets:
            ticket_key = issue.get("key")
            try:
                summary = await self.process_golang_cve_ticket(ticket_key)
                summaries.append(summary)
            except Exception:
                logger.exception(f"Failed to process {ticket_key}")
                summaries.append(RebuildSummary(golang_ticket=ticket_key, components_failed=1))

        return summaries


# ==========================================
# Entry Point (queue mode + direct mode)
# ==========================================


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    config_path = os.getenv("GOLANG_REBUILD_CONFIG")

    # Direct mode: process a single ticket
    if golang_ticket := os.getenv("GOLANG_TICKET"):
        logger.info(f"Running in direct mode for ticket: {golang_ticket}")

        gateway_tools = None
        mcp_url = os.getenv("MCP_GATEWAY_URL")
        if mcp_url:
            from ymir.agents.utils import mcp_tools

            async with mcp_tools(mcp_url) as tools:
                workflow = GolangRebuildWorkflow(
                    config_path=config_path, dry_run=dry_run, gateway_tools=tools
                )
                summary = await workflow.process_golang_cve_ticket(golang_ticket)
                logger.info(f"Direct run completed: {summary.model_dump_json(indent=4)}")
                return

        # No MCP gateway - run without Jira write support
        workflow = GolangRebuildWorkflow(config_path=config_path, dry_run=dry_run)
        summary = await workflow.process_golang_cve_ticket(golang_ticket)
        logger.info(f"Direct run completed: {summary.model_dump_json(indent=4)}")
        return

    # Queue mode: listen on Redis
    logger.info("Starting golang rebuild agent in queue mode")
    Task, ErrorData, redis_client, fix_await = _import_queue_deps()
    tasks = _import_tasks()
    async with redis_client(os.environ["REDIS_URL"]) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        container_version = os.getenv("CONTAINER_VERSION", "c9s")
        queue = (
            RedisQueues.GOLANG_REBUILD_QUEUE_C9S.value
            if container_version == "c9s"
            else RedisQueues.GOLANG_REBUILD_QUEUE_C10S.value
        )
        logger.info(f"Connected to Redis, listening to queue: {queue}")

        mcp_url = os.environ.get("MCP_GATEWAY_URL")

        while True:
            logger.info(f"Waiting for tasks from {queue} (timeout: 30s)...")
            element = await fix_await(redis.brpop([queue], timeout=30))
            if element is None:
                continue

            _, payload = element
            logger.info("Received task from queue.")

            try:
                task = Task.model_validate_json(payload)
                rebuild_data = GolangRebuildData.model_validate(
                    task.metadata.get("rebuild_data", task.metadata)
                )
            except Exception as e:
                logger.error(f"Failed to parse task from queue (malformed data): {e}")
                await fix_await(
                    redis.lpush(
                        RedisQueues.ERROR_LIST.value,
                        ErrorData(details=f"Malformed task: {e}", jira_issue="unknown").model_dump_json(),
                    )
                )
                continue

            async def retry(task, error, rd=rebuild_data):
                task.attempts += 1
                if task.attempts < max_retries:
                    logger.warning(f"Task failed (attempt {task.attempts}/{max_retries}), re-queuing")
                    await fix_await(redis.lpush(queue, task.model_dump_json()))
                else:
                    logger.error(f"Task failed after {max_retries} attempts, moving to error list")
                    await tasks.set_jira_labels(
                        jira_issue=rd.golang_ticket,
                        labels_to_add=[JiraLabels.GOLANG_REBUILD_ERRORED.value],
                        labels_to_remove=[
                            JiraLabels.GOLANG_REBUILD_TRIAGED.value,
                            GOLANG_REBUILD_QUEUE_LABEL,
                        ],
                        dry_run=dry_run,
                    )
                    await fix_await(
                        redis.lpush(
                            RedisQueues.ERROR_LIST.value,
                            ErrorData(details=error, jira_issue=rd.golang_ticket).model_dump_json(),
                        )
                    )

            try:
                gateway_tools = None
                if mcp_url:
                    from ymir.agents.utils import mcp_tools

                    async with mcp_tools(mcp_url) as tools:
                        gateway_tools = tools
                        workflow = GolangRebuildWorkflow(
                            config_path=config_path,
                            dry_run=dry_run,
                            gateway_tools=gateway_tools,
                        )
                        summary = await workflow.process_golang_cve_ticket(rebuild_data.golang_ticket)
                else:
                    workflow = GolangRebuildWorkflow(config_path=config_path, dry_run=dry_run)
                    summary = await workflow.process_golang_cve_ticket(rebuild_data.golang_ticket)

                if summary.components_failed == 0 and summary.components_succeeded > 0:
                    logger.info(f"Success for {rebuild_data.golang_ticket}")
                    await fix_await(
                        redis.lpush(
                            RedisQueues.GOLANG_REBUILD_COMPLETED.value,
                            summary.model_dump_json(),
                        )
                    )
                else:
                    error_msg = f"Some components failed: {summary.components_failed}"
                    await retry(task, error_msg)

            except Exception as e:
                error = "".join(traceback.format_exception(e))
                logger.error(f"Exception during processing: {error}")
                await retry(task, error)


if __name__ == "__main__":
    asyncio.run(main())
