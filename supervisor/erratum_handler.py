import logging
from textwrap import dedent

from .work_item_handler import WorkItemHandler
from .errata_utils import (
    ErratumPushStatus,
    TransitionRuleOutcome,
    erratum_change_state,
    erratum_get_latest_stage_push_status,
    erratum_push_to_stage,
    erratum_refresh_security_alerts,
    get_erratum_transition_rules,
)
from .jira_utils import (
    add_issue_label,
    create_issue,
    get_issue_by_jotnar_tag,
    get_issues_statuses,
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


def erratum_all_issues_are_release_pending(
    erratum: Erratum, issue_cache: dict[str, Issue]
) -> bool:
    # The errata data we fetch from errata-tool includes details
    # of the errata beyond the ID - in particular it has the status
    # of the issue - but due to a bug in errata tool that is returning
    # stale data, so we need to fetch the status from JIRA directly.
    # https://issues.redhat.com/browse/RHELWF-13481

    # Start with the statuses we have in the cache
    statuses = {
        key: issue.status if (issue := issue_cache.get(key)) else None
        for key in erratum.jira_issues
    }
    # Then fetch any that were missing
    to_fetch = [key for key, status in statuses.items() if status is None]
    if to_fetch:
        statuses.update(get_issues_statuses(to_fetch))

    return all(status == IssueStatus.RELEASE_PENDING for status in statuses.values())


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

    def try_to_advance_erratum(self, new_status: ErrataStatus) -> WorkflowResult:
        rule_set = get_erratum_transition_rules(self.erratum.id)
        if rule_set.to_status != new_status:
            return self.resolve_flag_attention(
                f"Next state is {rule_set.to_status} instead of {new_status}"
            )

        if rule_set.all_ok:
            return self.resolve_set_status(
                new_status, f"Moving to {new_status}, since all rules are OK"
            )
        else:
            for rule in rule_set.rules:
                if rule.outcome != TransitionRuleOutcome.OK:
                    if rule.name == "Stagepush":
                        # is it already running?
                        existing = erratum_get_latest_stage_push_status(self.erratum.id)
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
                    elif rule.name == "Securityalert":
                        erratum_refresh_security_alerts(
                            self.erratum.id, dry_run=self.dry_run
                        )
                        return self.resolve_wait(
                            f"Refreshing security alerts for erratum {self.erratum.id} before moving to {new_status}"
                        )

            return self.resolve_flag_attention(
                dedent(
                    f"""\
                    Transition to {new_status} is blocked by:\n
                    {"\n".join(f"{r.name}: {r.details}" for r in rule_set.rules if r.outcome == TransitionRuleOutcome.BLOCK)}
                    """
                ),
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

        if erratum.status == ErrataStatus.NEW_FILES:
            return self.try_to_advance_erratum(ErrataStatus.QE)
        elif erratum.status == ErrataStatus.QE:
            if not erratum_all_issues_are_release_pending(erratum, {}):
                return self.resolve_remove_work_item(
                    "Not all issues are release pending"
                )
            return self.try_to_advance_erratum(ErrataStatus.REL_PREP)
        else:
            return self.resolve_remove_work_item(f"status is {erratum.status}")
