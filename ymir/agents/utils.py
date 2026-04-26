import logging
import os

import beeai_framework.adapters.litellm.chat as _chat_adapter
from beeai_framework.agents.tool_calling.utils import ToolCallCheckerConfig
from beeai_framework.backend import ChatModel, ChatModelParameters
from beeai_framework.template import PromptTemplate
from pydantic import BaseModel

from ymir.common.utils import (  # noqa: F401 — re-exported for backward compatibility
    check_subprocess,
    get_absolute_path,
    mcp_tools,
    run_subprocess,
    run_tool,
)

logger = logging.getLogger(__name__)

_prompt_caching_applied = False


def get_chat_model() -> ChatModel:
    chat_model = os.environ["CHAT_MODEL"]
    model = ChatModel.from_name(
        chat_model,
        # this the preferred way to set parameters, don't do options=...
        # it was changed in beeai 0.1.48
        ChatModelParameters(
            # lowering the temperature makes the model stop backporting too soon
            # but should yield more predictable results, similar for top_p (tried 0.5)
            temperature=0.6
        ),
        timeout=1200,
    )
    if "gemini" in chat_model:
        # disable `required` for Gemini models
        model.tool_choice_support = {"single", "none", "auto"}
    return model


def get_agent_execution_config() -> dict[str, int]:
    return {
        "max_retries_per_step": int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5)),
        # 10 can easily be depleted by one of our tools failing 10 times
        # i.e. str_replace, view, etc.
        "total_max_retries": int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 25)),
        # 140 is not enough for a more complex rebase
        # 140 is not enough for a more complex rebase or for a backport
        # with 19 commits and numerous merge conflicts, so we have 255 now
        "max_iterations": int(os.getenv("BEEAI_MAX_ITERATIONS", 255)),
    }


def get_tool_call_checker_config() -> ToolCallCheckerConfig:
    return ToolCallCheckerConfig(
        # allow two consecutive identical tool calls
        max_strike_length=2,
        max_total_occurrences=5,
        window_size=10,
    )


def render_prompt(template: str, input: BaseModel) -> str:
    """Renders a prompt template with the specified input, according to its schema."""
    return PromptTemplate(template=template, schema=type(input)).render(input)


def enable_prompt_caching() -> None:
    """Inject Anthropic prompt caching on system messages.

    Patches the ``acompletion`` reference inside BeeAI's LiteLLM adapter
    so that every request to a Claude model carries ``cache_control`` on
    its system message.  Enabled by default; set ``DISABLE_PROMPT_CACHING=true``
    to turn off.

    TODO: Remove after upgrading to BeeAI >= 0.1.79, which natively supports
    cache_control_injection_points in RequirementAgent.
    """
    global _prompt_caching_applied
    if _prompt_caching_applied:
        return
    if os.getenv("DISABLE_PROMPT_CACHING", "").lower() == "true":
        return

    _original_acompletion = _chat_adapter.acompletion

    async def _acompletion_with_caching(*args, **kwargs):
        model = str(kwargs.get("model") or (args[0] if args else ""))
        if "claude" in model.lower():
            for msg in kwargs.get("messages", []):
                if msg.get("role") == "system":
                    content = msg.get("content")
                    if isinstance(content, str):
                        msg["content"] = [
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ]
                    elif isinstance(content, list) and content and isinstance(content[-1], dict):
                        content[-1]["cache_control"] = {"type": "ephemeral"}
                    break
        response = await _original_acompletion(*args, **kwargs)
        if "claude" in model.lower():
            usage = getattr(response, "usage", None)
            if usage:
                logger.info(
                    "Prompt caching usage: prompt=%s, cache_creation=%s, cache_read=%s",
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "cache_creation_input_tokens", None),
                    getattr(usage, "cache_read_input_tokens", None),
                )
        return response

    _chat_adapter.acompletion = _acompletion_with_caching
    _prompt_caching_applied = True
    logger.info("Prompt caching enabled for Anthropic models")


def set_litellm_debug() -> None:
    """Set litellm to print debug information.

    WARNING: This CAN LEAK TOKENS to the logs.  It is gated behind the
    LITELLM_DEBUG environment variable — only enable it in development.
    """
    if not os.getenv("LITELLM_DEBUG"):
        logger.warning(
            "set_litellm_debug() called but LITELLM_DEBUG env var is not set; "
            "ignoring to prevent credential leakage in production."
        )
        return
    # the following two modules call `litellm_debug(False)` on import
    # import them explicitly now to ensure our call to `litellm_debug()` is not negated later
    import beeai_framework.adapters.litellm.chat
    import beeai_framework.adapters.litellm.embedding  # noqa
    from beeai_framework.adapters.litellm.utils import litellm_debug

    litellm_debug(True)
