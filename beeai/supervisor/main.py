import asyncio
import logging
import os
import re

from attr import dataclass
import typer

from agents.observability import setup_observability
from .errata_utils import get_erratum, get_erratum_for_link
from .erratum_handler import ErratumHandler
from .issue_handler import IssueHandler
from .jira_utils import get_current_issues, get_issue
from .supervisor_types import ErrataStatus, IssueStatus
from .work_queue import WorkItem, WorkQueue, WorkItemType, work_queue

logger = logging.getLogger(__name__)


app = typer.Typer()


@dataclass
class State:
    dry_run: bool = False


app_state = State()


def check_env(chat: bool = False, jira: bool = False, redis: bool = False):
    required_vars = []
    if chat:
        required_vars.append(
            ("CHAT_MODEL", "name of model to use (e.g., gemini:gemini-2.5-pro)")
        )
    if jira:
        required_vars.extend(
            [
                ("JIRA_URL", "Jira instance URL (e.g., https://issues.redhat.com)"),
                ("JIRA_TOKEN", "Jira authentication token"),
            ]
        )
    if redis:
        required_vars.append(
            ("REDIS_URL", "Redis connection URL (e.g., redis://localhost:6379)")
        )

    missing_vars = [var for var in required_vars if not os.getenv(var[0])]

    if missing_vars:
        logger.error(
            f"Missing required environment variables: {', '.join(var[0] for var in missing_vars)}"
        )
        logger.info("Required environment variables:")
        for var in missing_vars:
            logger.info(f"  {var[0]} - {var[1]}")
        raise typer.Exit(1)


async def collect_once(queue: WorkQueue):
    logger.info("Getting all relevant issues from JIRA")
    issues = [i for i in get_current_issues()]

    erratum_links = set(i.errata_link for i in issues if i.errata_link is not None)
    errata = [get_erratum_for_link(link) for link in erratum_links]

    work_items = set(
        WorkItem(item_type=WorkItemType.PROCESS_ISSUE, item_data=i.key)
        for i in issues
        if i.status != IssueStatus.RELEASE_PENDING
    ) | set(
        WorkItem(item_type=WorkItemType.PROCESS_ERRATUM, item_data=str(e.id))
        for e in errata
        if (
            e.status == ErrataStatus.NEW_FILES
            or (e.status == ErrataStatus.QE and e.all_issues_release_pending)
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
        else:
            await collect_once(queue)


@app.command()
def collect(
    repeat: bool = typer.Option(True),
    repeat_delay: int = typer.Option(1200, "--repeat-delay"),
):
    check_env(jira=True, redis=True)

    asyncio.run(do_collect(repeat, repeat_delay))


async def process_once(queue: WorkQueue):
    work_item = await queue.wait_first_ready_work_item()
    if work_item.item_type == WorkItemType.PROCESS_ISSUE:
        issue = get_issue(work_item.item_data)
        result = await IssueHandler(issue, dry_run=app_state.dry_run).run()
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
        erratum = get_erratum(work_item.item_data)
        result = await ErratumHandler(erratum, dry_run=app_state.dry_run).run()
        if result.reschedule_in >= 0:
            await queue.schedule_work_items([work_item], delay=result.reschedule_in)
        else:
            await queue.remove_work_items([work_item])

        logger.info(
            "Erratum %s (%s) processed, status=%s, reschedule_in=%s",
            erratum.url,
            erratum.full_advisory,
            result.status,
            result.reschedule_in if result.reschedule_in >= 0 else "never",
        )
    else:
        logger.warning("Unknown work item type: %s", work_item)


async def do_process(repeat: bool):
    async with work_queue(os.environ["REDIS_URL"]) as queue:
        while repeat:
            try:
                await process_once(queue)
            except Exception:
                logger.exception("Error while processing work item")
                await asyncio.sleep(60)
        else:
            await process_once(queue)


@app.command()
def process(repeat: bool = typer.Option(True)):
    check_env(chat=True, jira=True, redis=True)

    asyncio.run(do_process(repeat))


async def do_process_issue(key: str):
    issue = get_issue(key)
    result = await IssueHandler(issue, dry_run=app_state.dry_run).run()
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
    check_env(chat=True, jira=True)

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
    check_env(chat=True, jira=True)

    erratum = get_erratum(id)
    result = await ErratumHandler(erratum, dry_run=app_state.dry_run).run()

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
        # requests_gssapi is very noisy at DEBUG level
        logging.getLogger("requests_gssapi").setLevel(logging.INFO)
    else:
        logging.basicConfig(level=logging.INFO)

    app_state.dry_run = dry_run

    collector_endpoint = os.environ.get("COLLECTOR_ENDPOINT")
    if collector_endpoint is not None:
        setup_observability(collector_endpoint)


if __name__ == "__main__":
    app()
