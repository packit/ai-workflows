"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import gzip
import logging
import os
import re
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
from specfile.utils import EVR

from ymir.common.base_utils import is_cs_branch
from ymir.common.constants import BREWHUB_URL, CENTOS_STREAM_KOJIHUB_URL
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.common.version_utils import (
    construct_internal_branch_name,
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
    package: str, fix_version: str, available_tools: list[Tool]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]] | None]:
    """Search Jira for completed builds with Fixed in Build set.

    Args:
        package: Package name
        fix_version: RHEL fix version (e.g., "rhel-10.2.z")
        available_tools: List of available tools for Jira queries

    Returns:
        Tuple of (closed_builds, active_builds) where:
        - closed_builds: List of (issue_key, nvr) tuples for closed builds
        - active_builds: List of (issue_key, nvr) tuples for active builds, or None if query failed
    """
    from ymir.common.version_utils import get_fix_version_variants

    fix_version_variants = get_fix_version_variants(fix_version)
    escaped_versions = [v.replace('"', '\\"') for v in fix_version_variants]
    fix_version_clause = ", ".join(f'"{v}"' for v in escaped_versions)

    # Escape package name to prevent JQL injection
    escaped_package = package.replace("\\", "\\\\").replace('"', '\\"')

    closed_jql = (
        f'project = RHEL AND component = "{escaped_package}" AND '
        f"fixVersion in ({fix_version_clause}) AND "
        f"status in (Closed, Done) AND "
        f'resolution in ("Done", "Done-Errata") AND '
        f"customfield_10578 IS NOT EMPTY"
    )
    closed_results = await run_tool(
        "search_jira_issues",
        available_tools=available_tools,
        jql=closed_jql,
        fields=["key", "customfield_10578"],
        max_results=50,
    )

    closed_results = closed_results if closed_results and isinstance(closed_results, list) else []

    # Also search for active builds (not yet closed but have Fixed in Build set)
    # These need validation since they might be rejected/abandoned
    active_jql = (
        f'project = RHEL AND component = "{escaped_package}" AND '
        f"fixVersion in ({fix_version_clause}) AND "
        f"status not in (Closed, Done) AND "
        f"customfield_10578 IS NOT EMPTY"
    )
    try:
        active_results = await run_tool(
            "search_jira_issues",
            available_tools=available_tools,
            jql=active_jql,
            fields=["key", "customfield_10578", "status"],
            max_results=50,
        )
        # Validate response - None or non-list is invalid, but empty list is valid
        if active_results is None or not isinstance(active_results, list):
            logger.warning(f"Invalid response from active builds query for {package}: {type(active_results)}")
            active_results = None
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
    if len(closed_results) >= 50:
        logger.warning(
            f"Found {len(closed_results)} closed builds for {package} in {fix_version}, "
            f"hit max_results limit. May have missed a rebuild."
        )

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
) -> tuple[str, str, EVR] | None:
    """Select build with highest EVR from candidate NVRs.

    Args:
        candidates: List of (issue_key, nvr) tuples
        expected_package: Expected package name to validate against

    Returns:
        Tuple of (nvr, issue_key, evr) for highest build, or None if none found
    """
    if not candidates:
        return None

    # Fetch all builds concurrently
    build_futures = [asyncio.to_thread(_get_koji_build, BREWHUB_URL, nvr) for _, nvr in candidates]
    builds = await asyncio.gather(*build_futures, return_exceptions=True)

    # Associate successful builds with their metadata
    candidate_builds = []
    for (issue_key, nvr), build in zip(candidates, builds, strict=True):
        if isinstance(build, Exception):
            logger.warning(f"Failed to fetch build {nvr}: {build}")
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
            evr = _evr_from_build(build)
            candidate_builds.append((evr, nvr, issue_key))

    if not candidate_builds:
        return None

    # Sort by EVR descending and pick highest
    candidate_builds.sort(key=lambda x: x[0], reverse=True)
    latest_evr, package_nvr, package_issue_key = candidate_builds[0]

    return package_nvr, package_issue_key, latest_evr


async def _fetch_root_log(package_nvr: str) -> list[tuple[str, str]]:
    """Fetch root.log from Brew for the given package NVR across all architectures.

    Different architectures can have different buildroots, so fetches logs from all
    available architectures. Caller must verify consistency across logs.

    Args:
        package_nvr: Package NVR (e.g., "go-fdo-client-1.0.0-4.el10_2.7")

    Returns:
        List of (architecture_url, log_content) tuples. Empty if no logs found.
    """
    # Parse NVR to construct root.log URL
    nvr_match = re.match(r"^(.+)-([^-]+)-([^-]+)$", package_nvr)
    if not nvr_match:
        logger.warning(f"Could not parse package NVR: {package_nvr}")
        return []

    package_name, version, release = nvr_match.groups()

    # Try multiple architectures concurrently with overall 30s timeout
    architectures = ["x86_64", "aarch64", "ppc64le", "s390x"]
    root_log_urls = [
        f"https://brewweb.engineering.redhat.com/brew/packages/"
        f"{package_name}/{version}/{release}/data/logs/{arch}/root.log"
        for arch in architectures
    ]

    async def check_and_fetch_log(client: httpx.AsyncClient, url: str) -> tuple[str, bytes | None]:
        """Check if root.log exists with HEAD, then fetch if available."""
        try:
            # First check if file exists with HEAD request
            head_response = await client.head(url)
            if head_response.status_code != 200:
                logger.debug(f"root.log not available at {url} (HTTP {head_response.status_code})")
                return url, None

            # File exists, fetch it
            response = await client.get(url)
            if response.status_code == 200:
                return url, response.content
        except Exception as e:
            logger.debug(f"Could not fetch {url}: {e}")
        return url, None

    fetch_tasks = []
    try:
        # Overall 30-second timeout for entire operation
        async with asyncio.timeout(30.0):
            # Per-request timeout of 10s as secondary safeguard
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                # Fetch all architecture logs to verify consistency across architectures
                fetch_tasks = [asyncio.create_task(check_and_fetch_log(client, url)) for url in root_log_urls]
                results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                # Process all fetched logs
                decoded_logs = []
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    url, content = result
                    if content:
                        try:
                            # Check if gzipped and decompress
                            if content[:2] == b"\x1f\x8b":
                                content = gzip.decompress(content)
                            # Decode to string
                            decoded_logs.append((url, content.decode("utf-8", errors="replace")))
                        except Exception as e:
                            logger.warning(f"Failed to decompress/decode root.log from {url}: {e}")
                            continue

                if not decoded_logs:
                    logger.warning(f"Could not fetch root.log for {package_nvr} from any architecture")
                    return []

                logger.debug(
                    f"Found root.log for {package_nvr} from {len(decoded_logs)} architecture(s): "
                    f"{[url.split('/')[-2] for url, _ in decoded_logs]}"
                )
                return decoded_logs

    except TimeoutError:
        logger.warning(f"Timeout (30s) fetching root.log for {package_nvr}")
        # Cancel any outstanding tasks
        for task in fetch_tasks:
            if not task.done():
                task.cancel()
        # Await them to clean up
        if fetch_tasks:
            await asyncio.gather(*fetch_tasks, return_exceptions=True)
        return []


async def _get_known_package_names(dep_component: str, fixed_dep_nvr: str) -> list[str] | None:
    """Get list of binary package names produced by the dependency source build.

    Args:
        dep_component: Source component name (e.g., "golang")
        fixed_dep_nvr: Fixed dependency NVR

    Returns:
        List of binary package names (includes source component name), or None if metadata unavailable
    """
    fixed_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, fixed_dep_nvr)
    if not fixed_build:
        logger.error(f"Could not find build info for fixed dependency: {fixed_dep_nvr}")
        return None

    # Validate that the fixed build is actually for the expected dependency
    build_name = fixed_build.get("name")
    if build_name != dep_component:
        logger.error(
            f"Fixed dependency NVR {fixed_dep_nvr} resolves to package '{build_name}', "
            f"not expected component '{dep_component}'. Possible data corruption or injection."
        )
        return None

    build_id = fixed_build.get("build_id")
    if not build_id:
        logger.error(f"No build_id for {fixed_dep_nvr}")
        return None

    # Get list of binary package names produced by this source build
    try:
        session = koji.ClientSession(BREWHUB_URL)
        rpms = await asyncio.to_thread(session.listRPMs, buildID=build_id)
        if not isinstance(rpms, list):
            logger.error(f"Invalid RPM list response from Koji for build {build_id}")
            return None
        known_names = [rpm.get("name") for rpm in rpms if rpm.get("name")]
    except Exception as e:
        logger.error(f"Failed to fetch RPM list for {fixed_dep_nvr}: {e}")
        return None

    # Also include the source component name itself
    if dep_component not in known_names:
        known_names.insert(0, dep_component)

    logger.debug(f"Known package names for {dep_component}: {known_names[:5]}...")
    return known_names


def _parse_dependency_from_root_log(
    root_log: str, dep_component: str, known_names: list[str]
) -> tuple[str | None, int | None]:
    """Parse root.log to find which version of dependency was installed.

    Args:
        root_log: Content of root.log
        dep_component: Source component name
        known_names: List of known binary package names to search for

    Returns:
        Tuple of (nvr, epoch) where nvr is in format "name-version-release"
        and epoch is the epoch number if present in root.log, else None
    """
    # Pattern: "Installing: [epoch:]<known_name>-<version>-<release>.<arch>"
    # Sort by length descending to match longer names first (e.g., golang-bin before golang)
    for pkg_name in sorted(known_names, key=len, reverse=True):
        # Escape package name for regex (handles dots, etc.)
        escaped_name = re.escape(pkg_name)
        # Pattern: optional epoch, exact package name, then version-release.arch
        # Use word boundary or hyphen after name to prevent matching subpackages as parent
        pattern = rf"Installing:\s+(?:(\d+):)?{escaped_name}-([^\s]+)"

        for line in root_log.splitlines():
            match = re.search(pattern, line)
            if match:
                epoch_str = match.group(1)  # May be None
                version_release = match.group(2)
                # Remove architecture suffix
                arch_pattern = r"\.(x86_64|aarch64|ppc64le|s390x|i686|noarch)$"
                version_release = re.sub(arch_pattern, "", version_release)
                # Keep epoch as None when absent (allows fallback to Koji's epoch)
                epoch = int(epoch_str) if epoch_str else None
                nvr = f"{dep_component}-{version_release}"
                logger.info(
                    f"Found {dep_component} package {pkg_name} in root.log: "
                    f"{f'{epoch}:' if epoch else ''}{nvr}"
                )
                return nvr, epoch

    logger.warning(f"Could not find {dep_component} or its subpackages in root.log")
    return None, None


async def _compare_dependency_evrs(
    used_dep_nvr: str, used_dep_epoch: int | None, fixed_dep_nvr: str, dep_component: str
) -> bool | None:
    """Compare used dependency EVR with fixed dependency EVR.

    Args:
        used_dep_nvr: NVR of dependency that was used during build
        used_dep_epoch: Epoch from root.log (None if not present)
        fixed_dep_nvr: NVR of fixed dependency
        dep_component: Expected dependency component name for validation

    Returns:
        True if used_dep >= fixed_dep, False if used_dep < fixed_dep, None if comparison failed
    """
    # Get build info for both dependencies
    used_build, fixed_build = await asyncio.gather(
        asyncio.to_thread(_get_koji_build, BREWHUB_URL, used_dep_nvr),
        asyncio.to_thread(_get_koji_build, BREWHUB_URL, fixed_dep_nvr),
    )

    if not used_build:
        logger.error(f"Could not find build info for used dependency: {used_dep_nvr}")
        return None

    if not fixed_build:
        logger.error(f"Could not find build info for fixed dependency: {fixed_dep_nvr}")
        return None

    # Validate both builds are for the expected dependency component
    used_name = used_build.get("name")
    if used_name != dep_component:
        logger.error(
            f"Used dependency NVR {used_dep_nvr} resolves to package '{used_name}', "
            f"not expected component '{dep_component}'. Data validation failed."
        )
        return None

    fixed_name = fixed_build.get("name")
    if fixed_name != dep_component:
        logger.error(
            f"Fixed dependency NVR {fixed_dep_nvr} resolves to package '{fixed_name}', "
            f"not expected component '{dep_component}'. Data validation failed."
        )
        return None

    fixed_evr = _evr_from_build(fixed_build)

    # Use the epoch from root.log if present, otherwise use Koji's epoch
    # (root.log epoch takes precedence as it's what was actually installed)
    if used_dep_epoch is not None:
        used_evr = EVR(
            epoch=used_dep_epoch,
            version=used_build["version"],
            release=used_build["release"],
        )
    else:
        used_evr = _evr_from_build(used_build)

    return used_evr >= fixed_evr


async def _compare_build_timestamps(
    package: str,
    package_nvr: str,
    dep_component: str,
    fixed_dep_nvr: str,
) -> bool | None:
    """Compare build timestamps as fallback when root.log is unavailable.

    Args:
        package: Package name (e.g., "go-fdo-client")
        package_nvr: Package NVR to check
        dep_component: Dependency component name (e.g., "golang")
        fixed_dep_nvr: Fixed dependency NVR

    Returns:
        True if package was built after dependency fix,
        False if built before fix,
        None if metadata unavailable or validation failed
    """
    package_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, package_nvr)
    fixed_build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, fixed_dep_nvr)

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

    # Validate fixed build is for expected component
    if fixed_build.get("name") != dep_component:
        logger.error(
            f"Fixed dependency {fixed_dep_nvr} has name '{fixed_build.get('name')}', "
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
    fixed_dep_nvr: str,
    available_tools: list[Tool],
) -> tuple[bool | None, str | None, str | None, str | None]:
    """Check if the package's latest build already used the fixed dependency.

    Searches for the most recent completed build of the package in the given
    fix_version, fetches its root.log from Brew, and checks which version of
    the dependency was used during that build.

    Args:
        package: Package name (e.g., "go-fdo-client")
        fix_version: RHEL fix version (e.g., "rhel-10.2.z")
        dep_component: Dependency component name (e.g., "golang")
        fixed_dep_nvr: The fixed dependency NVR from "Fixed in Build" field
        available_tools: List of available tools for Jira queries

    Returns:
        Tuple of (already_fixed, package_issue_key, package_nvr, reason):
        - already_fixed: True if confirmed fixed (root.log proves it),
                        False if needs rebuild (root.log shows old version or built before fix),
                        None if needs manual action
        - package_issue_key: Jira issue key for the existing build (if found)
        - package_nvr: NVR of the existing build (if found)
        - reason: When already_fixed is None, explains why (e.g., "built_after_fix_no_rootlog",
                 "koji_metadata_unavailable", "evr_comparison_failed")
    """
    try:
        # Step 1: Find builds in Jira
        closed_candidates, active_candidates = await _find_completed_builds_jira(
            package, fix_version, available_tools
        )

        # Handle case where active query failed
        if active_candidates is None:
            logger.warning(f"Active builds query failed for {package} in {fix_version}")
            # If we have closed candidates, we can still check them
            # But if we don't, we need clarification since we don't know if active builds exist
            if not closed_candidates:
                logger.warning(
                    f"No closed builds found and active builds query failed for {package}. "
                    f"Cannot determine build status."
                )
                return None, None, None, "jira_query_failed"

        if not closed_candidates and not active_candidates:
            logger.info(f"No builds found for {package} in {fix_version}")
            return False, None, None, None

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
            # If active query failed, we need clarification
            if active_candidates is None:
                logger.warning(
                    f"Closed builds invalid and active builds query failed for {package}. "
                    f"Cannot determine build status."
                )
                return None, None, None, "jira_query_failed"
            return False, None, None, None

        package_nvr, package_issue_key, latest_evr = result
        logger.info(
            f"Found latest build for {package} by EVR: {package_nvr} "
            f"(issue: {package_issue_key}, EVR: {latest_evr})"
        )

        # Step 3: Fetch root.log from Brew (all architectures)
        root_logs = await _fetch_root_log(package_nvr)
        if not root_logs:
            # Fallback: compare build timestamps
            logger.info(
                f"root.log unavailable for {package_nvr}, checking timestamps to determine next action"
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
                return None, package_issue_key, package_nvr, "built_after_fix_no_rootlog"
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
            # Timestamp comparison failed (metadata unavailable) - cannot determine
            logger.warning(
                f"Could not determine build order for {package} ({package_nvr}) and "
                f"{dep_component} ({fixed_dep_nvr}). Koji metadata unavailable."
            )
            return None, package_issue_key, package_nvr, "timestamp_comparison_failed"

        # Step 4: Get known package names for the dependency
        known_names = await _get_known_package_names(dep_component, fixed_dep_nvr)
        if known_names is None:
            logger.error(
                f"Could not fetch subpackage list for {dep_component} ({fixed_dep_nvr}). "
                f"Koji metadata unavailable."
            )
            return None, package_issue_key, package_nvr, "evr_comparison_failed"

        # Step 5: Parse dependency from all architecture root.logs and verify consistency
        dep_versions = {}
        missing_archs = []
        for arch_url, log_content in root_logs:
            arch_name = arch_url.split("/")[-2]
            used_dep_nvr, used_dep_epoch = _parse_dependency_from_root_log(
                log_content, dep_component, known_names
            )
            if used_dep_nvr:
                dep_versions[arch_name] = (used_dep_nvr, used_dep_epoch)
            else:
                missing_archs.append(arch_name)

        if not dep_versions:
            # No dependency found in any architecture log
            logger.warning(
                f"Could not find {dep_component} dependency in root.log for {package_nvr} "
                f"(checked {len(root_logs)} architecture(s))"
            )
            # Check if active builds exist before returning False
            if active_candidates:
                logger.warning(
                    f"Dependency not found in {package_nvr} root.log, but {len(active_candidates)} "
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
                f"{', '.join(dep_versions.keys())}. Needs manual verification."
            )
            return None, package_issue_key, package_nvr, "partial_architecture_coverage"

        # Verify all architectures agree on the dependency version
        # Normalize epochs through Koji to handle None vs 0 equivalence
        normalized_evrs = {}
        for arch, (nvr, epoch_from_log) in dep_versions.items():
            build = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, nvr)
            if not build:
                logger.error(f"Could not fetch Koji metadata for {nvr} from {arch} architecture")
                return None, package_issue_key, package_nvr, "evr_comparison_failed"

            # Use epoch from root.log if present, otherwise Koji's epoch
            if epoch_from_log is not None:
                evr = EVR(
                    epoch=epoch_from_log,
                    version=build["version"],
                    release=build["release"],
                )
            else:
                evr = _evr_from_build(build)

            normalized_evrs[arch] = (nvr, evr)

        # Compare normalized EVRs
        unique_evrs = {evr for _, evr in normalized_evrs.values()}
        if len(unique_evrs) > 1:
            # Different architectures have different dependency versions
            logger.warning(
                f"Dependency version conflict across architectures for {package_nvr}: "
                f"{', '.join(f'{arch}={nvr} (EVR: {evr})' for arch, (nvr, evr) in normalized_evrs.items())}. "
                f"Different buildroots used. Needs manual verification."
            )
            return None, package_issue_key, package_nvr, "architecture_dependency_conflict"

        # All architectures agree - use the common version (pick first)
        used_dep_nvr, _ = dep_versions[next(iter(dep_versions))]
        # Use epoch from first architecture's root.log for final comparison
        used_dep_epoch = dep_versions[next(iter(dep_versions))][1]
        logger.info(
            f"All {len(dep_versions)} architecture(s) agree: {package_nvr} used {dep_component}"
            f" {used_dep_nvr}"
        )

        # Step 6: Compare EVRs
        is_fixed = await _compare_dependency_evrs(used_dep_nvr, used_dep_epoch, fixed_dep_nvr, dep_component)

        if is_fixed is True:
            logger.info(
                f"{package} ({package_nvr}) was built with {used_dep_nvr}, "
                f"which is >= fixed dependency {fixed_dep_nvr}"
            )
            return True, package_issue_key, package_nvr, None
        if is_fixed is False:
            logger.info(
                f"{package} ({package_nvr}) was built with {used_dep_nvr}, "
                f"which is < fixed dependency {fixed_dep_nvr}"
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
        # EVR comparison failed due to missing Koji metadata or validation error
        logger.error(
            f"Could not compare dependency EVRs for {package} ({package_nvr}). "
            f"Koji metadata unavailable or validation failed."
        )
        return None, package_issue_key, package_nvr, "evr_comparison_failed"

    except Exception as e:
        logger.exception(
            f"Error checking if package was built with fixed dependency: {e}. "
            "Falling back to standard rebuild check."
        )
        return False, None, None, None


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
