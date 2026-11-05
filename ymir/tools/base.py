import asyncio
import copy
import logging
import weakref
from contextlib import contextmanager
from pathlib import Path
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
    **additional_context,
):
    """Context manager for unified tool error handling with observability.

    Catches exceptions and wraps them as ToolErrorWithContext with a clean
    LLM-facing message, preserving the original exception's type, message and
    additional context for logs and traces only.

    Args:
        error_message: Clean error message shown to the LLM.
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
    except asyncio.CancelledError:
        raise
    except Exception as e:
        additional_context["exception"] = f"{type(e).__name__}: {e}"
        raise ToolErrorWithContext(
            error_message,
            cause=e,
            additional_context=make_additional_context(**additional_context),
        ) from e


def make_additional_context(**additional_context) -> dict[str, str]:
    return {k: redact_credentials(str(v)) for k, v in additional_context.items()}


# Parallel tool calls run concurrently, so read-modify-write edits on the same
# file must be serialized or they overwrite (or truncate) each other.
_file_locks: weakref.WeakValueDictionary[Path, asyncio.Lock] = weakref.WeakValueDictionary()


def file_lock(path: Path) -> asyncio.Lock:
    key = path.resolve()
    lock = _file_locks.get(key)
    if lock is None:
        lock = _file_locks[key] = asyncio.Lock()
    return lock


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
