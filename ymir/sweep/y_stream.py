"""Y-stream sweep strategy.

Re-checks issues tagged ``ymir_postponed_y_stream`` — Y-stream CVEs that
were postponed because their Z-stream clones had not shipped yet
(the ``PENDING_DEPENDENCIES`` eligibility verdict in the triage agent).

Rather than reimplementing the shipping/dependency logic, this strategy
re-runs the very check that produced the postponement in the first place:
``CheckCveTriageEligibilityTool``.  ``PENDING_DEPENDENCIES`` is the *only*
eligibility verdict that maps to the ``ymir_postponed_y_stream`` label
(see ``ymir/agents/triage_agent.py``), so the sweep is simply its inverse:
the issue stays blocked while eligibility is still ``PENDING_DEPENDENCIES``
and is unblocked the moment it is anything else.

Delegating to the eligibility tool keeps triage-time and sweep-time logic
from ever diverging, and inherits all of the tool's handling
(SecurityTracking, target-version normalisation, duplicate detection,
embargo, severity, Y/Z-stream branching, and the "at least one clone
shipped" rule).
"""

import sentry_sdk

from ymir.common import CVEEligibilityResult, TriageEligibility
from ymir.common.constants import JiraLabels
from ymir.supervisor.supervisor_types import FullIssue
from ymir.sweep.base import SweepResult, SweepStrategy
from ymir.sweep.comment_parser import CommentData
from ymir.tools.privileged.jira import CheckCveTriageEligibilityTool


class YStreamSweep(SweepStrategy):
    """Re-runs the CVE eligibility check for postponed Y-stream issues.

    For each issue the strategy:

    1. Re-runs ``CheckCveTriageEligibilityTool`` for the issue key.
    2. On a tool failure or an eligibility ``error`` → keeps the issue
       postponed and retries on the next sweep (a transient Jira/Koji
       hiccup must not un-postpone an issue — several transient failures
       are reported as ``eligibility=NEVER`` *with* an ``error`` field,
       so the ``error`` check must come before the eligibility branch).
    3. While eligibility is still ``PENDING_DEPENDENCIES`` → still blocked.
    4. For any other verdict (``IMMEDIATELY``, or ``NEVER`` without an
       error) → unblocks and re-triages.  Re-triage applies the correct
       terminal outcome — full analysis for ``IMMEDIATELY``, or an
       open-ended / duplicate comment for ``NEVER`` — and never re-adds
       the ``ymir_postponed_y_stream`` label, so there is no loop.
    """

    name = "y_stream"
    label = JiraLabels.YMIR_POSTPONED_Y_STREAM

    async def is_unblocked(self, issue: FullIssue, comment_data: CommentData) -> SweepResult:
        # comment_data is part of the shared SweepStrategy.is_unblocked contract
        # (dependency/pr_pending rely on it) but is unused here: the eligibility
        # tool re-derives everything it needs from the issue key.
        issue_key = issue.key

        try:
            output = await CheckCveTriageEligibilityTool().run(input={"issue_key": issue_key})
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            return SweepResult(
                issue_key=issue_key,
                action="error",
                detail=f"Eligibility check failed for {issue_key}: {exc}",
            )

        result = CVEEligibilityResult.model_validate(output.result)

        # Transient/data errors are surfaced as an ``error`` field (often with
        # eligibility=NEVER, e.g. "clone dependency check failed" or "no target
        # release").  Keep the issue postponed and retry next sweep — do NOT
        # un-postpone on a failure.  Note: "no target release" is a persistent
        # data problem rather than transient, so it will be re-checked every
        # sweep; that is accepted as safer than flapping the label.
        if result.error:
            return SweepResult(
                issue_key=issue_key,
                action="error",
                detail=f"Eligibility error for {issue_key}: {result.error}",
            )

        # PENDING_DEPENDENCIES always carries a non-empty pending_zstream_issues
        # (see ymir/tools/privileged/jira.py), so no empty-pending edge case here.
        if result.eligibility == TriageEligibility.PENDING_DEPENDENCIES:
            return SweepResult(
                issue_key=issue_key,
                action="still_blocked",
                detail=result.reason,
            )

        return SweepResult(
            issue_key=issue_key,
            action="unblocked",
            detail=(
                f"No longer PENDING_DEPENDENCIES ({result.eligibility.value}): {result.reason}. Re-triaging."
            ),
        )
