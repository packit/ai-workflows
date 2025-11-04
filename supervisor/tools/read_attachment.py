import re
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

from ..jira_utils import get_issue_attachment


class ReadAttachmentInput(BaseModel):
    issue_key: str = Field(description="JIRA issue key")
    attachment_filename: str = Field(description="filename of attachment to read")


class ReadAttachmentTool(Tool[ReadAttachmentInput, ToolRunOptions, StringToolOutput]):
    name = "read_attachment"  # type: ignore
    description = "Read a JIRA issue attachment by filename"  # type: ignore
    input_schema = ReadAttachmentInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "read_attachment"],
            creator=self,
        )

    async def _run(
        self,
        input: ReadAttachmentInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:

        content = get_issue_attachment(input.issue_key, input.attachment_filename)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return StringToolOutput(
                result=f"Failed to decode attachment {input.attachment_filename} as UTF-8"
            )

        return StringToolOutput(
            result=text,
        )
