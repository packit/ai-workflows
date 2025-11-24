import logging
from datetime import datetime, timezone

from .constants import (
    ERRATA_JOTNAR_BOT_EMAIL,
    JIRA_JOTNAR_BOT_EMAIL,
    POST_PUSH_TESTING_TIMEOUT,
    POST_PUSH_TESTING_TIMEOUT_STR,
)
from .work_item_handler import WorkItemHandler
from .errata_utils import (
    ErratumBuild,
    ErratumPushStatus,
    TransitionRuleOutcome,
    erratum_add_comment,
    erratum_change_ownership,
    erratum_change_state,
    erratum_get_latest_stage_push_details,
    erratum_has_magic_string_in_comments,
    erratum_push_to_stage,
    erratum_refresh_security_alerts,
    get_erratum_build_map,
    get_erratum_transition_rules,
    get_previous_erratum,
)
from .jira_utils import (
    add_issue_label,
    create_issue,
    get_issue,
    get_issue_by_jotnar_tag,
)
from .supervisor_types import (
    ErrataStatus,
    Erratum,
    Issue,
    IssueStatus,
    JotnarTag,
    WorkflowResult,
)


logger = logging.getLogger(__name__)


# This tag identifies the issue that tracks any human work needed for an erratum.
# If there is an existing issue for the tag and it's not closed, we'll reuse
# it, but if the existing issue is closed, we'll create a new one.
#
# The string form of the tag is "::: JOTNAR needs_attention E: 123456 :::"


def _needs_attention_tag(erratum_id: int) -> JotnarTag:
    return JotnarTag(type="needs_attention", resource="erratum", id=str(erratum_id))


def erratum_needs_attention(erratum_id: int) -> bool:
    issue = get_issue_by_jotnar_tag(
        "RHELMISC",
        _needs_attention_tag(erratum_id),
        with_label="jotnar_needs_attention",
    )
    return issue is not None


def all_issues_are_release_pending(issues: list[Issue]) -> bool:
    return all(issue.status == IssueStatus.RELEASE_PENDING for issue in issues)


def erratum_get_issues(
    erratum: Erratum, *, issue_cache: dict[str, Issue] = {}, full: bool = False
):
    # The errata data we fetch from errata-tool includes details
    # of the errata beyond the ID - in particular it has the status
    # of the issue - but due to a bug in errata tool that is returning
    # stale data, so we need to fetch the status from JIRA directly.
    # https://issues.redhat.com/browse/RHELWF-13481

    return [
        issue_cache.get(issue_key) or get_issue(issue_key, full=full)
        for issue_key in erratum.jira_issues
    ]


def jotnar_owns_all_issues(issues: list[Issue]) -> bool:
    return all(issue.assignee_email == JIRA_JOTNAR_BOT_EMAIL for issue in issues)


def compare_file_lists(
    current_build: ErratumBuild,
    previous_build: ErratumBuild,
    previous_erratum_id: str | int,
) -> tuple[bool, str]:
    is_matched = current_build.package_file_list == previous_build.package_file_list

    comment = (
        f"jotnar-product-listings-checked({current_build.nvr})\n\n"
        f"Compared the file lists for {current_build.nvr} to the file lists for\n"
        f"{previous_build.nvr} in https://errata.engineering.redhat.com/advisory/{previous_erratum_id} -\n"
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


class ErratumHandler(WorkItemHandler):
    """
    Perform a single step in the lifecycle of an erratum. This might involve
    changing the erratum state, performing actions like pushing to staging,
    adding comments, or flagging it for human attention.
    """

    def __init__(
        self, erratum: Erratum, *, dry_run: bool, ignore_needs_attention: bool
    ):
        super().__init__(dry_run=dry_run, ignore_needs_attention=ignore_needs_attention)
        self.erratum = erratum

    def resolve_flag_attention(self, why: str):
        tag = _needs_attention_tag(self.erratum.id)

        issue = get_issue_by_jotnar_tag("RHELMISC", tag)
        if issue is not None:
            add_issue_label(
                issue.key,
                "jotnar_needs_attention",
                why,
                dry_run=self.dry_run,
            )
        else:
            summary = f"{self.erratum.full_advisory} ({self.erratum.synopsis}) needs attention"
            description = f"Erratum: {self.erratum.url}\n\n{why}"
            create_issue(
                project="RHELMISC",
                summary=summary,
                description=description,
                tag=tag,
                reporter_email="jotnar+bot@redhat.com",
                assignee_email="jotnar+bot@redhat.com",
                labels=["jotnar_needs_attention"],
                components=["jotnar-package-automation"],
                dry_run=self.dry_run,
            )

        return WorkflowResult(status=why, reschedule_in=-1)

    def resolve_set_status(self, status: ErrataStatus, why: str):
        erratum_change_state(self.erratum.id, status, dry_run=self.dry_run)

        if status in (ErrataStatus.NEW_FILES, ErrataStatus.QE):
            reschedule_delay = 0
        else:
            reschedule_delay = -1

        return WorkflowResult(status=why, reschedule_in=reschedule_delay)

    def resolve_wait_for_cat_tests(
        self, new_status: ErrataStatus, rule_set
    ) -> WorkflowResult:
        # get stage push details to check completion time
        push_details = erratum_get_latest_stage_push_details(self.erratum.id)

        # only apply timeout if push is complete
        if push_details.status != ErratumPushStatus.COMPLETE:
            return self.resolve_wait(
                f"Stage push status is {push_details.status} for erratum {self.erratum.id},"
                f" waiting for push to complete before moving to {new_status}"
            )

        # get completion time from the log to apply the timeout
        if push_details.updated_at is None:
            return self.resolve_flag_attention(
                "Cannot determine stage push completion time (no log timestamps available)."
            )

        cur_time = datetime.now(tz=timezone.utc)
        time_elapsed = cur_time - push_details.updated_at

        if time_elapsed > POST_PUSH_TESTING_TIMEOUT:
            return self.resolve_flag_attention(
                f"CAT tests didn't complete successfully after {POST_PUSH_TESTING_TIMEOUT_STR}"
            )
        else:
            # within timeout so wait to clear
            return self.resolve_wait(
                f"Stage push completed for erratum {self.erratum.id},"
                f" waiting for CAT tests to complete before moving to {new_status}"
            )

    def try_to_advance_erratum(self, new_status: ErrataStatus) -> WorkflowResult:
        rule_set = get_erratum_transition_rules(self.erratum.id)
        if rule_set.to_status != new_status:
            return self.resolve_flag_attention(
                f"Next state is {rule_set.to_status} instead of {new_status}"
            )

        if rule_set.all_ok:
            if new_status == ErrataStatus.REL_PREP:
                cur_build_map = get_erratum_build_map(self.erratum.id)

                mismatch_packages = []
                for package, cur_build in cur_build_map.root.items():
                    nvr = cur_build.nvr
                    if not erratum_has_magic_string_in_comments(
                        self.erratum.id, f"jotnar-product-listings-checked({nvr})"
                    ):
                        prev_erratum_id, _ = get_previous_erratum(
                            self.erratum.id, package
                        )

                        if prev_erratum_id:
                            other_build_map = get_erratum_build_map(prev_erratum_id)
                            prev_build = other_build_map.root[package]

                            is_matched, comment = compare_file_lists(
                                cur_build,
                                prev_build,
                                prev_erratum_id,
                            )

                            if not is_matched:
                                mismatch_packages.append(package)

                            erratum_add_comment(
                                self.erratum.id, comment, dry_run=self.dry_run
                            )
                        else:
                            erratum_add_comment(
                                self.erratum.id,
                                f"jotnar-product-listings-checked({nvr})\n\n"
                                "No previous erratum for this package - no need to check package file list change.",
                                dry_run=self.dry_run,
                            )
                if mismatch_packages:
                    return self.resolve_flag_attention(
                        f"The package file lists of this build don't match all of their previous builds - mismatch packages: {mismatch_packages}.\n"
                        "See erratum comments for details."
                    )

            return self.resolve_set_status(
                new_status, f"Moving to {new_status}, since all rules are OK"
            )
        else:
            # list of blocking rule names
            blocking_outcomes = [
                rule.name
                for rule in rule_set.rules
                if rule.outcome != TransitionRuleOutcome.OK
            ]

            # check blocking rules in order of priority
            if "Stagepush" in blocking_outcomes:
                # is it already running?
                push_details = erratum_get_latest_stage_push_details(self.erratum.id)
                existing = push_details.status
                # COMPLETE == not valid after respin ...
                if existing in (
                    None,
                    ErratumPushStatus.COMPLETE,
                ):
                    erratum_push_to_stage(self.erratum.id, dry_run=self.dry_run)
                    return self.resolve_wait(
                        f"Stage-pushing erratum {self.erratum.id} before moving to {new_status}"
                    )
                elif existing == ErratumPushStatus.FAILED:
                    return self.resolve_flag_attention(
                        f"Stage-push previously FAILED for erratum {self.erratum.id},"
                        f" needs manual intervention before moving to {new_status}"
                    )
                else:
                    return self.resolve_wait(
                        f"Stage-push already in progress ({existing}) for erratum {self.erratum.id},"
                        f" waiting for completion before moving to {new_status}"
                    )
            elif "Cat" in blocking_outcomes:
                return self.resolve_wait_for_cat_tests(new_status, rule_set)
            elif "Securityalert" in blocking_outcomes:
                erratum_refresh_security_alerts(self.erratum.id, dry_run=self.dry_run)
                return self.resolve_wait(
                    f"Refreshing security alerts for erratum {self.erratum.id} before moving to {new_status}"
                )
            else:
                # unknown blocking rules, flag for attention with details
                blocking_rules_details = "\n".join(
                    f"{r.name}: {r.details}"
                    for r in rule_set.rules
                    if r.outcome == TransitionRuleOutcome.BLOCK
                )
                return self.resolve_flag_attention(
                    f"Transition to {new_status} is blocked by:\n"
                    + blocking_rules_details,
                )

    async def run(self) -> WorkflowResult:
        erratum = self.erratum

        logger.info(
            "Running workflow for erratum %s (%s)",
            erratum.url,
            erratum.full_advisory,
        )

        if (not self.ignore_needs_attention) and erratum_needs_attention(erratum.id):
            return self.resolve_remove_work_item(
                "Erratum already flagged for human attention"
            )

        related_issues = erratum_get_issues(erratum)
        # Try to change the ownership to Jotnar if the erratum was not owned by Jotnar
        if (
            erratum.assigned_to_email != ERRATA_JOTNAR_BOT_EMAIL
            or erratum.package_owner_email != ERRATA_JOTNAR_BOT_EMAIL
        ):
            if jotnar_owns_all_issues(related_issues):
                erratum_change_ownership(erratum.id, ERRATA_JOTNAR_BOT_EMAIL)
                return WorkflowResult(
                    status=f"Changed ownership of erratum {erratum.id} to Jotnar bot, re-processing",
                    reschedule_in=0,
                )
            else:
                return self.resolve_flag_attention(
                    "Erratum has issues not owned by Project Jötnar. Please coordinate with QA Contact for these "
                    "issues to move those issues to Release Pending or change the Assigned Team for the issue to "
                    "rhel-jotnar. No further action will be taken on the erratum until jotnar_needs_attention is "
                    "cleared on this issue."
                )

        match erratum.status:
            case ErrataStatus.NEW_FILES:
                return self.try_to_advance_erratum(ErrataStatus.QE)
            case ErrataStatus.QE:
                if not all_issues_are_release_pending(related_issues):
                    return self.resolve_remove_work_item(
                        "Not all issues are release pending"
                    )
                return self.try_to_advance_erratum(ErrataStatus.REL_PREP)
            case _:
                return self.resolve_remove_work_item(f"status is {erratum.status}")
