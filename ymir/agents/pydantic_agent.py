"""Pydantic AI integration with Ymir's existing model settings and tools."""

import os
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from beeai_framework.tools import Tool as YmirTool
from beeai_framework.tools import ToolError
from pydantic_ai import ModelRetry, Tool
from pydantic_ai.models import infer_model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.settings import ModelSettings

from ymir.agents.utils import run_tool, unsupported_model_params


@asynccontextmanager
async def configured_model():
    """Resolve the existing CHAT_MODEL aliases and own the Vertex Claude client."""
    model_id = os.environ["CHAT_MODEL"]
    provider, _, name = model_id.partition(":")
    vertex = provider in {"vertexai", "vertex_ai"}
    if vertex and name.startswith("claude-"):
        async with anthropic.AsyncAnthropicVertex(
            project_id=os.getenv("GOOGLE_VERTEX_PROJECT"),
            region=os.getenv("GOOGLE_VERTEX_LOCATION", "global"),
            timeout=float(os.getenv("CHAT_MODEL_TIMEOUT", "1200")),
            max_retries=int(os.getenv("LITELLM_NUM_RETRIES", "3")),
        ) as client:
            yield AnthropicModel(name, provider=AnthropicProvider(anthropic_client=client))
    elif vertex or provider == "google-cloud":
        yield GoogleModel(
            name,
            provider=GoogleCloudProvider(
                project=os.getenv("GOOGLE_VERTEX_PROJECT"),
                location=os.getenv("GOOGLE_VERTEX_LOCATION"),
            ),
        )
    else:
        aliases = {"gemini": "google", "openai": "openai-chat"}
        yield infer_model(f"{aliases[provider]}:{name}" if provider in aliases else model_id)


def pydantic_model_settings(model_id: str) -> ModelSettings:
    settings = ModelSettings(timeout=float(os.getenv("CHAT_MODEL_TIMEOUT", "1200")))
    unsupported = unsupported_model_params(model_id)
    effort = os.getenv("REASONING_EFFORT")
    if "claude" in model_id and effort:
        settings["thinking"] = effort
        # Leave room for both reasoning and the structured response.
        settings["max_tokens"] = 16384
    elif "temperature" not in unsupported:
        settings["temperature"] = float(os.getenv("TEMPERATURE", "0.6"))
    if model_id.startswith(("vertexai:claude-", "vertex_ai:claude-")):
        settings["anthropic_cache_tool_definitions"] = True
        settings["anthropic_cache_messages"] = True
    return settings


async def invoke_pydantic_tool(tool: str | YmirTool, **arguments: Any) -> Any:
    try:
        # Pydantic AI instruments model tool calls through OpenTelemetry.
        return await run_tool(tool, trace_trajectory=False, **arguments)
    except ToolError as error:
        if error.fatal or not error.retryable:
            raise
        raise ModelRetry(str(error)) from error


def adapt_tool(tool: YmirTool) -> Tool:
    """Expose the existing schema and delegate to the existing tool runner.

    The runner still validates arguments and uses the active MCP session, including
    its issue metadata. Serial tool execution preserves workspace/Jira ordering.
    """

    async def call(**arguments: Any) -> Any:
        return await invoke_pydantic_tool(tool, **arguments)

    return Tool.from_schema(
        call,
        name=tool.name,
        description=tool.description,
        json_schema=tool.input_schema.model_json_schema(),
        sequential=True,
    )
