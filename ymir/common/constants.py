from enum import Enum

import aiohttp

BREWHUB_URL = "https://brewhub.engineering.redhat.com/brewhub"

AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

JIRA_SEARCH_PATH = "rest/api/3/search/jql"


class RedisQueues(Enum):
    """Constants for Redis queue names used by Ymir agents"""

    TRIAGE_QUEUE = "triage_queue"
    REBASE_QUEUE_C9S = "rebase_queue_c9s"
    REBASE_QUEUE_C10S = "rebase_queue_c10s"
    BACKPORT_QUEUE_C9S = "backport_queue_c9s"
    BACKPORT_QUEUE_C10S = "backport_queue_c10s"
    CLARIFICATION_NEEDED_QUEUE = "clarification_needed_queue"
    ERROR_LIST = "error_list"
    OPEN_ENDED_ANALYSIS_LIST = "open_ended_analysis_list"
    COMPLETED_REBASE_LIST = "completed_rebase_list"
    COMPLETED_BACKPORT_LIST = "completed_backport_list"
    REBUILD_QUEUE_C9S = "rebuild_queue_c9s"
    REBUILD_QUEUE_C10S = "rebuild_queue_c10s"
    COMPLETED_REBUILD_LIST = "completed_rebuild_list"
    REBASE_QUEUE = "rebase_queue"
    BACKPORT_QUEUE = "backport_queue"
    POSTPONED_LIST = "postponed_list"

    @classmethod
    def all_queues(cls) -> set[str]:
        """Return all Redis queue names for operations that need to check all queues"""
        return {queue.value for queue in cls}

    @classmethod
    def input_queues(cls) -> set[str]:
        """Return input queue names that contain Task objects with metadata"""
        return {
            cls.TRIAGE_QUEUE.value,
            cls.REBASE_QUEUE_C9S.value,
            cls.REBASE_QUEUE_C10S.value,
            cls.BACKPORT_QUEUE_C9S.value,
            cls.BACKPORT_QUEUE_C10S.value,
            cls.REBUILD_QUEUE_C9S.value,
            cls.REBUILD_QUEUE_C10S.value,
            cls.CLARIFICATION_NEEDED_QUEUE.value,
            cls.REBASE_QUEUE.value,
            cls.BACKPORT_QUEUE.value,
        }

    @classmethod
    def data_queues(cls) -> set[str]:
        """Return data queue names that contain schema objects"""
        return {
            cls.ERROR_LIST.value,
            cls.OPEN_ENDED_ANALYSIS_LIST.value,
            cls.COMPLETED_REBASE_LIST.value,
            cls.COMPLETED_BACKPORT_LIST.value,
            cls.COMPLETED_REBUILD_LIST.value,
            cls.POSTPONED_LIST.value,
        }

    @classmethod
    def get_rebase_queue_for_branch(cls, target_branch: str | None) -> str:
        """Return appropriate rebase queue based on target branch"""
        if target_branch and cls._use_c9s_branch(target_branch):
            return cls.REBASE_QUEUE_C9S.value
        return cls.REBASE_QUEUE_C10S.value

    @classmethod
    def get_backport_queue_for_branch(cls, target_branch: str | None) -> str:
        """Return appropriate backport queue based on target branch"""
        if target_branch and cls._use_c9s_branch(target_branch):
            return cls.BACKPORT_QUEUE_C9S.value
        return cls.BACKPORT_QUEUE_C10S.value

    @classmethod
    def get_rebuild_queue_for_branch(cls, target_branch: str | None) -> str:
        """Return appropriate rebuild queue based on target branch"""
        if target_branch and cls._use_c9s_branch(target_branch):
            return cls.REBUILD_QUEUE_C9S.value
        return cls.REBUILD_QUEUE_C10S.value

    @classmethod
    def _use_c9s_branch(cls, branch: str) -> bool:
        """Check if branch should use c9s container"""
        branch_lower = branch.lower()
        # use c9s for both RHEL 8 and 9
        return any(pattern in branch_lower for pattern in ["rhel-9", "c9s", "rhel-8", "c8s"])


class JiraLabels(Enum):
    """Constants for Jira labels used by Ymir agents"""

    NEEDS_ATTENTION = "ymir_needs_attention"
    TRIAGED = "ymir_triaged"
    TRIAGE_IN_PROGRESS = "ymir_triage_in_progress"
    TRIAGED_BACKPORT = "ymir_triaged_backport"
    TRIAGED_REBASE = "ymir_triaged_rebase"

    TRIAGED_REBUILD = "ymir_triaged_rebuild"

    REBASED = "ymir_rebased"
    BACKPORTED = "ymir_backported"
    REBUILT = "ymir_rebuilt"
    MERGED = "ymir_merged"

    REBASE_ERRORED = "ymir_rebase_errored"
    BACKPORT_ERRORED = "ymir_backport_errored"
    REBUILD_ERRORED = "ymir_rebuild_errored"
    TRIAGE_ERRORED = "ymir_triage_errored"

    REBASE_FAILED = "ymir_rebase_failed"
    BACKPORT_FAILED = "ymir_backport_failed"
    REBUILD_FAILED = "ymir_rebuild_failed"

    TRIAGED_POSTPONED = "ymir_triaged_postponed"
    TRIAGED_NOT_AFFECTED = "ymir_triaged_not_affected"

    RETRY_NEEDED = "ymir_retry_needed"
    FUSA = "ymir_fusa"

    @classmethod
    def all_labels(cls) -> set[str]:
        """Return all Ymir labels for cleanup operations"""
        return {label.value for label in cls}
