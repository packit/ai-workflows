"""Errata Workflow Agent — standalone BeeAI agent for erratum lifecycle management.

Advances errata through states (NEW_FILES → QE → REL_PREP), handles stage pushes,
CAT test timeouts, product listing verification, and flagging for human attention.
Communicates exclusively through MCP tools.
"""

import asyncio
import logging
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta

from beeai_framework.errors import FrameworkError
from beeai_framework.workflows import Workflow
from pydantic import BaseModel, Field

from ymir.agents.observability import setup_observability
from ymir.agents.utils import mcp_tools, run_tool
from ymir.common.constants import JiraLabels
from ymir.common.logging_setup import configure_logging
from ymir.common.models import (
    ErrataStatus,
    ErratumBuild,
    ErratumBuildMap,
    ErratumPushStatus,
    TransitionRuleOutcome,
    TransitionRuleSet,
    WorkflowResult,
    YmirTag,
)

logger = logging.getLogger(__name__)

# Constants
WAIT_DELAY = 20 * 60  # 20 minutes
POST_PUSH_TESTING_TIMEOUT = timedelta(hours=3)
POST_PUSH_TESTING_TIMEOUT_STR = "3 hours"
ERRATA_JOTNAR_BOT_EMAIL = "jotnar-bot@IPA.REDHAT.COM"
JIRA_JOTNAR_BOT_EMAIL = "jotnar+bot@redhat.com"
JIRA_JOTNAR_TEAM = "rhel-jotnar"
ET_URL = "https://errata.engineering.redhat.com"


class ErrataWorkflowState(BaseModel):
    erratum_id: str
    dry_run: bool = False
    ignore_needs_attention: bool = False

    erratum: dict | None = Field(default=None)
    related_issues: list[dict] | None = Field(default=None)
    target_status: str | None = Field(default=None)
    result: WorkflowResult | None = Field(default=None)


def _needs_attention_tag(erratum_id: int) -> YmirTag:
    return YmirTag(type="needs_attention", resource="erratum", id=str(erratum_id))


def _get_erratum_advisory_url(erratum_id: int | str) -> str:
    return f"{ET_URL}/advisory/{erratum_id}"


def compare_file_lists(
    current_build: ErratumBuild,
    previous_build: ErratumBuild,
    previous_erratum_id: str | int,
) -> tuple[bool, str]:
    is_matched = current_build.package_file_list == previous_build.package_file_list

    comment = (
        f"ymir-product-listings-checked({current_build.nvr})\n\n"
        f"Compared the file lists for {current_build.nvr} to the file lists for\n"
        f"{previous_build.nvr} in {_get_erratum_advisory_url(previous_erratum_id)} -\n"
    )

    if is_matched:
        comment += "the same subpackages are shipped to each variant. Proceeding with the errata workflow."
    else:
        comment += (
            "differences were found.\n\n"
            "Old file list:\n"
            f"{previous_build.model_dump_json(indent=2)}\n\n"
            "New file list:\n"
            f"{current_build.model_dump_json(indent=2)}\n\n"
            "Flagging for human attention."
        )
    return is_matched, comment


async def run_errata_workflow(
    erratum_id: str,
    dry_run: bool = False,
    ignore_needs_attention: bool = False,
) -> WorkflowResult:
    async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
        workflow = Workflow(ErrataWorkflowState, name="ErrataWorkflow")

        # -- Helper closures over gateway_tools --

        async def _flag_attention(
            state: ErrataWorkflowState,
            why: str,
        ) -> WorkflowResult:
            """Search for existing RHELMISC issue by YmirTag JQL, create or add label."""
            erratum = state.erratum
            tag = _needs_attention_tag(erratum["id"])

            # Search for existing issue with this tag
            description_filter = " OR ".join(f'description ~ "\\"{t}\\""' for t in tag.all_formats())
            jql = f"project = RHELMISC AND status NOT IN (Done, Closed) AND ({description_filter})"

            search_result = await run_tool(
                "search_jira_issues",
                available_tools=gateway_tools,
                jql=jql,
                fields=["key", "summary", "labels"],
                max_results=2,
            )
            issues = search_result.get("issues", [])

            if issues:
                if len(issues) > 1:
                    logger.warning("Multiple open issues found with YmirTag %s", tag)
                existing_key = issues[0]["key"]
                await run_tool(
                    "edit_jira_labels",
                    available_tools=gateway_tools,
                    issue_key=existing_key,
                    labels_to_add=[JiraLabels.NEEDS_ATTENTION.value],
                )
            else:
                summary = f"{erratum['full_advisory']} ({erratum['synopsis']}) needs attention"
                description = f"{tag}\n\nErratum: {erratum['url']}\n\n{why}"
                await run_tool(
                    "create_jira_issue",
                    available_tools=gateway_tools,
                    project="RHELMISC",
                    summary=summary,
                    description=description,
                    reporter_email=JIRA_JOTNAR_BOT_EMAIL,
                    assignee_email=JIRA_JOTNAR_BOT_EMAIL,
                    labels=[JiraLabels.NEEDS_ATTENTION.value],
                    components=["jotnar-package-automation"],
                )

            return WorkflowResult(status=why, reschedule_in=-1)

        async def _erratum_has_magic_string_in_comments(erratum_id: str | int, magic_string: str) -> bool:
            """Fetch full erratum and search comments client-side."""
            full_erratum = await run_tool(
                "get_erratum",
                available_tools=gateway_tools,
                erratum_id=str(erratum_id),
                full=True,
            )
            comments = full_erratum.get("comments") or []
            return any(magic_string in c.get("body", "") for c in comments)

        # -- Workflow steps --

        async def fetch_erratum(state: ErrataWorkflowState):
            """Fetch erratum details."""
            logger.info("Fetching erratum %s", state.erratum_id)
            state.erratum = await run_tool(
                "get_erratum",
                available_tools=gateway_tools,
                erratum_id=state.erratum_id,
            )
            logger.info(
                "Erratum %s (%s) status=%s",
                state.erratum["url"],
                state.erratum["full_advisory"],
                state.erratum["status"],
            )
            return "check_needs_attention"

        async def check_needs_attention(state: ErrataWorkflowState):
            """Check if erratum is already flagged for human attention."""
            if state.ignore_needs_attention:
                return "fetch_related_issues"

            erratum_id = state.erratum["id"]
            tag = _needs_attention_tag(erratum_id)

            description_filter = " OR ".join(f'description ~ "\\"{t}\\""' for t in tag.all_formats())
            jql = (
                f"project = RHELMISC AND status NOT IN (Done, Closed) "
                f"AND ({description_filter}) "
                f'AND labels = "{JiraLabels.NEEDS_ATTENTION.value}"'
            )

            search_result = await run_tool(
                "search_jira_issues",
                available_tools=gateway_tools,
                jql=jql,
                fields=["key"],
                max_results=1,
            )
            issues = search_result.get("issues", [])
            if issues:
                logger.info("Erratum %s already flagged for human attention", erratum_id)
                state.result = WorkflowResult(
                    status="Erratum already flagged for human attention",
                    reschedule_in=-1,
                )
                return Workflow.END

            return "fetch_related_issues"

        async def fetch_related_issues(state: ErrataWorkflowState):
            """Fetch JIRA issue details for each issue linked to the erratum."""
            jira_issues = state.erratum.get("jira_issues", [])
            logger.info("Fetching %d related JIRA issues", len(jira_issues))
            state.related_issues = []
            for issue_key in jira_issues:
                try:
                    issue_data = await run_tool(
                        "get_jira_details",
                        available_tools=gateway_tools,
                        issue_key=issue_key,
                    )
                    state.related_issues.append(issue_data)
                except Exception as e:
                    logger.warning("Failed to fetch issue %s: %s", issue_key, e)

            return "check_ownership"

        async def check_ownership(state: ErrataWorkflowState):
            """Verify erratum is owned by Ymir bot, change ownership if needed."""
            erratum = state.erratum
            assigned_to = erratum.get("assigned_to_email", "")
            package_owner = erratum.get("package_owner_email", "")

            if assigned_to == ERRATA_JOTNAR_BOT_EMAIL and package_owner == ERRATA_JOTNAR_BOT_EMAIL:
                return "route_by_status"

            # Check if Ymir owns all related issues
            all_owned = all(
                _get_assigned_team(issue) == JIRA_JOTNAR_TEAM for issue in (state.related_issues or [])
            )

            if all_owned:
                await run_tool(
                    "erratum_change_ownership",
                    available_tools=gateway_tools,
                    erratum_id=str(erratum["id"]),
                    new_owner_email=ERRATA_JOTNAR_BOT_EMAIL,
                )
                state.result = WorkflowResult(
                    status=f"Changed ownership of erratum {erratum['id']} to Ymir bot, re-processing",
                    reschedule_in=0,
                )
                return Workflow.END

            state.result = await _flag_attention(
                state,
                "Erratum has issues not owned by Project Ymir. Please coordinate with QA Contact for these "
                "issues to move those issues to Release Pending or change the Assigned Team for the issue "
                "to rhel-jotnar. No further action will be taken on the erratum until ymir_needs_attention "
                "is cleared on this issue.",
            )
            return Workflow.END

        async def route_by_status(state: ErrataWorkflowState):
            """Route to appropriate handler based on erratum status."""
            status = state.erratum["status"]

            match status:
                case "NEW_FILES":
                    state.target_status = "QE"
                    return "try_to_advance"
                case "QE":
                    if not _all_issues_release_pending(state.related_issues or []):
                        state.result = WorkflowResult(
                            status="Not all issues are release pending",
                            reschedule_in=-1,
                        )
                        return Workflow.END
                    state.target_status = "REL_PREP"
                    return "try_to_advance"
                case _:
                    state.result = WorkflowResult(
                        status=f"status is {status}",
                        reschedule_in=-1,
                    )
                    return Workflow.END

        async def try_to_advance(state: ErrataWorkflowState):
            """Get transition rules and try to advance the erratum."""
            erratum_id = str(state.erratum["id"])
            new_status = state.target_status

            rule_set_data = await run_tool(
                "get_erratum_transition_rules",
                available_tools=gateway_tools,
                erratum_id=erratum_id,
            )
            rule_set = TransitionRuleSet.model_validate(rule_set_data)

            if rule_set.to_status != new_status:
                state.result = await _flag_attention(
                    state,
                    f"Next state is {rule_set.to_status} instead of {new_status}",
                )
                return Workflow.END

            if rule_set.all_ok:
                if new_status == ErrataStatus.REL_PREP:
                    # Verify product listings before advancing
                    return "verify_product_listings"

                # Change state
                status_changes_allowed = os.getenv(
                    "ERRATA_ALLOW_STATUS_CHANGES", "false"
                ).lower() == "true"
                if state.dry_run or not status_changes_allowed:
                    reason = "dry run" if state.dry_run else "ERRATA_ALLOW_STATUS_CHANGES is not set"
                    logger.info(
                        "Skipping erratum state change of %s to %s (%s)",
                        erratum_id,
                        new_status,
                        reason,
                    )
                else:
                    await run_tool(
                        "erratum_change_state",
                        available_tools=gateway_tools,
                        erratum_id=erratum_id,
                        new_state=new_status,
                    )
                reschedule_delay = 0 if new_status in (ErrataStatus.NEW_FILES, ErrataStatus.QE) else -1
                state.result = WorkflowResult(
                    status=f"Moving to {new_status}, since all rules are OK",
                    reschedule_in=reschedule_delay,
                )
                return Workflow.END

            # Handle blocking rules
            blocking_outcomes = [r.name for r in rule_set.rules if r.outcome != TransitionRuleOutcome.OK]

            if "Stagepush" in blocking_outcomes:
                push_details = await run_tool(
                    "get_erratum_stage_push_details",
                    available_tools=gateway_tools,
                    erratum_id=erratum_id,
                )
                existing = push_details.get("status")

                if existing in (None, ErratumPushStatus.COMPLETE):
                    await run_tool(
                        "erratum_push_to_stage",
                        available_tools=gateway_tools,
                        erratum_id=erratum_id,
                    )
                    state.result = WorkflowResult(
                        status=f"Stage-pushing erratum {erratum_id} before moving to {new_status}",
                        reschedule_in=WAIT_DELAY,
                    )
                    return Workflow.END

                if existing == ErratumPushStatus.FAILED:
                    state.result = await _flag_attention(
                        state,
                        f"Stage-push previously FAILED for erratum {erratum_id},"
                        f" needs manual intervention before moving to {new_status}",
                    )
                    return Workflow.END

                state.result = WorkflowResult(
                    status=(
                        f"Stage-push already in progress ({existing}) for erratum {erratum_id},"
                        f" waiting for completion before moving to {new_status}"
                    ),
                    reschedule_in=WAIT_DELAY,
                )
                return Workflow.END

            if "Cat" in blocking_outcomes:
                state.result = await _handle_cat_tests(state, new_status)
                return Workflow.END

            if "Securityalert" in blocking_outcomes:
                await run_tool(
                    "erratum_refresh_security_alerts",
                    available_tools=gateway_tools,
                    erratum_id=erratum_id,
                )
                state.result = WorkflowResult(
                    status=(
                        f"Refreshing security alerts for erratum {erratum_id}"
                        f" before moving to {new_status}"
                    ),
                    reschedule_in=WAIT_DELAY,
                )
                return Workflow.END

            # Unknown blocking rules
            blocking_rules_details = "\n".join(
                f"{r.name}: {r.details}" for r in rule_set.rules if r.outcome == TransitionRuleOutcome.BLOCK
            )
            state.result = await _flag_attention(
                state,
                f"Transition to {new_status} is blocked by:\n" + blocking_rules_details,
            )
            return Workflow.END

        async def _handle_cat_tests(state: ErrataWorkflowState, new_status: str) -> WorkflowResult:
            """Handle CAT test blocking rule with timeout."""
            erratum_id = str(state.erratum["id"])
            push_details = await run_tool(
                "get_erratum_stage_push_details",
                available_tools=gateway_tools,
                erratum_id=erratum_id,
            )

            push_status = push_details.get("status")
            if push_status != ErratumPushStatus.COMPLETE:
                return WorkflowResult(
                    status=(
                        f"Stage push status is {push_status} for erratum {erratum_id},"
                        f" waiting for push to complete before moving to {new_status}"
                    ),
                    reschedule_in=WAIT_DELAY,
                )

            updated_at_str = push_details.get("updated_at")
            if updated_at_str is None:
                return await _flag_attention(
                    state,
                    "Cannot determine stage push completion time (no log timestamps available).",
                )

            if isinstance(updated_at_str, str):
                updated_at = datetime.fromisoformat(updated_at_str)
            else:
                updated_at = updated_at_str

            cur_time = datetime.now(tz=UTC)
            time_elapsed = cur_time - updated_at

            if time_elapsed > POST_PUSH_TESTING_TIMEOUT:
                return await _flag_attention(
                    state,
                    f"CAT tests didn't complete successfully after {POST_PUSH_TESTING_TIMEOUT_STR}",
                )

            return WorkflowResult(
                status=(
                    f"Stage push completed for erratum {erratum_id},"
                    f" waiting for CAT tests to complete before moving to {new_status}"
                ),
                reschedule_in=WAIT_DELAY,
            )

        async def verify_product_listings(state: ErrataWorkflowState):
            """REL_PREP-specific: compare build maps with previous erratum."""
            erratum_id = str(state.erratum["id"])
            new_status = state.target_status

            build_map_data = await run_tool(
                "get_erratum_build_map",
                available_tools=gateway_tools,
                erratum_id=erratum_id,
            )
            cur_build_map = ErratumBuildMap.model_validate(build_map_data)

            mismatch_packages = []
            for package, cur_build in cur_build_map.root.items():
                nvr = cur_build.nvr

                # Check if already verified
                already_checked = await _erratum_has_magic_string_in_comments(
                    erratum_id, f"ymir-product-listings-checked({nvr})"
                ) or await _erratum_has_magic_string_in_comments(
                    erratum_id, f"jotnar-product-listings-checked({nvr})"
                )

                if already_checked:
                    continue

                prev_result = await run_tool(
                    "get_previous_erratum",
                    available_tools=gateway_tools,
                    erratum_id=erratum_id,
                    package_name=package,
                )
                prev_erratum_id = prev_result.get("id")

                if prev_erratum_id:
                    other_build_map_data = await run_tool(
                        "get_erratum_build_map",
                        available_tools=gateway_tools,
                        erratum_id=str(prev_erratum_id),
                    )
                    other_build_map = ErratumBuildMap.model_validate(other_build_map_data)
                    prev_build = other_build_map.root[package]

                    is_matched, comment = compare_file_lists(cur_build, prev_build, prev_erratum_id)

                    if not is_matched:
                        mismatch_packages.append(package)

                    await run_tool(
                        "erratum_add_comment",
                        available_tools=gateway_tools,
                        erratum_id=erratum_id,
                        comment=comment,
                    )
                else:
                    await run_tool(
                        "erratum_add_comment",
                        available_tools=gateway_tools,
                        erratum_id=erratum_id,
                        comment=(
                            f"ymir-product-listings-checked({nvr})\n\n"
                            "No previous erratum for this package - "
                            "no need to check package file list change."
                        ),
                    )

            if mismatch_packages:
                state.result = await _flag_attention(
                    state,
                    "The package file lists of this build don't match all "
                    f"of their previous builds - mismatch packages: {mismatch_packages}.\n"
                    "See erratum comments for details.",
                )
                return Workflow.END

            # All clear, advance to REL_PREP
            status_changes_allowed = os.getenv(
                "ERRATA_ALLOW_STATUS_CHANGES", "false"
            ).lower() == "true"
            if state.dry_run or not status_changes_allowed:
                reason = "dry run" if state.dry_run else "ERRATA_ALLOW_STATUS_CHANGES is not set"
                logger.info(
                    "Skipping erratum state change of %s to %s (%s)",
                    erratum_id,
                    new_status,
                    reason,
                )
            else:
                await run_tool(
                    "erratum_change_state",
                    available_tools=gateway_tools,
                    erratum_id=erratum_id,
                    new_state=new_status,
                )
            state.result = WorkflowResult(
                status=f"Moving to {new_status}, since all rules are OK",
                reschedule_in=-1,
            )
            return Workflow.END

        # Register workflow steps
        workflow.add_step("fetch_erratum", fetch_erratum)
        workflow.add_step("check_needs_attention", check_needs_attention)
        workflow.add_step("fetch_related_issues", fetch_related_issues)
        workflow.add_step("check_ownership", check_ownership)
        workflow.add_step("route_by_status", route_by_status)
        workflow.add_step("try_to_advance", try_to_advance)
        workflow.add_step("verify_product_listings", verify_product_listings)

        response = await workflow.run(
            ErrataWorkflowState(
                erratum_id=erratum_id,
                dry_run=dry_run,
                ignore_needs_attention=ignore_needs_attention,
            )
        )

        return response.state.result


ASSIGNED_TEAM_CUSTOM_FIELD = "customfield_10371"


def _get_assigned_team(issue_data: dict) -> str | None:
    """Extract assigned team from JIRA issue data."""
    fields = issue_data.get("fields", {})
    assigned_team = fields.get(ASSIGNED_TEAM_CUSTOM_FIELD)
    if isinstance(assigned_team, dict):
        return assigned_team.get("value")
    return None


def _all_issues_release_pending(related_issues: list[dict]) -> bool:
    """Check if all issues are in Release Pending status."""
    for issue_data in related_issues:
        fields = issue_data.get("fields", {})
        status = fields.get("status", {}).get("name", "")
        if status != "Release Pending":
            return False
    return True


async def main() -> None:
    configure_logging(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"
    ignore_needs_attention = os.getenv("IGNORE_NEEDS_ATTENTION", "false").lower() == "true"

    erratum_id = os.getenv("ERRATUM_ID")
    if not erratum_id:
        logger.error("ERRATUM_ID environment variable is required")
        sys.exit(1)

    # Handle URL input — extract the ID from the end
    if "/" in erratum_id:
        erratum_id = erratum_id.rstrip("/").split("/")[-1]

    logger.info(
        "Running errata workflow for erratum %s (dry_run=%s, ignore_needs_attention=%s)",
        erratum_id,
        dry_run,
        ignore_needs_attention,
    )

    result = await run_errata_workflow(
        erratum_id,
        dry_run=dry_run,
        ignore_needs_attention=ignore_needs_attention,
    )

    separator = "=" * 60
    print(f"\n{separator}")
    print(f"  STATUS: {result.status}")
    print(f"  RESCHEDULE_IN: {result.reschedule_in}")
    print(f"{separator}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
