"""
Golang-specific constants for the rebuild agent.

Infrastructure constants (Redis queues, Jira labels) are in common/constants.py.
This module contains only domain-specific constants for golang rebuilds.
"""

from enum import Enum

# Jira statuses indicating Golang CVE is fixed and ready for rebuild
GOLANG_CVE_FIXED_STATUSES = ["Integration", "Release Pending", "Done"]

# Component ticket statuses valid for processing
COMPONENT_VALID_STATUSES = ["New", "Assigned", "In Progress", "Triaging"]

# Common golang-dependent components
GOLANG_COMPONENTS = [
    "podman",
    "buildah",
    "skopeo",
    "cri-o",
    "runc",
    "grafana",
    "prometheus",
    "containers-common",
    "conmon-rs",
    "containernetworking-plugins",
    "gvisor-tap-vsock",
    "grafana-pcp",
]


class RebuildStatus(Enum):
    """Status of a rebuild task"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SCRATCH_BUILD = "scratch_build"
    SCRATCH_COMPLETE = "scratch_complete"
    FINAL_BUILD = "final_build"
    COMPLETED = "completed"
    FAILED = "failed"
    ERRORED = "errored"


class BrewBuildState(Enum):
    """Brew build task states"""

    FREE = 0
    OPEN = 1
    CLOSED = 2
    CANCELED = 3
    ASSIGNED = 4
    FAILED = 5


# Brew URLs
BREW_URL = "https://brewweb.engineering.redhat.com/brew"
BREW_HUB_URL = "https://brewhub.engineering.redhat.com/brewhub"

# Agent identity (matching jotnar-se pattern)
AGENT_NAME = "Golang Rebuild Agent"
AGENT_EMAIL = "jotnar@redhat.com"

# Changelog entry template
CHANGELOG_TEMPLATE = """* {date} {name} <{email}> - {nvr}
- Rebuilding with new golang {golang_version}
- Fixes: {cves}
- Resolves: {jiras}
"""

# Commit message template
COMMIT_MESSAGE_TEMPLATE = """Rebuilding with new golang {golang_version}
Fixes: {cves}
Resolves: {jiras}

Signed-off-by: {name} <{email}>"""
