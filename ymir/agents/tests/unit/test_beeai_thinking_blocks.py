"""Verify the patched BeeAI adapter preserves Claude thinking across tool calls."""

from beeai_framework.adapters.vertexai.backend.chat import VertexAIChatModel
from beeai_framework.backend import MessageToolResultContent, ToolMessage, UserMessage
from beeai_framework.backend.types import ChatModelInput
from litellm.litellm_core_utils.prompt_templates.factory import anthropic_messages_pt
from litellm.types.utils import ModelResponse


def test_vertex_adapter_round_trips_redacted_thinking_with_tool_use():
    blocks = [
        {"type": "thinking", "thinking": "First thought", "signature": "first-signature"},
        {"type": "redacted_thinking", "data": "opaque-data"},
        {"type": "thinking", "thinking": "", "signature": "empty-signature"},
    ]
    raw = ModelResponse(
        id="response-id",
        model="claude-opus-5-5",
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "thinking_blocks": blocks,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "view", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
    )
    model = VertexAIChatModel("claude-opus-5-5", project="test", location="global", credentials={})

    assistant = model._transform_output(raw).output[0]
    assert assistant.meta["thinking_blocks"] == blocks

    request = model._transform_input(
        ChatModelInput(
            messages=[
                UserMessage("Read the file."),
                assistant,
                ToolMessage(
                    MessageToolResultContent(tool_name="view", tool_call_id="call-1", result="contents")
                ),
            ],
            tools=[],
        )
    )
    native = anthropic_messages_pt(
        messages=request["messages"], model="claude-opus-5-5", llm_provider="vertex_ai"
    )
    assert native[1]["content"][:3] == blocks
    assert native[1]["content"][3]["type"] == "tool_use"
