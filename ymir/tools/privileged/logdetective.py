import logging
import os

import aiohttp
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)

LOG_DETECTIVE_URL = os.getenv("LOG_DETECTIVE_URL")
LOG_DETECTIVE_TOKEN = os.getenv("LOG_DETECTIVE_TOKEN")
MAX_LOG_DETECTIVE_FILES = int(os.getenv("MAX_LOG_DETECTIVE_FILES", 5))
LOG_DETECTIVE_TIMEOUT = int(os.getenv("LOG_DETECTIVE_TIMEOUT", 180))


# --- Input models ---


class LogDetectiveFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique name identifying this file")
    url: str = Field(description="URL to fetch file content from")


class LogDetectiveBuildMetadata(BaseModel):
    specfile: str | None = Field(default=None, description="RPM specfile content")
    last_patch: str | None = Field(default=None, description="Content of the last applied patch")
    commentary: str | None = Field(default=None, description="Additional context about the build")
    infra_status: str | None = Field(default=None, description="Infrastructure status information")


class AnalyzeLogsToolInput(BaseModel):
    files: list[LogDetectiveFile] = Field(
        description=f"List of log files to analyze (1-{MAX_LOG_DETECTIVE_FILES} items, unique names). "
        "Each file must have a 'name' and a 'url' pointing to the log content.",
        min_length=1,
        max_length=MAX_LOG_DETECTIVE_FILES,
    )
    build_metadata: LogDetectiveBuildMetadata | None = Field(
        default=None,
        description="Optional build metadata providing context for the analysis",
    )


# --- Output models ---


class LogDetectiveSnippet(BaseModel):
    text: str = Field(description="The relevant build artifact snippet text")
    line_number: int = Field(description="Line number in the source file")
    source_file: str | None = Field(default=None, description="Source file name")
    snippet_analysis: str | None = Field(default=None, description="Analysis of this snippet")


class LogDetectiveResult(BaseModel):
    explanation: str = Field(description="Explanation of the build failure")
    snippets: list[LogDetectiveSnippet] | None = Field(
        default=None, description="Relevant build artifact snippets identified"
    )
    solution: str | None = Field(default=None, description="Suggested solution")
    no_issue_found: bool = Field(description="Whether no issues were found in build artifacts")


class AnalyzeLogsToolOutput(JSONToolOutput[LogDetectiveResult]):
    def get_text_content(self) -> str:
        """Build more token efficient version of the API response."""
        result: LogDetectiveResult = self.result
        if result.no_issue_found:
            parts = ["No issues found.", f"Explanation: {result.explanation}"]
        else:
            parts = [f"Explanation: {result.explanation}"]

        if result.snippets:
            parts.append("Snippets:")
            for s in result.snippets:
                loc = f"{s.source_file}:{s.line_number}" if s.source_file else f"line {s.line_number}"
                parts.append(f"- {loc}: {s.text}")
                if s.snippet_analysis:
                    parts.append(f"  Analysis: {s.snippet_analysis}")

        if result.solution:
            parts.append(f"Solution: {result.solution}")

        return "\n".join(parts)


# --- Internal API response models ---


class _APITextField(BaseModel):
    text: str


class _APISnippet(BaseModel):
    text: str
    line_number: int
    source_file: str | None = None
    snippet_analysis: str | None = None


class _APIResponse(BaseModel):
    explanation: _APITextField
    snippets: list[_APISnippet] | None = None
    solution: _APITextField | None = None
    no_issue_found: bool = False


# --- Tool ---


class AnalyzeLogsTool(Tool[AnalyzeLogsToolInput, ToolRunOptions, AnalyzeLogsToolOutput]):
    _description = """
        Analyzes at most {max_log_detective_files} build log files using the Log Detective service.
        Pass each file as a URL pointing to the hosted log (e.g. Koji, COPR build artifacts).
        Optionally attach build_metadata (specfile, last_patch, commentary, infra_status)
        to give the analysis additional context.
        Returns an explanation of the failure, relevant log snippets with line numbers,
        and a suggested solution.
    """

    @property
    def name(self) -> str:
        return "analyze_logs"

    @property
    def description(self) -> str:
        return self._description.format(max_log_detective_files=MAX_LOG_DETECTIVE_FILES)

    @property
    def input_schema(self) -> type[AnalyzeLogsToolInput]:
        return AnalyzeLogsToolInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "logdetective", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: AnalyzeLogsToolInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> AnalyzeLogsToolOutput:
        if not LOG_DETECTIVE_URL:
            raise ToolError(
                "Log Detective URL not configured. Set the LOGDETECTIVE_URL environment variable."
            )

        names = [f.name for f in input.files]
        if len(names) != len(set(names)):
            raise ToolError("File names must be unique.")

        files_payload = [f.model_dump() for f in input.files]

        payload: dict = {"files": files_payload}
        if input.build_metadata is not None:
            metadata = input.build_metadata.model_dump(exclude_none=True)
            if metadata:
                payload["build_metadata"] = metadata

        headers: dict = {"Content-Type": "application/json"}
        if LOG_DETECTIVE_TOKEN:
            headers["Authorization"] = f"Bearer {LOG_DETECTIVE_TOKEN}"

        analyze_url = f"{LOG_DETECTIVE_URL.rstrip('/')}/analyze"
        logger.info("Sending analysis request to Log Detective: %s", analyze_url)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=LOG_DETECTIVE_TIMEOUT)
        ) as session:
            try:
                async with session.post(
                    analyze_url,
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status == 401:
                        raise ToolError(
                            "Authentication failed: invalid or missing token for Log Detective API."
                        )
                    if response.status == 422:
                        error_text = await response.text()
                        raise ToolError(f"Log Detective API validation error (422): {error_text}")
                    if response.status == 503:
                        raise ToolError(
                            "Log Detective service is temporarily unavailable (503). Please try again later."
                        )
                    if response.status >= 400:
                        error_text = await response.text()
                        raise ToolError(f"Log Detective API request failed ({response.status}): {error_text}")
                    data = await response.json()
            except ToolError:
                raise
            except (aiohttp.ContentTypeError, ValueError) as e:
                raise ToolError(f"Log Detective returned invalid or a non-JSON response: {e}") from e
            except aiohttp.ClientError as e:
                raise ToolError(f"Failed to connect to Log Detective service: {e}") from e
            except TimeoutError as e:
                raise ToolError("Timeout while contacting Log Detective service") from e

        try:
            api_response = _APIResponse(**data)
        except (ValidationError, TypeError) as e:
            raise ToolError(f"Unexpected response from Log Detective API: {e}") from e

        snippets = None
        if api_response.snippets is not None:
            snippets = [
                LogDetectiveSnippet(
                    text=s.text,
                    line_number=s.line_number,
                    source_file=s.source_file,
                    snippet_analysis=s.snippet_analysis,
                )
                for s in api_response.snippets
            ]

        result = LogDetectiveResult(
            explanation=api_response.explanation.text,
            snippets=snippets,
            solution=api_response.solution.text if api_response.solution else None,
            no_issue_found=api_response.no_issue_found,
        )
        return AnalyzeLogsToolOutput(result=result)
