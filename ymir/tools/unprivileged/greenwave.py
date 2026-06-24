import logging
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.base import CloneableTool as Tool
from ymir.tools.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT
from ymir.tools.http import aiohttp_get_with_retries

TESTING_FARM_ARTIFACTS_URL = "https://artifacts.osci.redhat.com/testing-farm"

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
            async with (
                aiohttp.ClientSession(
                    timeout=AIOHTTP_TIMEOUT, headers={"User-Agent": YMIR_USER_AGENT}
                ) as session,
                aiohttp_get_with_retries(session, url) as response,
            ):
                if response.status == 200:
                    html = await response.text()
                    return StringToolOutput(result=html)
                text = await response.text()
                logger.error(
                    "GreenWave request failed with status %d: %s",
                    response.status,
                    text,
                )
                return StringToolOutput(
                    result=f"Failed to fetch GreenWave gating status (HTTP {response.status}): {text}"
                )
        except (aiohttp.ClientError, TimeoutError) as e:
            # Here we handle ClientError as ToolError, because it signals
            # networking issues. Error statuses are handled above by StringToolOutput
            raise ToolError(f"Failed to fetch GreenWave gating status: {e}") from e
        except Exception as e:
            logger.error("Error fetching GreenWave gating status: %s", e)
            return StringToolOutput(result=f"Error fetching GreenWave gating status: {e}")


class FetchTestingFarmResultsInput(BaseModel):
    artifact_url: str = Field(
        description=(
            "Base artifact URL for a Testing Farm run, e.g. "
            "http://artifacts.osci.redhat.com/testing-farm/<uuid>. "
            "Can also be a full URL to a specific file such as results-junit.xml."
        )
    )


class FetchTestingFarmResultsTool(Tool[FetchTestingFarmResultsInput, ToolRunOptions, StringToolOutput]):
    """
    Fetches test results from a Testing Farm artifact URL.
    Retrieves results-junit.xml to show which individual tests passed, failed, or errored.
    """

    name = "fetch_testing_farm_results"  # type: ignore
    description = (  # type: ignore
        "Fetch individual test results from a Testing Farm artifact URL. "
        "Given a base artifact URL (e.g. from a GreenWave NEEDS_INSPECTION/FAILED/WAIVED result "
        "or from a GitLab MR pipeline job), retrieves the results-junit.xml file which lists "
        "each individual test case and its outcome (passed, failed, error). "
        "Use this to find out which specific tests failed inside a tier* job."
    )
    input_schema = FetchTestingFarmResultsInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "testing_farm", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: FetchTestingFarmResultsInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        base_url = input.artifact_url.rstrip("/")

        parsed = urlparse(base_url)
        if parsed.netloc != "artifacts.osci.redhat.com" or parsed.scheme not in ("http", "https"):
            return StringToolOutput(
                result=f"Invalid artifact URL: {input.artifact_url}. "
                "Only URLs from artifacts.osci.redhat.com are allowed."
            )

        # If a full path to a specific file was given, fetch it directly
        if base_url.endswith((".xml", ".yaml", ".html")):
            urls_to_try = [base_url]
        else:
            urls_to_try = [
                f"{base_url}/results-junit.xml",
                f"{base_url}/results.xml",
            ]

        async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
            for url in urls_to_try:
                logger.info("Fetching Testing Farm results from %s", url)
                try:
                    async with aiohttp_get_with_retries(session, url) as response:
                        if response.status == 200:
                            # Limit reading to 10MB to prevent OOM on large files
                            content_bytes = await response.content.read(10 * 1024 * 1024)
                            content = content_bytes.decode("utf-8", errors="replace")
                            return StringToolOutput(result=content)
                        logger.warning("Got HTTP %d for %s", response.status, url)
                except Exception as e:
                    logger.warning("Error fetching %s: %s", url, e)

        return StringToolOutput(
            result=f"Could not fetch test results from {input.artifact_url} — tried {urls_to_try}"
        )
