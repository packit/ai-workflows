import asyncio
import time
import re
import os

from fastmcp import Context
from copr.v3 import BuildProxy, ProjectProxy
from typing import Annotated

from pydantic import Field
from utils import init_kerberos_ticket

class CoprToolException(Exception):
    pass

MAX_BUILD_TIME_SECONDS = 3600
OWNER = "jotnar-bot"
COPR_URL = "https://copr.devel.redhat.com"
PRESERVE_PROJECT = 14 # None|-1|any number of days
COPR_CONFIG = {
    'copr_url': COPR_URL,
    'username': OWNER,
    'gssapi': True,
}
DEFAULT_DESCRIPTION = (
    "Builds initiated by the jotnar-bot.\n"
)
DEFAULT_INSTRUCTIONS = (
    "This copr project is created and handled by the Jötnar project."
)


def _extract_package_name_regex(srpm_path: str) -> str | None:
    """Extract package name using regex pattern."""
    filename = os.path.basename(srpm_path)
    # Pattern: name-version-release.src.rpm
    match = re.match(r'^(.*)-[^-]+-[^-]+\.src\.rpm$', filename)
    if match:
        return match.group(1)
    return None


def _get_build_urls(copr_build_url: str, chroots: list[str], build_id: str, srpm_path: str) -> list[str]:
    """
    Get the URLs of the builds for all given chroots.
    """
    package_name = _extract_package_name_regex(srpm_path)
    urls = []
    for chroot in chroots:
        urls.append(f"{copr_build_url}/{chroot}/{int(build_id):08d}-{package_name}")
    return urls


async def build_package(
    project: Annotated[str, Field(description="Project name, e.g. 'RHEL-12345'")],
    chroots: Annotated[list[str], Field(description="List of chroots to build in")],
    srpm_path: Annotated[str, Field(description="Path to the SRPM file")],
    ctx: Context,
) -> Annotated[tuple[str, list[str]] | tuple[str, str], Field(description="The state of the build and the URLs of the builds or an error message")]:

    """
    Builds a package in a Copr project.
    Returns the URLs of the builds or an error message on failure.
    """
    if not init_kerberos_ticket():
        return ("failed", "Failed to initialize Kerberos ticket")

    project_proxy = ProjectProxy(COPR_CONFIG)
    try:
        await asyncio.to_thread(project_proxy.add,
            ownername=OWNER,
            projectname=project,
            chroots=chroots,
            description=DEFAULT_DESCRIPTION,
            instructions=DEFAULT_INSTRUCTIONS,
            delete_after_days=PRESERVE_PROJECT,
        )
        await ctx.info(f"Copr project {project} created")
    except Exception as e:
        if "already exists" in str(e):
            return ("failed", "Copr project already exists")
        raise e

    build_proxy = BuildProxy(COPR_CONFIG)
    build = await asyncio.to_thread(build_proxy.create_from_file,
        ownername=OWNER,
        projectname=project,
        path=srpm_path,
        buildopts={
            "chroots": chroots,
            "background": True,
            }
    )

    progress_counter = 0
    
    timeout_seconds = MAX_BUILD_TIME_SECONDS
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        result = await asyncio.to_thread(build_proxy.get, build.id)
        if result.state == "" or result.state in ["running", "pending", "starting", "importing", "forked", "waiting"]:
            await ctx.info(f"Copr build {build.id} for {project} is in state {result.state}")
            await ctx.report_progress(progress=progress_counter)
            progress_counter += 1
            await asyncio.sleep(10)
            continue
        elif result.state in ["succeeded"]:
            await ctx.info(f"Copr build {build.id} for {project} succeeded")
            return ("succeeded", _get_build_urls(result.repo_url, chroots, build.id, srpm_path))
        elif result.state in ["failed"]:
            await ctx.info(f"Copr build {build.id} for {project} failed")
            return ("failed", _get_build_urls(result.repo_url, chroots, build.id, srpm_path))
        elif result.state in ["canceled", "skipped", "unknown"]:
            await ctx.info(f"Copr build {build.id} for {project} failed with state {result.state}")
            raise CoprToolException(f"Copr build {build.id} for {project} failed with state {result.state}")
