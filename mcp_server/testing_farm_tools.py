import asyncio
import logging
import re
import shlex
from typing import Annotated, Tuple

from fastmcp.exceptions import ToolError
from pydantic import Field

from common.utils import init_kerberos_ticket, KerberosError

logger = logging.getLogger(__name__)

async def get_copr_repo(issue: Annotated[str, Field(description="Jira issue to get the Copr repo for")]) -> str:
    """Gets the Copr repo for the package"""
    try:
        principal = await init_kerberos_ticket()
    except KerberosError as e:
        raise ToolError(f"Failed to initialize Kerberos ticket: {e}") from e
    copr_user = principal.split("@", maxsplit=1)[0]
    return f"copr.devel.redhat.com/{copr_user}/{issue}"

async def get_compose_from_branch(dist_git_branch: Annotated[str, Field(description="Branch to get the compose from")]) -> str:
    """Gets the compose from the branch"""
    if dist_git_branch == "rhel-8.10":
        # There is one more .0 needed only for the RHEL 8.10 compose (not for 10.X branches)
        return "RHEL-8.10.0-Nightly"
    elif dist_git_branch.startswith("rhel-"):
        return dist_git_branch.upper() + "-Nightly"
    else:
        match = re.match(r'^c(\d+)s$', dist_git_branch)
        if match:
            number = match.group(1)
            return f"CentOS-Stream-{number}"
        else:
            raise ToolError(f"Invalid branch format, can't get compose from branch: {dist_git_branch}")

async def run_testing_farm_test(
    git_url: Annotated[str, Field(description="Git URL to the test repository")],
    git_ref: Annotated[str, Field(description="Git reference to the test repository")],
    path_to_test: Annotated[str, Field(description="Path to the test to run")],
    package: Annotated[str, Field(description="Package URL to be installed in the test environment")],
    dist_git_branch: Annotated[str, Field(description="Dist Git branch to use to get the compose")],
) -> Tuple[bool, str]:
    """Runs the specified testing-farm test and returns True if the test passed, False otherwise."""

    tmt_prepare = f'--insert --how install --package {shlex.quote(package)}'
    compose = await get_compose_from_branch(dist_git_branch)

    # Build the command arguments
    cmd = [
        "testing-farm",
        "request",
        "--tmt-prepare", tmt_prepare,
        "--compose", compose,
        "--git-ref", git_ref,
        "--git-url", git_url,
        "--test", path_to_test,
    ]

    logger.info(f"Running testing-farm command: {' '.join(shlex.quote(arg) for arg in cmd)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            msg = f"Testing Farm test passed: \n stdout {'='*60}\n {stdout.decode()}\n {'='*60}\n stderr {'='*60}\n {stderr.decode()}\n {'='*60}"
            logger.info(msg)
            return True, msg
        else:
            msg = f"Testing Farm test failed (exit code {process.returncode}): \n stdout {'='*60}\n {stdout.decode()}\n {'='*60}\n stderr {'='*60}\n {stderr.decode()}\n {'='*60}"
            msg += f"\n Ran command: {' '.join(shlex.quote(arg) for arg in cmd)}"
            logger.error(msg)
            return False, msg

    except Exception as e:
        logger.error(f"Failed to run testing-farm command: {e}")
        raise ToolError(f"Failed to run testing-farm test: {e}") from e
