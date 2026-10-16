"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import gzip
import logging
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar, overload

import httpx
import koji
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool, ToolError
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel
from specfile import Specfile
from specfile.sourcelist import Sourcelist
from specfile.sources import Patches, Sources
from specfile.utils import EVR, NEVR, NEVRA

from ymir.common.base_utils import is_cs_branch
from ymir.common.config import current_rhel_config_override
from ymir.common.constants import BREWHUB_URL, CENTOS_STREAM_KOJIHUB_URL
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.common.version_utils import (
    construct_internal_branch_name,
    get_fix_version_variants,
    get_maintenance_majors,
    parse_rhel_version,
)

logger = logging.getLogger(__name__)

FIXED_IN_BUILD_CUSTOM_FIELD = "customfield_10578"
DOWNSTREAM_COMPONENT_CUSTOM_FIELD = "customfield_10669"  # Downstream Component Name

ToolResultT = TypeVar("ToolResultT", bound=BaseModel)


class _MetaInjectingSession:
    """Transparent wrapper around ``ClientSession`` that injects ``meta``
    into every ``call_tool`` invocation.

    All other attribute accesses are forwarded to the underlying session so
    that ``MCPTool.from_session`` (which calls ``list_tools``, ``initialize``,
    etc.) keeps working unchanged.
    """

    def __init__(self, session: ClientSession, meta: dict[str, Any]) -> None:
        self._session = session
        self._meta = meta

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: Any = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        merged = {**self._meta, **(meta or {})}
        return await self._session.call_tool(
            name,
            arguments,
            read_timeout_seconds=read_timeout_seconds,
            progress_callback=progress_callback,
            meta=merged,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def get_absolute_path(path: Path, tool: Tool) -> Path:
    if path.is_absolute():
        return path
    cwd = (tool.options or {}).get("working_directory") or Path.cwd()
    return Path(cwd) / path


@overload
async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    *,
    expected_output: type[ToolResultT],
    **kwargs: Any,
) -> ToolResultT: ...


@overload
async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    *,
    expected_output: None = None,
    **kwargs: Any,
) -> str | dict | list: ...


async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    *,
    expected_output: type[ToolResultT] | None = None,
    **kwargs: Any,
) -> ToolResultT | str | dict | list:
    """Run a tool once, optionally decoding and validating its result as a model.

    Without ``expected_output``, preserve the unwrapped result, including plain
    text. With a schema, accept either JSON text or structured data and let
    validation errors propagate to the caller.
    """
    if isinstance(tool, str):
        selected_tool = next((t for t in available_tools or [] if t.name == tool), None)
        if selected_tool is None:
            raise ToolError(f"Required tool '{tool}' is unavailable")
        tool = selected_tool
    output = await tool.run(input=kwargs).middleware(
        GlobalTrajectoryMiddleware(pretty=True, target=get_trajectory_writeable())
    )
    match output:
        case StringToolOutput():
            result = output.get_text_content()
        case JSONToolOutput():
            result = output.to_json_safe()
        case _:
            result = str(output)
    if isinstance(result, list):
        result = [_unpack_tool_result(item) for item in result]
    else:
        result = _unpack_tool_result(result)
    if expected_output is not None:
        if isinstance(result, str):
            return expected_output.model_validate_json(result)
        return expected_output.model_validate(result)
    return result


def _unpack_tool_result(result: Any) -> Any:
    if isinstance(result, TextContent):
        result = result.text
    if isinstance(result, dict) and len(result) == 1 and "result" in result:
        result = result["result"]
    return result


def _is_connection_error(exc: Exception) -> bool:
    if isinstance(exc, ExceptionGroup):
        return any(_is_connection_error(e) for e in exc.exceptions)
    return isinstance(exc, (httpx.ConnectError, httpx.ReadError, ConnectionError, OSError))


@asynccontextmanager
async def mcp_tools(
    sse_url: str,
    filter: Callable[[str], bool] | None = None,
    max_retries: int = 10,
    retry_delay: float = 3.0,
    call_meta: dict[str, Any] | None = None,
) -> AsyncGenerator[list[MCPTool]]:
    """Connect to an MCP gateway and yield the available tools.

    Args:
        sse_url: SSE endpoint of the MCP gateway.
        filter: Optional predicate to keep only matching tool names.
        max_retries: How many connection attempts before giving up.
        retry_delay: Seconds between retries.
        call_meta: Optional dict injected as MCP ``_meta`` on every
            ``call_tool`` invocation.  Use this to propagate context
            such as ``{"jira_issue": "RHEL-12345"}`` so that the
            gateway can scope operations per-caller.
    """
    if (override := current_rhel_config_override.get()) is not None:
        call_meta = call_meta.copy() if call_meta else {}
        call_meta["rhel_config_override"] = override

    connected = False
    for attempt in range(max_retries):
        try:
            caller_error = None
            async with sse_client(sse_url) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                effective_session: Any = session
                if call_meta:
                    effective_session = _MetaInjectingSession(session, call_meta)
                tools = await MCPTool.from_session(effective_session)
                if filter:
                    tools = [t for t in tools if filter(t.name)]
                connected = True
                try:
                    yield tools
                except Exception as error:
                    # Let the SSE task group exit normally so it cannot wrap an
                    # exception raised by the caller in an ExceptionGroup.
                    caller_error = error
            if caller_error is not None:
                raise caller_error
            return
        except Exception as e:
            if not connected and _is_connection_error(e) and attempt < max_retries - 1:
                logger.warning(
                    "MCP gateway not ready, retrying in %.0fs (attempt %d/%d)...",
                    retry_delay,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(retry_delay)
                continue
            raise


def _evr_from_build(build: dict) -> EVR:
    """Extract an EVR from a Koji build dict."""
    return EVR(
        epoch=build.get("epoch") or 0,
        version=build["version"],
        release=build["release"],
    )


def _nevr_from_build(build: dict) -> NEVR:
    """Extract a NEVR from a Koji build dict."""
    return NEVR(
        name=build["name"],
        epoch=build.get("epoch") or 0,
        version=build["version"],
        release=build["release"],
    )


def get_all_sources(spec: Specfile) -> Sources:
    parsed_sections = spec.parsed_sections
    sourcelists = [Sourcelist.parse(s, context=spec) for s in parsed_sections if s.id == "sourcelist"]
    return Sources(spec.tags(parsed_sections.package).content, sourcelists, context=spec)


def get_all_patches(spec: Specfile) -> Patches:
    parsed_sections = spec.parsed_sections
    patchlists = [Sourcelist.parse(s, context=spec) for s in parsed_sections if s.id == "patchlist"]
    return Patches(spec.tags(parsed_sections.package).content, patchlists, context=spec)


def _get_latest_koji_build(koji_url: str, tag: str, package: str) -> dict | None:
    """Query a single Koji tag for the latest build of *package*."""
    builds = koji.ClientSession(koji_url).listTagged(
        package=package,
        tag=tag,
        latest=True,
        inherit=True,
        strict=False,
    )
    return builds[0] if builds else None


def _get_koji_build(koji_url: str, nvr: str) -> dict | None:
    """Look up a build by NVR on the given Koji instance."""
    return koji.ClientSession(koji_url).getBuild(nvr)


def parse_koji_build_source(build: dict) -> tuple[str, str]:
    """Return the repository and ref recorded in Koji build metadata."""
    source = build.get("source")
    if not isinstance(source, str):
        raise ValueError("Koji build has no source")

    repository, separator, source_ref = source.rpartition("#")
    if not separator or not repository or not source_ref:
        raise ValueError(f"Koji build has an invalid source: {source!r}")
    return repository, source_ref


class NoBuildFoundError(Exception):
    """Raised when no build exists in any of the queried tags (as opposed to a lookup failure)."""


class TransientInfrastructureError(Exception):
    """Raised when infrastructure services are temporarily unavailable (Koji, Jira, etc).

    The task should be retried later rather than requesting user clarification.
    """


class BuildLogMissingError(TransientInfrastructureError):
    """Brew confirmed a requested build log is absent (HTTP 404 or 410).

    Attributes:
        partial_logs: List of (url, content) tuples for logs that were successfully fetched,
                     even when some required logs are missing.
        missing_architectures: Set of architecture names that failed to fetch (404/410).
    """

    def __init__(
        self,
        message: str,
        partial_logs: list[tuple[str, str]] | None = None,
        missing_architectures: set[str] | None = None,
    ):
        super().__init__(message)
        self.partial_logs = partial_logs or []
        self.missing_architectures = missing_architectures or set()


class PermanentValidationError(Exception):
    """Raised when data validation fails due to permanent issues (name mismatches, missing data).

    These are not transient infrastructure failures. Examples:
    - Dependency NVR resolves to wrong package name
    - Jira component metadata is missing or invalid
    - Data consistency checks reveal permanent problems

    Should be handled gracefully (logged + fallback) rather than retried.
    """


class InvalidDependencyDataError(PermanentValidationError):
    """Raised when dependency metadata contains invalid or inconsistent data.

    Examples:
    - Fixed dependency NVR resolves to a different component name than expected
    - Data corruption or injection attack in dependency records

    Should be routed to user clarification, not retried.

    Attributes:
        dep_component: Expected dependency component name
        actual_component: Actual component name from the resolved build
        fixed_dep_nvr: The NVR that was resolved
    """

    def __init__(self, dep_component: str, actual_component: str, fixed_dep_nvr: NEVR):
        self.dep_component = dep_component
        self.actual_component = actual_component
        self.fixed_dep_nvr = fixed_dep_nvr
        super().__init__(
            f"Fixed dependency NVR {fixed_dep_nvr.name}-{fixed_dep_nvr.version}-{fixed_dep_nvr.release} "
            f"resolves to package '{actual_component}', not expected component '{dep_component}'. "
            f"Possible data corruption or injection."
        )


async def _get_latest_build_from_tags(
    package: str,
    *tags: str,
) -> tuple[EVR, str]:
    results = await asyncio.gather(
        *(asyncio.to_thread(_get_latest_koji_build, BREWHUB_URL, tag, package) for tag in tags),
    )
    latest = None
    for build in results:
        if build is None:
            continue
        evr = _evr_from_build(build)
        if latest is None or latest[0] < evr:
            latest = (evr, build["build_id"])
    if latest is None:
        raise NoBuildFoundError(f"There are no builds of {package} in {' or '.join(tags)}")
    evr, build_id = latest
    session = koji.ClientSession(BREWHUB_URL)
    metadata = await asyncio.to_thread(session.getBuild, build_id, strict=True)
    _, source_ref = parse_koji_build_source(metadata)
    return evr, source_ref


async def get_latest_candidate_build(package: str, dist_git_branch: str) -> tuple[EVR, str]:
    return await _get_latest_build_from_tags(
        package,
        f"{dist_git_branch}-candidate",
        f"{dist_git_branch}-z-candidate",
    )


async def get_latest_z_pending_build(package: str, dist_git_branch: str) -> tuple[EVR, str]:
    return await _get_latest_build_from_tags(
        package,
        f"{dist_git_branch}-z-pending",
    )


async def get_latest_buildroot_build(package: str, dist_git_branch: str) -> tuple[EVR, str]:
    return await _get_latest_build_from_tags(
        package,
        f"{dist_git_branch}-buildrequires",
    )


def _resolve_buildroot_checks(
    target_branch: str, fix_version: str, rhel_config: dict | None = None
) -> list[tuple[str, str]]:
    """Return a list of (koji_hub_url, build_tag) pairs to verify.

    For CS branches with a Z-stream fix_version, both the CS Koji
    buildroot and the Brew Z-stream buildroot are checked (CS-first
    approach produces two builds).  CS branches in maintenance phase
    (z-stream only, no y-stream) are an exception: only the Brew
    Z-stream buildroot is checked since CentOS Stream Koji is stale.
    For internal RHEL branches with a Z-stream fix_version, only the
    Brew Z-stream buildroot is checked.
    """
    is_zstream = fix_version.lower().endswith(".z")

    if is_cs_branch(target_branch):
        if is_zstream and (parsed := parse_rhel_version(fix_version)):
            major, minor, _ = parsed
            rhel_branch = construct_internal_branch_name(major, minor)
            if rhel_config and major in get_maintenance_majors(rhel_config):
                return [(BREWHUB_URL, f"{rhel_branch}-z-build")]
            return [
                (CENTOS_STREAM_KOJIHUB_URL, f"{target_branch}-build"),
                (BREWHUB_URL, f"{rhel_branch}-z-build"),
            ]
        return [(CENTOS_STREAM_KOJIHUB_URL, f"{target_branch}-build")]

    suffix = "-z-build" if is_zstream else "-build"
    return [(BREWHUB_URL, f"{target_branch}{suffix}")]


async def check_build_in_buildroot(
    target_branch: str,
    dep_component: str,
    fixed_in_build_nvr: str,
    fix_version: str = "",
) -> bool:
    """Check if the dependency's fixed build (or newer) is in all relevant buildroots.

    Queries the appropriate Koji instance(s) based on ``target_branch`` and
    ``fix_version``.  For CS Z-stream fixes, both the CS Koji and Brew
    Z-stream buildroots are checked (unless the major version is in
    maintenance, in which case only Brew is checked).
    """
    from ymir.common.config import load_rhel_config

    rhel_config = await load_rhel_config()
    checks = _resolve_buildroot_checks(target_branch, fix_version, rhel_config)

    # Always resolve the fixed build's epoch from Brew — the NVR in
    # Jira's "Fixed in Build" is a Brew NVR (e.g. .el9_8) and may
    # not exist in CS Koji (which uses .el9).
    fixed_build_future = asyncio.to_thread(_get_koji_build, BREWHUB_URL, fixed_in_build_nvr)
    tag_futures = [
        asyncio.to_thread(_get_latest_koji_build, koji_url, build_tag, dep_component)
        for koji_url, build_tag in checks
    ]
    results = await asyncio.gather(fixed_build_future, *tag_futures)
    fixed_build = results[0]
    tag_results = list(zip([tag for _, tag in checks], results[1:], strict=True))

    if not fixed_build:
        logger.warning(f"Build {fixed_in_build_nvr} not found in Koji")
        return False

    fixed_evr = _evr_from_build(fixed_build)

    for build_tag, latest in tag_results:
        if not latest:
            logger.info(f"No builds of {dep_component} found in {build_tag}")
            return False

        latest_evr = _evr_from_build(latest)

        if latest_evr >= fixed_evr:
            logger.info(f"{dep_component} in {build_tag}: {latest['nvr']} >= {fixed_in_build_nvr}")
        else:
            logger.info(
                f"{dep_component} in {build_tag}: "
                f"{latest['nvr']} < {fixed_in_build_nvr} — not yet in buildroot"
            )
            return False

    return True


async def _find_completed_builds_jira(
    package: str, fix_version: str, available_tools: list[Tool], jira_issue: str | None = None
) -> tuple[list[tuple[str, str]], list[tuple[str, str]] | None]:
    """Search Jira for completed builds with Fixed in Build set.

    Args:
        package: Package name
        fix_version: RHEL fix version (e.g., "rhel-10.2.z")
        available_tools: List of available tools for Jira queries
        jira_issue: Jira issue key to exclude from search (the ticket being triaged)

    Returns:
        Tuple of (closed_builds, active_builds) where:
        - closed_builds: List of (issue_key, nvr) tuples for closed builds
        - active_builds: List of (issue_key, nvr) tuples for active builds, or None if query failed
    """
    fix_version_variants = get_fix_version_variants(fix_version)
    escaped_versions = [v.replace('"', '\\"') for v in fix_version_variants]
    fix_version_clause = ", ".join(f'"{v}"' for v in escaped_versions)

    # Escape package name to prevent JQL injection
    escaped_package = package.replace("\\", "\\\\").replace('"', '\\"')

    exclude_clause = f' AND key != "{jira_issue}"' if jira_issue else ""

    closed_jql = (
        f'project = RHEL AND component = "{escaped_package}" AND '
        f"fixVersion in ({fix_version_clause}) AND "
        f"status in (Closed, Done) AND "
        f'resolution in ("Done", "Done-Errata") AND '
        f"customfield_10578 IS NOT EMPTY"
        f"{exclude_clause}"
    )

    async def search_all(jql: str, fields: list[str], kind: str) -> list[dict]:
        results = await run_tool(
            "search_jira_issues",
            available_tools=available_tools,
            jql=jql,
            fields=fields,
            max_results=50,
            fetch_all=True,
        )
        if not isinstance(results, list):
            raise TransientInfrastructureError(
                f"Invalid response from {kind}-build Jira query for {package}: {type(results)}"
            )
        return results

    try:
        closed_results = await search_all(closed_jql, ["key", "customfield_10578"], "closed")
    except TransientInfrastructureError:
        raise
    except (ToolError, ConnectionError, OSError, TimeoutError) as e:
        raise TransientInfrastructureError(
            f"Closed-build Jira query failed for {package} in {fix_version}: {e}"
        ) from e

    # Empty list is valid — no closed builds found

    # Also search for active builds (not yet closed but have Fixed in Build set)
    # These need validation since they might be rejected/abandoned
    active_jql = (
        f'project = RHEL AND component = "{escaped_package}" AND '
        f"fixVersion in ({fix_version_clause}) AND "
        f"status not in (Closed, Done) AND "
        f"customfield_10578 IS NOT EMPTY"
        f"{exclude_clause}"
    )
    try:
        active_results = await search_all(active_jql, ["key", "customfield_10578", "status"], "active")
    except Exception as e:
        logger.error(f"Failed to query active builds for {package} in {fix_version}: {e}")
        active_results = None

    if not closed_results and not active_results:
        # If active query failed (None), we can't determine if there are active builds
        if active_results is None:
            logger.warning(
                f"No closed builds found for {package} and active builds query failed. "
                f"Cannot determine build status."
            )
            return [], None
        # Both queries succeeded but found nothing
        return [], []

    # Process closed results
    closed_candidates = []
    for issue in closed_results:
        issue_key = issue.get("key")
        nvr_raw = issue.get("fields", {}).get("customfield_10578")

        if not issue_key:
            continue
        if not isinstance(nvr_raw, str):
            logger.warning(f"Issue {issue_key} has non-string Fixed in Build: {type(nvr_raw).__name__}")
            continue

        nvr = nvr_raw.strip()
        if not nvr:
            logger.warning(f"Issue {issue_key} has empty Fixed in Build")
            continue

        closed_candidates.append((issue_key, nvr))

    # Process active results (if query succeeded)
    if active_results is None:
        # Active query failed - signal this to caller
        active_candidates = None
    else:
        active_candidates = []
        for issue in active_results:
            issue_key = issue.get("key")
            nvr_raw = issue.get("fields", {}).get("customfield_10578")
            status = issue.get("fields", {}).get("status", {}).get("name", "Unknown")

            if not issue_key:
                continue
            if not isinstance(nvr_raw, str):
                logger.warning(f"Active issue {issue_key} (status: {status}) has non-string Fixed in Build")
                continue

            nvr = nvr_raw.strip()
            if not nvr:
                logger.warning(f"Active issue {issue_key} (status: {status}) has empty Fixed in Build")
                continue

            active_candidates.append((issue_key, nvr))

    if active_candidates:
        logger.info(
            f"Found {len(active_candidates)} active (not closed) builds for {package} in "
            f"{fix_version}: {', '.join(f'{key} ({nvr})' for key, nvr in active_candidates)}"
        )

    return closed_candidates, active_candidates


async def _select_highest_evr_build(
    candidates: list[tuple[str, str]], expected_package: str
) -> tuple[NEVR, str] | None:
    """Select build with highest EVR from candidate NVRs.

    Args:
        candidates: List of (issue_key, nvr) tuples
        expected_package: Expected package name to validate against

    Returns:
        Tuple of (nevr, issue_key) for highest build, or None if none found
    """
    if not candidates:
        return None

    # Fetch all builds concurrently
    build_futures = [asyncio.to_thread(_get_koji_build, BREWHUB_URL, nvr) for _, nvr in candidates]
    builds = await asyncio.gather(*build_futures, return_exceptions=True)

    # Associate successful builds with their metadata
    candidate_builds = []
    koji_failures = []
    missing_builds = []
    for (issue_key, nvr), build in zip(candidates, builds, strict=True):
        if isinstance(build, Exception):
            logger.warning(f"Failed to fetch build {nvr}: {build}")
            koji_failures.append((nvr, build))
            continue
        if build:
            # Verify package name matches to avoid cross-package contamination
            build_package_name = build.get("name")
            if build_package_name != expected_package:
                logger.warning(
                    f"Skipping build {nvr} from issue {issue_key}: "
                    f"package name '{build_package_name}' does not match expected '{expected_package}'"
                )
                continue
            nevr = _nevr_from_build(build)
            candidate_builds.append((nevr, issue_key))
        else:
            # Build exists in Jira but not in Koji
            missing_builds.append((issue_key, nvr))

    if koji_failures:
        failed_nvrs = [nvr for nvr, _ in koji_failures]
        raise TransientInfrastructureError(
            f"Koji lookup failed for {len(koji_failures)} candidate(s): {failed_nvrs}"
        )

    # Log warning if builds are missing from Koji
    if missing_builds:
        missing_issues = ", ".join(f"{key} ({nvr})" for key, nvr in missing_builds)
        logger.warning(f"Builds referenced in Jira but not found in Koji: {missing_issues}")

    # If no valid builds found in Koji, return None to signal invalid_build_records
    if not candidate_builds:
        if missing_builds:
            logger.warning(
                f"All {len(candidates)} candidate builds are missing from Koji. "
                f"Latest build verification cannot proceed."
            )
        return None

    # Sort by EVR descending and pick highest
    candidate_builds.sort(key=lambda x: x[0], reverse=True)
    latest_nevr, package_issue_key = candidate_builds[0]

    return latest_nevr, package_issue_key


async def _fetch_installed_pkgs_log(
    package_nevr: NEVR, built_architectures: set[str], *, require_all: bool = True
) -> list[tuple[str, str]]:
    """Fetch installed_pkgs.log from Brew for the given package for specific architectures.

    Args:
        package_nevr: Package NEVR
        built_architectures: Set of architectures to try
        require_all: If True, raise if any architecture's log is missing.
            If False, return whatever logs are found (for noarch builds).

    Returns:
        List of (architecture_url, log_content) tuples

    Raises:
        TransientInfrastructureError: If require_all and logs missing, or no logs at all
    """
    package_nvr = f"{package_nevr.name}-{package_nevr.version}-{package_nevr.release}"  # For error messages

    # Fetch logs only for architectures that were actually built
    installed_pkgs_log_urls = [
        (
            f"https://brewweb.engineering.redhat.com/brew/packages/"
            f"{package_nevr.name}/{package_nevr.version}/{package_nevr.release}/"
            f"data/logs/{arch}/installed_pkgs.log"
        )
        for arch in built_architectures
    ]

    class _LogAbsent(Exception):
        """Log confirmed absent (404/410) — not an infrastructure error."""

    class _LogFetchError(Exception):
        """Infrastructure error fetching log (5xx, auth, transport)."""

    async def check_and_fetch_log(client: httpx.AsyncClient, url: str) -> tuple[str, bytes | None]:
        """Check if installed_pkgs.log exists with HEAD, then fetch if available."""
        try:
            head_response = await client.head(url)
            if head_response.status_code in (404, 410):
                raise _LogAbsent(f"HTTP {head_response.status_code}")
            if head_response.status_code != 200:
                raise _LogFetchError(f"HTTP {head_response.status_code} from {url}")

            response = await client.get(url)
            if response.status_code == 200:
                return url, response.content
            if response.status_code in (404, 410):
                raise _LogAbsent(f"GET HTTP {response.status_code}")
            raise _LogFetchError(f"GET HTTP {response.status_code} from {url}")
        except (_LogAbsent, _LogFetchError):
            raise
        except Exception as e:
            raise _LogFetchError(f"Transport error fetching {url}: {e}") from e

    fetch_tasks = []
    try:
        # Overall 30-second timeout for entire operation
        async with asyncio.timeout(30.0):
            # Per-request timeout of 10s as secondary safeguard
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                # Fetch all architecture logs to verify consistency across architectures
                fetch_tasks = [
                    asyncio.create_task(check_and_fetch_log(client, url)) for url in installed_pkgs_log_urls
                ]
                results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                # Process results — separate absent logs from infra errors
                decoded_logs = []
                infra_errors = []
                for result in results:
                    if isinstance(result, _LogAbsent):
                        continue
                    if isinstance(result, _LogFetchError):
                        infra_errors.append(str(result))
                        continue
                    if isinstance(result, Exception):
                        infra_errors.append(str(result))
                        continue
                    url, content = result
                    if content:
                        try:
                            if content[:2] == b"\x1f\x8b":
                                content = gzip.decompress(content)
                            decoded_logs.append((url, content.decode("utf-8")))
                        except Exception as e:
                            infra_errors.append(f"Failed to decode {url}: {e}")
                    else:
                        infra_errors.append(f"Empty installed_pkgs.log from {url}")

                if infra_errors:
                    raise TransientInfrastructureError(
                        f"Infrastructure error fetching installed_pkgs.log for {package_nvr}: {infra_errors}"
                    )

                if not decoded_logs:
                    raise BuildLogMissingError(
                        f"No installed_pkgs.log available for {package_nvr} from any architecture "
                        f"(all returned 404/410, expected: {list(built_architectures)})",
                        partial_logs=[],
                        missing_architectures=built_architectures,
                    )

                fetched_archs = {url.split("/")[-2] for url, _ in decoded_logs}
                missing_archs = built_architectures - fetched_archs

                if missing_archs and require_all:
                    raise BuildLogMissingError(
                        f"Missing installed_pkgs.log for {package_nvr} from {len(missing_archs)} "
                        f"architecture(s): {list(missing_archs)} (fetched: {list(fetched_archs)})",
                        partial_logs=decoded_logs,  # Include partial logs for analysis
                        missing_architectures=missing_archs,
                    )

                logger.debug(
                    f"Found installed_pkgs.log for {package_nvr} from {len(decoded_logs)} "
                    f"architecture(s): {list(fetched_archs)}"
                )
                return decoded_logs

    except TimeoutError:
        # Cancel any outstanding tasks
        for task in fetch_tasks:
            if not task.done():
                task.cancel()
        # Await them to clean up
        if fetch_tasks:
            await asyncio.gather(*fetch_tasks, return_exceptions=True)
        raise TransientInfrastructureError(
            f"Timeout (30s) fetching installed_pkgs.log for {package_nvr} "
            f"from architectures: {list(built_architectures)}"
        ) from None


async def _get_known_package_names(
    dep_component: str, fixed_dep_nvr: NEVR
) -> tuple[list[str], int, NEVR] | None:
    """Get binary package names and validated NEVR for the fixed dependency build.

    Args:
        dep_component: Dependency component name (e.g., "golang"). This is kept as a
            cross-validation input from the triage LLM, independent of fixed_dep_nvr.name,
            used to detect data corruption or LLM hallucination.
        fixed_dep_nvr: Fixed dependency NEVR object

    Returns:
        Tuple of (known_names, real_binary_rpm_count, fixed_dep_nevr) or None if
        metadata unavailable.
        known_names: search terms for _parse_dependency_from_installed_pkgs_log.
            Always includes the dependency component name (e.g., "python-foobar") even
            when the build only produces differently-named binary RPMs (e.g., "python3-foobar").
        real_binary_rpm_count: distinct installable binary RPMs from the build (excluding
            debuginfo, debugsource, src). Used to decide ambiguity: 1 means the fix
            is unambiguously in that binary RPM (already_fixed); >1 means we can't tell
            which binary RPMs carry the fix (clarification needed). Computed before the
            synthetic component-name insertion so it reflects actual build output.
        fixed_dep_nevr: validated NEVR with authoritative epoch from Brew.

    Raises:
        InvalidDependencyDataError: If the fixed_dep_nvr resolves to a build with a different
            component name than dep_component (data corruption or injection).
    """
    fixed_dep_nvr_str = f"{fixed_dep_nvr.name}-{fixed_dep_nvr.version}-{fixed_dep_nvr.release}"
    fixed_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, fixed_dep_nvr_str)
    if not fixed_build:
        logger.error(f"Could not find build info for fixed dependency: {fixed_dep_nvr_str}")
        return None

    build_name = fixed_build.get("name")
    if build_name != dep_component:
        logger.error(
            f"Fixed dependency NVR {fixed_dep_nvr_str} resolves to package '{build_name}', "
            f"not expected component '{dep_component}'. Possible data corruption or injection."
        )
        raise InvalidDependencyDataError(dep_component, build_name, fixed_dep_nvr)

    build_id = fixed_build.get("build_id")
    if not build_id:
        logger.error(f"No build_id for {fixed_dep_nvr_str}")
        return None

    fixed_dep_nevr = _nevr_from_build(fixed_build)

    # Get list of binary package names produced by this source build
    try:
        session = koji.ClientSession(BREWHUB_URL)
        rpms = await asyncio.to_thread(session.listRPMs, buildID=build_id)
        if not isinstance(rpms, list):
            logger.error(f"Invalid RPM list response from Brew for build {build_id}")
            return None
        # Filter installable binary RPMs for the count (exclude debuginfo, debugsource, src)
        installable_binary_rpms = [
            rpm.get("name")
            for rpm in rpms
            if rpm.get("name")
            and not rpm.get("name", "").endswith("-debuginfo")
            and not rpm.get("name", "").endswith("-debugsource")
            and rpm.get("arch") != "src"
        ]
        real_binary_rpm_count = len(set(installable_binary_rpms))

        # Keep all non-debug packages in known_names for search (including src)
        known_names = [
            rpm.get("name")
            for rpm in rpms
            if rpm.get("name")
            and not rpm.get("name", "").endswith("-debuginfo")
            and not rpm.get("name", "").endswith("-debugsource")
        ]
    except Exception as e:
        logger.error(f"Failed to fetch RPM list for {fixed_dep_nvr_str}: {e}")
        return None

    # The component name should already be in the list from the src RPM,
    # but include this as a safety measure for edge cases
    if dep_component not in known_names:
        known_names.insert(0, dep_component)

    logger.debug(f"Known package names for {dep_component}: {known_names[:5]}...")
    return known_names, real_binary_rpm_count, fixed_dep_nevr


async def _resolve_binary_rpm_to_source_build(
    koji_url: str, binary_nevra: str, binary_pkg_name: str, dep_component: str
) -> NEVR | None:
    """Resolve a binary RPM NEVRA to its source build NEVR.

    This handles independently versioned subpackages (e.g., perl-Errno with its own version)
    by using Koji API to query the actual source build that produced the binary RPM.

    Args:
        koji_url: Koji hub URL
        binary_nevra: Full NEVRA of the binary package as found in installed_pkgs.log
        binary_pkg_name: Binary package name (e.g., "perl-Errno")
        dep_component: Source component name (e.g., "perl")

    Returns:
        Source build NEVR if successfully resolved, None if not found in Koji.

    Raises:
        TransientInfrastructureError: If Koji API calls fail (network, service issues)
    """

    def _do_koji_lookup():
        """Synchronous Koji lookup wrapped for asyncio.to_thread."""
        try:
            session = koji.ClientSession(koji_url)
            nevra = NEVRA.from_string(binary_nevra)

            rpm_info = session.getRPM(
                {"name": nevra.name, "version": nevra.version, "release": nevra.release, "arch": nevra.arch}
            )
            if not rpm_info:
                logger.warning(
                    f"Koji getRPM returned nothing for {binary_nevra} "
                    f"(binary_pkg: {binary_pkg_name}, source: {dep_component})"
                )
                return None

            build_id = rpm_info.get("build_id")
            if not build_id:
                logger.warning(f"No build_id found for RPM {binary_nevra} ({binary_pkg_name})")
                return None

            build = session.getBuild(build_id)
            if not build:
                logger.warning(f"Could not fetch Brew build info for build_id {build_id}")
                return None

            if build["name"] != dep_component:
                logger.warning(
                    f"Binary RPM {binary_nevra} ({binary_pkg_name}) resolves to source "
                    f"'{build['name']}', not expected '{dep_component}'"
                )
                return None

            # Get source build NEVR
            source_nevr = _nevr_from_build(build)
            epoch_prefix = f"{source_nevr.epoch}:" if source_nevr.epoch else ""
            logger.info(
                f"Resolved binary RPM {binary_nevra} ({binary_pkg_name}) to source build: "
                f"{epoch_prefix}{source_nevr.name}-{source_nevr.version}-{source_nevr.release}"
            )
            return source_nevr

        except koji.GenericError as e:
            # Koji API failures are transient - should retry
            logger.error(f"Transient Koji failure resolving {binary_nevra} ({binary_pkg_name}): {e}")
            raise TransientInfrastructureError(
                f"Failed to query Koji for binary RPM {binary_nevra}: {e}"
            ) from e
        except Exception as e:
            # Unexpected errors - also treat as transient to avoid wrong rebuild decisions
            logger.error(
                f"Unexpected error resolving {binary_nevra} ({binary_pkg_name}) to source build: {e}"
            )
            raise TransientInfrastructureError(
                f"Unexpected error querying Koji for {binary_nevra}: {e}"
            ) from e

    try:
        return await asyncio.to_thread(_do_koji_lookup)
    except TransientInfrastructureError:
        raise


async def _parse_dependency_from_installed_pkgs_log(
    installed_pkgs_log: str, dep_component: str, known_names: list[str], brew_url: str | None = None
) -> tuple[NEVR | None, list[str] | None]:
    """Parse installed_pkgs.log to find which version of dependency was installed.

    For independently versioned subpackages (e.g., perl-Errno), uses Brew API to resolve
    the binary RPM to its source package instead of string manipulation.

    Args:
        installed_pkgs_log: Content of installed_pkgs.log
        dep_component: Source component name
        known_names: List of known binary RPM names to search for
        brew_url: Brew hub URL for resolving binary RPMs to source packages.
                  If provided, uses Brew API for robust resolution. Otherwise falls back
                  to string manipulation (which fails for independently versioned subpackages).

    Returns:
        Tuple of (used_dep_nevr, matched_pkg_names) where:
        - used_dep_nevr is the source NEVR of the installed dependency (None if no match)
        - matched_pkg_names is a list with the matched binary RPM name (single element).
          When used_dep_nevr is None and matched_pkg_names is not None, it contains
          unresolved subpackage names (Brew resolution to source RPM failed).

    Raises:
        TransientInfrastructureError: If brew_url is provided but Brew query fails for subpackages
    """

    known_names_set = set(known_names)
    unresolved_subpackages = []

    for line in installed_pkgs_log.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[-1] != "installed":
            continue

        try:
            nevra = NEVRA.from_string(fields[0])
        except ValueError:
            continue

        if nevra.name not in known_names_set:
            continue

        pkg_name = nevra.name
        epoch = nevra.epoch if ":" in fields[0] else 0

        if brew_url and pkg_name != dep_component:
            resolved_nevr = await _resolve_binary_rpm_to_source_build(
                brew_url, fields[0], pkg_name, dep_component
            )
            if resolved_nevr:
                used_nevr = resolved_nevr
            else:
                logger.debug(
                    f"Could not resolve {fields[0]} ({pkg_name}) to source package in Brew, "
                    f"skipping this match"
                )
                unresolved_subpackages.append(pkg_name)
                continue
        else:
            used_nevr = NEVR(name=dep_component, epoch=epoch, version=nevra.version, release=nevra.release)

        logger.info(
            f"Found {dep_component} package {pkg_name} in installed_pkgs.log: "
            f"{f'{used_nevr.epoch}:' if used_nevr.epoch else ''}"
            f"{used_nevr.name}-{used_nevr.version}-{used_nevr.release}"
        )
        return used_nevr, [pkg_name]

    if unresolved_subpackages:
        logger.warning(
            f"Found evidence of {dep_component} subpackage(s) in installed_pkgs.log "
            f"but could not resolve to source package in Brew: {unresolved_subpackages}. "
            f"Cannot determine installed version — needs manual verification."
        )
        return None, unresolved_subpackages
    logger.warning(f"Could not find {dep_component} or its subpackages in installed_pkgs.log")
    return None, None


async def _compare_build_timestamps(
    package: str,
    package_nvr: str,
    dep_component: str,
    fixed_dep_nvr: NEVR,
) -> bool | None:
    """Compare build timestamps as fallback when installed_pkgs.log is unavailable.

    Args:
        package: Package name (e.g., "go-fdo-client")
        package_nvr: Package NVR to check
        dep_component: Dependency component name (e.g., "golang")
        fixed_dep_nvr: Fixed dependency NEVR object

    Returns:
        True if package was built after dependency fix,
        False if built before fix,
        None if metadata unavailable or validation failed
    """
    fixed_dep_nvr_str = f"{fixed_dep_nvr.name}-{fixed_dep_nvr.version}-{fixed_dep_nvr.release}"
    package_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, package_nvr)
    fixed_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, fixed_dep_nvr_str)

    if not package_build or not fixed_build:
        logger.warning("Could not fetch build metadata for timestamp comparison")
        return None

    # Validate package build is for expected package
    if package_build.get("name") != package:
        logger.error(
            f"Package build {package_nvr} has name '{package_build.get('name')}', "
            f"expected '{package}'. Possible data error."
        )
        return None

    if fixed_build.get("name") != dep_component:
        logger.error(
            f"Fixed dependency {fixed_dep_nvr_str} has name '{fixed_build.get('name')}', "
            f"expected '{dep_component}'. Possible data error."
        )
        return None

    package_completion = package_build.get("completion_time")
    fixed_completion = fixed_build.get("completion_time")

    if not package_completion or not fixed_completion:
        logger.warning("Missing completion_time for timestamp comparison")
        return None

    # If package was built after dependency fix, it likely has the fix
    if package_completion >= fixed_completion:
        logger.info(
            f"{package} ({package_nvr}) completed at {package_completion}, "
            f"which is >= {dep_component} fix completion at {fixed_completion}. "
            f"Assuming fixed dependency was used."
        )
        return True

    logger.info(
        f"{package} ({package_nvr}) completed at {package_completion}, "
        f"which is < {dep_component} fix completion at {fixed_completion}. "
        f"Package was built before fix."
    )
    return False


async def check_package_built_with_fixed_dependency(
    package: str,
    fix_version: str,
    dep_component: str,
    fixed_dep_nvr: NEVR,
    available_tools: list[Tool],
    jira_issue: str | None = None,
) -> tuple[bool | None, str | None, str | None, str | None]:
    """Check if the package's latest build already used the fixed dependency.

    Searches for the most recent completed build of the package in the given
    fix_version, fetches its installed_pkgs.log from Brew, and checks which version of
    the dependency was used during that build.

    Args:
        package: Package name (e.g., "go-fdo-client")
        fix_version: RHEL fix version (e.g., "rhel-10.2.z")
        dep_component: Dependency component name (e.g., "golang"). This is a cross-validation
            input from the triage LLM, independent of fixed_dep_nvr.name, used to detect data
            corruption or LLM hallucination.
        fixed_dep_nvr: The fixed dependency NEVR object (from "Fixed in Build" field)
        available_tools: List of available tools for Jira queries
        jira_issue: Jira issue key to exclude from search (the ticket being triaged)

    Returns:
        Tuple of (already_fixed, package_issue_key, package_nvr, reason):
        - already_fixed: True if confirmed fixed (installed_pkgs.log proves it),
                        False if needs rebuild (installed_pkgs.log shows old version or built before fix),
                        None if needs manual action
        - package_issue_key: Jira issue key for the existing build (if found)
        - package_nvr: NVR of the existing build (if found)
        - reason: When already_fixed is None, explains why. Possible reasons:
                 "built_after_fix_no_installed_pkgs_log" - installed_pkgs.log unavailable (Brew retention)
                 "architecture_dependency_conflict" - different dependency versions per arch
                 "partial_architecture_coverage" - dependency missing from some arch logs
                 "active_builds_not_closed" - builds in progress (might be rejected)
                 "subpackage_match_needs_verification" - dependency matched via subpackage(s),
                      not the component name directly; cannot confirm which subpackage carries
                      the fix. Format: "subpackage_match_needs_verification:<subpkg1>, <subpkg2>"

    Raises:
        TransientInfrastructureError: When Jira/Koji temporarily unavailable (task should retry)
    """
    try:
        # Step 1: Find builds in Jira
        closed_candidates, active_candidates = await _find_completed_builds_jira(
            package, fix_version, available_tools, jira_issue
        )

        # Handle case where active query failed
        if active_candidates is None:
            raise TransientInfrastructureError(
                f"Active-build Jira query failed for {package} in {fix_version}"
            )

        if not closed_candidates and not active_candidates:
            logger.info(f"No builds found for {package} in {fix_version}")
            return False, None, None, None

        # Validate active candidates to filter out builds for different packages
        # This prevents unrelated active builds from blocking rebuild decisions
        if active_candidates:
            filtered_active = []
            for active_key, active_nvr in active_candidates:
                try:
                    active_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, active_nvr)
                    if not active_build:
                        # Active build not in Koji yet (might still be building)
                        # Keep it so we can request clarification
                        logger.info(
                            f"Active build {active_nvr} (issue {active_key}) "
                            f"not yet in Koji — keeping for clarification"
                        )
                        filtered_active.append((active_key, active_nvr))
                        continue
                    # Check if the active build is for the expected package
                    active_build_name = active_build.get("name")
                    if active_build_name != package:
                        logger.warning(
                            f"Active build {active_nvr} (issue {active_key}) "
                            f"resolves to '{active_build_name}', "
                            f"not expected package '{package}' — ignoring"
                        )
                        continue
                    # Build belongs to the correct package
                    filtered_active.append((active_key, active_nvr))
                except Exception as e:
                    logger.warning(f"Error validating active build {active_nvr}: {e}")
                    # Keep it anyway to request clarification
                    filtered_active.append((active_key, active_nvr))
            active_candidates = filtered_active if filtered_active else None

        # If only active (not closed) builds exist, request clarification
        # These might be rejected/abandoned, so we can't trust them yet
        if not closed_candidates and active_candidates:
            logger.warning(
                f"Found {len(active_candidates)} active builds for {package} in {fix_version}, "
                f"but none are closed/resolved. Cannot determine if rebuild already done."
            )
            active_issues = ", ".join(key for key, _ in active_candidates)
            return None, None, None, f"active_builds_not_closed:{active_issues}"

        # Step 2: Select build with highest EVR from closed builds
        result = await _select_highest_evr_build(closed_candidates, package)
        if not result:
            logger.warning(f"No valid closed builds found in Koji for {package} in {fix_version}")
            # If active builds exist, request clarification on those instead of rebuilding
            # Note: active_candidates might be None if query failed
            if active_candidates:
                logger.info(
                    f"Closed builds invalid, but {len(active_candidates)} active builds exist. "
                    f"Requesting clarification on active builds."
                )
                active_issues = ", ".join(key for key, _ in active_candidates)
                return None, None, None, f"active_builds_not_closed:{active_issues}"
            # If active query failed, this is a transient infrastructure failure
            if active_candidates is None:
                raise TransientInfrastructureError(
                    f"Jira query failed for {package} in {fix_version} - "
                    f"closed builds invalid and active builds query failed"
                )
            # Closed builds exist in Jira but all NVRs resolve to a different
            # package name in Koji — flag the data inconsistency for human review.
            rejected_issues = ", ".join(key for key, _ in closed_candidates)
            return None, None, None, f"invalid_build_records:{rejected_issues}"

        latest_nevr, package_issue_key = result
        package_nvr = f"{latest_nevr.name}-{latest_nevr.version}-{latest_nevr.release}"
        logger.info(
            f"Found latest build for {package}: {package_nvr} "
            f"(issue: {package_issue_key}, NEVR: {latest_nevr})"
        )

        # Step 2b: Filter active builds to only those with NEVR > selected closed build
        # Note: package-name validation already done earlier, so names always match.
        # If we can't resolve an active build's metadata but it has a valid NVR pattern,
        # keep it (it might be building) to request clarification from the user.
        if active_candidates:
            filtered_active = []
            for active_key, active_nvr in active_candidates:
                try:
                    active_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, active_nvr)
                    if not active_build:
                        # Active build not in Koji yet (might still be building)
                        # Keep it so we can request clarification
                        logger.info(
                            f"Active build {active_nvr} (issue {active_key}) "
                            f"not yet in Koji — keeping for clarification"
                        )
                        filtered_active.append((active_key, active_nvr))
                        continue
                    active_nevr = _nevr_from_build(active_build)
                    if active_nevr <= latest_nevr:
                        logger.info(
                            f"Active build {active_nvr} (issue {active_key}) has NEVR {active_nevr} "
                            f"<= selected closed build NEVR {latest_nevr} — ignoring"
                        )
                        continue
                    # Active build exists and has higher NEVR
                    filtered_active.append((active_key, active_nvr))
                except Exception as e:
                    logger.warning(f"Error validating active build {active_nvr}: {e}")
                    # Keep it anyway to request clarification
                    filtered_active.append((active_key, active_nvr))
            active_candidates = filtered_active if filtered_active else None

        # Step 3a: Get build metadata to determine which architectures were built
        package_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, package_nvr)
        if not package_build:
            raise TransientInfrastructureError(f"Koji metadata unavailable for package build: {package_nvr}")

        # Get list of architectures from RPMs
        build_id = package_build.get("build_id")
        if not build_id:
            raise TransientInfrastructureError(f"No build_id for {package_nvr}")

        try:
            session = koji.ClientSession(BREWHUB_URL)
            rpms = await asyncio.to_thread(session.listRPMs, buildID=build_id)
            if not isinstance(rpms, list):
                raise TransientInfrastructureError(f"Invalid RPM list response for {package_nvr}")

            # Extract unique architectures (exclude 'src' and 'noarch')
            built_archs = {
                rpm["arch"]
                for rpm in rpms
                if isinstance(rpm, dict) and rpm.get("arch") not in ("src", "noarch")
            }

            if not built_archs:
                # Noarch builds have installed_pkgs.log under noarch/ directory
                logger.info(f"Noarch-only build {package_nvr}, fetching from noarch directory")
                built_archs = {"noarch"}
                noarch_build = True
            else:
                noarch_build = False

        except TransientInfrastructureError:
            raise
        except (koji.GenericError, ConnectionError, OSError) as e:
            raise TransientInfrastructureError(
                f"Failed to fetch architecture list for {package_nvr}: {e}"
            ) from e

        if built_archs:
            logger.debug(f"Build {package_nvr} produced architectures: {list(built_archs)}")

        # Step 3b: Fetch installed_pkgs.log from Brew
        # For arch builds: require ALL arches. For noarch: accept any log found.
        installed_pkgs_logs = []
        missing_archs_from_error = set()  # Track architectures missing from BuildLogMissingError
        partial_logs_error = False
        if built_archs:
            try:
                installed_pkgs_logs = await _fetch_installed_pkgs_log(
                    latest_nevr, built_archs, require_all=not noarch_build
                )
            except BuildLogMissingError as e:
                # installed_pkgs.log is absent (404/410). This indicates the log is genuinely
                # unavailable (e.g., retention policy, very old build), not a transient error.
                # Distinguish between: (1) no logs at all → timestamp fallback
                # (2) some logs retrieved but not all → use partial logs if they exist
                if "from any architecture" in str(e):
                    # Case 1: No logs at all — fall back to timestamp comparison
                    logger.info(
                        f"installed_pkgs.log unavailable for {package_nvr}, "
                        f"falling back to timestamp comparison"
                    )
                elif e.partial_logs:
                    # Case 2: Partial logs retrieved — use them for verification
                    # Preserve missing architectures so we can check them later
                    installed_pkgs_logs = e.partial_logs
                    missing_archs_from_error = e.missing_architectures
                    logger.warning(f"Using partial installed_pkgs.log for {package_nvr}: {e}")
                else:
                    # Case 3: Error says partial but no logs attached — request clarification
                    partial_logs_error = True
                    missing_archs_from_error = e.missing_architectures
                    logger.warning(f"Partial installed_pkgs.log availability for {package_nvr}: {e}")

        if not installed_pkgs_logs:
            # If partial logs were available but not all architectures, request manual verification
            if partial_logs_error:
                logger.warning(
                    f"Some but not all architecture logs available for {package_nvr}. "
                    f"Requesting manual verification."
                )
                return None, package_issue_key, package_nvr, "partial_architecture_coverage"

            # Fallback: compare build timestamps
            logger.info(
                f"installed_pkgs.log unavailable for {package_nvr}; "
                "checking timestamps to determine next action"
            )
            built_after_fix = await _compare_build_timestamps(
                package, package_nvr, dep_component, fixed_dep_nvr
            )

            if built_after_fix is True:
                # Package built after fix - likely has it, but need manual verification
                logger.info(
                    f"{package} ({package_nvr}) was built after {dep_component} fix. "
                    f"Returning None to request manual verification."
                )
                return None, package_issue_key, package_nvr, "built_after_fix_no_installed_pkgs_log"
            if built_after_fix is False:
                # Package built before fix - but check if active builds exist
                logger.info(f"{package} ({package_nvr}) was built before {dep_component} fix")
                if active_candidates:
                    logger.warning(
                        f"Closed build {package_nvr} built before fix, but {len(active_candidates)} "
                        f"active builds exist. Requesting clarification on active builds."
                    )
                    active_issues = ", ".join(key for key, _ in active_candidates)
                    return None, package_issue_key, package_nvr, f"active_builds_not_closed:{active_issues}"
                return False, package_issue_key, package_nvr, None
            fixed_dep_nvr_str = f"{fixed_dep_nvr.name}-{fixed_dep_nvr.version}-{fixed_dep_nvr.release}"
            raise TransientInfrastructureError(
                f"Koji metadata unavailable for timestamp comparison: {package} ({package_nvr}) "
                f"vs {dep_component} ({fixed_dep_nvr_str})"
            )

        # Step 4: Get known package names and validated NEVR for the fixed dependency
        try:
            known_names_result = await _get_known_package_names(dep_component, fixed_dep_nvr)
            if known_names_result is None:
                fixed_dep_nvr_str = f"{fixed_dep_nvr.name}-{fixed_dep_nvr.version}-{fixed_dep_nvr.release}"
                raise TransientInfrastructureError(
                    f"Brew metadata unavailable: could not fetch subpackage list for "
                    f"{dep_component} ({fixed_dep_nvr_str})"
                )
            known_names, real_binary_rpm_count, fixed_dep_nevr = known_names_result
        except InvalidDependencyDataError as e:
            # Invalid dependency record - route to clarification, not retry
            logger.warning(
                f"Invalid dependency data: {dep_component} resolves to {e.actual_component}. "
                f"Requesting clarification."
            )
            return (
                None,
                package_issue_key,
                package_nvr,
                f"invalid_dependency_record:{dep_component}_resolves_to_{e.actual_component}",
            )

        # Step 5: Parse dependency from all architecture installed_pkgs.logs and verify consistency
        # Parse all architectures concurrently
        async def _parse_arch(arch_url, log_content):
            arch_name = arch_url.split("/")[-2]
            return arch_name, await _parse_dependency_from_installed_pkgs_log(
                log_content, dep_component, known_names, BREWHUB_URL
            )

        parse_tasks = [_parse_arch(arch_url, log_content) for arch_url, log_content in installed_pkgs_logs]
        parse_results = await asyncio.gather(*parse_tasks, return_exceptions=True)

        dep_nevrs = {}  # arch -> NEVR
        matched_pkg_names = {}  # arch -> list of pkg names
        missing_archs = list(
            missing_archs_from_error
        )  # Start with architectures missing from BuildLogMissingError
        unresolved_subpackage_match = False  # Track if we found subpackage but couldn't resolve
        unresolved_subpkg_names = set()  # Collect all unresolved subpackage names

        for result in parse_results:
            # Handle any exceptions raised during parsing
            if isinstance(result, Exception):
                raise result

            arch_name, (used_dep_nevr, matched_pkg_names_list) = result
            if used_dep_nevr is not None:
                dep_nevrs[arch_name] = used_dep_nevr
                matched_pkg_names[arch_name] = matched_pkg_names_list
            elif matched_pkg_names_list:
                # Package found in log but couldn't be resolved via Brew (unresolved subpackage)
                unresolved_subpackage_match = True
                unresolved_subpkg_names.update(matched_pkg_names_list)
                missing_archs.append(arch_name)
            else:
                missing_archs.append(arch_name)

        if not dep_nevrs:
            # No dependency found in any architecture log
            # But check if we found evidence of an unresolved subpackage match
            if unresolved_subpackage_match:
                logger.warning(
                    f"Found evidence of {dep_component} subpackage(s) in {package_nvr} installed_pkgs.log "
                    f"but could not resolve via Brew: {unresolved_subpkg_names}. "
                    f"Cannot determine whether rebuilt with fixed dependency."
                )
                subpkg_list = ", ".join(sorted(unresolved_subpkg_names))
                return (
                    None,
                    package_issue_key,
                    package_nvr,
                    f"subpackage_match_needs_verification:{subpkg_list}",
                )

            logger.warning(
                f"Could not find {dep_component} dependency in installed_pkgs.log for {package_nvr} "
                f"(checked {len(installed_pkgs_logs)} architecture(s))"
            )
            # Check if active builds exist before returning False
            if active_candidates:
                logger.warning(
                    f"Dependency not found in {package_nvr} installed_pkgs.log, but {len(active_candidates)} "
                    f"active builds exist. Requesting clarification on active builds."
                )
                active_issues = ", ".join(key for key, _ in active_candidates)
                return None, package_issue_key, package_nvr, f"active_builds_not_closed:{active_issues}"
            return False, package_issue_key, package_nvr, None

        # If some architectures are missing the dependency, cannot trust the result
        if missing_archs:
            logger.warning(
                f"Dependency {dep_component} missing from {len(missing_archs)} architecture(s) "
                f"for {package_nvr}: {', '.join(missing_archs)}. Found in: "
                f"{', '.join(dep_nevrs.keys())}. Needs manual verification."
            )
            return None, package_issue_key, package_nvr, "partial_architecture_coverage"

        # Verify all architectures agree on the dependency version
        unique_nevrs = set(dep_nevrs.values())
        if len(unique_nevrs) > 1:
            logger.warning(
                f"Dependency version conflict across architectures for {package_nvr}: "
                f"{', '.join(f'{arch}={nevr}' for arch, nevr in dep_nevrs.items())}. "
                f"Different buildroots used. Needs manual verification."
            )
            return None, package_issue_key, package_nvr, "architecture_dependency_conflict"

        # All architectures agree — use the common NEVR
        used_dep_nevr = next(iter(dep_nevrs.values()))
        used_dep_nvr = f"{used_dep_nevr.name}-{used_dep_nevr.version}-{used_dep_nevr.release}"
        logger.info(
            f"All {len(dep_nevrs)} architecture(s) agree: {package_nvr} used {dep_component} {used_dep_nvr}"
        )

        # Step 5b: Check if dependency component produces multiple binary packages
        # When the component produces more than one installable binary (after filtering
        # debuginfo/debugsource), we can't be sure which binary carries the fixed code.

        # Collect all matched package names from all architectures for the clarification message
        all_matched_names = set()
        for pkg_list in matched_pkg_names.values():
            all_matched_names.update(pkg_list)

        matched_via_subpackage = real_binary_rpm_count > 1

        # Step 6: Compare NEVRs directly
        is_fixed = used_dep_nevr >= fixed_dep_nevr
        fixed_dep_nvr_str = f"{fixed_dep_nevr.name}-{fixed_dep_nvr.version}-{fixed_dep_nvr.release}"

        if is_fixed:
            if matched_via_subpackage:
                subpkg_list = ", ".join(sorted(all_matched_names))
                logger.info(
                    f"{package} ({package_nvr}) was built with {used_dep_nvr} >= {fixed_dep_nvr_str}, "
                    f"but {dep_component} produces multiple binary RPMs. "
                    f"Matched package(s): {subpkg_list}. "
                    f"Cannot confirm which binary RPM contains the fix — requesting verification."
                )
                return (
                    None,
                    package_issue_key,
                    package_nvr,
                    f"subpackage_match_needs_verification:{subpkg_list}",
                )
            logger.info(
                f"{package} ({package_nvr}) was built with {used_dep_nvr}, "
                f"which is >= fixed dependency {fixed_dep_nvr_str}"
            )
            return True, package_issue_key, package_nvr, None
        logger.info(
            f"{package} ({package_nvr}) was built with {used_dep_nvr}, "
            f"which is < fixed dependency {fixed_dep_nvr_str}"
        )
        # Check if there are active builds that might be newer
        if active_candidates:
            logger.warning(
                f"Closed build {package_nvr} does not have fix, but {len(active_candidates)} "
                f"active builds exist. Requesting clarification on active builds."
            )
            active_issues = ", ".join(key for key, _ in active_candidates)
            return None, package_issue_key, package_nvr, f"active_builds_not_closed:{active_issues}"
        return False, package_issue_key, package_nvr, None

    except TransientInfrastructureError:
        # Re-raise infrastructure errors for task retry
        raise
    except Exception as e:
        raise TransientInfrastructureError(
            f"Build verification failed for {package} in {fix_version}: {e}"
        ) from e


def extract_text_from_adf(adf_body) -> str:
    """Extract plain text from Jira ADF (Atlassian Document Format) comment body.

    The MCP get_jira_details tool returns comment bodies in ADF JSON format:
    {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Actual comment text here"},
                    {"type": "inlineCard", "attrs": {"url": "https://..."}},
                    ...
                ]
            },
            ...
        ]
    }

    This function recursively extracts all "text" values and URLs from inlineCard nodes.

    When MCP returns HTML format, it may contain smartlink tags like:
    <custom data-type="smartlink">https://redhat.atlassian.net/browse/RHEL-123</custom>

    We extract issue keys from these URLs to make them searchable.
    """
    if isinstance(adf_body, str):
        # Handle HTML smartlink tags: extract issue keys from URLs
        # Pattern: <custom data-type="smartlink"...>URL</custom>
        import re

        result = adf_body
        # Find all smartlink tags and extract issue keys from their URLs
        smartlink_pattern = r'<custom[^>]*data-type="smartlink"[^>]*>([^<]+)</custom>'
        for match in re.finditer(smartlink_pattern, result):
            url = match.group(1)
            # Extract issue key from URL (e.g., RHEL-234905 from https://.../browse/RHEL-234905)
            issue_match = re.search(r"browse/([A-Z]+-\d+)", url)
            if issue_match:
                issue_key = issue_match.group(1)
                # Replace the entire smartlink tag with just the issue key
                result = result.replace(match.group(0), issue_key)
        return result
    if isinstance(adf_body, dict):
        node_type = adf_body.get("type")
        # If this is a text node, return its text
        if node_type == "text" and "text" in adf_body:
            return adf_body["text"]
        # If this is an inlineCard, extract the URL (contains issue key)
        if node_type == "inlineCard" and "attrs" in adf_body and "url" in adf_body["attrs"]:
            return adf_body["attrs"]["url"]
        # Recursively extract from content array
        if "content" in adf_body:
            return " ".join(extract_text_from_adf(item) for item in adf_body["content"])
        return ""
    if isinstance(adf_body, list):
        return " ".join(extract_text_from_adf(item) for item in adf_body)
    return ""


def __traces_sampler(sampling_context: dict) -> float:
    """
    Compute sample rate or sampling decision for a transaction.
    https://docs.sentry.io/platforms/python/performance/
    https://docs.sentry.io/platforms/python/configuration/sampling

    Args:
        sampling_context: context data

    Returns: traces sample rate (between 0 and 1)
    """
    if rate := os.getenv("SENTRY_TRACES_SAMPLE_RATE"):
        return float(rate)
    # TODO: Take sampling_context into account
    return 0.5


def init_sentry() -> None:
    """Initialize Sentry, if the DSN is set."""
    if not (dsn := os.getenv("SENTRY_DSN")):
        # no DSN, no reporting
        return

    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.litellm import LiteLLMIntegration
    from sentry_sdk.integrations.logging import (
        ignore_logger,
        ignore_logger_for_sentry_logs,
    )

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT"),
        enable_logs=True,
        traces_sampler=__traces_sampler,
        # Add data like inputs and responses;
        # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        stream_gen_ai_spans=True,
        send_default_pii=True,
        integrations=[
            AsyncioIntegration(),
            LiteLLMIntegration(),
        ],
    )

    for ignored_logger in ("agent.redis", "agent.task_loop", "agent.trajectory"):
        ignore_logger(ignored_logger)
        ignore_logger_for_sentry_logs(ignored_logger)
