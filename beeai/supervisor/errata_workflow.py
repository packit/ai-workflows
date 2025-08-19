import logging
from textwrap import dedent

from .errata_utils import (
    TransitionRuleOutcome,
    get_erratum_transition_rules,
)
from .supervisor_types import ErrataStatus, Erratum, WorkflowResult


logger = logging.getLogger(__name__)


WAIT_DELAY = 20 * 60  # 20 minutes


def resolve_remove_task(erratum: Erratum, why: str):
    return WorkflowResult(status=why, reschedule_in=-1)


def resolve_wait(erratum: Erratum, why: str):
    return WorkflowResult(status=why, reschedule_in=WAIT_DELAY)


def resolve_flag_attention(erratum: Erratum, why: str):
    return WorkflowResult(status=why, reschedule_in=-1)


def resolve_set_status(erratum: Erratum, status: ErrataStatus, why: str):
    if status in (ErrataStatus.NEW_FILES, ErrataStatus.QE):
        reschedule_delay = 0
    else:
        reschedule_delay = -1

    return WorkflowResult(status=why, reschedule_in=reschedule_delay)


def try_to_advance_erratum(
    erratum: Erratum, new_status: ErrataStatus
) -> WorkflowResult:
    rule_set = get_erratum_transition_rules(erratum.id)
    if rule_set.to_status != new_status:
        return resolve_flag_attention(
            erratum, f"Next state is {rule_set.to_status} instead of {new_status}"
        )

    if rule_set.all_ok:
        return resolve_set_status(
            erratum, new_status, f"Moving to {new_status}, since all rules are OK"
        )
    else:
        return resolve_flag_attention(
            erratum,
            dedent(
                f"""\
                Transition to {new_status} is blocked by:
                {"\n".join(f"{r.name}: {r.details}\n" for r in rule_set.rules if r.outcome == TransitionRuleOutcome.BLOCK)}
                """
            ),
        )


async def run_errata_workflow(erratum: Erratum) -> WorkflowResult:
    """
    Runs the workflow for a single issue.
    """
    logger.info(
        "Running workflow for erratum %s (%s)",
        erratum.url,
        erratum.full_advisory,
    )

    if erratum.status == ErrataStatus.NEW_FILES:
        return try_to_advance_erratum(erratum, ErrataStatus.QE)
    elif erratum.status == ErrataStatus.QE:
        if not erratum.all_issues_release_pending:
            return resolve_remove_task(erratum, "Not all issues are release pending")
        return try_to_advance_erratum(erratum, ErrataStatus.REL_PREP)
    else:
        return resolve_remove_task(erratum, f"status is {erratum.status}")
