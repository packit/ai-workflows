import asyncio
import aiohttp
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions

MAX_CONTENT_LENGTH = 2000

class PatchValidatorInput(BaseModel):
    url: str = Field(description="URL to validate as a patch/commit")


class PatchValidatorResult(BaseModel):
    url: str = Field(description="URL of the patch/commit")
    is_accessible: bool = Field(description="Whether the URL is accessible and not an issue reference")
    status_code: int | None = Field(description="HTTP status code")
    content: str | None = Field(description="Content of the URL (truncated if too long)")
    reason: str = Field(description="Brief explanation")


class PatchValidatorOutput(JSONToolOutput[PatchValidatorResult]):
    pass


class PatchValidatorTool(Tool[PatchValidatorInput, ToolRunOptions, PatchValidatorOutput]):
    name = "PatchValidator"
    description = """Fetches a patch from a URL after validating it's not an issue/bug reference and is accessible."""
    input_schema = PatchValidatorInput

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "patch", "validator"],
            creator=self,
        )

    def _is_issue_reference(self, url: str) -> tuple[bool, str]:
        """Check if URL is an issue/bug reference. Returns (is_issue, reason)"""
        url_lower = url.lower()

        # Issue indicators that make URL invalid
        issue_patterns = [
            '/issues/', '/issue/', '/bug/', '/bugs/',
            'bugzilla', 'jira', '/tickets/', '/ticket/',
        ]

        for pattern in issue_patterns:
            if pattern in url_lower:
                return True, f"Contains '{pattern}' - appears to be issue reference"

        return False, "Does not appear to be an issue reference"

    def _truncate_content(self, content: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
        """Truncate content if it's too long for the LLM context"""
        if len(content) <= max_length:
            return content

        return content[:max_length] + f"\n\n[Content truncated - showing first {max_length} characters of {len(content)} total]"

    async def _run(
        self, tool_input: PatchValidatorInput, options: ToolRunOptions | None, context: RunContext
    ) -> PatchValidatorOutput:
        url = tool_input.url.strip()

        is_issue, reason = self._is_issue_reference(url)

        status_code = None
        is_accessible = False
        content = None

        if not is_issue:
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as response:
                        status_code = response.status
                        is_accessible = response.status < 400

                        if is_accessible:
                            max_length = MAX_CONTENT_LENGTH
                            if response.content_length and response.content_length > max_length:
                                # Avoid reading a huge response into memory.
                                partial_bytes = await response.content.read(max_length)
                                content = partial_bytes.decode(response.get_encoding() or 'utf-8', errors='ignore')
                                content += f"\n\n[Content truncated - showing first {max_length} bytes of {response.content_length} total]"
                            else:
                                # For responses without content-length or smaller ones, read fully and then truncate.
                                raw_content = await response.text()
                                content = self._truncate_content(raw_content)
                            reason = "URL is accessible and content fetched successfully"
                        else:
                            reason = f"Not an issue reference but not accessible (HTTP {response.status})"

            except asyncio.TimeoutError:
                reason = "Not an issue reference but request timeout"
            except Exception as e:
                reason = f"Not an issue reference but raised exception: {str(e)}"

        result = PatchValidatorResult(
            url=url,
            is_accessible=is_accessible,
            status_code=status_code,
            content=content,
            reason=reason,
        )

        return PatchValidatorOutput(result)
