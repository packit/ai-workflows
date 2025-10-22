import logging

from common.utils import init_kerberos_ticket

from .errata_utils import get_erratum_for_link
from .erratum_handler import (
    erratum_all_issues_are_release_pending,
    erratum_needs_attention,
)
from .http_utils import with_http_sessions
from .jira_utils import get_current_issues
from .supervisor_types import ErrataStatus, IssueStatus
from .work_queue import WorkItem, WorkQueue, WorkItemType

logger = logging.getLogger(__name__)

CURRENT_ISSUES_JQL = """
project = RHEL AND AssignedTeam = rhel-jotnar
AND status in ('New', 'In Progress', 'Integration', 'Release Pending')
AND 'Errata Link' is not EMPTY
AND labels != jotnar_needs_attention
"""


@with_http_sessions()
async def collect_and_schedule_work_items(queue: WorkQueue):
    await init_kerberos_ticket()

    logger.info("Getting all relevant issues from JIRA")
    issues = {i.key: i for i in get_current_issues(CURRENT_ISSUES_JQL)}

    erratum_links = set(
        i.errata_link for i in issues.values() if i.errata_link is not None
    )
    errata = [get_erratum_for_link(link) for link in erratum_links]

    work_items = set(
        WorkItem(item_type=WorkItemType.PROCESS_ISSUE, item_data=i.key)
        for i in issues.values()
        if i.status != IssueStatus.RELEASE_PENDING
    ) | set(
        WorkItem(item_type=WorkItemType.PROCESS_ERRATUM, item_data=str(e.id))
        for e in errata
        if (
            (
                e.status == ErrataStatus.NEW_FILES
                or (
                    e.status == ErrataStatus.QE
                    and erratum_all_issues_are_release_pending(e, issues)
                )
            )
            and not erratum_needs_attention(e.id)
        )
    )

    new_work_items = work_items - set(await queue.get_all_work_items())
    await queue.schedule_work_items(new_work_items)

    for new_work_item in new_work_items:
        logger.info("New work item: %s", new_work_item)

    logger.info("Scheduled %d new work items", len(new_work_items))
