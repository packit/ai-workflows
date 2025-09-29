from datetime import datetime
from enum import StrEnum
import logging
from urllib.parse import quote as urlquote

from aiohttp import client_exceptions
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import ToolOutput, Tool, ToolRunOptions

from ..http_utils import aiohttp_session


logger = logging.getLogger(__name__)


RESULTS_DB_URL = "https://resultsdb-api.engineering.redhat.com"


class ResultsdbOutput(StrEnum):
    """The possible resultsdb outcome values, we only care about a subset of them."""

    PASSED = "PASSED"
    INFO = "INFO"
    FAILED = "FAILED"
    NEEDS_INSPECTION = "NEEDS_INSPECTION"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    ERROR = "ERROR"
    PENDING = "PENDING"


class ResultsDbResult(BaseModel):
    """A small subset of the full resultsdb result schema, but it is all we need."""

    testcase_name: str
    outcome: ResultsdbOutput
    ref_url: str | None
    # Renamed from submit_time, since that sounds like when the test was
    # started, but it means when the result was submitted to resultsdb
    last_updated: datetime


# Arbitrary limit, we shouldn't get this many results for a single NVR
MAX_RESULTS = 100


async def search_resultsdb(
    package_nvr: str, name_pattern: str
) -> list[ResultsDbResult]:
    session = aiohttp_session()
    url = (
        f"{RESULTS_DB_URL}/api/v2.0/results"
        f"?item={urlquote(package_nvr)}"
        f"&testcases:like={urlquote(name_pattern)}"
        f"&limit={MAX_RESULTS}"
    )
    logger.info("Fetching resultsdb data from %s", url)
    async with session.get(url) as response:
        if response.status == 200:
            response_json = await response.json()

            if response_json.get("next") is not None:
                raise ValueError(f"ResultsDB returned more than {MAX_RESULTS} results")

            results: list[ResultsDbResult] = [
                ResultsDbResult(
                    testcase_name=r["testcase"]["name"],
                    outcome=ResultsdbOutput(r["outcome"]),
                    ref_url=r["ref_url"],
                    last_updated=datetime.fromisoformat(r["submit_time"]),
                )
                for r in response_json.get("data", [])
            ]

            # Now only keep the latest result for each testcase
            latest_results: dict[str, ResultsDbResult] = {}
            for r in results:
                if (
                    r.testcase_name not in latest_results
                    or r.last_updated > latest_results[r.testcase_name].last_updated
                ):
                    latest_results[r.testcase_name] = r

            logger.info(
                "Found %d results in resultsdb (%d after filtering for latest submissions)",
                len(results),
                len(latest_results),
            )

            return list(latest_results.values())
        else:
            text = await response.text()
            raise client_exceptions.ClientResponseError(
                response.request_info,
                response.history,
                status=response.status,
                message=text,
                headers=response.headers,
            )


class SearchResultsdbInput(BaseModel):
    package_nvr: str = Field(
        description="NVR of the package to search in the results database"
    )
    name_pattern: str = Field(
        description="Pattern to search for in the testcase names, e.g. 'frontend.regression.test-%'"
    )


class SearchResultsdbOutput(BaseModel, ToolOutput):
    results: list[ResultsDbResult]

    def get_text_content(self) -> str:
        return self.model_dump_json(indent=2, exclude_unset=True)

    def is_empty(self) -> bool:
        return len(self.results) == 0


class SearchResultsdbTool(
    Tool[SearchResultsdbInput, ToolRunOptions, SearchResultsdbOutput]
):
    """
    Tool to search for results for a specific package build in resultsdb.
    https://github.com/release-engineering/resultsdb
    """

    name = "search_resultsdb"  # type: ignore
    description = "Search for results for a specific package build in resultsdb"  # type: ignore
    input_schema = SearchResultsdbInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "search_resultsdb"],
            creator=self,
        )

    async def _run(
        self,
        input: SearchResultsdbInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> SearchResultsdbOutput:
        try:
            return SearchResultsdbOutput(
                results=await search_resultsdb(input.package_nvr, input.name_pattern)
            )
        except Exception as e:
            logger.exception("Error fetching or parsing resultsdb data: %s", e)
            raise e
