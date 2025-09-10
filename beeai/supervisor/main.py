import asyncio
import logging
import os
import re

from attr import dataclass
import typer

from agents.observability import setup_observability
from .errata_utils import get_errata_info, get_errata_info_for_link
from .erratum_workflow import ErratumWorkflow, erratum_needs_attention
from .issue_workflow import IssueWorkflow
from .jira_utils import get_current_issues, get_issue
from .supervisor_types import ErrataStatus, IssueStatus
from .work_queue import WorkItem, WorkQueue, WorkItemType, work_queue

logger = logging.getLogger(__name__)


app = typer.Typer()


@dataclass
class State:
    dry_run: bool = False


app_state = State()


async def collect_once(queue: WorkQueue):
    logger.info("Getting all relevant issues from JIRA")
    issues = [i for i in get_current_issues()]

    errata_links = set(i.errata_link for i in issues if i.errata_link is not None)
    errata = [get_errata_info_for_link(link) for link in errata_links]

    work_items = set(
        WorkItem(item_type=WorkItemType.PROCESS_ISSUE, item_data=i.key)
        for i in issues
        if i.status != IssueStatus.RELEASE_PENDING
    ) | set(
        WorkItem(item_type=WorkItemType.PROCESS_ERRATUM, item_data=str(e.id))
        for e in errata
        if (
            (
                e.status == ErrataStatus.NEW_FILES
                or (e.status == ErrataStatus.QE and e.all_issues_release_pending)
            )
            and not erratum_needs_attention(e.id)
        )
    )

    new_work_items = work_items - set(await queue.get_all_work_items())
    await queue.schedule_work_items(new_work_items)

    for new_work_item in new_work_items:
        logger.info("New work item: %s", new_work_item)

    logger.info("Scheduled %d new work items", len(new_work_items))


async def do_collect(repeat: bool, repeat_delay: int):
    async with work_queue(os.environ["REDIS_URL"]) as queue:
        while repeat:
            try:
                await collect_once(queue)
            except Exception:
                logger.exception("Error while collecting work items")
            await asyncio.sleep(repeat_delay)


@app.command()
def collect(
    repeat: bool = typer.Option(True, "--repeat"),
    repeat_delay=typer.Option(1200, "--repeat-delay"),
):
    asyncio.run(do_collect(repeat, repeat_delay))


async def execute_once(queue: WorkQueue):
    work_item = await queue.wait_first_ready_work_item()
    if work_item.item_type == WorkItemType.PROCESS_ISSUE:
        issue = get_issue(work_item.item_data, full=True)
        result = await IssueWorkflow(issue, dry_run=app_state.dry_run).run()
        if result.reschedule_in >= 0:
            await queue.schedule_work_items([work_item], delay=result.reschedule_in)
        else:
            await queue.remove_work_items([work_item])

        logger.info(
            "Issue %s processed, status=%s, reschedule_in=%s",
            issue.url,
            result.status,
            result.reschedule_in if result.reschedule_in >= 0 else "never",
        )
    elif work_item.item_type == WorkItemType.PROCESS_ERRATUM:
        erratum = get_errata_info(work_item.item_data)
        result = await ErratumWorkflow(erratum, dry_run=app_state.dry_run).run()
        if result.reschedule_in >= 0:
            await queue.schedule_work_items([work_item], delay=result.reschedule_in)
        else:
            await queue.remove_work_items([work_item])

        logger.info(
            "Errata %s (%s) processed, status=%s, reschedule_in=%s",
            erratum.url,
            erratum.full_advisory,
            result.status,
            result.reschedule_in if result.reschedule_in >= 0 else "never",
        )
    else:
        logger.warning("Unknown work item type: %s", work_item)


async def do_execute(repeat: bool):
    async with work_queue(os.environ["REDIS_URL"]) as queue:
        while repeat:
            try:
                await execute_once(queue)
            except Exception:
                logger.exception("Error while executing work item")
                await asyncio.sleep(60)
        else:
            await execute_once(queue)


@app.command()
def execute(repeat: bool = typer.Option(True)):
    asyncio.run(do_execute(repeat))


async def do_process_issue(key: str):
    assert app_state.dry_run
    issue = get_issue(key, full=True)
    result = await IssueWorkflow(issue, dry_run=app_state.dry_run).run()
    logger.info(
        "Issue %s processed, status=%s, reschedule_in=%s",
        key,
        result.status,
        result.reschedule_in if result.reschedule_in >= 0 else "never",
    )


@app.command()
def process_issue(
    key_or_url: str,
):
    if key_or_url.startswith("http"):
        m = re.match(r"https://issues.redhat.com/browse/([^/?]+)(?:\?.*)?$", key_or_url)
        if m is None:
            raise typer.BadParameter(f"Invalid issue URL {key_or_url}")
        key = m.group(1)
    else:
        key = key_or_url

    if not key.startswith("RHEL-"):
        raise typer.BadParameter("Issue must be in the RHEL project")

    asyncio.run(do_process_issue(key))


async def do_process_erratum(id: str):
    erratum = get_errata_info(id)
    result = await ErratumWorkflow(erratum, dry_run=app_state.dry_run).run()

    logger.info(
        "Erratum %s (%s) processed, status=%s, reschedule_in=%s",
        erratum.url,
        erratum.full_advisory,
        result.status,
        result.reschedule_in if result.reschedule_in >= 0 else "never",
    )


@app.command()
def process_erratum(id_or_url: str):
    if id_or_url.startswith("http"):
        m = re.match(
            r"https://errata.engineering.redhat.com/advisory/(\d+)$", id_or_url
        )
        if m is None:
            raise typer.BadParameter(f"Invalid advisory URL {id_or_url}")
        id = m.group(1)
    else:
        id = id_or_url

    asyncio.run(do_process_erratum(id))


@app.callback()
def main(
    debug: bool = typer.Option(False, help="Enable debug mode."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Don't actually change anything."
    ),
):
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    app_state.dry_run = dry_run

    collector_endpoint = os.environ.get("COLLECTOR_ENDPOINT")
    if collector_endpoint is not None:
        setup_observability(collector_endpoint)


if __name__ == "__main__":
    app()
