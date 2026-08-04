import copy
import logging
from contextlib import contextmanager
from typing import Self

from beeai_framework.tools.tool import TInput, Tool, TOutput, TRunOptions

from ymir.tools.errors import ToolErrorWithContext

logger = logging.getLogger(__name__)


@contextmanager
def tool_error_context(error_message: str, **additional_context):
    try:
        yield
    except ToolErrorWithContext:
        raise
    except Exception as e:
        additional_context["exception"] = f"{type(e).__name__}: {e}"
        raise ToolErrorWithContext(error_message, cause=e, additional_context=additional_context) from e


class CloneableTool(Tool[TInput, TRunOptions, TOutput]):
    async def clone(self) -> Self:
        cloned = copy.copy(self)
        cloned.middlewares = list(self.middlewares)
        cloned._cache = await self.cache.clone()
        if self._options is not None:
            cloned._options = copy.copy(self._options)
        return cloned
