import logging

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ReadLogfileInput(BaseModel):
    logfile_url: str = Field(description="URL of logfile to read")


class ReadLogfileTool(Tool[ReadLogfileInput, ToolRunOptions, StringToolOutput]):
    name = "read_logfile"
    description = "Read logfile from URL"
    input_schema = ReadLogfileInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "read_logfile"],
            creator=self,
        )

    async def _run(
        self,
        input: ReadLogfileInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        logger.info("Reading logfile from URL: %s", input.logfile_url)
        async with aiohttp.ClientSession() as session, session.get(input.logfile_url) as response:
            if response.status == 200:
                return StringToolOutput(
                    result=await response.text(),
                )

        return StringToolOutput(result=f"Failed to read logfile from {input.logfile_url}")
