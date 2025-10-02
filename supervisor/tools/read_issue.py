import logging
import re
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

from ..jira_utils import get_issue

logger = logging.getLogger(__name__)


class ReadIssueInput(BaseModel):
    issue_url: str = Field(description="URL of JIRA ticket to read issue from")


class ReadIssueTool(Tool[ReadIssueInput, ToolRunOptions, StringToolOutput]):
    name = "read_issue"  # type: ignore
    description = "Read JIRA issue from URL to get details, comments, and test results"  # type: ignore
    input_schema = ReadIssueInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "read_issue"],
            creator=self,
        )

    async def _run(
        self,
        input: ReadIssueInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        try:
            #extract issue key from URL
            issue_key = self._extract_issue_key(input.issue_url)
            if not issue_key:
                return StringToolOutput(
                    result=f"Error: Could not extract JIRA issue key from URL: {input.issue_url}"
                )

            #fetch using existing jira utils
            issue = get_issue(issue_key, full=True)

            # return formatted issue data
            return StringToolOutput(
                result=issue.model_dump_json(indent=2)
            )

        except Exception as e:
            logger.error(f"Failed to read JIRA issue {input.issue_url}: {e}")
            return StringToolOutput(
                result=f"Error: Failed to read JIRA issue {input.issue_url}: {str(e)}"
            )

    def _extract_issue_key(self, url: str) -> str | None:
        """Extract JIRA issue key from various URL formats."""
        # pattern for https://issues.redhat.com/browse/RHELMISC-12345
        pattern = r'https://issues\.redhat\.com/browse/([A-Z]+-\d+)'
        match = re.search(pattern, url)
        if match:
            return match.group(1)

        # pattern for just the issue key (RHELMISC-12345)
        pattern = r'([A-Z]+-\d+)'
        match = re.search(pattern, url)
        if match:
            return match.group(1)

        return None
