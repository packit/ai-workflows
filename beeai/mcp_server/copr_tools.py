import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urljoin

import rpm
from copr.v3 import BuildProxy, ProjectProxy
from fastmcp import Context
from pydantic import BaseModel, Field

from utils import init_kerberos_ticket

COPR_USER = "jotnar-bot"
COPR_CONFIG = {
    "copr_url": "https://copr.devel.redhat.com",
    "username": COPR_USER,
    "gssapi": True,
}
COPR_PROJECT_LIFETIME = 7  # days
COPR_BUILD_TIMEOUT = 3 * 60 * 60  # seconds
COPR_ARCHES = {
    "aarch64",
    "ppc64le",  # emulated
    "s390x",
    "x86_64",
}

logger = logging.getLogger(__name__)


class BuildResult(BaseModel):
    success: bool = Field(description="Whether the build succeeded")
    error_message: str | None = Field(description="Error message in case of failure", default=None)
    artifacts_urls: list[str] | None = Field(description="URLs to build artifacts (logs and RPM files)", default=None)


async def _get_exclusive_arches(srpm_path: Path) -> set[str]:
    def read_header():
        ts = rpm.TransactionSet()
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)
        with srpm_path.open() as f:
            return ts.hdrFromFdno(f.fileno())

    header = await asyncio.to_thread(read_header)
    exclude_arches = set(header[rpm.RPMTAG_EXCLUDEARCH])
    exclusive_arches = set(header[rpm.RPMTAG_EXCLUSIVEARCH])
    return (COPR_ARCHES - exclude_arches) & exclusive_arches


def _branch_to_chroot(dist_git_branch: str) -> str:
    m = re.match(r"^c(\d+)s|rhel-(\d+)-main|rhel-(\d+)\.\d+.*$", dist_git_branch)
    if not m:
        raise ValueError(f"Unsupported branch name: {dist_git_branch}")
    return f"rhel-{m.group(1)}.dev"


async def build_package(
    srpm_path: Annotated[Path, Field(description="Absolute path to SRPM (*.src.rpm) file to build")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
    jira_issue: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> BuildResult:
    """Builds the specified SRPM in Copr."""
    if not await init_kerberos_ticket():
        return BuildResult(success=False, error_message="Failed to initialize Kerberos ticket")
    # build for x86_64 unless the package is exclusive to other arch(es),
    # in such case build for either of them
    exclusive_arches = await _get_exclusive_arches(srpm_path)
    build_arch = exclusive_arches.pop() if exclusive_arches else "x86_64"
    try:
        chroot = _branch_to_chroot(dist_git_branch) + f"-{build_arch}"
    except ValueError as e:
        return BuildResult(success=False, error_message=f"Failed to deduce Copr chroot: {e}")
    project_proxy = ProjectProxy(COPR_CONFIG)
    project = await asyncio.to_thread(project_proxy.get, COPR_USER, jira_issue)
    kwargs = {
        "ownername": COPR_USER,
        "projectname": jira_issue,
        "chroots": [chroot],
        "description": f"Test builds for {jira_issue}",
        "delete_after_days": COPR_PROJECT_LIFETIME,
    }
    await asyncio.to_thread(
        project_proxy.edit if project else project_proxy.add,
        **kwargs,
    )
    build_proxy = BuildProxy(COPR_CONFIG)
    build = await asyncio.to_thread(
        build_proxy.create_from_file,
        ownername=COPR_USER,
        projectname=jira_issue,
        path=srpm_path,
        buildopts={"chroots": [chroot], "timeout": COPR_BUILD_TIMEOUT},
    )

    logger.info(f"{jira_issue}: build of {srpm_path} in {chroot} started: {build.id:08d}")

    async def get_artifacts_urls(build):
        if (package := build.source_package.get("name")):
            baseurl = f"{build.repo_url}/{chroot}/{build.id:08d}-{package}/"
            built_packages = await asyncio.to_thread(build_proxy.get_built_packages, build_id)
            artifacts = ["builder-live.log.gz", "root.log.gz"]
            for package in (built_packages or {}).get(chroot, {}).get("packages", []):
                artifacts.append("{name}-{version}-{release}.{arch}.rpm".format(**package))
            return [urljoin(baseurl, f) for f in artifacts]
        return None

    build_start_time = time.time()
    build_id = build.id
    while time.time() - build_start_time < COPR_BUILD_TIMEOUT + 60:
        build = await asyncio.to_thread(build_proxy.get, build_id)
        match build.state:
            case "running" | "pending" | "starting" | "importing" | "forked" | "waiting":
                logger.info(f"Build {build.id:08d} is still running")
                await asyncio.sleep(30)
                continue
            case "succeeded":
                logger.info(f"Build {build.id:08d} succeeded")
                return BuildResult(success=True, artifacts_urls=await get_artifacts_urls(build))
            case _:
                logger.info(f"Build {build.id:08d} failed")
                return BuildResult(success=False, artifacts_urls=await get_artifacts_urls(build))
    # the build should have timed out by now
    build = await asyncio.to_thread(build_proxy.get, build_id)
    logger.info(f"Reached timeout for build {build.id:08d}")
    return BuildResult(success=False, artifacts_urls=await get_artifacts_urls(build))
