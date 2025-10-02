import logging
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

from ..jira_utils import get_issue

logger = logging.getLogger(__name__)


class ReadIssueInput(BaseModel):
    issue_key: str = Field(description="JIRA issue key (e.g.: RHELMISC-12345)")


class ReadIssueTool(Tool[ReadIssueInput, ToolRunOptions, StringToolOutput]):
    name = "read_issue"  # type: ignore
    description = "Read JIRA issue by key to get details, comments, and test results"  # type: ignore
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
            #fetch the issue using jira utils
            issue = get_issue(input.issue_key, full=True)

            #return formatted issue data
            return StringToolOutput(
                result=issue.model_dump_json(indent=2)
            )

        except Exception as e:
            logger.error(f"Failed to read JIRA issue {input.issue_key}: {e}")
            return StringToolOutput(
                result=f"Error: Failed to read JIRA issue {input.issue_key}: {str(e)}"
            )