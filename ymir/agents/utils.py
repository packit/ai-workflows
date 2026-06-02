import logging
import os
from collections.abc import Callable
from typing import Any

from beeai_framework.agents.tool_calling.utils import ToolCallCheckerConfig
from beeai_framework.backend import ChatModel, ChatModelParameters
from beeai_framework.template import PromptTemplate
from pydantic import BaseModel

from ymir.common.base_utils import check_subprocess, run_subprocess  # noqa: F401 — re-exported
from ymir.common.mock_repos import (
    apply_zstream_override_from_env,
    setup_mock_repos_from_env,
)
from ymir.common.utils import get_absolute_path, mcp_tools, run_tool  # noqa: F401 — re-exported

logger = logging.getLogger(__name__)


def resolve_chat_model_override(agent_type: str) -> None:
    """Override CHAT_MODEL with a per-agent value if set.

    Call once at container startup so all agents in the process inherit the override.
    """
    override = os.environ.get(f"CHAT_MODEL_{agent_type.upper()}", "")
    if override:
        logger.info("Using model override for %s: %s", agent_type, override)
        os.environ["CHAT_MODEL"] = override


def is_reasoning_enabled() -> bool:
    chat_model = os.environ.get("CHAT_MODEL", "")
    return "claude" in chat_model and bool(os.getenv("REASONING_EFFORT"))


def get_chat_model() -> ChatModel:
    chat_model = os.environ["CHAT_MODEL"]
    # lowering the temperature makes the model stop backporting too soon
    # but should yield more predictable results, similar for top_p (tried 0.5)
    temperature = float(os.getenv("TEMPERATURE", "0.6"))
    reasoning_effort = os.getenv("REASONING_EFFORT")

    settings: dict[str, Any] = {"num_retries": int(os.getenv("LITELLM_NUM_RETRIES", 3))}

    # opus-4-8 deprecates temperature entirely; tell litellm to strip it.
    if "opus-4-8" in chat_model:
        settings["drop_params"] = True
        settings["additional_drop_params"] = ["temperature"]

    model = ChatModel.from_name(
        chat_model,
        # this the preferred way to set parameters, don't do options=...
        # it was changed in beeai 0.1.48
        ChatModelParameters(
            # Anthropic requires temperature=1 when extended thinking is enabled
            temperature=1 if "claude" in chat_model and reasoning_effort else temperature,
            reasoning_effort=reasoning_effort,
        ),
        timeout=1200,
        # beeai hardcodes max_retries=0 in its litellm adapter; num_retries
        # bypasses that and enables litellm's built-in retry with back-off
        # for transient 429 / rate-limit errors from the provider.
        settings=settings,
        allow_parallel_tool_calls=bool(reasoning_effort),
    )
    if "gemini" in chat_model:
        # disable `required` for Gemini models
        model.tool_choice_support = {"single", "none", "auto"}
        model.allow_prompt_caching = False
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


def build_agent_factory_with_mock_repos(
    agent_factory: Callable[[list, dict[str, Any] | None], Any], jira_issue: str
) -> Callable[[list, dict[str, Any] | None], Any]:
    """Prepare mock repos for the standalone agent path.

    If ``MOCK_REPOS_DIR`` is set, prepares bare clones and writes
    ``insteadOf`` rewrites to both the shared ``.mock_gitconfig`` and a
    per-issue file.  The agent container picks up the shared file via
    ``include.path`` in its global gitconfig; the MCP gateway uses the
    per-issue file scoped through ``_meta`` (see ``_get_mock_git_env``
    in ``ymir/tools/privileged/gitlab.py``).

    Args:
        agent_factory: The original agent factory callable
            (e.g. ``create_triage_agent``).
        jira_issue: The Jira issue key (e.g. ``RHEL-15216``).

    Returns:
        The original factory unchanged (mock repos are visible through
        gitconfig files, no per-tool env injection needed).
    """
    apply_zstream_override_from_env()

    git_env = setup_mock_repos_from_env(jira_issue)
    if git_env is None:
        return agent_factory

    logger.info("Mock repos configured for %s via MOCK_REPOS_DIR", jira_issue)

    return agent_factory


def format_mr_justification(justification: str | None) -> str:
    """Format justification text for MR descriptions.

    Args:
        justification: Optional justification text from triage agent

    Returns:
        Formatted string with "Justification:" header and trailing newlines,
        or empty string if justification is None
    """
    if justification:
        return f"Triage Decision Justification:\n{justification}\n\n"
    return ""


def init_sentry() -> None:
    """Initialize Sentry, if the DSN is set."""
    if not (dsn := os.getenv("SENTRY_DSN")):
        # no DSN, no reporting
        return

    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.litellm import LiteLLMIntegration

    sentry_sdk.init(
        dsn=dsn,
        enable_logs=True,
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for tracing.
        traces_sample_rate=1.0,
        # Add data like inputs and responses;
        # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        stream_gen_ai_spans=True,
        send_default_pii=True,
        integrations=[
            AsyncioIntegration(),
            LiteLLMIntegration(),
        ],
    )
