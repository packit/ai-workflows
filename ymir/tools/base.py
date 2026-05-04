import copy
from typing import Self

from beeai_framework.tools.tool import TInput, Tool, TOutput, TRunOptions


class CloneableTool(Tool[TInput, TRunOptions, TOutput]):
    async def clone(self) -> Self:
        cloned = copy.copy(self)
        cloned.middlewares = list(self.middlewares)
        cloned._cache = await self.cache.clone()
        if self._options is not None:
            cloned._options = copy.copy(self._options)
        return cloned
