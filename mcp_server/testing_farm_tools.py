import asyncio
import logging
import shlex
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

logger = logging.getLogger(__name__)


async def run_testing_farm_test(
    git_url: Annotated[str, Field(description="Git URL to the test repository")],
    git_ref: Annotated[str, Field(description="Git reference to the test repository")],
    path_to_test: Annotated[str, Field(description="Path to the test to run")],
    package: Annotated[str, Field(description="Package URL to be installed in the test environment")],
    compose: Annotated[str, Field(description="Testing Farm compose to use")],
) -> bool:
    """Runs the specified testing-farm test and returns True if the test passed, False otherwise."""

    tmt_prepare = f'--insert --how install --package {shlex.quote(package)}'

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
            logger.info(f"Testing Farm test passed: {stdout.decode()}")
            return True
        else:
            logger.error(f"Testing Farm test failed (exit code {process.returncode}): {stderr.decode()}")
            return False

    except Exception as e:
        logger.error(f"Failed to run testing-farm command: {e}")
        raise ToolError(f"Failed to run testing-farm test: {e}") from e
