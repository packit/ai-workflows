import copy
import logging
from contextlib import contextmanager
from typing import Any, ClassVar, Self

from beeai_framework.context import Run
from beeai_framework.tools.tool import TInput, Tool, TOutput, TRunOptions
from beeai_framework.tools.types import ToolRunOptions
from beeai_framework.utils.cancellation import AbortController, AbortSignal, register_signals

from ymir.tools.errors import ToolErrorWithContext
from ymir.tools.gateway_utils import redact_credentials

logger = logging.getLogger(__name__)


@contextmanager
def tool_error_context(
    error_message: str,
    include_exception_message_for: tuple[type[Exception], ...] = (),
    **additional_context,
):
    """Context manager for unified tool error handling with observability.

    Catches exceptions and wraps them as ToolErrorWithContext with a clean
    LLM-facing message while preserving more specific details (exception type,
    exception message and additional context) for logs and traces.

    Args:
        error_message: Clean error message shown to the LLM.
        include_exception_message_for: Exception types whose exception messages
            should be appended to the LLM-facing error_message.
        **additional_context: Key-value pairs for observability, automatically
            redacted for credentials.

    Raises:
        ToolErrorWithContext: Wraps any caught exception (except ToolErrorWithContext
            which passes through unchanged) and adds provided additional context
            for observability.
    """
    try:
        yield
    except ToolErrorWithContext:
        raise
    except Exception as e:
        if isinstance(e, include_exception_message_for):
            error_message = f"{error_message}: {redact_credentials(str(e))}"

        additional_context["exception"] = f"{type(e).__name__}: {e}"
        raise ToolErrorWithContext(
            error_message,
            cause=e,
            additional_context=make_additional_context(**additional_context),
        ) from e


def make_additional_context(**additional_context) -> dict[str, str]:
    return {k: redact_credentials(str(v)) for k, v in additional_context.items()}


class CloneableTool(Tool[TInput, TRunOptions, TOutput]):
    """Tool with clone method and built-in timeout handling"""

    timeout: ClassVar[float | None] = None

    def run(self, input: TInput | dict[str, Any], options: TRunOptions | None = None) -> Run[TOutput]:
        """Inject a per-tool AbortSignal timeout into options before delegating to the framework."""

        if self.timeout is not None:
            timeout_signal = AbortSignal.timeout(self.timeout)

            if options is not None and options.signal is not None:
                controller = AbortController()
                register_signals(controller, [options.signal, timeout_signal])
                timeout_signal = controller.signal

            if options is not None:
                options = options.model_copy(update={"signal": timeout_signal})
            else:
                # No tool currently uses a custom TRunOptions subclass; revisit if one does.
                options = ToolRunOptions(signal=timeout_signal)  # type: ignore[assignment]

        return super().run(input, options)

    async def clone(self) -> Self:
        cloned = copy.copy(self)
        cloned.middlewares = list(self.middlewares)
        cloned._cache = await self.cache.clone()
        if self._options is not None:
            cloned._options = copy.copy(self._options)
        return cloned
