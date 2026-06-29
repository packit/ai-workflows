from datetime import UTC, datetime, timedelta
from enum import Enum

BREWHUB_URL = "https://brewhub.engineering.redhat.com/brewhub"
CENTOS_STREAM_KOJIHUB_URL = "https://kojihub.stream.centos.org/kojihub"

JIRA_SEARCH_PATH = "rest/api/3/search/jql"

# Compares correctly - all our dates are tz-aware
DATETIME_MIN_UTC = datetime.min.replace(tzinfo=UTC)
# Groups within the redhat organization where we can find issues
GITLAB_GROUPS = ["rhel/rpms", "centos-stream/rpms"]
# Timeout for post-push testing (e.g., CAT tests) after stage push completes
POST_PUSH_TESTING_TIMEOUT = timedelta(hours=3)
POST_PUSH_TESTING_TIMEOUT_STR = "3 hours"


class RedisQueues(Enum):
    """Constants for Redis queue names used by Ymir agents"""

    TRIAGE_QUEUE = "triage_queue"
    # Priority twin of TRIAGE_QUEUE for ymir_todo-triggered tasks. The triage
    # agent BRPOPs from [TRIAGE_QUEUE_TODO, TRIAGE_QUEUE] so Redis serves the
    # priority queue first whenever it has anything.
    TRIAGE_QUEUE_TODO = "triage_queue_todo"
    REBASE_QUEUE_C9S = "rebase_queue_c9s"
    REBASE_QUEUE_C10S = "rebase_queue_c10s"
    BACKPORT_QUEUE_C9S = "backport_queue_c9s"
    BACKPORT_QUEUE_C10S = "backport_queue_c10s"
    # Priority twins of the downstream queues for ymir_todo-triggered tasks. Each
    # downstream agent BRPOPs from [<queue>_todo, <queue>] so Redis serves the
    # priority queue first whenever it has anything.
    REBASE_QUEUE_C9S_TODO = "rebase_queue_c9s_todo"
    REBASE_QUEUE_C10S_TODO = "rebase_queue_c10s_todo"
    BACKPORT_QUEUE_C9S_TODO = "backport_queue_c9s_todo"
    BACKPORT_QUEUE_C10S_TODO = "backport_queue_c10s_todo"
    CLARIFICATION_NEEDED_QUEUE = "clarification_needed_queue"
    ERROR_LIST = "error_list"
    OPEN_ENDED_ANALYSIS_LIST = "open_ended_analysis_list"
    COMPLETED_REBASE_LIST = "completed_rebase_list"
    COMPLETED_BACKPORT_LIST = "completed_backport_list"
    REBUILD_QUEUE_C9S = "rebuild_queue_c9s"
    REBUILD_QUEUE_C10S = "rebuild_queue_c10s"
    REBUILD_QUEUE_C9S_TODO = "rebuild_queue_c9s_todo"
    REBUILD_QUEUE_C10S_TODO = "rebuild_queue_c10s_todo"
    COMPLETED_REBUILD_LIST = "completed_rebuild_list"
    REBASE_QUEUE = "rebase_queue"
    BACKPORT_QUEUE = "backport_queue"
    POSTPONED_LIST = "postponed_list"
    # Redis Hash for MR consolidation queue (not a list — uses hash fields
    # with at-most-one-active/one-pending semantics per package-branch pair).
    MERGE_CONSOLIDATION_QUEUE = "merge_consolidation_queue"

    @classmethod
    def all_queues(cls) -> set[str]:
        """Return all Redis list queue names (excludes Hash-based keys like the consolidation queue)."""
        return {q.value for q in cls if q is not cls.MERGE_CONSOLIDATION_QUEUE}

    @classmethod
    def input_queues(cls) -> set[str]:
        """Return input queue names that contain Task objects with metadata"""
        return {
            cls.TRIAGE_QUEUE.value,
            cls.TRIAGE_QUEUE_TODO.value,
            cls.REBASE_QUEUE_C9S.value,
            cls.REBASE_QUEUE_C10S.value,
            cls.BACKPORT_QUEUE_C9S.value,
            cls.BACKPORT_QUEUE_C10S.value,
            cls.REBUILD_QUEUE_C9S.value,
            cls.REBUILD_QUEUE_C10S.value,
            cls.REBASE_QUEUE_C9S_TODO.value,
            cls.REBASE_QUEUE_C10S_TODO.value,
            cls.BACKPORT_QUEUE_C9S_TODO.value,
            cls.BACKPORT_QUEUE_C10S_TODO.value,
            cls.REBUILD_QUEUE_C9S_TODO.value,
            cls.REBUILD_QUEUE_C10S_TODO.value,
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
    def priority_twin(cls, queue: str) -> str:
        """Return the priority (_todo) twin for an input queue (ymir_todo tasks)."""
        return f"{queue}_todo"

    @classmethod
    def get_rebase_queue_for_branch(cls, target_branch: str | None, user_triggered: bool = False) -> str:
        """Return rebase queue for the branch; the priority twin if user-triggered."""
        base = (
            cls.REBASE_QUEUE_C9S.value
            if target_branch and cls._use_c9s_branch(target_branch)
            else cls.REBASE_QUEUE_C10S.value
        )
        return cls.priority_twin(base) if user_triggered else base

    @classmethod
    def get_backport_queue_for_branch(cls, target_branch: str | None, user_triggered: bool = False) -> str:
        """Return backport queue for the branch; the priority twin if user-triggered."""
        base = (
            cls.BACKPORT_QUEUE_C9S.value
            if target_branch and cls._use_c9s_branch(target_branch)
            else cls.BACKPORT_QUEUE_C10S.value
        )
        return cls.priority_twin(base) if user_triggered else base

    @classmethod
    def get_rebuild_queue_for_branch(cls, target_branch: str | None, user_triggered: bool = False) -> str:
        """Return rebuild queue for the branch; the priority twin if user-triggered."""
        base = (
            cls.REBUILD_QUEUE_C9S.value
            if target_branch and cls._use_c9s_branch(target_branch)
            else cls.REBUILD_QUEUE_C10S.value
        )
        return cls.priority_twin(base) if user_triggered else base

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

    MR_CONSOLIDATED = "ymir_consolidated"

    CONSOLIDATE_BASE = "ymir_consolidate_base"
    CONSOLIDATE_NEXT = "ymir_consolidate_next"

    # Maintainer-facing trigger: when a Red Hat Employee adds this label to a CVE
    # issue, the fetcher enqueues it for an e2e run and swaps the label for
    # TRIAGE_IN_PROGRESS on enqueue.
    TODO = "ymir_todo"

    @classmethod
    def all_labels(cls) -> set[str]:
        """Return all Ymir labels for cleanup operations"""
        return {label.value for label in cls}
