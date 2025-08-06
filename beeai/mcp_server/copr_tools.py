import time
import re
import os

from copr.v3 import BuildProxy, ProjectProxy
from typing import Annotated

from pydantic import Field
from utils import init_kerberos_ticket

class CoprToolException(Exception):
    pass

OWNER = "jotnar-bot"
COPR_URL = "https://copr.devel.redhat.com"
PRESERVE_PROJECT = 1 # None|-1|any number of days
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


def _extract_package_name_regex(srpm_path):
    """Extract package name using regex pattern."""
    filename = os.path.basename(srpm_path)
    # Pattern: name-version-release.src.rpm
    match = re.match(r'^([^-]+)-.*\.src\.rpm$', filename)
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
        urls.append(f"{copr_build_url}/{chroot}/00{build_id}-{package_name}") # 00 prefix is required by Copr, why???
    return urls


def build_package(
    project: Annotated[str, Field(description="Project name, e.g. 'RHEL-12345'")],
    chroots: Annotated[list[str], Field(description="List of chroots to build in")],
    srpm_path: Annotated[str, Field(description="Path to the SRPM file")],
) -> str:
    """
    Builds a package in a Copr project.
    Returns the URLs of the builds or an error message on failure.
    """
    if not init_kerberos_ticket():
        return "Failed to initialize Kerberos ticket"

    project_proxy = ProjectProxy(COPR_CONFIG)
    project_proxy.add(
        ownername=OWNER,
        projectname=project,
        chroots=chroots,
        description=DEFAULT_DESCRIPTION,
        instructions=DEFAULT_INSTRUCTIONS,
        delete_after_days=PRESERVE_PROJECT,
        exist_ok=True,
    )

    build_proxy = BuildProxy(COPR_CONFIG)
    build = build_proxy.create_from_file(
        ownername=OWNER,
        projectname=project,
        path=srpm_path,
        buildopts={
            "chroots": chroots,
            "background": True,
            }
    )

    while True:
        result = build_proxy.get(build.id)
        if result.state == "" or result.state in ["running", "pending", "starting", "importing", "forked", "waiting"]:
            time.sleep(10)
            continue
        elif result.state in ["succeeded"]:
            return _get_build_urls(result.repo_url, chroots, build.id, srpm_path)
        elif result.state in ["failed", "canceled", "skipped", "unknown"]:
            raise CoprToolException(f"Copr build {build.id} for {project} failed with state {result.state}")
