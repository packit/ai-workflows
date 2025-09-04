from enum import Enum
from string import Template

BRANCH_PREFIX = "automated-package-update"

AGENT_WARNING = (
    "Warning: This is an AI-Generated contribution and may contain mistakes. "
    "Please carefully review the contributions made by AI agents.\n"
    "You can learn more about the Jotnar Pilot at https://docs.google.com/document/d/1mXTymiIe7MfjEDq6s4x0s3XnriC9db11DokdgZ5g9KU/edit"
)

JIRA_COMMENT_TEMPLATE = Template(f"""Output from $AGENT_TYPE Agent: \n\n$JIRA_COMMENT\n\n{AGENT_WARNING}""")

I_AM_JOTNAR = "by Jotnar, a Red Hat Enterprise Linux software maintenance AI agent."
CAREFULLY_REVIEW_CHANGES = "Carefully review the changes and make sure they are correct."

class JiraLabels(Enum):
    """Constants for Jira labels used by Jotnar agents"""
    REBASE_IN_PROGRESS = "jotnar_rebase_in_progress"
    BACKPORT_IN_PROGRESS = "jotnar_backport_in_progress"
    NEEDS_ATTENTION = "jotnar_needs_attention"
    NO_ACTION_NEEDED = "jotnar_no_action_needed"

    REBASED = "jotnar_rebased"
    BACKPORTED = "jotnar_backported"

    REBASE_ERRORED = "jotnar_rebase_errored"
    BACKPORT_ERRORED = "jotnar_backport_errored"
    TRIAGE_ERRORED = "jotnar_triage_errored"

    REBASE_FAILED = "jotnar_rebase_failed"
    BACKPORT_FAILED = "jotnar_backport_failed"

    RETRY_NEEDED = "jotnar_retry_needed"

    @classmethod
    def all_labels(cls) -> set[str]:
        """Return all Jotnar labels for cleanup operations"""
        return {label.value for label in cls}
