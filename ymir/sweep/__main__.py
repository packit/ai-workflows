"""Entry point for postponed-issue sweeps: ``python -m ymir.sweep``.

Supports running a single strategy (``--strategy dependency``) for
per-strategy CronJobs, or all strategies in sequence (``--all``) for a
combined CronJob or local development.

The sweep runs inside ``asyncio.run()`` so that both the HTTP session
ContextVar (required by ``jira_utils``) and async buildroot checks
(``check_build_in_buildroot``) are handled in the same event loop.
"""

import argparse
import asyncio
import logging
import os
import sys

from ymir.common.base_utils import redis_client
from ymir.supervisor.http_utils import with_requests_session
from ymir.sweep.dependency import DependencySweep
from ymir.sweep.no_patch import NoPatchSweep
from ymir.sweep.pr_pending import PRPendingSweep
from ymir.sweep.y_stream import YStreamSweep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

STRATEGIES: dict = {
    "dependency": DependencySweep,
    "y_stream": YStreamSweep,
    "pr_pending": PRPendingSweep,
    "no_patch": NoPatchSweep,
}


async def run_sweep(strategy_names: list[str]) -> None:
    """Run the specified sweep strategies in sequence.

    Sets up a shared ``requests.Session`` (required by ``jira_utils``)
    and creates a single synchronous Redis connection shared across all
    strategies.  Strategies run sequentially to avoid concurrent Jira
    API pressure.
    """
    async with with_requests_session(), redis_client(os.environ["REDIS_URL"]) as redis:
        for name in strategy_names:
            strategy = STRATEGIES[name]()
            logger.info("Starting %s sweep", name)
            summary = await strategy.run(redis)
            logger.info(
                "%s sweep result: %s",
                name,
                ", ".join(f"{k}={v}" for k, v in summary.items()),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run postponed-issue sweep")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--strategy",
        choices=STRATEGIES.keys(),
        help="Run a single strategy",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Run all strategies in sequence",
    )
    args = parser.parse_args()

    if args.all:
        asyncio.run(run_sweep(list(STRATEGIES.keys())))
    else:
        asyncio.run(run_sweep([args.strategy]))
