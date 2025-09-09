from .supervisor_types import (
    WorkflowResult,
)


WAIT_DELAY = 20 * 60  # 20 minutes


class BaseWorkflow(object):
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def resolve_remove_task(self, why: str):
        return WorkflowResult(status=why, reschedule_in=-1)

    def resolve_wait(self, why: str):
        return WorkflowResult(status=why, reschedule_in=WAIT_DELAY)
