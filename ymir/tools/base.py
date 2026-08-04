import copy
from typing import Any, ClassVar, Self

from beeai_framework.context import Run
from beeai_framework.tools.tool import TInput, Tool, TOutput, TRunOptions
from beeai_framework.tools.types import ToolRunOptions
from beeai_framework.utils.cancellation import AbortController, AbortSignal, register_signals


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
