"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import gzip
import logging
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, TypeVar, overload

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
from specfile.exceptions import SpecfileException
from specfile.sourcelist import Sourcelist
from specfile.sources import Patches, Sources
from specfile.utils import NEVR, NEVRA

from ymir.common.base_utils import is_cs_branch
from ymir.common.config import current_rhel_config_override
from ymir.common.constants import BREWHUB_URL, CENTOS_STREAM_KOJIHUB_URL
from ymir.common.logging_setup import get_trajectory_writeable
from ymir.common.version_utils import (
    construct_internal_branch_name,
    get_fix_version_variants,
    get_maintenance_majors,
    nevr_matches_fix_version_stream,
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


def _nevr_from_build(build: dict) -> NEVR:
    """Extract a NEVR from complete Brew metadata; an explicit null epoch means zero."""
    try:
        for field_name in ("name", "version", "release"):
            if not isinstance(build[field_name], str) or not build[field_name]:
                raise ValueError(f"Invalid Brew {field_name}: {build[field_name]!r}")
        raw_epoch = build.get("epoch")
        if raw_epoch is None:
            epoch = 0
        elif isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
            # int() would silently accept booleans and truncate floats,
            # letting fabricated epochs influence build ordering.
            raise ValueError(f"Invalid Brew epoch: {raw_epoch!r}")
        else:
            epoch = raw_epoch
        return NEVR(
            name=build["name"],
            epoch=epoch,
            version=build["version"],
            release=build["release"],
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Incomplete or invalid Brew build metadata for %r", build.get("name"), exc_info=True)
        raise


def get_epochless_nvr_string(nevr: NEVR) -> str:
    """Serialize a resolved NEVR for external APIs that require an epochless NVR."""
    return f"{nevr.name}-{nevr.version}-{nevr.release}"


@dataclass(frozen=True)
class BrewBuild:
    """A fully qualified build identity and the metadata returned by Brew."""

    metadata: dict
    nevr: NEVR = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "nevr", _nevr_from_build(self.metadata))


@dataclass
class BuildVerificationReport:
    """All discovered Jira build references and the evidence needed for clarification."""

    candidates: list[tuple[str, str, str]] = field(default_factory=list)
    resolved_builds: dict[str, BrewBuild] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    active_problems: list[str] = field(default_factory=list)
    selected_issue_key: str | None = None
    selected_nevr: NEVR | None = None
    reason: str | None = None

    def describe(self, max_chars: int | None = None) -> str:
        """Render the report as human-readable text.

        Args:
            max_chars: Optional cap on the rendered length. Jira rejects comment
                bodies beyond its size limit (~32k characters); when the full
                candidate list does not fit, the candidates that fit are kept and
                the remainder is summarized in a single line so a clarification
                comment can always be posted.
        """

        def candidate_lines() -> list[str]:
            lines = []
            for issue_key, nvr, status in self.candidates:
                build = self.resolved_builds.get(nvr)
                details = "Brew metadata could not be resolved"
                if build is not None:
                    details = f"Brew NEVR {build.nevr}"
                    if (build_id := build.metadata.get("build_id")) is not None:
                        details += f", build ID {build_id}"
                    if (completed := build.metadata.get("completion_time")) is not None:
                        details += f", completed {completed}"
                lines.append(f"- {issue_key} ({status}), Fixed in Build: {nvr}; {details}")
            return lines

        def render(lines: list[str]) -> str:
            text = ["Referenced builds:"]
            if not self.candidates:
                text.append("No build references could be retrieved from Jira.")
            text.extend(lines)
            if self.problems or self.active_problems:
                text.append("Verification problems:")
                text.extend(f"- {problem}" for problem in self.problems + self.active_problems)
            return "\n".join(text)

        all_lines = candidate_lines()
        text = render(all_lines)
        if max_chars is None or len(text) <= max_chars:
            return text

        omitted = "({remaining} more build reference(s) omitted to stay within the comment size limit)"
        kept: list[str] = []
        for line in all_lines:
            remaining = len(all_lines) - len(kept) - 1
            if len(render([*kept, line, omitted.format(remaining=remaining)])) > max_chars:
                break
            kept.append(line)
        remaining = len(all_lines) - len(kept)
        if remaining > 0:
            kept.append(omitted.format(remaining=remaining))
        text = render(kept)
        # Last resort (e.g., the problems list alone exceeds the cap): hard-truncate.
        return text[:max_chars]


async def build_tags_in_stream(build: BrewBuild, fix_version: str) -> tuple[str, list[str], list[str]] | None:
    """Return the build's tags that belong to the fix version's stream.

    The dist tag is a convention; tag membership is the authoritative signal.
    This answers "is this build part of the stream's pipeline at all": any tag
    under the stream's branch prefix proves membership — a build carrying none
    of them is a scratch/side-tag build or belongs to another stream. Y-stream
    and z-stream fix versions of the same release share the branch, so both are
    covered by the same prefix.

    Returns ``(branch, shipped_tags, in_progress_tags)``, where shipped tags are
    the release tag and the pending-stage tags (committed to compose/errata)
    and in-progress tags are candidate/gate/override-stage tags (fix being
    worked on, not yet committed). Both lists empty means the build is not part
    of the stream. Returns ``None`` when the check cannot be performed
    (unparseable fix version, missing build id, or a Brew failure) — callers
    must fail open on ``None``.
    """
    parsed = parse_rhel_version(fix_version)
    if not parsed:
        return None
    major, minor, _is_zstream = parsed
    branch = construct_internal_branch_name(major, minor)
    build_id = build.metadata.get("build_id")
    if not build_id:
        return None
    try:
        tags = await asyncio.to_thread(lambda: koji.ClientSession(BREWHUB_URL).listTags(build=build_id))
    except Exception as error:
        logger.warning("Could not list tags of build %s: %s", build.nevr, error)
        return None
    names = [tag.get("name") for tag in tags or [] if isinstance(tag.get("name"), str)]
    matched = sorted(name for name in names if name == branch or name.startswith(f"{branch}-"))
    shipped = sorted(name for name in matched if name == branch or name.endswith(("-pending", "-z-pending")))
    in_progress = [name for name in matched if name not in shipped]
    return branch, shipped, in_progress


async def resolve_brew_build(nvr: str) -> BrewBuild | None:
    """Resolve a Jira Fixed in Build NVR against Brew before comparing versions.

    Epoch-bearing values are looked up by their epochless form (koji's getBuild
    does not match epoch-bearing strings); the returned BrewBuild carries the
    authoritative epoch from Brew metadata.

    Raises PermanentValidationError when the value cannot be a Brew NVR at all
    (free text, typos), so callers can request a correction instead of retrying.
    """
    try:
        nevr = NEVR.from_string(nvr)
    except (SpecfileException, ValueError) as error:
        raise PermanentValidationError(f"Not a valid Brew NVR: {nvr!r} ({error})") from error
    try:
        metadata = await asyncio.to_thread(_get_koji_build, BREWHUB_URL, get_epochless_nvr_string(nevr))
    except (koji.GenericError, httpx.HTTPError, ConnectionError, OSError, TimeoutError) as error:
        raise TransientInfrastructureError(f"Brew build lookup failed for {nvr}: {error}") from error
    if metadata is None:
        logger.warning("Build %s was not found in Brew", nvr)
        return None
    return BrewBuild(metadata)


def get_all_sources(spec: Specfile) -> Sources:
    parsed_sections = spec.parsed_sections
    sourcelists = [Sourcelist.parse(s, context=spec) for s in parsed_sections if s.id == "sourcelist"]
    return Sources(spec.tags(parsed_sections.package).content, sourcelists, context=spec)


def get_all_patches(spec: Specfile) -> Patches:
    parsed_sections = spec.parsed_sections
    patchlists = [Sourcelist.parse(s, context=spec) for s in parsed_sections if s.id == "patchlist"]
    return Patches(spec.tags(parsed_sections.package).content, patchlists, context=spec)


def _get_latest_koji_build(koji_url: str, tag: str, package: str) -> dict | None:
    """Query a build tag for the latest build of the given package."""
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
        logger.warning("Koji build %s has no source", build.get("build_id"))
        raise ValueError("Koji build has no source")

    repository, separator, source_ref = source.rpartition("#")
    if not separator or not repository or not source_ref:
        logger.warning("Koji build %s has an invalid source: %r", build.get("build_id"), source)
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
        fixed_dep_nevr: The authoritative NEVR that was resolved
    """

    def __init__(self, dep_component: str, actual_component: str, fixed_dep_nevr: NEVR):
        self.dep_component = dep_component
        self.actual_component = actual_component
        self.fixed_dep_nevr = fixed_dep_nevr
        super().__init__(
            f"Fixed dependency NVR {get_epochless_nvr_string(fixed_dep_nevr)} "
            f"resolves to package '{actual_component}', not expected component '{dep_component}'. "
            f"Possible data corruption or injection."
        )


async def _get_latest_build_from_tags(
    package: str,
    *tags: str,
) -> tuple[NEVR, str]:
    results = await asyncio.gather(
        *(asyncio.to_thread(_get_latest_koji_build, BREWHUB_URL, tag, package) for tag in tags),
    )
    latest: tuple[NEVR, str] | None = None
    for build in results:
        if build is None:
            continue
        nevr = _nevr_from_build(build)
        if latest is None or latest[0] < nevr:
            latest = (nevr, build["build_id"])
    if latest is None:
        logger.info("No Brew builds of %s found in %s", package, tags)
        raise NoBuildFoundError(f"There are no builds of {package} in {' or '.join(tags)}")
    nevr, build_id = latest
    session = koji.ClientSession(BREWHUB_URL)
    metadata = await asyncio.to_thread(session.getBuild, build_id, strict=True)
    _, source_ref = parse_koji_build_source(metadata)
    return nevr, source_ref


async def get_latest_candidate_build(package: str, dist_git_branch: str) -> tuple[NEVR, str]:
    return await _get_latest_build_from_tags(
        package,
        f"{dist_git_branch}-candidate",
        f"{dist_git_branch}-z-candidate",
    )


async def get_latest_z_pending_build(package: str, dist_git_branch: str) -> tuple[NEVR, str]:
    return await _get_latest_build_from_tags(
        package,
        f"{dist_git_branch}-z-pending",
    )


async def get_latest_buildroot_build(package: str, dist_git_branch: str) -> tuple[NEVR, str]:
    return await _get_latest_build_from_tags(
        package,
        f"{dist_git_branch}-buildrequires",
    )


def _resolve_buildroot_checks(
    target_branch: str, fix_version: str, rhel_config: dict | None = None
) -> list[tuple[str, str]]:
    """Return a list of (hub_url, build_tag) pairs to verify.

    For CS branches with a Z-stream fix_version, both the CentOS Stream
    buildroot and the Brew Z-stream buildroot are checked (CS-first
    approach produces two builds).  CS branches in maintenance phase
    (z-stream only, no y-stream) are an exception: only the Brew
    Z-stream buildroot is checked since the CentOS Stream buildroot is stale.
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
    fixed_in_build: NEVR,
    fix_version: str = "",
) -> bool:
    """Check if the dependency's fixed build (or newer) is in all relevant buildroots.

    Queries the selected build-service hubs based on ``target_branch`` and
    ``fix_version``.  For CS Z-stream fixes, both the CentOS Stream and Brew
    Z-stream buildroots are checked (unless the major version is in
    maintenance, in which case only Brew is checked).
    """
    from ymir.common.config import load_rhel_config

    try:
        rhel_config = await load_rhel_config()
    except (koji.GenericError, httpx.HTTPError, ConnectionError, OSError, TimeoutError, ToolError) as error:
        logger.warning("Buildroot configuration lookup failed; retrying: %s", error)
        raise TransientInfrastructureError(f"Buildroot configuration lookup failed: {error}") from error
    checks = _resolve_buildroot_checks(target_branch, fix_version, rhel_config)

    # The caller has already resolved Jira's epochless NVR against Brew.
    tag_futures = [
        asyncio.to_thread(_get_latest_koji_build, koji_url, build_tag, dep_component)
        for koji_url, build_tag in checks
    ]
    try:
        tag_results_raw = await asyncio.gather(*tag_futures)
    except (koji.GenericError, httpx.HTTPError, ConnectionError, OSError, TimeoutError, ToolError) as error:
        logger.warning("Brew buildroot lookup failed for %s; retrying: %s", dep_component, error)
        raise TransientInfrastructureError(
            f"Brew buildroot lookup failed for {dep_component}: {error}"
        ) from error
    tag_results = list(zip([tag for _, tag in checks], tag_results_raw, strict=True))

    fixed_nevr = fixed_in_build
    if fixed_nevr.name != dep_component:
        logger.warning("Brew fixed build %s does not match expected component %s", fixed_nevr, dep_component)
        raise InvalidDependencyDataError(dep_component, fixed_nevr.name, fixed_nevr)

    for build_tag, latest in tag_results:
        if not latest:
            logger.info(f"No builds of {dep_component} found in {build_tag}")
            return False

        latest_nevr = _nevr_from_build(latest)
        if latest_nevr.name != dep_component:
            logger.warning(
                "Brew buildroot %s contains %s, expected %s", build_tag, latest_nevr, dep_component
            )
            raise InvalidDependencyDataError(dep_component, latest_nevr.name, fixed_nevr)

        if latest_nevr >= fixed_nevr:
            logger.info(f"{dep_component} in {build_tag}: {latest_nevr} >= {fixed_nevr}")
        else:
            logger.info(
                f"{dep_component} in {build_tag}: {latest_nevr} < {fixed_nevr} — not yet in buildroot"
            )
            return False

    return True


async def _find_completed_builds_jira(
    package: str,
    fix_version: str,
    available_tools: list[Tool],
    jira_issue: str | None = None,
    report: BuildVerificationReport | None = None,
    downstream_component: str | None = None,
) -> tuple[list[tuple[str, NEVR]], list[tuple[str, NEVR]] | None]:
    """Search Jira for completed builds with Fixed in Build set.

    Args:
        package: Package name
        fix_version: RHEL fix version (e.g., "rhel-10.2.z")
        available_tools: List of available tools for Jira queries
        jira_issue: Jira issue key to exclude from search (the ticket being triaged)
        downstream_component: Raw modular Downstream Component Name, when present.

    Returns:
        Tuple of (closed_builds, active_builds) where:
        - closed_builds: List of (issue_key, authoritative NEVR) tuples
        - active_builds: List of (issue_key, authoritative NEVR) tuples, or None if query failed
    """
    fix_version_variants = get_fix_version_variants(fix_version)
    escaped_versions = [v.replace('"', '\\"') for v in fix_version_variants]
    fix_version_clause = ", ".join(f'"{v}"' for v in escaped_versions)

    # Escape package name to prevent JQL injection
    escaped_package = package.replace("\\", "\\\\").replace('"', '\\"')

    stream_clause = ""
    if downstream_component and "/" in downstream_component:
        escaped_component = downstream_component.replace("\\", "\\\\").replace('"', '\\"')
        stream_clause = f' AND cf[10669] = "{escaped_component}"'

    exclude_clause = f' AND key != "{jira_issue}"' if jira_issue else ""

    closed_jql = (
        f'project = RHEL AND component = "{escaped_package}" AND '
        f"fixVersion in ({fix_version_clause}) AND "
        f"status in (Closed, Done, Resolved) AND "
        f'resolution in ("Done", "Done-Errata") AND '
        f"customfield_10578 IS NOT EMPTY"
        f"{stream_clause}{exclude_clause}"
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
            logger.error("Invalid %s-build Jira response for %s: %s", kind, package, type(results))
            raise TransientInfrastructureError(
                f"Invalid response from {kind}-build Jira query for {package}: {type(results)}"
            )
        return results

    # Also search for active builds (not yet closed but have Fixed in Build set)
    # These need validation since they might be rejected/abandoned
    active_jql = (
        f'project = RHEL AND component = "{escaped_package}" AND '
        f"fixVersion in ({fix_version_clause}) AND "
        f"status not in (Closed, Done, Resolved) AND "
        f"customfield_10578 IS NOT EMPTY"
        f"{stream_clause}{exclude_clause}"
    )
    closed_results, active_results = await asyncio.gather(
        search_all(closed_jql, ["key", "customfield_10578"], "closed"),
        search_all(active_jql, ["key", "customfield_10578", "status"], "active"),
        return_exceptions=True,
    )
    if report is not None:
        for status, results in (("closed", closed_results), ("active", active_results)):
            if isinstance(results, Exception):
                report.problems.append(f"{status.capitalize()}-build Jira query failed: {results}")
                continue
            for issue in results:
                key = issue.get("key")
                nvr = issue.get("fields", {}).get(FIXED_IN_BUILD_CUSTOM_FIELD)
                if key and isinstance(nvr, str) and nvr.strip():
                    report.candidates.append((key, nvr.strip(), status))
    if isinstance(active_results, Exception):
        logger.error("Failed to query active builds for %s in %s: %s", package, fix_version, active_results)
        active_results = None
    if isinstance(closed_results, Exception):
        logger.error("Failed to query closed builds for %s in %s: %s", package, fix_version, closed_results)
        if isinstance(closed_results, TransientInfrastructureError):
            raise closed_results
        raise TransientInfrastructureError(
            f"Closed-build Jira query failed for {package} in {fix_version}: {closed_results}"
        ) from closed_results

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
            if report is not None:
                report.problems.append(
                    f"{issue_key}: Fixed in Build is not a string ({type(nvr_raw).__name__})"
                )
            continue

        if not (nvr := nvr_raw.strip()):
            logger.warning(f"Issue {issue_key} has empty Fixed in Build")
            if report is not None:
                report.problems.append(f"{issue_key}: Fixed in Build is empty")
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
                if report is not None:
                    report.active_problems.append(
                        f"{issue_key}: Fixed in Build is not a string ({type(nvr_raw).__name__})"
                    )
                continue

            if not (nvr := nvr_raw.strip()):
                logger.warning(f"Active issue {issue_key} (status: {status}) has empty Fixed in Build")
                if report is not None:
                    report.active_problems.append(f"{issue_key}: Fixed in Build is empty")
                continue

            active_candidates.append((issue_key, nvr))

    if active_candidates:
        logger.info(
            f"Found {len(active_candidates)} active (not closed) builds for {package} in "
            f"{fix_version}: {', '.join(f'{key} ({nvr})' for key, nvr in active_candidates)}"
        )

    async def resolve_candidates(candidates: list[tuple[str, str]], status: str) -> list[tuple[str, NEVR]]:
        if not candidates:
            return []
        # Brew lookups need the epochless NVR: Jira Fixed in Build values may
        # carry an epoch prefix that koji's getBuild does not match, which
        # would report an existing build as missing. The raw Jira value is
        # kept for the report; unparseable values skip the lookup entirely and
        # are recorded as data problems below.
        lookup_nvrs: list[str | None] = []
        for _, nvr in candidates:
            try:
                lookup_nvrs.append(get_epochless_nvr_string(NEVR.from_string(nvr)))
            except (SpecfileException, ValueError):
                lookup_nvrs.append(None)

        async def _lookup(nvr_or_none: str | None) -> dict | None:
            if nvr_or_none is None:
                return None
            return await asyncio.to_thread(_get_koji_build, BREWHUB_URL, nvr_or_none)

        builds = await asyncio.gather(
            *(_lookup(lookup_nvr) for lookup_nvr in lookup_nvrs),
            return_exceptions=True,
        )
        resolved = []
        lookup_failures = []
        for (issue_key, nvr), metadata in zip(candidates, builds, strict=True):
            try:
                NEVR.from_string(nvr)
            except (SpecfileException, ValueError):
                # A value that cannot be an NVR at all is a permanent data
                # problem — clarify instead of retrying a lookup that can
                # never succeed.
                problem = f"{issue_key} ({nvr}): not a valid Brew NVR"
                logger.warning("Brew lookup skipped: %s", problem)
                if report is not None:
                    if status == "active":
                        report.active_problems.append(problem)
                    else:
                        report.problems.append(problem)
                continue
            if isinstance(metadata, Exception):
                failure = metadata
                problem = f"{issue_key} ({nvr}): {failure}"
                logger.warning("Brew lookup failed: %s", problem)
                # Unparseable values were rejected above, so any failure here is
                # an infrastructure problem (including koji.GenericError) —
                # retry the whole verification so the candidate set is never
                # incomplete.
                lookup_failures.append(problem)
                continue
            if metadata is None:
                problem = f"{issue_key} ({nvr}): build not found in Brew"
                logger.warning("Brew lookup failed: %s", problem)
                if report is not None:
                    if status == "active":
                        # An active build that is not yet in Brew is common (build in
                        # progress); record it for the report without blocking the
                        # verification of the closed builds.
                        report.active_problems.append(problem)
                    else:
                        # A closed build missing from Brew is a data inconsistency,
                        # not an outage — clarify instead of retrying.
                        report.problems.append(problem)
                continue
            try:
                build = BrewBuild(metadata)
                if report is not None:
                    report.resolved_builds[nvr] = build
                    canonical_nvr = get_epochless_nvr_string(build.nevr)
                    if canonical_nvr != nvr:
                        # Also index under the canonical epochless NVR so later
                        # lookups rebuilt from Brew metadata succeed even when
                        # the Jira value carries an epoch prefix.
                        report.resolved_builds[canonical_nvr] = build
                resolved.append((issue_key, build.nevr))
            except Exception as error:
                problem = f"{issue_key} ({nvr}): Brew metadata cannot be compared: {error}"
                logger.warning(problem, exc_info=True)
                if report is not None:
                    if status == "active":
                        report.active_problems.append(problem)
                    else:
                        report.problems.append(problem)
        if lookup_failures:
            message = f"Brew lookup failed for {len(lookup_failures)} candidate(s): " + "; ".join(
                lookup_failures
            )
            logger.warning("%s; retrying verification", message)
            raise TransientInfrastructureError(message)
        return resolved

    resolved_closed = await resolve_candidates(closed_candidates, "closed")
    resolved_active = (
        None if active_candidates is None else await resolve_candidates(active_candidates, "active")
    )
    return resolved_closed, resolved_active


async def _fetch_installed_pkgs_log(
    package_nevr: NEVR, built_architectures: set[str], require_all: bool = True
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
    package_nvr = get_epochless_nvr_string(package_nevr)

    from ymir.common.config import load_rhel_config

    rhel_config = await load_rhel_config()
    repos_host = rhel_config.get("internal_repos_host")
    if not isinstance(repos_host, str) or not repos_host:
        raise PermanentValidationError("internal_repos_host is required to fetch Brew build logs")
    topurl = repos_host.rstrip("/") + "/brewroot"

    # Fetch logs only for architectures that were actually built
    installed_pkgs_log_urls = [
        (
            f"{topurl}/packages/"
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
                    try:
                        if content[:2] == b"\x1f\x8b":
                            content = gzip.decompress(content)
                        # Let the parser classify an empty body as invalid evidence.
                        decoded_logs.append((url, content.decode("utf-8")))
                    except Exception as e:
                        infra_errors.append(f"Failed to decode {url}: {e}")

                if infra_errors:
                    logger.warning("Brew log retrieval failed for %s: %s", package_nvr, infra_errors)
                    raise TransientInfrastructureError(
                        f"Infrastructure error fetching installed_pkgs.log for {package_nvr}: {infra_errors}"
                    )

                if not decoded_logs:
                    logger.warning("No Brew installed_pkgs.log available for %s", package_nvr)
                    raise BuildLogMissingError(
                        f"No installed_pkgs.log available for {package_nvr} from any architecture "
                        f"(all returned 404/410, expected: {list(built_architectures)})",
                        partial_logs=[],
                        missing_architectures=built_architectures,
                    )

                fetched_archs = {url.split("/")[-2] for url, _ in decoded_logs}
                missing_archs = built_architectures - fetched_archs

                if missing_archs and require_all:
                    logger.warning(
                        "Missing Brew architecture logs for %s: %s", package_nvr, sorted(missing_archs)
                    )
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
        logger.warning("Timed out retrieving Brew installed_pkgs.log for %s", package_nvr)
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
    fixed_dep_build: BrewBuild,
) -> tuple[list[str], int, str] | None:
    """Get the binary RPM names produced by the fixed dependency build.

    Args:
        fixed_dep_build: Authoritative Brew build metadata and NEVR.

    Returns:
        Tuple of (known_names, real_binary_rpm_count, build_name) or None if
        metadata unavailable.
        known_names: search terms for _parse_dependency_from_installed_pkgs_log.
            Always includes the dependency component name (e.g., "python-foobar") even
            when the build only produces differently-named binary RPMs (e.g., "python3-foobar").
            Binary RPM names introduced by newer builds are deliberately NOT included here;
            they are looked up lazily by _get_renamed_binary_rpm_names only when the fixed
            build's names match nothing in installed_pkgs.log. Duplicate names (the
            same binary RPM built for several architectures) are collapsed.
        real_binary_rpm_count: distinct installable binary RPMs from the fixed build (excluding
            debuginfo, debugsource, src). Used to decide ambiguity: 1 means the fix
            is unambiguously in that binary RPM (already_fixed); >1 means we can't tell
            which binary RPMs carry the fix (clarification needed). Computed before the
            synthetic component-name insertion so it reflects actual build output.
        build_name: the actual component name from the resolved build.
    """
    fixed_dep_nvr = get_epochless_nvr_string(fixed_dep_build.nevr)
    build_name = fixed_dep_build.metadata.get("name")
    build_id = fixed_dep_build.metadata.get("build_id")
    if not build_id:
        logger.error(f"No build_id for {fixed_dep_nvr}")
        return None

    # Get the list of binary RPM names produced by this source RPM
    try:
        session = koji.ClientSession(BREWHUB_URL)
        rpms = await asyncio.to_thread(session.listRPMs, buildID=build_id)
        if not isinstance(rpms, list):
            logger.error(f"Invalid RPM list response from Brew for build {build_id}")
            return None
        # Filter out debuginfo and debugsource packages
        non_debug_rpms = [
            rpm
            for rpm in rpms
            if rpm.get("name")
            and not rpm.get("name", "").endswith("-debuginfo")
            and not rpm.get("name", "").endswith("-debugsource")
        ]
        real_binary_rpm_count = len({rpm.get("name") for rpm in non_debug_rpms if rpm.get("arch") != "src"})
        # Collapse duplicate names (the same binary RPM built for several
        # architectures) so the log parser does not resolve them repeatedly.
        known_names = list(dict.fromkeys(rpm.get("name") for rpm in non_debug_rpms))
    except Exception as e:
        logger.error(f"Failed to fetch RPM list for {fixed_dep_nvr}: {e}")
        return None

    # The component name should already be in the list from the src RPM,
    # but include this as a safety measure for edge cases
    if build_name and build_name not in known_names:
        known_names.insert(0, build_name)

    logger.debug(f"Known package names for {build_name}: {known_names[:5]}...")
    return known_names, real_binary_rpm_count, build_name


async def _get_renamed_binary_rpm_names(
    fixed_dep_build: BrewBuild, known_names: set[str]
) -> list[str] | None:
    """Find binary RPM names that newer builds of the component produce but the fixed build does not.

    Called only when the fixed build's binary RPM names matched nothing in
    installed_pkgs.log. A newer build of the dependency may have renamed one of
    its binary RPMs while keeping the source RPM (component) name — renaming
    components during a RHEL major version lifetime is forbidden, so a renamed
    binary RPM still ships under the unchanged SRPM name, and installed_pkgs.log
    then shows only the new binary RPM name. Walks the completed builds
    newer than the fixed one in bounded-concurrency batches, retaining coverage
    for binary RPMs that were introduced in an earlier build and dropped again
    later.

    Returns the collected names (possibly an empty list) when the build history
    was fully covered, or ``None`` when coverage is incomplete (missing build
    metadata or a failed Brew lookup). Callers must not treat ``None`` as
    evidence that no renames exist.
    """
    package_id = fixed_dep_build.metadata.get("package_id")
    completion_time = fixed_dep_build.metadata.get("completion_time")
    if not package_id or not completion_time:
        # Without build metadata we cannot enumerate newer builds, so binary
        # RPM name coverage is incomplete.
        logger.warning(
            "Missing package_id or completion_time for %s; cannot check renamed binary RPMs",
            fixed_dep_build.nevr.name,
        )
        return None
    try:
        session = koji.ClientSession(BREWHUB_URL)
        newer_builds = await asyncio.to_thread(
            session.listBuilds, packageID=package_id, state=1, completeAfter=completion_time
        )
        if not isinstance(newer_builds, list):
            logger.warning("Invalid build list response from Brew for package %s", package_id)
            return None
        newer_build_ids = []
        for candidate in newer_builds:
            try:
                if _nevr_from_build(candidate) > fixed_dep_build.nevr:
                    newer_build_ids.append(candidate["build_id"])
            except (KeyError, TypeError, ValueError):
                continue
        newer_build_ids = sorted(newer_build_ids, reverse=True)
    except Exception as e:
        logger.warning("Failed to list newer builds of %s: %s", fixed_dep_build.nevr.name, e)
        return None

    def _fetch_build_rpms(build_id: int) -> list:
        # A fresh session per call: a koji ClientSession is not safe to share
        # across concurrent calls.
        return koji.ClientSession(BREWHUB_URL).listRPMs(buildID=build_id)

    renamed: list[str] = []
    batch_size = 8
    for start in range(0, len(newer_build_ids), batch_size):
        batch = newer_build_ids[start : start + batch_size]
        rpm_lists = await asyncio.gather(
            *(asyncio.to_thread(_fetch_build_rpms, build_id) for build_id in batch),
            return_exceptions=True,
        )
        for build_id, rpms in zip(batch, rpm_lists, strict=True):
            if isinstance(rpms, BaseException):
                # A failed RPM-list lookup leaves binary RPM name coverage
                # incomplete for that build; signal it rather than pretend the
                # search was exhaustive.
                logger.warning("Failed to fetch RPM list for build %s: %s", build_id, rpms)
                return None
            if not isinstance(rpms, list):
                logger.warning("Invalid RPM list response from Brew for build %s", build_id)
                return None
            for rpm in rpms:
                name = rpm.get("name") if isinstance(rpm, dict) else None
                if (
                    name
                    and not name.endswith("-debuginfo")
                    and not name.endswith("-debugsource")
                    and name not in known_names
                    and name not in renamed
                ):
                    renamed.append(name)
    if renamed:
        logger.info(
            "Binary RPM names introduced by newer builds of %s: %s", fixed_dep_build.nevr.name, renamed
        )
    return renamed


async def _get_source_nevr(brew_url: str, binary_rpm_nevra: NEVRA, dep_component: str) -> NEVR | None:
    """Get the source NEVR for a binary RPM NEVRA.

    This handles independently versioned subpackages (e.g., perl-Errno with its own version)
    by using the Brew API to query the actual source RPM that produced the binary RPM.

    Args:
        brew_url: Brew hub URL
        binary_rpm_nevra: Installed binary RPM identity parsed from installed_pkgs.log.
        dep_component: Source component name (e.g., "perl")

    Returns:
        Source RPM NEVR if successfully resolved, None if not found in Brew.

    Raises:
        TransientInfrastructureError: If Brew API calls fail (network, service issues)
    """

    def _do_brew_lookup():
        """Synchronous Brew lookup wrapped for asyncio.to_thread."""
        try:
            session = koji.ClientSession(brew_url)
            rpm_records = session.getRPM(
                {
                    "name": binary_rpm_nevra.name,
                    "version": binary_rpm_nevra.version,
                    "release": binary_rpm_nevra.release,
                    "arch": binary_rpm_nevra.arch,
                },
                multi=True,
            )
            for record in rpm_records or []:
                # The epoch field must be present and a valid integer (or None);
                # silently coercing booleans or floats would skew the comparison.
                epoch = record.get("epoch", "<missing>")
                if isinstance(epoch, bool) or not isinstance(epoch, (int, type(None))):
                    raise ValueError(f"invalid epoch {epoch!r} in RPM record for {binary_rpm_nevra}")
            matching_rpms = [
                record
                for record in rpm_records or []
                if int(record.get("epoch") or 0) == binary_rpm_nevra.epoch
            ]
            if len(matching_rpms) != 1:
                logger.warning(
                    f"Brew getRPM returned {len(matching_rpms)} matching epochs for {binary_rpm_nevra} "
                    f"(binary RPM: {binary_rpm_nevra.name}, source: {dep_component})"
                )
                return None

            rpm_info = matching_rpms[0]

            build_id = rpm_info.get("build_id")
            if not build_id:
                logger.warning(f"No build_id found for RPM {binary_rpm_nevra} ({binary_rpm_nevra.name})")
                return None

            build = session.getBuild(build_id)
            if not build:
                logger.warning(f"Could not fetch Brew build info for build_id {build_id}")
                return None

            if build["name"] != dep_component:
                logger.warning(
                    f"Binary RPM {binary_rpm_nevra} ({binary_rpm_nevra.name}) resolves to source "
                    f"'{build['name']}', not expected '{dep_component}'"
                )
                return None

            # Get source RPM NEVR
            source_nevr = _nevr_from_build(build)
            logger.info(
                f"Resolved binary RPM {binary_rpm_nevra} ({binary_rpm_nevra.name}) "
                f"to source RPM: {source_nevr}"
            )
            return source_nevr

        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Invalid Brew source metadata for %s: %s", binary_rpm_nevra, e)
            raise PermanentValidationError(f"Invalid Brew source metadata for {binary_rpm_nevra}: {e}") from e
        except koji.GenericError as e:
            # Brew API failures are transient - should retry
            logger.error(
                f"Transient Brew failure resolving {binary_rpm_nevra} ({binary_rpm_nevra.name}): {e}"
            )
            raise TransientInfrastructureError(
                f"Failed to query Brew for binary RPM {binary_rpm_nevra}: {e}"
            ) from e
        except Exception as e:
            # Unexpected errors - also treat as transient to avoid wrong rebuild decisions
            logger.error(
                f"Unexpected error resolving {binary_rpm_nevra} ({binary_rpm_nevra.name}) to source RPM: {e}"
            )
            raise TransientInfrastructureError(
                f"Unexpected error querying Brew for {binary_rpm_nevra}: {e}"
            ) from e

    try:
        return await asyncio.to_thread(_do_brew_lookup)
    except TransientInfrastructureError:
        raise


async def _parse_dependency_from_installed_pkgs_log(
    installed_pkgs_log: str,
    dep_component: str,
    known_names: list[str],
    brew_url: str,
    source_resolution_cache: dict[str, NEVR | None] | None = None,
) -> tuple[NEVR | None, list[str] | None]:
    """Parse installed_pkgs.log to find which version of dependency was installed.

    For independently versioned subpackages (e.g., perl-Errno), uses Brew API to resolve
    the binary RPM to its source package instead of string manipulation.

    Args:
        installed_pkgs_log: Content of installed_pkgs.log
        dep_component: Source component name
        known_names: List of known binary RPM names to search for
        brew_url: Brew hub URL for resolving binary RPMs to source packages.
            Binary fields alone cannot establish the source version or epoch.
        source_resolution_cache: Optional mapping of NEVRA string to resolved
            source NEVR shared across architecture logs, so the same installed
            RPM is resolved through Brew only once per verification.

    Returns:
        Tuple of (used_dep_nevr, matched_pkg_names) where:
        - used_dep_nevr is the source NEVR of the installed dependency (None if no match)
        - matched_pkg_names is a list of all matched binary RPM names from this log.
          When used_dep_nevr is None and matched_pkg_names is not None, it contains
          unresolved subpackage names (Brew resolution to source RPM failed).

    Raises:
        TransientInfrastructureError: If a Brew query fails
    """

    known_names_set = set(known_names)
    unresolved_subpackages = []
    resolution_errors = []
    transient_resolution_failure = False
    matched_nevrs: set[NEVR] = set()
    matched_names = []
    valid_records = 0

    for line in installed_pkgs_log.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[-1] != "installed":
            continue

        try:
            nevra = NEVRA.from_string(fields[0])
        except (SpecfileException, ValueError):
            continue

        valid_records += 1
        if nevra.name not in known_names_set:
            continue

        pkg_name = nevra.name

        cache_key = str(nevra)
        try:
            if source_resolution_cache is not None and cache_key in source_resolution_cache:
                used_nevr = source_resolution_cache[cache_key]
            else:
                used_nevr = await _get_source_nevr(brew_url, nevra, dep_component)
                if source_resolution_cache is not None:
                    source_resolution_cache[cache_key] = used_nevr
        except Exception as error:
            problem = f"{nevra}: {error}"
            logger.warning("Brew resolution failed for %s", problem, exc_info=True)
            resolution_errors.append(problem)
            if not isinstance(error, (PermanentValidationError, KeyError, TypeError, ValueError)):
                transient_resolution_failure = True
            unresolved_subpackages.append(pkg_name)
            continue
        if used_nevr is None:
            unresolved_subpackages.append(pkg_name)
            continue

        logger.info(f"Found {dep_component} package {pkg_name} in installed_pkgs.log: {used_nevr}")
        matched_nevrs.add(used_nevr)
        if pkg_name not in matched_names:
            matched_names.append(pkg_name)

    if not valid_records:
        raise PermanentValidationError("installed_pkgs.log contains no valid installed RPM records")
    if resolution_errors:
        message = "Brew could not resolve installed binary RPMs: " + "; ".join(resolution_errors)
        logger.warning(message)
        if transient_resolution_failure:
            raise TransientInfrastructureError(message)
        raise PermanentValidationError(message)
    if unresolved_subpackages:
        names = sorted(set(matched_names + unresolved_subpackages))
        logger.warning("Incomplete Brew source metadata for %s binary RPMs: %s", dep_component, names)
        return None, names

    if matched_names:
        if len(matched_nevrs) > 1:
            logger.warning(
                f"Conflicting {dep_component} versions within single architecture log: "
                f"{', '.join(str(n) for n in matched_nevrs)}. "
                f"Cannot determine installed version — needs manual verification."
            )
            return None, matched_names
        return next(iter(matched_nevrs)), matched_names

    logger.warning(f"Could not find {dep_component} or its subpackages in installed_pkgs.log")
    return None, None


async def _compare_build_timestamps(
    fixed_dep_build: BrewBuild,
    package_build: NEVR | BrewBuild,
) -> bool | None:
    """Compare completion timestamps as a fallback when installed_pkgs.log is unavailable.

    The package build is assumed to contain the fixed dependency when it
    completed at or after the fixed dependency build.

    Args:
        fixed_dep_build: The fixed dependency's Brew build.
        package_build: The package's build identity — either the resolved
            BrewBuild, or a NEVR that is looked up in Brew.

    Returns:
        True if the package build completed at or after the dependency fix,
        False if it completed before,
        None if build metadata is unavailable.
    """
    if isinstance(package_build, NEVR):
        requested_nevr = package_build
        try:
            raw_build = await asyncio.to_thread(
                _get_koji_build, BREWHUB_URL, get_epochless_nvr_string(requested_nevr)
            )
        except (koji.GenericError, httpx.HTTPError, ConnectionError, OSError, TimeoutError) as error:
            # A failed service lookup is transient: retry with backoff.
            raise TransientInfrastructureError(
                f"Brew build lookup failed for {requested_nevr}: {error}"
            ) from error
        if raw_build is None:
            logger.warning("Could not fetch build metadata for timestamp comparison")
            return None
        package_build = BrewBuild(raw_build)
        # Sanity check: the Brew-resolved build must be for the requested package.
        if package_build.nevr.name != requested_nevr.name:
            logger.error(
                f"Package build {requested_nevr} has name '{package_build.nevr.name}', "
                f"expected '{requested_nevr.name}'. Possible data error."
            )
            return None

    package_nevr = package_build.nevr
    package_completion = package_build.metadata.get("completion_time")
    fixed_completion = fixed_dep_build.metadata.get("completion_time")

    if not package_completion or not fixed_completion:
        logger.warning("Missing completion_time for timestamp comparison")
        return None

    dep_name = fixed_dep_build.nevr.name
    # If package was built after dependency fix, it likely has the fix
    if package_completion >= fixed_completion:
        logger.info(
            f"{package_nevr.name} ({package_nevr}) completed at {package_completion}, "
            f"which is >= {dep_name} fix completion at {fixed_completion}. "
            f"Assuming fixed dependency was used."
        )
        return True

    logger.info(
        f"{package_nevr.name} ({package_nevr}) completed at {package_completion}, "
        f"which is < {dep_name} fix completion at {fixed_completion}. "
        f"Package was built before fix."
    )
    return False


class VerificationReason(StrEnum):
    """Why a build verification could not reach a rebuild/already-fixed verdict."""

    NO_BUILDS_FOUND = "no_builds_found"
    ACTIVE_BUILDS_NOT_CLOSED = "active_builds_not_closed"
    INVALID_BUILD_RECORDS = "invalid_build_records"
    BUILD_NOT_TAGGED_IN_STREAM = "build_not_tagged_in_stream"
    BUILDS_NOT_IN_STREAM = "builds_not_in_stream"
    FIX_IN_PROGRESS_IN_STREAM = "fix_in_progress_in_stream"
    PARTIAL_ARCHITECTURE_COVERAGE = "partial_architecture_coverage"
    BUILT_AFTER_FIX_NO_INSTALLED_PKGS_LOG = "built_after_fix_no_installed_pkgs_log"
    TIMESTAMP_COMPARISON_FAILED = "timestamp_comparison_failed"
    RENAMED_BINARY_LOOKUP_FAILED = "renamed_binary_lookup_failed"
    UNRESOLVED_SUBPACKAGE_NEEDS_VERIFICATION = "unresolved_subpackage_needs_verification"
    SUBPACKAGE_MATCH_NEEDS_VERIFICATION = "subpackage_match_needs_verification"
    ARCHITECTURE_DEPENDENCY_CONFLICT = "architecture_dependency_conflict"


class VerificationOutcome(NamedTuple):
    """The result of a build verification.

    Unpacking the first four fields keeps the historical
    ``(already_fixed, issue_key, nevr, reason)`` tuple contract; ``reason`` is a
    :class:`VerificationReason` (string-comparable) whose details live in
    ``payload``, and ``report`` carries the full candidate evidence for
    clarifications.
    """

    already_fixed: bool | None
    issue_key: str | None = None
    nevr: NEVR | None = None
    reason: VerificationReason | None = None
    payload: dict | None = None
    report: BuildVerificationReport | None = None

    @property
    def data(self) -> dict:
        """The reason payload (empty when the reason carries no details)."""
        return self.payload or {}


async def check_package_built_with_fixed_dependency(
    package: str,
    fix_version: str,
    fixed_dep_build: BrewBuild,
    available_tools: list[Tool],
    jira_issue: str | None = None,
    downstream_component: str | None = None,
) -> VerificationOutcome:
    """Verify the latest rebuild using Brew, retrying transient service failures.

    Modular searches use the raw downstream_component to match the exact stream.
    Final metadata problems and ambiguous results preserve all discovered build
    references in the outcome's report. Transient Brew, Jira, and log retrieval
    failures propagate to the existing task retry handler.

    Args:
        package: Package name (e.g., "go-fdo-client")
        fix_version: RHEL fix version (e.g., "rhel-10.2.z")
        fixed_dep_build: Authoritative Brew build containing the dependency NEVR and metadata.
            Callers must have cross-validated the dependency component name against
            fixed_dep_build.nevr.name (the triage agent does this when the LLM-provided
            component is first resolved); the component name used throughout the
            verification is the authoritative fixed_dep_build.nevr.name.
        available_tools: List of available tools for Jira queries
        jira_issue: Jira issue key to exclude from search (the ticket being triaged)

    Returns:
        A :class:`VerificationOutcome` whose ``already_fixed`` is True when
        installed_pkgs.log proves the fix, False when a rebuild is needed, and
        None when manual action is required (``reason`` says why, ``report``
        carries the evidence).
    """
    verifier = BuildVerifier(
        package, fix_version, fixed_dep_build, available_tools, jira_issue, downstream_component
    )
    try:
        return await verifier.run()
    except TransientInfrastructureError:
        logger.warning(
            "Transient build verification failure for %s in %s; retrying", package, fix_version, exc_info=True
        )
        raise


class SelectedBuild(NamedTuple):
    """The build a verification analyses, with the Jira issue that references it."""

    build: BrewBuild
    issue_key: str


class BuildVerifier:
    """Verify whether a package's latest build already used the fixed dependency.

    The verification walks the Jira build references of ``package`` in
    ``fix_version`` through Brew resolution, release-stream checks, and
    installed_pkgs.log parsing, and produces a :class:`VerificationOutcome`.
    Shared state (the candidate report, the selected build, the log-parsing
    caches) lives on the instance so each step is an independently readable
    method instead of a closure.

    Raises:
        TransientInfrastructureError: If Brew or Jira metadata cannot be retrieved;
            propagated to the existing task retry handler.
    """

    def __init__(
        self,
        package: str,
        fix_version: str,
        fixed_dep_build: BrewBuild,
        available_tools: list[Tool],
        jira_issue: str | None = None,
        downstream_component: str | None = None,
    ) -> None:
        self.package = package
        self.fix_version = fix_version
        self.fixed_dep_build = fixed_dep_build
        self.available_tools = available_tools
        self.jira_issue = jira_issue
        self.downstream_component = downstream_component
        self.report = BuildVerificationReport()
        # Authoritative dependency component name; the triage agent
        # cross-validates the LLM-provided component against it before
        # constructing the verifier.
        self.dep_component = fixed_dep_build.nevr.name
        self.fixed_dep_nevr = fixed_dep_build.nevr
        # The selected package build, captured by _select_build; later steps
        # read this instead of fishing the build out of the report.
        # The build under analysis, captured by _verify once a selection
        # succeeds; every other view of it (NEVR, NVR, issue key) is derived.
        self.selection: SelectedBuild | None = None
        # Brew source-resolution cache shared across architecture log parses,
        # so the same installed RPM is resolved only once per verification.
        self._source_resolution_cache: dict[str, NEVR | None] = {}
        # Brew tag-membership results per build, so selection and the
        # post-selection stream check share one query per candidate.
        self._stream_tags_cache: dict[str, tuple[str, list[str], list[str]] | None] = {}

    @property
    def selected_build(self) -> BrewBuild:
        return self.selection.build

    @property
    def latest_nevr(self) -> NEVR:
        return self.selection.build.nevr

    @property
    def package_nvr(self) -> str:
        return get_epochless_nvr_string(self.selection.build.nevr)

    @property
    def selected_issue_key(self) -> str | None:
        return self.selection.issue_key if self.selection else None

    async def run(self) -> VerificationOutcome:
        """Run the verification and translate failures per the retry contract."""
        try:
            outcome = await self._verify()
        except TransientInfrastructureError:
            # Preserve the existing task retry behavior for infrastructure failures.
            raise
        except (PermanentValidationError, KeyError, TypeError, ValueError) as error:
            logger.warning("Invalid build metadata for %s in %s: %s", self.package, self.fix_version, error)
            self.report.problems.append(str(error))
            outcome = self._clarify(None)
        except Exception as error:
            logger.warning(
                "Build verification failed for %s in %s: %s", self.package, self.fix_version, error
            )
            raise TransientInfrastructureError(
                f"Build verification failed for {self.package} in {self.fix_version}: {error}"
            ) from error
        if outcome.reason is not None:
            self.report.reason = outcome.reason.value
        if outcome.already_fixed is None:
            logger.warning(
                "Build verification needs clarification for %s: %s",
                self.package,
                self.report.describe(),
            )
        return outcome._replace(report=self.report)

    # ------------------------------------------------------------------ helpers

    def _clarify(
        self,
        reason: VerificationReason | None,
        **payload,
    ) -> VerificationOutcome:
        """Build a needs-manual-action outcome for the currently selected build."""
        return VerificationOutcome(
            already_fixed=None,
            issue_key=self.selected_issue_key,
            nevr=self.selection.build.nevr if self.selection else None,
            reason=reason,
            payload=payload or None,
        )

    def _active_builds_not_closed(self, active_candidates: list[tuple[str, NEVR]]) -> VerificationOutcome:
        issues = [key for key, _ in active_candidates]
        return self._clarify(VerificationReason.ACTIVE_BUILDS_NOT_CLOSED, issues=issues)

    # ------------------------------------------------------------------ steps

    async def _verify(self) -> VerificationOutcome:
        """Orchestrate the verification steps."""
        # Step 1: Find builds in Jira.
        closed_candidates, active_candidates = await _find_completed_builds_jira(
            self.package,
            self.fix_version,
            self.available_tools,
            self.jira_issue,
            self.report,
            self.downstream_component,
        )
        closed_candidates = self._filter_target_stream(closed_candidates)
        if active_candidates is not None:
            active_candidates = self._filter_target_stream(active_candidates)

        # Handle case where active query failed.
        if active_candidates is None:
            logger.warning("Active-build Jira query failed for %s in %s", self.package, self.fix_version)
            raise TransientInfrastructureError(
                f"Active-build Jira query failed for {self.package} in {self.fix_version}"
            )

        if not closed_candidates and not active_candidates:
            # Classify from the report's raw references: unresolvable ones
            # never became candidates, so the filtered lists alone cannot see
            # them. Unparseable references stay out (fail open to the generic
            # problems clarification below).
            out_of_stream_refs = [
                (key, nvr)
                for key, nvr, status in self.report.candidates
                if status == "closed" and self._nvr_is_out_of_stream(nvr)
            ]
            if out_of_stream_refs:
                # Every completed-build reference on this ticket points at
                # another stream while its fix version names this one — the
                # references and the fix version contradict each other, and
                # nothing in this stream can be verified. Ask a human instead
                # of silently recommending a rebuild, whether or not the
                # references resolve in Brew.
                logger.warning(
                    "All completed-build references of %s in %s point at another stream: %s",
                    self.package,
                    self.fix_version,
                    ", ".join(f"{key} ({nvr})" for key, nvr in out_of_stream_refs),
                )
                return self._clarify(
                    VerificationReason.BUILDS_NOT_IN_STREAM,
                    references=[f"{key} ({nvr})" for key, nvr in out_of_stream_refs],
                )
            if self.report.problems or self.report.active_problems:
                return self._clarify(None)
            logger.info(f"No builds found for {self.package} in {self.fix_version}")
            return VerificationOutcome(already_fixed=False)

        # Validate active candidates to filter out builds for different packages.
        # This prevents unrelated active builds from blocking rebuild decisions.
        active_candidates = self._filter_active_package(active_candidates)

        # Drop lookup problems recorded for candidates whose Jira-recorded NVR
        # belongs to another release stream: a suitable in-stream build was
        # found (the flow would have clarified otherwise), so the out-of-stream
        # references cannot contribute to this stream's verdict and must not
        # block it with their lookup problems.
        self._drop_out_of_stream_problems()

        selection = await self._select_build(closed_candidates, active_candidates)
        if isinstance(selection, VerificationOutcome):
            return selection
        self.selection, active_candidates = selection
        # The report mirrors the selection once, for its own rendering.
        self.report.selected_issue_key = self.selection.issue_key
        self.report.selected_nevr = self.selection.build.nevr

        # Closed-build data problems (missing from Brew, unparseable NVRs) do not
        # prevent selecting the highest resolvable build, but they do prevent
        # trusting its verification result — surface them as a clarification.
        if self.report.problems:
            return self._clarify(None)

        # Only resolved active builds can be ordered. Unresolved builds still
        # require clarification.
        active_candidates = [
            (key, active_nevr) for key, active_nevr in active_candidates if active_nevr > self.latest_nevr
        ]

        stream_outcome = await self._verify_stream_tags()
        if stream_outcome is not None:
            return stream_outcome

        return await self._verify_from_logs(active_candidates)

    def _filter_target_stream(
        self, candidates: list[tuple[str, str | NEVR]]
    ) -> list[tuple[str, str | NEVR]]:
        """Keep only candidates whose NVR belongs to the fix version's stream.

        Jira matches both the Y-stream (rhel-9.8) and Z-stream (rhel-9.8.z)
        forms of a fix version (see get_fix_version_variants). This is a
        selection pre-filter, not the membership decision: it runs before the
        highest-EVR selection so an out-of-stream reference cannot win it (the
        authoritative tag check only runs on the single selected build), and
        it classifies candidates that could not be resolved in Brew (where no
        tag check is possible) so their lookup problems do not block a
        rebuild. The authoritative membership decision is the Brew tag check
        in _verify_stream_tags. Unparseable NVRs are kept so the Brew
        resolution path reports them.

        Returns ``(kept, dropped)``; the caller clarifies when the dropped
        references were the only completed-build evidence on the ticket.
        """
        kept = []
        for key, nvr in candidates:
            nevr = nvr if isinstance(nvr, NEVR) else None
            if nevr is None:
                try:
                    nevr = NEVR.from_string(nvr)
                except (SpecfileException, ValueError):
                    kept.append((key, nvr))
                    continue
            if nevr_matches_fix_version_stream(nevr, self.fix_version):
                kept.append((key, nvr))
            else:
                logger.info(
                    "Ignoring %s (%s): build stream does not match fix version %s",
                    key,
                    nvr,
                    self.fix_version,
                )
        return kept

    def _nvr_is_out_of_stream(self, nvr: str) -> bool:
        try:
            return not nevr_matches_fix_version_stream(NEVR.from_string(nvr), self.fix_version)
        except (SpecfileException, ValueError):
            return False

    def _drop_out_of_stream_problems(self) -> None:
        """Remove report problems of candidates from another release stream."""
        out_of_stream_keys = {
            key for key, nvr, _status in self.report.candidates if self._nvr_is_out_of_stream(nvr)
        }
        if out_of_stream_keys:
            self.report.problems = [
                problem
                for problem in self.report.problems
                if not any(problem.startswith(f"{key} (") for key in out_of_stream_keys)
            ]
            self.report.active_problems = [
                problem
                for problem in self.report.active_problems
                if not any(problem.startswith(f"{key} (") for key in out_of_stream_keys)
            ]

    def _filter_active_package(self, active_candidates: list[tuple[str, NEVR]]) -> list[tuple[str, NEVR]]:
        """Drop active builds that resolve to a different package."""
        filtered = []
        for active_key, active_nevr in active_candidates:
            if active_nevr.name != self.package:
                logger.warning(
                    f"Active build {get_epochless_nvr_string(active_nevr)} (issue {active_key}) "
                    f"resolves to '{active_nevr.name}', "
                    f"not expected package '{self.package}' — ignoring"
                )
                continue
            filtered.append((active_key, active_nevr))
        return filtered

    async def _select_build(
        self, closed_candidates: list[tuple[str, NEVR]], active_candidates: list[tuple[str, NEVR]]
    ) -> VerificationOutcome | tuple[SelectedBuild, list[tuple[str, NEVR]]]:
        """Select the highest authoritative NEVR among the closed builds.

        Returns either a clarification outcome, or the
        ``(selection, active_candidates)`` pair; the caller records the
        selection as the verification state.
        """
        # If only active (not closed) builds exist, request clarification:
        # these might be rejected/abandoned, so we can't trust them yet.
        if not closed_candidates and active_candidates:
            if self.report.problems:
                return self._clarify(None)
            logger.warning(
                f"Found {len(active_candidates)} active builds for {self.package} in "
                f"{self.fix_version}, but none are closed/resolved. "
                f"Cannot determine if rebuild already done."
            )
            return self._active_builds_not_closed(active_candidates)

        comparable_builds = sorted(
            [(nevr, issue_key) for issue_key, nevr in closed_candidates if nevr.name == self.package],
            key=lambda item: item[0],
            reverse=True,
        )
        for issue_key, nevr in closed_candidates:
            if nevr.name != self.package:
                logger.warning(
                    "Ignoring %s (%s): Brew package %r does not match %r",
                    issue_key,
                    get_epochless_nvr_string(nevr),
                    nevr.name,
                    self.package,
                )
        # Walk the candidates from the highest EVR down and select the first
        # one that belongs to the fix version's stream per its Brew tags: a
        # higher-version build from another stream must not hide a valid one.
        # Candidates whose membership cannot be determined are eligible (fail
        # open). When every candidate is tagged into another stream, the
        # highest one is selected anyway so the post-selection stream check
        # clarifies with the referenced build.
        for selected_nevr, package_issue_key in comparable_builds:
            # The report's resolved_builds map is evidence for clarifications,
            # not the verifier's working state; the epoch-aware NVR indexing is
            # isolated to this single place.
            candidate_build = self.report.resolved_builds[get_epochless_nvr_string(selected_nevr)]
            tags = await self._stream_tags(candidate_build)
            if tags is not None and not tags[1] and not tags[2]:
                logger.info(
                    "Ignoring %s (%s): carries no tag of the %s stream",
                    package_issue_key,
                    selected_nevr,
                    tags[0],
                )
                continue
            logger.info(
                f"Found latest build for {self.package}: {selected_nevr} (issue: {package_issue_key})"
            )
            return SelectedBuild(candidate_build, package_issue_key), active_candidates
        if comparable_builds:
            selected_nevr, package_issue_key = comparable_builds[0]
            logger.info(
                f"Found latest build for {self.package}: {selected_nevr} (issue: {package_issue_key})"
            )
            candidate_build = self.report.resolved_builds[get_epochless_nvr_string(selected_nevr)]
            return SelectedBuild(candidate_build, package_issue_key), active_candidates

        logger.warning(f"No valid closed builds found in Brew for {self.package} in {self.fix_version}")
        if self.report.problems:
            # Closed builds that could not be resolved are reported for clarification.
            return self._clarify(None)
        # If active builds exist, request clarification on those instead of rebuilding.
        if active_candidates:
            logger.info(
                f"Closed builds invalid, but {len(active_candidates)} active builds exist. "
                f"Requesting clarification on active builds."
            )
            return self._active_builds_not_closed(active_candidates)
        if not closed_candidates:
            logger.info(
                f"No builds found for {self.package} in {self.fix_version} after filtering. "
                f"All builds belonged to other packages."
            )
            return self._clarify(VerificationReason.NO_BUILDS_FOUND)
        # Closed builds exist in Jira but all NVRs resolve to a different
        # package name in Brew — flag the data inconsistency for human review.
        rejected_issues = [key for key, _ in closed_candidates]
        return self._clarify(VerificationReason.INVALID_BUILD_RECORDS, issues=rejected_issues)

    async def _stream_tags(self, build: BrewBuild) -> tuple[str, list[str], list[str]] | None:
        """Brew tag membership of the build in the fix version's stream (cached).

        See :func:`build_tags_in_stream` for the return contract; results are
        cached per build so candidate selection and the post-selection stream
        check issue a single Brew query per build.
        """
        key = get_epochless_nvr_string(build.nevr)
        if key not in self._stream_tags_cache:
            self._stream_tags_cache[key] = await build_tags_in_stream(build, self.fix_version)
        return self._stream_tags_cache[key]

    async def _verify_stream_tags(self) -> VerificationOutcome | None:
        """Verify the selected build's stream membership via Brew tags.

        The dist-tag filter is a convention; tag membership is the
        authoritative signal that the selected build is part of the fix
        version's stream. A build with only candidate/gate-stage tags is a
        fix in progress, not a shipped one — stop and tell the user instead
        of verifying it as already fixed. Unperformable checks fail open.
        """
        tags_in_stream = self._stream_tags_cache.get(get_epochless_nvr_string(self.selected_build.nevr))
        if tags_in_stream is None:
            return None
        branch, shipped_tags, in_progress_tags = tags_in_stream
        if not shipped_tags and not in_progress_tags:
            logger.warning("Selected build %s carries no tag of the %s stream", self.latest_nevr, branch)
            return self._clarify(VerificationReason.BUILD_NOT_TAGGED_IN_STREAM, branch=branch)
        if not shipped_tags:
            logger.info(
                "Selected build %s is still in progress (%s)",
                self.latest_nevr,
                ", ".join(in_progress_tags),
            )
            return self._clarify(VerificationReason.FIX_IN_PROGRESS_IN_STREAM, tags=in_progress_tags)
        return None

    async def _verify_from_logs(self, active_candidates: list[tuple[str, NEVR]]) -> VerificationOutcome:
        """Verify the selected build from its installed_pkgs.log."""
        package_build = self.selected_build.metadata

        # Get list of architectures from RPMs.
        build_id = package_build.get("build_id")
        if not build_id:
            logger.warning("No Brew build ID for %s", self.latest_nevr)
            raise TransientInfrastructureError(f"No build_id for {self.latest_nevr}")

        try:
            session = koji.ClientSession(BREWHUB_URL)
            rpms = await asyncio.to_thread(session.listRPMs, buildID=build_id)
            if not isinstance(rpms, list):
                logger.warning("Invalid Brew architecture list for %s: %r", self.package_nvr, rpms)
                raise TransientInfrastructureError(f"Invalid RPM list response for {self.package_nvr}")

            # Extract unique architectures (exclude 'src' and 'noarch').
            built_archs = {
                rpm["arch"]
                for rpm in rpms
                if isinstance(rpm, dict) and rpm.get("arch") not in ("src", "noarch")
            }
            noarch_build = not built_archs
            if noarch_build:
                # Noarch builds have installed_pkgs.log under noarch/ directory.
                logger.info(f"Noarch-only build {self.package_nvr}, fetching from noarch directory")
                built_archs = {"noarch"}
        except TransientInfrastructureError:
            raise
        except (koji.GenericError, ConnectionError, OSError) as e:
            logger.warning("Failed to fetch Brew architecture list for %s: %s", self.package_nvr, e)
            raise TransientInfrastructureError(
                f"Failed to fetch architecture list for {self.package_nvr}: {e}"
            ) from e

        if built_archs:
            logger.debug(f"Build {self.package_nvr} produced architectures: {list(built_archs)}")

        # For arch builds: require ALL arches. For noarch: accept any log found.
        installed_pkgs_logs = []
        missing_archs_from_error: set[str] = set()
        partial_logs_error = False
        try:
            installed_pkgs_logs = await _fetch_installed_pkgs_log(
                self.latest_nevr, built_archs, require_all=not noarch_build
            )
        except BuildLogMissingError as e:
            # installed_pkgs.log is absent (404/410). This indicates the log is
            # genuinely unavailable (e.g., retention policy, very old build), not
            # a transient error. Distinguish between: (1) no logs at all →
            # timestamp fallback (2) some logs retrieved but not all → use
            # partial logs if they exist.
            if "from any architecture" in str(e):
                logger.info(
                    f"installed_pkgs.log unavailable for {self.package_nvr}, "
                    f"falling back to timestamp comparison"
                )
            elif e.partial_logs:
                installed_pkgs_logs = e.partial_logs
                missing_archs_from_error = e.missing_architectures
                logger.warning(f"Using partial installed_pkgs.log for {self.package_nvr}: {e}")
            else:
                partial_logs_error = True
                missing_archs_from_error = e.missing_architectures
                logger.warning(f"Partial installed_pkgs.log availability for {self.package_nvr}: {e}")

        if not installed_pkgs_logs:
            # If partial logs were available but not all architectures, request
            # manual verification.
            if partial_logs_error:
                logger.warning(
                    f"Some but not all architecture logs available for {self.package_nvr}. "
                    f"Requesting manual verification."
                )
                return self._clarify(VerificationReason.PARTIAL_ARCHITECTURE_COVERAGE)
            return await self._timestamp_fallback(active_candidates)

        # Get known binary RPM names for the fixed dependency build.
        known_names_result = await _get_known_package_names(self.fixed_dep_build)
        if known_names_result is None:
            fixed_dep_nevr_str = get_epochless_nvr_string(self.fixed_dep_nevr)
            raise TransientInfrastructureError(
                f"Brew metadata unavailable: could not fetch subpackage list for "
                f"{self.dep_component} ({fixed_dep_nevr_str})"
            )
        known_names, real_binary_rpm_count, _build_name = known_names_result

        # Parse all architectures concurrently, sharing the resolution cache.
        (
            dep_nevrs,
            matched_pkg_names,
            missing_archs,
            unresolved_subpackage_match,
            unresolved_subpkg_names,
        ) = await self._parse_all_logs(known_names, installed_pkgs_logs, missing_archs_from_error)
        if self.report.problems:
            return self._clarify(None)

        if not dep_nevrs and not matched_pkg_names and not unresolved_subpackage_match:
            # The fixed build's binary RPM names matched nothing at all. Before
            # concluding a rebuild is needed, check whether a newer build of
            # the dependency renamed one of its binary RPMs and re-parse with
            # those names. (Lazy and bounded: this only runs on the no-match path.)
            renamed_names = await _get_renamed_binary_rpm_names(self.fixed_dep_build, set(known_names))
            if renamed_names is None:
                # Incomplete binary RPM name coverage: a failed Brew lookup (or
                # missing build metadata) means we cannot rule out that a
                # newer build renamed a binary RPM. Do not conclude a rebuild
                # is needed from a partial search.
                logger.warning(
                    f"Could not enumerate renamed binary RPMs for {self.dep_component} "
                    f"while checking {self.package_nvr}; binary RPM name coverage incomplete. "
                    f"Needs manual verification."
                )
                return self._clarify(VerificationReason.RENAMED_BINARY_LOOKUP_FAILED)
            if renamed_names:
                logger.info(
                    f"Re-parsing installed_pkgs.log for {self.package_nvr} with renamed "
                    f"binary RPM names from newer {self.dep_component} builds: {renamed_names}"
                )
                (
                    dep_nevrs,
                    matched_pkg_names,
                    missing_archs,
                    unresolved_subpackage_match,
                    (unresolved_subpkg_names),
                ) = await self._parse_all_logs(
                    known_names + renamed_names, installed_pkgs_logs, missing_archs_from_error
                )
                if self.report.problems:
                    return self._clarify(None)

        if not dep_nevrs:
            # No dependency found in any architecture log. But check if we
            # found evidence of an unresolved subpackage match.
            if unresolved_subpackage_match:
                logger.warning(
                    f"Found evidence of {self.dep_component} subpackage(s) in "
                    f"{self.package_nvr} installed_pkgs.log but could not resolve via "
                    f"Brew: {unresolved_subpkg_names}. "
                    f"Cannot determine whether rebuilt with fixed dependency."
                )
                return self._clarify(
                    VerificationReason.UNRESOLVED_SUBPACKAGE_NEEDS_VERIFICATION,
                    subpackages=sorted(unresolved_subpkg_names),
                )

            logger.warning(
                f"Could not find {self.dep_component} dependency in installed_pkgs.log "
                f"for {self.package_nvr} (checked {len(installed_pkgs_logs)} architecture(s))"
            )
            # Check if active builds exist before returning False.
            if active_candidates:
                logger.warning(
                    f"Dependency not found in {self.package_nvr} installed_pkgs.log, but "
                    f"{len(active_candidates)} active builds exist. "
                    f"Requesting clarification on active builds."
                )
                return self._active_builds_not_closed(active_candidates)
            if self.report.active_problems:
                # An in-progress build could not be resolved in Brew — a
                # rebuild might duplicate it.
                return self._clarify(None)
            # Check if we have incomplete architecture coverage.
            if missing_archs:
                logger.warning(
                    f"Dependency {self.dep_component} not found in any retrieved log for "
                    f"{self.package_nvr}, but {len(missing_archs)} architecture(s) missing: "
                    f"{', '.join(missing_archs)}. Cannot confirm absence — needs manual "
                    f"verification."
                )
                return self._clarify(VerificationReason.PARTIAL_ARCHITECTURE_COVERAGE)
            return VerificationOutcome(
                already_fixed=False,
                issue_key=self.selected_issue_key,
                nevr=self.latest_nevr,
            )

        # If some architectures are missing the dependency, cannot trust the result.
        if missing_archs:
            logger.warning(
                f"Dependency {self.dep_component} missing from {len(missing_archs)} "
                f"architecture(s) for {self.package_nvr}: {', '.join(missing_archs)}. "
                f"Found in: {', '.join(dep_nevrs.keys())}. Needs manual verification."
            )
            return self._clarify(VerificationReason.PARTIAL_ARCHITECTURE_COVERAGE)

        # Verify all architectures agree on the dependency version.
        unique_nevrs = set(dep_nevrs.values())
        if len(unique_nevrs) > 1:
            logger.warning(
                f"Dependency version conflict across architectures for {self.package_nvr}: "
                f"{', '.join(f'{arch}={nevr}' for arch, nevr in dep_nevrs.items())}. "
                f"Different buildroots used. Needs manual verification."
            )
            return self._clarify(VerificationReason.ARCHITECTURE_DEPENDENCY_CONFLICT)

        # All architectures agree — use the common NEVR.
        used_dep_nevr = next(iter(dep_nevrs.values()))
        used_dep_nvr = get_epochless_nvr_string(used_dep_nevr)
        logger.info(
            f"All {len(dep_nevrs)} architecture(s) agree: {self.package_nvr} used "
            f"{self.dep_component} {used_dep_nvr}"
        )

        # When the component produces more than one installable binary (after
        # filtering debuginfo/debugsource), we can't be sure which binary
        # carries the fixed code.
        all_matched_names = set()
        for pkg_list in matched_pkg_names.values():
            all_matched_names.update(pkg_list)
        matched_via_subpackage = real_binary_rpm_count > 1

        # Compare NEVRs directly.
        is_fixed = used_dep_nevr >= self.fixed_dep_nevr
        fixed_dep_nevr_str = get_epochless_nvr_string(self.fixed_dep_nevr)

        if is_fixed:
            if matched_via_subpackage:
                logger.info(
                    f"{self.package} ({self.package_nvr}) was built with {used_dep_nvr} >= "
                    f"{fixed_dep_nevr_str}, but {self.dep_component} produces multiple "
                    f"binary RPMs. Cannot confirm which binary RPM contains the fix — "
                    f"requesting verification."
                )
                return self._clarify(
                    VerificationReason.SUBPACKAGE_MATCH_NEEDS_VERIFICATION,
                    subpackages=sorted(all_matched_names),
                )
            logger.info(
                f"{self.package} ({self.package_nvr}) was built with {used_dep_nvr}, "
                f"which is >= fixed dependency {fixed_dep_nevr_str}"
            )
            return VerificationOutcome(
                already_fixed=True,
                issue_key=self.selected_issue_key,
                nevr=self.latest_nevr,
            )
        logger.info(
            f"{self.package} ({self.package_nvr}) was built with {used_dep_nvr}, "
            f"which is < fixed dependency {fixed_dep_nevr_str}"
        )
        # Check if there are active builds that might be newer.
        if active_candidates:
            logger.warning(
                f"Closed build {self.package_nvr} does not have fix, but "
                f"{len(active_candidates)} active builds exist. "
                f"Requesting clarification on active builds."
            )
            return self._active_builds_not_closed(active_candidates)
        if self.report.active_problems:
            # An in-progress build could not be resolved in Brew — a
            # rebuild might duplicate it.
            return self._clarify(None)
        return VerificationOutcome(
            already_fixed=False,
            issue_key=self.selected_issue_key,
            nevr=self.latest_nevr,
        )

    async def _timestamp_fallback(self, active_candidates: list[tuple[str, NEVR]]) -> VerificationOutcome:
        """Compare build completion timestamps when installed_pkgs.log is unavailable."""
        logger.info(
            f"installed_pkgs.log unavailable for {self.package_nvr}; "
            "checking timestamps to determine next action"
        )
        built_after_fix = await _compare_build_timestamps(self.fixed_dep_build, self.selected_build)
        if built_after_fix is True:
            # Package built after fix - likely has it, but need manual verification.
            logger.info(
                f"{self.package} ({self.package_nvr}) was built after {self.dep_component} fix. "
                f"Returning None to request manual verification."
            )
            return self._clarify(VerificationReason.BUILT_AFTER_FIX_NO_INSTALLED_PKGS_LOG)
        if built_after_fix is False:
            # Package built before fix - but check if active builds exist.
            logger.info(f"{self.package} ({self.package_nvr}) was built before {self.dep_component} fix")
            if active_candidates:
                logger.warning(
                    f"Closed build {self.package_nvr} built before fix, but "
                    f"{len(active_candidates)} active builds exist. "
                    f"Requesting clarification on active builds."
                )
                return self._active_builds_not_closed(active_candidates)
            if self.report.active_problems:
                # An in-progress build could not be resolved in Brew — a
                # rebuild might duplicate it.
                return self._clarify(None)
            return VerificationOutcome(
                already_fixed=False,
                issue_key=self.selected_issue_key,
                nevr=self.latest_nevr,
            )
        # Indeterminate comparison (missing completion metadata on an otherwise
        # resolved build): a permanent data gap, not an outage — request manual
        # verification instead of retrying forever.
        fixed_dep_nevr_str = get_epochless_nvr_string(self.fixed_dep_nevr)
        logger.warning(
            f"Completion timestamps unavailable for {self.package} ({self.package_nvr}) vs "
            f"{self.dep_component} ({fixed_dep_nevr_str}); requesting manual verification"
        )
        return self._clarify(VerificationReason.TIMESTAMP_COMPARISON_FAILED)

    async def _parse_all_logs(
        self,
        names: list[str],
        installed_pkgs_logs: list[tuple[str, str]],
        missing_archs_from_error: set[str],
    ) -> tuple[dict, dict, list, bool, set]:
        """Parse every architecture log against ``names`` and aggregate the results.

        Returns (dep_nevrs, matched_pkg_names, missing_archs,
        unresolved_subpackage_match, unresolved_subpkg_names). May append data
        problems to the report and raises TransientInfrastructureError on
        transient parse failures.
        """

        async def _parse_arch(arch_url, log_content):
            arch_name = arch_url.split("/")[-2]
            return arch_name, await _parse_dependency_from_installed_pkgs_log(
                log_content,
                self.dep_component,
                names,
                BREWHUB_URL,
                self._source_resolution_cache,
            )

        parse_results = await asyncio.gather(
            *(_parse_arch(url, content) for url, content in installed_pkgs_logs),
            return_exceptions=True,
        )

        dep_nevrs = {}  # arch -> NEVR
        matched = {}  # arch -> list of pkg names
        missing = list(missing_archs_from_error)
        unresolved_subpackage = False  # Found a subpackage but couldn't resolve it
        unresolved_names = set()

        transient_parse_failures = []
        for (arch_url, _), result in zip(installed_pkgs_logs, parse_results, strict=True):
            if isinstance(result, Exception):
                arch = arch_url.split("/")[-2]
                problem = f"{self.selected_issue_key} ({self.package_nvr}), {arch}: {result}"
                logger.warning("Dependency verification failed: %s", problem)
                if isinstance(result, (PermanentValidationError, KeyError, TypeError, ValueError)):
                    self.report.problems.append(problem)
                else:
                    transient_parse_failures.append(problem)
                continue

            arch_name, (used_dep_nevr, matched_pkg_names_list) = result
            if used_dep_nevr is not None:
                dep_nevrs[arch_name] = used_dep_nevr
                matched[arch_name] = matched_pkg_names_list
            elif matched_pkg_names_list:
                # Package found in log but couldn't be resolved via Brew
                # (unresolved subpackage).
                unresolved_subpackage = True
                unresolved_names.update(matched_pkg_names_list)
                missing.append(arch_name)

        if transient_parse_failures:
            raise TransientInfrastructureError(
                "Dependency verification failed: " + "; ".join(transient_parse_failures)
            )
        return dep_nevrs, matched, missing, unresolved_subpackage, unresolved_names


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
