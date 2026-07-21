"""Container management helpers for the ymir CLI.

Detects the available compose tool (podman compose / podman-compose)
and provides functions to start, stop, and check infrastructure
services required by the triage workflow.
"""

import logging
import shutil
import subprocess
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)

STOPPABLE_SERVICES = ["mcp-gateway-cli"]
INFRASTRUCTURE_SERVICES = [*STOPPABLE_SERVICES, "trace-server"]


def detect_compose_cmd() -> list[str]:
    """Detect and return available compose command.

    Mirrors the detection logic in the Makefile:
    podman compose > podman-compose > docker compose > docker-compose
    """
    for runtime in ("podman", "docker"):
        runtime_path = shutil.which(runtime)
        if not runtime_path:
            continue
        try:
            subprocess.run(  # noqa: S603
                [runtime_path, "compose", "version"],
                capture_output=True,
                check=True,
            )
            return [runtime_path, "compose"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        if standalone := shutil.which(f"{runtime}-compose"):
            return [standalone]

    raise RuntimeError(
        "No compose tool found. Install one of: "
        "podman compose, podman-compose, docker compose, docker-compose"
    )


@cache
def _compose_base_cmd(compose_file: Path) -> list[str]:
    """Build the base compose command with file and profile flags."""
    cmd = detect_compose_cmd()
    return [*cmd, "-f", str(compose_file), "--profile=cli"]


def start_services(compose_file: Path) -> None:
    """Start infrastructure containers required by the CLI.

    Uses the ``cli`` compose profile which starts ``mcp-gateway-cli``
    (with ``keep-id`` UID mapping) instead of ``mcp-gateway``.
    """
    cmd = [
        *_compose_base_cmd(compose_file),
        "up",
        "-d",
        "--force-recreate",
        *INFRASTRUCTURE_SERVICES,
    ]
    logger.info("Starting infrastructure services: %s", ", ".join(INFRASTRUCTURE_SERVICES))
    subprocess.run(cmd, check=True, cwd=compose_file.parent)  # noqa: S603


def stop_services(compose_file: Path) -> None:
    """Stop infrastructure containers."""
    cmd = [*_compose_base_cmd(compose_file), "stop", *STOPPABLE_SERVICES]
    logger.info("Stopping infrastructure services: %s", ", ".join(STOPPABLE_SERVICES))
    logger.warning("The trace-server container is still running, so you can inspect collected traces.")
    subprocess.run(cmd, check=True, cwd=compose_file.parent)  # noqa: S603


def run_agent(
    compose_file: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run the triage agent as a one-shot container via ``compose run --rm``.

    Environment variables in *env* are forwarded to the container with
    ``-e`` flags.  Output streams directly to the terminal.
    """
    cmd = [*_compose_base_cmd(compose_file), "run", "--rm"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd.append("triage-cli")
    logger.info("Running agent container: triage-cli")
    return subprocess.run(cmd, check=True, cwd=compose_file.parent)  # noqa: S603
