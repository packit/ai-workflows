import logging
from pathlib import Path

from pydantic import BaseModel, Field

from ymir.common.models import LogOutputSchema

logger = logging.getLogger(__name__)


class PackageUpdateState(BaseModel):
    jira_issue: str | None
    package: str
    dist_git_branch: str
    local_clone: Path | None = Field(default=None)
    update_branch: str | None = Field(default=None)
    fork_url: str | None = Field(default=None)
    build_error: str | None = Field(default=None)
    log_result: LogOutputSchema | None = Field(default=None)
    merge_request_url: str | None = Field(default=None)
    merge_request_newly_created: bool = Field(default=False)  # was the MR newly created?


class PackageUpdateStep:
    """
    Steps for package update operations (backport and rebase steps).

    A place where to share common steps between backport and rebase workflows.
    """
