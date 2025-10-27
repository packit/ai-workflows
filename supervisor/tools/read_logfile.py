import logging
import re
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

from ..http_utils import aiohttp_session

logger = logging.getLogger(__name__)


class ReadLogfileInput(BaseModel):
    logfile_url: str = Field(description="URL of logfile to read")


#
# We don't want to allow arbitrary URLs to be read, only specific known
# patterns to test log files - this reduces the possibility of misuse
# of the tool to say, read sensitive internal files. Note that the
# URL is normalized to remove any .. path components before matching.
#
LOGFILE_PATTERNS = [
    "https://beaker.engineering.redhat.com/recipes/.*/taskout.log",
    "https://artifacts.osci.redhat.com/testing-farm/.*/output.txt",
]


# Normalize an URL so it doesn't have any .. components in the path
def normalize_url(url: str) -> str:
    from urllib.parse import urlparse, urlunparse
    import os

    parsed = urlparse(url)
    normalized_path = os.path.normpath(parsed.path)
    parsed = parsed._replace(path=normalized_path)
    return urlunparse(parsed)


class ReadLogfileTool(Tool[ReadLogfileInput, ToolRunOptions, StringToolOutput]):
    name = "read_logfile"  # type: ignore
    description = "Read logfile from URL. Allowed URL patterns:\n" + "\n".join(  # type: ignore
        f"- {pattern}" for pattern in LOGFILE_PATTERNS
    )
    input_schema = ReadLogfileInput  # type: ignore

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
        session = aiohttp_session()

        url = normalize_url(input.logfile_url)
        matched = False
        for pattern in LOGFILE_PATTERNS:
            if re.match(pattern, url):
                matched = True
                break

        if not matched:
            return StringToolOutput(
                result=f"URL does not match allowed patterns: {input.logfile_url}"
            )

        logger.info("Reading logfile from URL: %s", url)
        async with session.get(url) as response:
            if response.status == 200:
                return StringToolOutput(
                    result=await response.text(),
                )

        return StringToolOutput(
            result=f"Failed to read logfile from {input.logfile_url}"
        )
