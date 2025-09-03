from enum import Enum

class RedisQueues(Enum):
    """Constants for Redis queue names used by Jotnar agents"""
    TRIAGE_QUEUE = "triage_queue"
    REBASE_QUEUE = "rebase_queue"
    BACKPORT_QUEUE = "backport_queue"
    CLARIFICATION_NEEDED_QUEUE = "clarification_needed_queue"
    ERROR_LIST = "error_list"
    NO_ACTION_LIST = "no_action_list"
    COMPLETED_REBASE_LIST = "completed_rebase_list"
    COMPLETED_BACKPORT_LIST = "completed_backport_list"

    @classmethod
    def all_queues(cls) -> set[str]:
        """Return all Redis queue names for operations that need to check all queues"""
        return {queue.value for queue in cls}

    @classmethod
    def input_queues(cls) -> set[str]:
        """Return input queue names that contain Task objects with metadata"""
        return {cls.TRIAGE_QUEUE.value, cls.REBASE_QUEUE.value, cls.BACKPORT_QUEUE.value}

    @classmethod
    def data_queues(cls) -> set[str]:
        """Return data queue names that contain schema objects"""
        return {cls.CLARIFICATION_NEEDED_QUEUE.value, cls.ERROR_LIST.value,
                cls.NO_ACTION_LIST.value, cls.COMPLETED_REBASE_LIST.value,
                cls.COMPLETED_BACKPORT_LIST.value}


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
