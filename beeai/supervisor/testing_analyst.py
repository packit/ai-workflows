import logging
import os

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.backend import ChatModel
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool

from agents.utils import get_agent_execution_config, mcp_tools
from .supervisor_types import TestingState


logger = logging.getLogger(__name__)


class InputSchema(BaseModel):
    issue: str = Field(description="Jira issue identifier to analyze (e.g. RHEL-12345)")


class OutputSchema(BaseModel):
    state: TestingState = Field(description="State of tests")
    comment: str | None = Field(description="Comment to add to the JIRA issue")


def render_prompt(input: InputSchema) -> str:
    template = """
      You are an agent that analyzes a RHEL JIRA issue with a fix attached and determines
      the state and what needs to be done.

      **Output Format**

      Your output must strictly follow the format below.

      JIRA_ISSUE: {{ issue }}
      STATE: tests-not-running | tests-pending | tests-running | tests-failed | tests-passed

      If STATE is tests-not-running:
          COMMENT: [explain what needs to be done to start tests]

      If STATE is tests-failed:
          COMMENT: [list failed tests with URLs. Use the JIRA comment format]

      If STATE is tests-passed:
          COMMENT: [Give a brief summary of what was tested with a link to the result. Use the JIRA comment format]
    """
    return PromptTemplate(
        PromptTemplateInput(schema=InputSchema, template=template)
    ).render(input)


async def analyze_issue(jira_issue) -> OutputSchema:
    async with mcp_tools(os.environ["MCP_GATEWAY_URL"]) as gateway_tools:
        agent = RequirementAgent(
            llm=ChatModel.from_name(os.environ["CHAT_MODEL"]),
            tools=[ThinkTool()]
            + [t for t in gateway_tools if t.name == "get_jira_details"],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(
                    ThinkTool, force_after=Tool, consecutive_allowed=False
                ),
                ConditionalRequirement("get_jira_details", min_invocations=1),
            ],
            # middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
            role="Red Hat Enterprise Linux developer",
            instructions=[
                "Use the `think` tool to reason through complex decisions and document your approach.",
            ],
        )

        async def run(input):
            response = await agent.run(
                render_prompt(input),
                expected_output=OutputSchema,
                **get_agent_execution_config(),  # type: ignore
            )
            return OutputSchema.model_validate_json(response.answer.text)

        output = await run(InputSchema(issue=jira_issue))
        logger.info(f"Direct run completed: {output.model_dump_json(indent=4)}")
        return output
