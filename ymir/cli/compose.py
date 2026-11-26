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

STOPPABLE_SERVICES = ["triage-cli", "mcp-gateway-cli"]


def detect_compose_cmd() -> list[str]:
    """Detect and return available compose command.

    The Compose containers and execution pods must share one Podman runtime.
    """
    if runtime_path := shutil.which("podman"):
        try:
            subprocess.run(  # noqa: S603
                [runtime_path, "compose", "version"],
                capture_output=True,
                check=True,
            )
            return [runtime_path, "compose"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        if standalone := shutil.which("podman-compose"):
            return [standalone]

    raise RuntimeError("The Compose-backed triage CLI requires Podman with a Compose provider")


@cache
def _compose_base_cmd(compose_file: Path) -> list[str]:
    """Build the base compose command with file and profile flags."""
    cmd = detect_compose_cmd()
    return [*cmd, "-f", str(compose_file), "--profile=cli"]


def start_services(compose_file: Path) -> None:
    """Start the CLI worker and its Compose dependencies."""
    cmd = [*_compose_base_cmd(compose_file), "up", "-d", "triage-cli"]
    subprocess.run(cmd, check=True, cwd=compose_file.parent)  # noqa: S603


def stop_services(compose_file: Path) -> None:
    """Stop the CLI worker and its gateway."""
    cmd = [*_compose_base_cmd(compose_file), "stop", *STOPPABLE_SERVICES]
    logger.info("Stopping infrastructure services: %s", ", ".join(STOPPABLE_SERVICES))
    subprocess.run(cmd, check=True, cwd=compose_file.parent)  # noqa: S603


def submit_triage(compose_file: Path, issue: str, *, dry_run: bool, force_cve_triage: bool) -> str:
    """Submit a triage-only request to the Compose Temporal server and return its JSON."""
    cmd = [
        *_compose_base_cmd(compose_file),
        "exec",
        "-T",
        "triage-cli",
        "python3",
        "-m",
        "ymir.agents.temporal_triage_admin",
        "submit",
        issue,
        "--wait",
        "--user-triggered",
        "--triage-only",
    ]
    if dry_run:
        cmd.append("--dry-run")
    if force_cve_triage:
        cmd.append("--force-cve-triage")
    return subprocess.run(cmd, check=True, cwd=compose_file.parent, stdout=subprocess.PIPE, text=True).stdout  # noqa: S603
