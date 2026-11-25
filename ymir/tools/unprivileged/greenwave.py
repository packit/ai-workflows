import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.common.constants import AIOHTTP_TIMEOUT, YMIR_USER_AGENT
from ymir.common.http_utils import aiohttp_get_with_retries
from ymir.common.junit import testcase_status
from ymir.tools.base import CloneableTool as Tool

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
    timeout = 120
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


def _summarize_results_xml(content: str) -> str | None:
    """Condense a JUnit results file to one line per testcase. The raw file can be megabytes of
    system-out, which would flood the agent's context. Returns None if the content isn't parseable XML."""
    try:
        root = ET.fromstring(content)  # noqa: S314 -- same parser as the MR agent's results.xml handling
    except ET.ParseError:
        return None
    lines = []
    for suite in root.iter("testsuite"):
        cases = suite.findall("testcase")
        lines.append(f"Testsuite {suite.get('name')}: {len(cases)} testcases")
        for case in cases:
            status, message = testcase_status(case)
            line = f"  [{status}] {case.get('name')}"
            lines.append(f"{line}: {message}" if message else line)
    return "\n".join(lines) if lines else None


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
    timeout = 120
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
                            # Limit reading to 10MB to prevent OOM on large files. StreamReader.read(n)
                            # returns only the first buffered chunk (~8KB), so read chunks up to the cap.
                            max_bytes = 10 * 1024 * 1024
                            content_bytes = bytearray()
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                content_bytes.extend(chunk)
                                if len(content_bytes) >= max_bytes:
                                    break
                            content = bytes(content_bytes[:max_bytes]).decode("utf-8", errors="replace")
                            summary = _summarize_results_xml(content)
                            return StringToolOutput(result=summary if summary is not None else content)
                        logger.warning("Got HTTP %d for %s", response.status, url)
                except Exception as e:
                    logger.warning("Error fetching %s: %s", url, e)

        return StringToolOutput(
            result=f"Could not fetch test results from {input.artifact_url} — tried {urls_to_try}"
        )
