import asyncio
import gzip
import logging
import re
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urljoin, urlparse

import aiohttp
import rpm
from copr.v3 import BuildProxy, ProjectProxy
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from common.validators import AbsolutePath
from utils import init_kerberos_ticket

COPR_USER = "jotnar-bot"
COPR_CONFIG = {
    "copr_url": "https://copr.devel.redhat.com",
    "username": COPR_USER,
    "gssapi": True,
}
COPR_PROJECT_LIFETIME = 7  # days
COPR_BUILD_TIMEOUT = 3 * 60 * 60  # seconds
COPR_TIMEOUT_GRACE_PERIOD = 60  # seconds
COPR_POLLING_INTERVAL = 30  # seconds
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
    artifacts_urls: list[str] | None = Field(
        description="URLs to build artifacts (logs and RPM files)", default=None
    )


async def _get_exclusive_arches(srpm_path: Path) -> set[str]:
    def read_header():
        ts = rpm.TransactionSet()
        ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)
        with srpm_path.open("rb") as f:
            return ts.hdrFromFdno(f.fileno())

    header = await asyncio.to_thread(read_header)
    exclude_arches = set(header[rpm.RPMTAG_EXCLUDEARCH])
    exclusive_arches = set(header[rpm.RPMTAG_EXCLUSIVEARCH])
    return (COPR_ARCHES - exclude_arches) & exclusive_arches


def _branch_to_chroot(dist_git_branch: str) -> str:
    m = re.match(r"^c(\d+)s|rhel-(\d+)-main|rhel-(\d+)\.\d+.*$", dist_git_branch)
    if not m:
        raise ValueError(f"Unsupported branch name: {dist_git_branch}")
    majorver = next(g for g in m.groups() if g is not None)
    return f"rhel-{majorver}.dev"


async def build_package(
    srpm_path: Annotated[AbsolutePath, Field(description="Absolute path to SRPM (*.src.rpm) file to build")],
    dist_git_branch: Annotated[str, Field(description="dist-git branch")],
    jira_issue: Annotated[str, Field(description="Jira issue key (e.g. RHEL-12345)")],
) -> BuildResult:
    """Builds the specified SRPM in Copr."""
    if not await init_kerberos_ticket():
        raise ToolError("Failed to initialize Kerberos ticket")
    try:
        exclusive_arches = await _get_exclusive_arches(srpm_path)
    except Exception as e:
        raise ToolError(f"Failed to read SRPM header: {e}") from e
    # build for x86_64 unless the package is exclusive to other arch(es),
    # in such case build for either of them
    build_arch = exclusive_arches.pop() if exclusive_arches else "x86_64"
    try:
        chroot = _branch_to_chroot(dist_git_branch) + f"-{build_arch}"
    except ValueError as e:
        raise ToolError(f"Failed to deduce Copr chroot: {e}") from e
    project_proxy = ProjectProxy(COPR_CONFIG)
    kwargs = {
        "ownername": COPR_USER,
        "projectname": jira_issue,
        "chroots": [chroot],
        "description": f"Test builds for {jira_issue}",
        "delete_after_days": COPR_PROJECT_LIFETIME,
    }
    try:
        # if the project already exists, nothing is updated, so we have to edit it
        # afterwards unless our chroot is already enabled
        project = await asyncio.to_thread(project_proxy.add, exist_ok=True, **kwargs)
        if chroot not in project.chroot_repos:
            # make sure to preserve existing chroots
            kwargs["chroots"] = sorted(set(project.chroot_repos.keys()) | {chroot})
            await asyncio.to_thread(project_proxy.edit, **kwargs)
    except Exception as e:
        raise ToolError(f"Failed to create or update Copr project: {e}") from e
    build_proxy = BuildProxy(COPR_CONFIG)
    try:
        build = await asyncio.to_thread(
            build_proxy.create_from_file,
            ownername=COPR_USER,
            projectname=jira_issue,
            path=str(srpm_path),
            buildopts={"chroots": [chroot], "timeout": COPR_BUILD_TIMEOUT},
        )
    except Exception as e:
        raise ToolError(f"Failed to submit Copr build: {e}") from e
    else:
        logger.info(f"{jira_issue}: build of {srpm_path} in {chroot} started: {build.id:08d}")

    async def get_artifacts_urls(build):
        if build.source_package and (package := build.source_package.get("name")):
            baseurl = f"{build.repo_url}/{chroot}/{build.id:08d}-{package}/"
            try:
                built_packages = await asyncio.to_thread(build_proxy.get_built_packages, build.id)
            except Exception as e:
                logger.error(f"Failed to get built packages for Copr build {build.id:08d}: {e}")
                built_packages = None
            artifacts = ["builder-live.log.gz", "root.log.gz"]
            for nevra in (built_packages or {}).get(chroot, {}).get("packages", []):
                artifacts.append("{name}-{version}-{release}.{arch}.rpm".format(**nevra))
            return [urljoin(baseurl, f) for f in artifacts]
        return None

    build_start_time = time.monotonic()
    build_id = build.id
    while time.monotonic() - build_start_time < COPR_BUILD_TIMEOUT + COPR_TIMEOUT_GRACE_PERIOD:
        try:
            build = await asyncio.to_thread(build_proxy.get, build_id)
        except Exception as e:
            logger.warning(f"Failed to get build info for Copr build {build_id:08d}: {e}")
            # try again later
            await asyncio.sleep(COPR_POLLING_INTERVAL)
            continue
        match build.state:
            case "running" | "pending" | "starting" | "importing" | "forked" | "waiting":
                logger.info(f"Build {build.id:08d} is still running")
                await asyncio.sleep(COPR_POLLING_INTERVAL)
                continue
            case "succeeded":
                logger.info(f"Build {build.id:08d} succeeded")
                return BuildResult(success=True, artifacts_urls=await get_artifacts_urls(build))
            case _:
                message = f"Build {build.id:08d} finished with state: {build.state}"
                logger.info(message)
                return BuildResult(
                    success=False, error_message=message, artifacts_urls=await get_artifacts_urls(build)
                )
    # the build should have timed out by now
    try:
        build = await asyncio.to_thread(build_proxy.get, build_id)
    except Exception as e:
        logger.warning(f"Failed to get build info for Copr build {build_id:08d}: {e}")
        build = None
    message = f"Reached timeout for build {build_id:08d}"
    logger.info(message)
    return BuildResult(
        success=False,
        error_message=message,
        artifacts_urls=await get_artifacts_urls(build) if build else None,
    )


async def download_artifacts(
    artifacts_urls: Annotated[list[str], Field(description="URLs to build artifacts (logs and RPM files)")],
    target_path: Annotated[AbsolutePath, Field(description="Absolute path where to download the artifacts")],
) -> str:
    """Downloads build artifacts to the specified location."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in artifacts_urls:
            try:
                async with session.get(url) as response:
                    if response.status < 400:
                        target = Path(Path(urlparse(url).path).name)
                        content = await response.read()
                        if ".log" in target.suffixes:
                            if content.startswith(b"\x1f\x8b"):
                                # decompress logs on-the-fly
                                content = gzip.decompress(content)
                            if target.suffix == ".gz":
                                target = target.with_suffix("")
                        (target_path / target).write_bytes(content)
                    else:
                        raise ToolError(f"Failed to download {url}: {response.status} {response.reason}")
            except asyncio.TimeoutError as e:
                raise ToolError(f"Failed to download {url}: timed out") from e
    return "Successfully downloaded the specified build artifacts"
