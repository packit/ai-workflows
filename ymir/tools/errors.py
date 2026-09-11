from typing import Any

from beeai_framework.errors import FrameworkError
from beeai_framework.tools import ToolError
from beeai_framework.utils.strings import to_json

# Context keys safe to expose to the LLM. Everything else on an error's context
# (notably additional_context) is withheld unless listed here.
#    -> "name" is the tool name BeeAI attaches via ToolError.ensure(e, tool=self).
_LLM_SAFE_CONTEXT_KEYS: frozenset[str] = frozenset({"name"})


def _format_error_message_for_llm(error: FrameworkError, *, offset: int = 0) -> str:
    """LLM-safe counterpart to beeai's _format_error_message. Composes the error
    message for the LLM without exposing overly comprehensive or sensitive
    information. It exposes only the error type, message and allowlisted context
    (never the raw cause, the stack trace, or additional_context).
    """
    prefix = "  " * offset
    formatted = f"{type(error).__name__}: {error.message}"
    safe = {k: v for k, v in (error.context or {}).items() if k in _LLM_SAFE_CONTEXT_KEYS}
    if safe:
        formatted += f"\nContext: {to_json(safe, sort_keys=True)}"
    return "\n".join(f"{prefix}{line}" for line in formatted.split("\n"))


def explain_to_llm(error: BaseException) -> str:
    """LLM-safe counterpart to FrameworkError.explain(). Renders the FrameworkError
    chain for the LLM while omitting the raw cause and observability context that
    explain() would otherwise leak into LLM-facing output.
    """
    if not isinstance(error, FrameworkError):
        return type(error).__name__

    return "\n".join(
        _format_error_message_for_llm(error, offset=offset) for offset, error in enumerate(error.traverse())
    ).strip()


class ToolErrorWithContext(ToolError):
    """ToolError subclass that carries additional observability context.

    The additional_context is not rendered by explain() so it won't
    leak into the LLM error message. It is available on the error
    instance for emitter listeners and span processors to read.
    """

    def __init__(
        self,
        message: str = "Tool Error",
        *,
        cause: Exception | None = None,
        context: dict[str, Any] | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> None:
        # Merge additional_context into context under special key for observability
        merged_context = context.copy() if context else {}
        if additional_context:
            merged_context["additional_context"] = additional_context

        super().__init__(message, cause=cause, context=merged_context)
