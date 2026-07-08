"""
Common utility functions shared across the BeeAI system.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import koji
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.mcp import MCPTool
from beeai_framework.tools.types import JSONToolOutput, StringToolOutput
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult, TextContent
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


async def run_tool(
    tool: str | Tool,
    available_tools: list[Tool] | None = None,
    **kwargs: Any,
) -> str | dict | list:
    if isinstance(tool, str):
        tool = next(t for t in available_tools or [] if t.name == tool)
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
        return [_unpack_tool_result(item) for item in result]
    return _unpack_tool_result(result)


def _unpack_tool_result(result: Any) -> Any:
    if isinstance(result, TextContent):
        result = result.text
    if isinstance(result, dict) and len(result) == 1 and "result" in result:
        result = result["result"]
    return result


def _is_connection_error(exc: Exception) -> bool:
    if isinstance(exc, ExceptionGroup):
        return any(_is_connection_error(e) for e in exc.exceptions)
    return isinstance(exc, (httpx.ConnectError, ConnectionError, OSError))


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
            async with sse_client(sse_url) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                effective_session: Any = session
                if call_meta:
                    effective_session = _MetaInjectingSession(session, call_meta)
                tools = await MCPTool.from_session(effective_session)
                if filter:
                    tools = [t for t in tools if filter(t.name)]
                connected = True
                yield tools
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


async def get_latest_candidate_build(package: str, dist_git_branch: str) -> tuple[EVR, str]:
    candidate_tags = [
        f"{dist_git_branch}-candidate",
        f"{dist_git_branch}-z-candidate",
    ]

    results = await asyncio.gather(
        *(asyncio.to_thread(_get_latest_koji_build, BREWHUB_URL, tag, package) for tag in candidate_tags),
    )
    latest = None
    for build in results:
        if build is None:
            continue
        evr = _evr_from_build(build)
        if latest is None or latest[0] < evr:
            latest = (evr, build["build_id"])
    if latest is None:
        raise RuntimeError(f"There are no builds of {package} in {' or '.join(candidate_tags)}")
    evr, build_id = latest
    session = koji.ClientSession(BREWHUB_URL)
    metadata = await asyncio.to_thread(session.getBuild, build_id, strict=True)
    source_ref = metadata["source"].split("#")[-1]
    return evr, source_ref


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
