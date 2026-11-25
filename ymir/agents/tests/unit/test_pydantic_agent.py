import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropicVertex
from beeai_framework.tools.mcp import MCPTool
from mcp.types import CallToolResult
from mcp.types import Tool as MCPToolInfo

from ymir.agents.pydantic_agent import run_pydantic_agent
from ymir.agents.triage_agent import TriageRunContext, create_triage_agent
from ymir.common.models import ErrorData, Resolution
from ymir.common.utils import _MetaInjectingSession


@pytest.mark.asyncio
async def test_vertex_opus_triage_round_trips_mcp_and_redacted_thinking(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "vertexai:claude-opus-5-5")
    monkeypatch.setenv("REASONING_EFFORT", "high")
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_VERTEX_LOCATION", "global")
    monkeypatch.setenv("LITELLM_NUM_RETRIES", "0")
    monkeypatch.setenv("ANTHROPIC_VERTEX_BASE_URL", "https://vertex.invalid/v1")

    async def read_jira(name, arguments, *, meta, **kwargs):
        assert name == "get_jira_details"
        assert arguments == {"issue_key": "RHEL-2"}
        assert meta == {"jira_issue": "RHEL-1"}
        return CallToolResult(content=[], structuredContent={"result": {"fields": {}}})

    session = _MetaInjectingSession(SimpleNamespace(call_tool=read_jira), {"jira_issue": "RHEL-1"})
    jira_tool = MCPTool(
        session,
        MCPToolInfo(
            name="get_jira_details",
            description="Read a Jira issue",
            inputSchema={
                "type": "object",
                "properties": {"issue_key": {"type": "string"}},
                "required": ["issue_key"],
            },
        ),
    )
    requests = []

    def respond(request):
        assert request.url.path.endswith(
            "/projects/test-project/locations/global/publishers/anthropic/models/claude-opus-5-5:rawPredict"
        )
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"]["effort"] == "high"
        assert body["tool_choice"]["type"] == "auto"
        assert "temperature" not in body
        assert "get_jira_details" in {tool["name"] for tool in body["tools"]}
        thinking = {"type": "redacted_thinking", "data": "opaque-thinking-block"}
        if len(requests) == 1:
            content = [
                thinking,
                {
                    "type": "tool_use",
                    "id": "read_jira",
                    "name": "get_jira_details",
                    "input": {"issue_key": "RHEL-2"},
                },
            ]
        else:
            assert any(thinking in message["content"] for message in body["messages"])
            output = {"resolution": "error", "data": {"details": "Test response"}}
            content = [{"type": "text", "text": json.dumps(output)}]
        return httpx.Response(
            200,
            json={
                "type": "message",
                "id": f"message-{len(requests)}",
                "role": "assistant",
                "content": content,
                "model": "claude-opus-5-5",
                "stop_reason": "tool_use" if len(requests) == 1 else "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )

    class MockVertexClient(AsyncAnthropicVertex):
        def __init__(self, **kwargs):
            super().__init__(
                **kwargs,
                access_token="test-token",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
            )

    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", MockVertexClient)
    result = await run_pydantic_agent(
        create_triage_agent([jira_tool]),
        "Triage RHEL-1",
        deps=TriageRunContext("RHEL-1", None, None),
    )

    assert len(requests) == 2
    assert result.output.resolution == Resolution.ERROR
    assert result.output.data == ErrorData(jira_issue="RHEL-1", details="Test response")
