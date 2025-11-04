import asyncio
import logging
import os
import re

from attr import dataclass
import typer

from agents.observability import setup_observability
from common.utils import init_kerberos_ticket
from .collect import collect_and_schedule_work_items
from .errata_utils import get_erratum
from .erratum_handler import (
    ErratumHandler,
)
from .issue_handler import IssueHandler
from .jira_utils import get_issue
from .http_utils import with_http_sessions
from .work_queue import WorkQueue, WorkItemType, work_queue

logger = logging.getLogger(__name__)


app = typer.Typer()


@dataclass
class State:
    dry_run: bool = False
    ignore_needs_attention: bool = False


app_state = State()


def check_env(
    chat: bool = False,
    jira: bool = False,
    redis: bool = False,
    gitlab: bool = False,
    testing_farm: bool = False,
):
    required_vars = []
    if chat:
        required_vars.append(
            ("CHAT_MODEL", "name of model to use (e.g., gemini:gemini-2.5-pro)")
        )
    if jira:
        required_vars.append(
            ("JIRA_TOKEN", "Jira authentication token"),
        )
    if redis:
        required_vars.append(
            ("REDIS_URL", "Redis connection URL (e.g., redis://localhost:6379)")
        )
    if gitlab:
        required_vars.append(("GITLAB_TOKEN", "Gitlab authentication token"))
    if testing_farm:
        required_vars.append(("TESTING_FARM_API_TOKEN", "Testing Farm API token"))

    missing_vars = [var for var in required_vars if not os.getenv(var[0])]

    if missing_vars:
        logger.error(
            f"Missing required environment variables: {', '.join(var[0] for var in missing_vars)}"
        )
        logger.info("Required environment variables:")
        for var in missing_vars:
            logger.info(f"  {var[0]} - {var[1]}")
        raise typer.Exit(1)


async def do_collect(repeat: bool, repeat_delay: int):
    async with work_queue(os.environ["REDIS_URL"]) as queue:
        while repeat:
            try:
                await collect_and_schedule_work_items(queue)
            except Exception:
                logger.exception("Error while collecting work items")
            await asyncio.sleep(repeat_delay)
        else:
            await collect_and_schedule_work_items(queue)


@app.command()
def collect(
    repeat: bool = typer.Option(True),
    repeat_delay: int = typer.Option(1200, "--repeat-delay"),
):
    check_env(jira=True, redis=True)

    asyncio.run(do_collect(repeat, repeat_delay))


async def do_clear_queue():
    async with work_queue(os.environ["REDIS_URL"]) as queue:
        await queue.remove_all_work_items()
        logger.info("Cleared the work item queue")


@app.command()
def clear_queue():
    check_env(redis=True)

    asyncio.run(do_clear_queue())


@with_http_sessions()
async def process_once(queue: WorkQueue):
    work_item = await queue.wait_first_ready_work_item()

    # Calling this on every work item is a little inefficient, but it makes
    # sure that we'll get a new ticket if the old one expires.
    await init_kerberos_ticket()

    if work_item.item_type == WorkItemType.PROCESS_ISSUE:
        issue = get_issue(work_item.item_data, full=True)
        result = await IssueHandler(
            issue,
            dry_run=app_state.dry_run,
            ignore_needs_attention=app_state.ignore_needs_attention,
        ).run()
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
        result = await ErratumHandler(
            erratum,
            dry_run=app_state.dry_run,
            ignore_needs_attention=app_state.ignore_needs_attention,
        ).run()
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
    check_env(chat=True, jira=True, redis=True, testing_farm=True)

    asyncio.run(do_process(repeat))


@with_http_sessions()
async def do_process_issue(key: str):
    await init_kerberos_ticket()

    issue = get_issue(key, full=True)
    result = await IssueHandler(
        issue,
        dry_run=app_state.dry_run,
        ignore_needs_attention=app_state.ignore_needs_attention,
    ).run()
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
    check_env(chat=True, jira=True, testing_farm=True)

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


@with_http_sessions()
async def do_process_erratum(id: str):
    await init_kerberos_ticket()

    erratum = get_erratum(id)
    result = await ErratumHandler(
        erratum,
        dry_run=app_state.dry_run,
        ignore_needs_attention=app_state.ignore_needs_attention,
    ).run()

    logger.info(
        "Erratum %s (%s) processed, status=%s, reschedule_in=%s",
        erratum.url,
        erratum.full_advisory,
        result.status,
        result.reschedule_in if result.reschedule_in >= 0 else "never",
    )


@app.command()
def process_erratum(id_or_url: str):
    check_env(chat=True, jira=True)

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
    dry_run: bool = typer.Option(False, help="Don't actually change anything."),
    ignore_needs_attention: bool = typer.Option(
        False, help="Process issues or errata flagged with jotnar_needs_attention."
    ),
):
    if debug:
        logging.basicConfig(level=logging.DEBUG)
        # requests_gssapi is very noisy at DEBUG level
        logging.getLogger("requests_gssapi").setLevel(logging.INFO)
    else:
        logging.basicConfig(level=logging.INFO)

    app_state.dry_run = dry_run
    app_state.ignore_needs_attention = ignore_needs_attention

    collector_endpoint = os.environ.get("COLLECTOR_ENDPOINT")
    if collector_endpoint is not None:
        setup_observability(collector_endpoint)


if __name__ == "__main__":
    app()
