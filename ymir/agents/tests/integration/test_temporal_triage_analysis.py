"""Compare both triage paths using scripted models and a real local MCP/Temporal stack."""

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
import uvicorn
from flexmock import flexmock
from mcp.server.fastmcp import Context, FastMCP
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from ymir.agents import tasks, triage_agent
from ymir.common.models import Resolution
from ymir.common.utils import FIXED_IN_BUILD_CUSTOM_FIELD

pytest.importorskip("temporalio")

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client

from ymir.agents.temporal_triage_analysis import (
    TriageAnalysisRequest,
    TriageAnalysisWorkflow,
    create_analysis_worker,
    triage_issue_workflow_id,
)

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")
pytestmark = pytest.mark.skipif(not ADDRESS, reason="Set TEMPORAL_TEST_ADDRESS to a local Temporal server")

CASES = [
    ("RHEL-178684", "nginx", "rhel-9.8.z", "backport", Resolution.BACKPORT, "rhel-9.8.0"),
    ("RHEL-112546", "nginx", "rhel-9.9", "rebase", Resolution.REBASE, "c9s"),
    ("RHEL-287570", "git-lfs", "rhel-8.10.z", "rebuild", Resolution.REBUILD, "rhel-8.10.0"),
    ("RHEL-278652", "butane", "rhel-9.8.z", "rebuild", Resolution.NOT_AFFECTED, "rhel-9.8.0"),
    (
        "RHEL-285393",
        "opentelemetry-collector",
        "rhel-10.3",
        "rebuild",
        Resolution.POSTPONED_DEPENDENCY,
        "c10s",
    ),
    ("RHEL-900001", "nginx", "rhel-9.8.z", "no_fix_version", Resolution.CLARIFICATION_NEEDED, None),
    ("RHEL-900002", "nginx", "rhel-9.8.z", "pending_zstream", Resolution.POSTPONED_Y_STREAM, None),
    ("RHEL-900003", "nginx", "rhel-9.9", "unauthorized_rebase", Resolution.CLARIFICATION_NEEDED, None),
]

DEPENDENCY = "RHEL-287268"


@asynccontextmanager
async def fixture_gateway(issue, package, version, scenario, calls) -> AsyncIterator[str]:
    gateway = FastMCP("Temporal triage parity fixtures")

    @gateway.tool()
    def check_cve_triage_eligibility(issue_key: str) -> dict:
        assert issue_key == issue
        if scenario == "no_fix_version":
            return {
                "is_cve": True,
                "eligibility": "never",
                "reason": "Missing version",
                "error": "Fix Versions field is empty",
            }
        if scenario == "pending_zstream":
            return {
                "is_cve": True,
                "eligibility": "pending-dependencies",
                "reason": "Waiting for Z-stream",
                "pending_zstream_issues": [DEPENDENCY],
            }
        is_cve = scenario not in {"rebase", "unauthorized_rebase"}
        return {"is_cve": is_cve, "eligibility": "immediately", "reason": "Eligible fixture"}

    @gateway.tool()
    def get_jira_details(issue_key: str, ctx: Context) -> dict:
        meta = ctx.request_context.meta
        calls.append((issue_key, meta.model_dump().get("jira_issue") if meta else None))
        if issue_key == DEPENDENCY:
            return {
                "key": DEPENDENCY,
                "fields": {"summary": "Fixed Go toolchain", FIXED_IN_BUILD_CUSTOM_FIELD: "golang-1.25-1.el8"},
            }
        assert issue_key == issue
        return {
            "key": issue,
            "fields": {
                "summary": f"CVE-2026-111943 {package} fixture",
                "status": {"name": "New"},
                "fixVersions": [{"name": version}],
            },
        }

    @gateway.tool()
    def get_internal_rhel_branches(package: str) -> list[str]:
        return ["rhel-8.10.0", "rhel-9.8.0"]

    @gateway.tool()
    def verify_issue_author(issue_key: str) -> bool:
        return scenario != "unauthorized_rebase"

    @gateway.tool()
    def set_jira_fields(issue_key: str) -> str:
        raise AssertionError("The analysis must not write Jira fields")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()
        server = uvicorn.Server(uvicorn.Config(gateway.sse_app(), log_level="warning"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("Fixture gateway exited during startup")
                    await asyncio.sleep(0.01)
            yield f"http://{host}:{port}/sse"
        finally:
            server.should_exit = True
            await serving


def scripted_model(issue, package, scenario, *, read_only):
    async def respond(messages, info):
        tools = {tool.name for tool in info.function_tools}
        tool_result = next(
            (part for msg in messages for part in msg.parts if isinstance(part, ToolReturnPart)), None
        )
        if read_only:
            assert "set_jira_fields" not in tools
        if "view" in tools:
            if tool_result is None:
                return ModelResponse([ToolCallPart("view", {"path": "README.fixture"})])
            assert "Mocked package source" in str(tool_result.content)
            return ModelResponse(
                [
                    TextPart(
                        json.dumps(
                            {
                                "is_affected": package != "butane",
                                "justification_category": "Vulnerable Code not in Execute Path"
                                if package == "butane"
                                else None,
                                "explanation": "Scripted source assessment",
                                "confidence": {"level": "high", "reason": "Fixture source evidence"},
                            }
                        )
                    )
                ]
            )

        if tool_result is None:
            assert any(
                "Jira issue data (JSON)" in str(part) and issue in str(part)
                for msg in messages
                for part in msg.parts
            )
            return ModelResponse([ToolCallPart("get_jira_details", {"issue_key": issue})])
        data = {"package": package}
        if scenario == "backport":
            data.update(patch_urls=[], justification="Fixture backport")
        elif scenario in {"rebase", "unauthorized_rebase"}:
            data["version"] = "1.2.3"
        elif scenario == "rebuild":
            data.update(dependency_issue=DEPENDENCY, dependency_component="golang")
        else:
            raise AssertionError(f"Unexpected model request for {scenario}")
        resolution = "rebase" if scenario == "unauthorized_rebase" else scenario
        return ModelResponse([TextPart(json.dumps({"resolution": resolution, "data": data}))])

    return FunctionModel(respond)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issue", "package", "version", "scenario", "resolution", "branch", "follow_up"),
    [(*case, False) for case in CASES] + [(*CASES[0], True)],
)
async def test_analysis_matches_current_triage(
    issue, package, version, scenario, resolution, branch, follow_up, monkeypatch, tmp_path
):
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    config = tmp_path / "rhel-config.json"
    config.write_text(
        json.dumps(
            {
                "current_y_streams": {"9": "rhel-9.9", "10": "rhel-10.3"},
                "current_z_streams": {"8": "rhel-8.10.z", "9": "rhel-9.8.z", "10": "rhel-10.2.z"},
            }
        )
    )
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config))

    async def prep(*, workspace=None, **kwargs):
        directory = workspace or (tmp_path / "legacy-sources")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "README.fixture").write_text("Mocked package source\n")
        return directory, directory, True, None

    async def buildroot(*args, **kwargs):
        return package != "opentelemetry-collector"

    async def legacy_model(agent, prompt, **kwargs):
        return await agent.run(
            prompt, model=scripted_model(issue, package, scenario, read_only=False), **kwargs
        )

    flexmock(tasks).should_receive("clone_and_prep_sources").replace_with(prep)
    flexmock(triage_agent).should_receive("check_build_in_buildroot").replace_with(buildroot)
    flexmock(triage_agent).should_receive("run_pydantic_agent").replace_with(legacy_model)

    calls = []
    triage_only = scenario != "backport"
    async with fixture_gateway(issue, package, version, scenario, calls) as gateway:
        monkeypatch.setenv("MCP_GATEWAY_URL", gateway)
        legacy = await triage_agent.run_workflow(
            issue,
            True,
            triage_agent.create_triage_agent,
            auto_chain=follow_up,
            user_triggered=True,
            triage_only_requested=triage_only,
        )
        calls.clear()

        client = await Client.connect(ADDRESS, plugins=[PydanticAIPlugin()])
        queue = f"triage-parity-{uuid4().hex}"
        model = scripted_model(issue, package, scenario, read_only=True)
        async with await create_analysis_worker(client, queue, model):
            handle = await client.start_workflow(
                TriageAnalysisWorkflow.run,
                TriageAnalysisRequest(
                    issue,
                    {},
                    8,
                    user_triggered=True,
                    triage_only_requested=triage_only,
                    follow_up_requested=follow_up,
                ),
                id=triage_issue_workflow_id(issue),
                task_queue=queue,
                execution_timeout=timedelta(seconds=30),
            )
            result = await handle.result()
        history = await handle.fetch_history()
        activities = {
            event.activity_task_scheduled_event_attributes.activity_type.name
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        }

    assert result.state.triage_result.resolution == resolution
    assert result.state.target_branch == branch
    assert result.state == legacy
    assert result.follow_up_requested is follow_up
    assert "read_eligibility" in activities
    if branch is not None:
        assert (issue, issue) in calls
        assert {"prepare_analysis", "resolve_target", "agent__TriageAgent__model_request"} <= activities
        assert result.triage_usage.tool_calls == 1
    if "prepare_applicability" in activities:
        assert "agent__ApplicabilityAgent__model_request" in activities
        assert "agent__ApplicabilityAgent__toolset__<agent>__call_tool" in activities
        assert "cleanup_applicability" in activities
    assert not list((tmp_path / "temporal-triage").glob("*"))
