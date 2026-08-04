from typing import Any

from beeai_framework.tools import ToolError


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
        self.additional_context = additional_context or {}
