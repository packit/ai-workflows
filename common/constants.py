from enum import Enum

class RedisQueues(Enum):
    """Constants for Redis queue names used by Jotnar agents"""
    TRIAGE_QUEUE = "triage_queue"
    REBASE_QUEUE_C9S = "rebase_queue_c9s"
    REBASE_QUEUE_C10S = "rebase_queue_c10s"
    BACKPORT_QUEUE_C9S = "backport_queue_c9s"
    BACKPORT_QUEUE_C10S = "backport_queue_c10s"
    CLARIFICATION_NEEDED_QUEUE = "clarification_needed_queue"
    ERROR_LIST = "error_list"
    NO_ACTION_LIST = "no_action_list"
    COMPLETED_REBASE_LIST = "completed_rebase_list"
    COMPLETED_BACKPORT_LIST = "completed_backport_list"
    REBASE_QUEUE = "rebase_queue"
    BACKPORT_QUEUE = "backport_queue"

    @classmethod
    def all_queues(cls) -> set[str]:
        """Return all Redis queue names for operations that need to check all queues"""
        return {queue.value for queue in cls}

    @classmethod
    def input_queues(cls) -> set[str]:
        """Return input queue names that contain Task objects with metadata"""
        return {cls.TRIAGE_QUEUE.value, cls.REBASE_QUEUE_C9S.value, cls.REBASE_QUEUE_C10S.value,
                cls.BACKPORT_QUEUE_C9S.value, cls.BACKPORT_QUEUE_C10S.value, cls.CLARIFICATION_NEEDED_QUEUE.value,
                cls.REBASE_QUEUE.value, cls.BACKPORT_QUEUE.value}

    @classmethod
    def data_queues(cls) -> set[str]:
        """Return data queue names that contain schema objects"""
        return {cls.ERROR_LIST.value,
                cls.NO_ACTION_LIST.value, cls.COMPLETED_REBASE_LIST.value,
                cls.COMPLETED_BACKPORT_LIST.value}

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
    def _use_c9s_branch(cls, branch: str) -> bool:
        """Check if branch should use c9s container"""
        branch_lower = branch.lower()
        # use c9s for both RHEL 8 and 9
        return any(pattern in branch_lower for pattern in ['rhel-9', 'c9s', 'rhel-8', 'c8s'])


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
