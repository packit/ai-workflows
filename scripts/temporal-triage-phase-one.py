#!/usr/bin/env python3
"""Run the opt-in triage experiment against a local Temporal server."""

import argparse
import asyncio
import json
import os
import socket
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from temporalio.client import Client

from ymir.agents.pydantic_agent import configured_model, pydantic_model_settings
from ymir.agents.temporal_triage import TemporalTriageInput, TemporalTriageWorkflow, create_worker
from ymir.agents.triage_agent import TriageRunContext
from ymir.agents.utils import resolve_chat_model_override
from ymir.common.models import RebuildData, Resolution

ISSUE = "RHEL-287570"
DEPENDENCY = "RHEL-287268"
FIX_VERSION = "rhel-8.10.z"
SUMMARY = "CVE-2026-111943 git-lfs: rebuild against the fixed Go toolchain"
FIXTURES = {
    ISSUE: {
        "key": ISSUE,
        "fields": {
            "summary": SUMMARY,
            "description": f"Rebuild git-lfs against the Go fix in {DEPENDENCY}.",
            "fixVersions": [{"name": FIX_VERSION}],
        },
    },
    DEPENDENCY: {
        "key": DEPENDENCY,
        "fields": {"summary": "golang: fix shipped", "status": {"name": "Closed"}},
    },
}


@asynccontextmanager
async def mock_gateway(calls: list[tuple[str, str]]):
    gateway = FastMCP("Phase 1 Jira fixtures")

    @gateway.tool()
    def get_jira_details(issue_key: str, ctx: Context) -> dict:
        """Get details for a mocked Jira issue."""
        meta = ctx.request_context.meta
        caller = meta.model_dump().get("jira_issue") if meta else None
        if caller != ISSUE:
            raise ValueError("Missing the main Jira issue in the MCP call metadata")
        calls.append((caller, issue_key))
        return FIXTURES[issue_key]

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(gateway.sse_app(), log_level="warning"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("The mock MCP gateway stopped before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}/sse"
        finally:
            server.should_exit = True
            await task


def scripted_model(calls: list[str], pause: asyncio.Event | None = None) -> FunctionModel:
    async def respond(messages, _info) -> ModelResponse:
        jira_result = next(
            (p for message in messages for p in message.parts if isinstance(p, ToolReturnPart)), None
        )
        if jira_result is None:
            calls.append("request Jira dependency")
            return ModelResponse([ToolCallPart("get_jira_details", {"issue_key": DEPENDENCY})])

        if jira_result.content != FIXTURES[DEPENDENCY]:
            raise ValueError("The model did not receive the recorded Jira response")
        if pause is not None:
            pause.set()
            await asyncio.Future()

        calls.append("return triage result")
        return ModelResponse(
            [
                TextPart(
                    json.dumps(
                        {
                            "resolution": "rebuild",
                            "data": {
                                "package": "git-lfs",
                                "dependency_issue": DEPENDENCY,
                                "dependency_component": "golang",
                            },
                        }
                    )
                )
            ]
        )

    return FunctionModel(respond, model_name="phase-one-scripted-triage")


async def completed_activities(handle) -> Counter:
    history = await handle.fetch_history()
    scheduled = {
        event.event_id: event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    return Counter(
        scheduled[event.activity_task_completed_event_attributes.scheduled_event_id]
        for event in history.events
        if event.HasField("activity_task_completed_event_attributes")
    )


async def start_run(client: Client, task_queue: str, model_settings: dict):
    return await client.start_workflow(
        TemporalTriageWorkflow.run,
        TemporalTriageInput(
            f"Triage {ISSUE}. Check the dependency with get_jira_details.\n"
            f"Jira issue data (JSON):\n{json.dumps(FIXTURES[ISSUE])}",
            TriageRunContext(ISSUE, FIX_VERSION, SUMMARY),
            model_settings,
        ),
        id=task_queue,
        task_queue=task_queue,
        execution_timeout=timedelta(minutes=6),
    )


async def prove_restart(client: Client, task_queue: str, gateway_calls: list[tuple[str, str]]):
    calls: list[str] = []
    second_request_started = asyncio.Event()
    async with create_worker(client, task_queue, scripted_model(calls, second_request_started)):
        handle = await start_run(client, task_queue, {})
        print(f"Workflow ID: {handle.id}", flush=True)
        async with asyncio.timeout(30):
            await second_request_started.wait()
        before = await completed_activities(handle)
        if (
            sum(before.values()) != 2
            or calls != ["request Jira dependency"]
            or gateway_calls != [(ISSUE, DEPENDENCY)]
        ):
            raise RuntimeError(
                f"Unexpected activity history before shutdown: {before}, {calls}, {gateway_calls}"
            )
        print(f"Before worker shutdown: {dict(before)}", flush=True)

    async with create_worker(client, task_queue, scripted_model(calls)):
        async with asyncio.timeout(90):
            result = await handle.result()

    after = await completed_activities(handle)
    if (
        sum(after.values()) != 3
        or calls != ["request Jira dependency", "return triage result"]
        or gateway_calls != [(ISSUE, DEPENDENCY)]
    ):
        raise RuntimeError(
            f"Unexpected activity history after replacement: {after}, {calls}, {gateway_calls}"
        )
    print(f"After replacement worker completed: {dict(after)}")
    print(f"Model calls: {calls}; MCP calls: {gateway_calls}")
    return result


async def main(address: str, vertex: bool) -> None:
    client = await Client.connect(address, plugins=[PydanticAIPlugin()])
    queue = f"triage-phase-one-{uuid4().hex[:12]}"
    gateway_calls: list[tuple[str, str]] = []
    async with mock_gateway(gateway_calls) as url:
        os.environ["MCP_GATEWAY_URL"] = url
        if vertex:
            resolve_chat_model_override("triage")
            model_id = os.environ["CHAT_MODEL"]
            if not model_id.startswith(("vertexai:claude-", "vertex_ai:claude-")):
                raise ValueError("Set CHAT_MODEL or CHAT_MODEL_TRIAGE to a Vertex Claude model")
            async with configured_model() as model, create_worker(client, queue, model):
                handle = await start_run(client, queue, pydantic_model_settings(model_id))
                print(f"Workflow ID: {handle.id}", flush=True)
                result = await handle.result()
                print(f"Completed activities: {dict(await completed_activities(handle))}")
        else:
            result = await prove_restart(client, queue, gateway_calls)
        if (ISSUE, DEPENDENCY) not in gateway_calls or (
            not isinstance(result.data, RebuildData)
            or result.resolution != Resolution.REBUILD
            or (result.data.jira_issue, result.data.fix_version, result.data.dependency_issue)
            != (ISSUE, FIX_VERSION, DEPENDENCY)
        ):
            raise RuntimeError(f"Unexpected triage result or MCP calls: {result}, {gateway_calls}")
        print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--vertex", action="store_true", help="Use the configured Vertex Claude model")
    args = parser.parse_args()
    asyncio.run(main(args.address, args.vertex))
