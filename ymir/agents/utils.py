import logging
import os

from beeai_framework.agents.tool_calling.utils import ToolCallCheckerConfig
from beeai_framework.backend import ChatModel, ChatModelParameters
from pydantic import BaseModel

from beeai_framework.template import PromptTemplate
from beeai_framework.tools import Tool

from ymir.common.utils import (  # noqa: F401 — re-exported for backward compatibility
    check_subprocess,
    get_absolute_path,
    mcp_tools,
    run_subprocess,
    run_tool,
)

logger = logging.getLogger(__name__)


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
    return dict(
        max_retries_per_step=int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5)),
        # 10 can easily be depleted by one of our tools failing 10 times
        # i.e. str_replace, view, etc.
        total_max_retries=int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 25)),
        # 140 is not enough for a more complex rebase
        # 140 is not enough for a more complex rebase or for a backport
        # with 19 commits and numerous merge conflicts, so we have 255 now
        max_iterations=int(os.getenv("BEEAI_MAX_ITERATIONS", 255)),
    )

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
    import beeai_framework.adapters.litellm.embedding
    from beeai_framework.adapters.litellm.utils import litellm_debug
    litellm_debug(True)
