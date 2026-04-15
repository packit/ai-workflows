import logging
from urllib.parse import quote as urlquote

import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

logger = logging.getLogger(__name__)

GREENWAVE_URL = "https://gating-status.osci.redhat.com"


class FetchGreenWaveInput(BaseModel):
    nvr: str = Field(description="NVR (Name-Version-Release) of the build to check gating status for")


class FetchGreenWaveTool(Tool[FetchGreenWaveInput, ToolRunOptions, StringToolOutput]):
    """
    Tool to fetch the gating status page from GreenWave Monitor for a given build NVR.
    The page contains OSCI gating test results that determine whether a build can be
    added to a compose and erratum.
    """

    name = "fetch_greenwave"  # type: ignore
    description = (  # type: ignore
        "Fetch the OSCI gating status page from GreenWave Monitor for a given build NVR. "
        "Returns the HTML content of the gating status page which contains test results "
        "and their pass/fail status. Use this to determine if gating tests have passed."
    )
    input_schema = FetchGreenWaveInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "greenwave", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: FetchGreenWaveInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        url = f"{GREENWAVE_URL}/query?nvr={urlquote(input.nvr)}"
        logger.info("Fetching GreenWave gating status from %s", url)

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        html = await response.text()
                        return StringToolOutput(result=html)
                    else:
                        text = await response.text()
                        logger.error(
                            "GreenWave request failed with status %d: %s",
                            response.status,
                            text,
                        )
                        return StringToolOutput(
                            result=f"Failed to fetch GreenWave gating status (HTTP {response.status}): {text}"
                        )
        except Exception as e:
            logger.error("Error fetching GreenWave gating status: %s", e)
            return StringToolOutput(
                result=f"Error fetching GreenWave gating status: {e}"
            )
