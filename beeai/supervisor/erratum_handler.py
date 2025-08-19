import logging
from textwrap import dedent

from .work_item_handler import WorkItemHandler
from .errata_utils import (
    TransitionRuleOutcome,
    get_erratum_transition_rules,
)
from .supervisor_types import ErrataStatus, Erratum, WorkflowResult


logger = logging.getLogger(__name__)


class ErratumHandler(WorkItemHandler):
    """
    Perform a single step in the lifecycle of an erratum. This might involve
    changing the erratum state, performing actions like pushing to staging,
    adding comments, or flagging it for human attention.
    """

    def __init__(self, erratum: Erratum, *, dry_run: bool):
        super().__init__(dry_run=dry_run)
        self.erratum = erratum

    def resolve_flag_attention(self, why: str):
        # TODO: implement this - we need to file a JIRA issue
        # against RHELMISC with the jotnar_needs_attention label
        return WorkflowResult(status=why, reschedule_in=-1)

    def resolve_set_status(self, status: ErrataStatus, why: str):
        # TODO: Actally change the state
        logger.info("Would set erratum %d to %s: %s", self.erratum.id, status, why)

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
                        # TODO try pushing to stage
                        logger.info("Would stage-push erratum %d", self.erratum.id)
                    elif rule.name == "Securityalert":
                        # TODO: try refreshing security alerts
                        logger.info(
                            "Would refresh security alerts for erratum %d",
                            self.erratum.id,
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

        if erratum.status == ErrataStatus.NEW_FILES:
            return self.try_to_advance_erratum(ErrataStatus.QE)
        elif erratum.status == ErrataStatus.QE:
            if not erratum.all_issues_release_pending:
                return self.resolve_remove_work_item(
                    "Not all issues are release pending"
                )
            return self.try_to_advance_erratum(ErrataStatus.REL_PREP)
        else:
            return self.resolve_remove_work_item(f"status is {erratum.status}")
