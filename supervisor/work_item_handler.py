from abc import abstractmethod, ABC

from .supervisor_types import (
    WorkflowResult,
)


WAIT_DELAY = 20 * 60  # 20 minutes


class WorkItemHandler(ABC):
    def __init__(self, dry_run: bool = False, ignore_needs_attention: bool = False):
        self.dry_run = dry_run
        self.ignore_needs_attention = ignore_needs_attention

    def resolve_remove_work_item(self, why: str):
        return WorkflowResult(status=why, reschedule_in=-1)

    def resolve_wait(self, why: str, *, reschedule_in: float = WAIT_DELAY):
        return WorkflowResult(
            status=why,
            reschedule_in=reschedule_in,
        )

    @abstractmethod
    async def run(self) -> WorkflowResult:
        raise NotImplementedError("Subclasses must implement run()")
