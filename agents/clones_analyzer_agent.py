import os
import copy
import logging
from typing import Any
from textwrap import dedent

from beeai_framework.agents.requirement import RequirementAgent
from beeai_framework.agents.requirement.prompts import RequirementAgentSystemPrompt
from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.workflows import Workflow

from pydantic import BaseModel, Field
from observability import setup_observability

from tools.commands import RunShellCommandTool
from tools.version_mapper import VersionMapperTool
from common.models import ClonesInputSchema, ClonesOutputSchema
from utils import get_chat_model, get_tool_call_checker_config
from utils import mcp_tools, get_agent_execution_config

logger = logging.getLogger(__name__)


def get_instructions() -> str:
    return """
      You are an expert on finding other Jira issues related to a given Jira issue
      in RHEL Jira project by analyzing the Jira fields and comments.

      To find other Jira issues which are clones of <JIRA_ISSUE> Jira issue, do the following:

      1. Search for other Jira issues which have the same affected component as <JIRA_ISSUE> Jira issue in RHEL Jira project and extract their titles.

      2. Compare the titles of the found Jira issues with the title of <JIRA_ISSUE> Jira issue and identify the ones which are clones.
      For example, if the title of <JIRA_ISSUE> Jira issue is "CVE-YYYY-XXXXX libsoup3: Out-of-Bounds Read in Cookie Date Handling of libsoup HTTP Library [rhel-10.1]"
      and you have found another Jira issue with the title "CVE-YYYY-XXXXX libsoup3: Out-of-Bounds Read in Cookie Date Handling of libsoup HTTP Library [rhel-10.0z]",
      then it is a clone of <JIRA_ISSUE> Jira issue or <JIRA_ISSUE> Jira issue is a clone of the found Jira issue.

      3.Usually clones are already linked to each other in Jira through the "Issue Links" field.
      If not, link the found Jira issues to <JIRA_ISSUE> Jira issue and the <JIRA_ISSUE> Jira issue to the found Jira issues
      through the "is related" relationship.

     General instructions:
      - If in DRY RUN mode, do not link the Jira issues to each other but tell the user that you would have linked them.
    """


def get_prompt(input: ClonesInputSchema) -> str:
    return f"""
      Find other Jira issues which are clones of {input.jira_issue} Jira issue and link them to each other.
      Also check if {input.jira_issue} Jira issue is a clone of any of the found Jira issues and link them to each other.
    """

def get_agent_definition(gateway_tools: list[Tool]) -> dict[str, Any]:
    return {
    "name": "ExistingClonesAnalyzerAgent",
    "llm": get_chat_model(),
    "tool_call_checker": get_tool_call_checker_config(),
    "tools": [ThinkTool(), RunShellCommandTool(), VersionMapperTool()]
        + [t for t in gateway_tools if t.name in ["get_jira_details", "set_jira_fields"]],
    "memory": UnconstrainedMemory(),
    "requirements": [
        ConditionalRequirement(
            ThinkTool,
            force_at_step=1,
            force_after=Tool,
            consecutive_allowed=False,
            only_success_invocations=False,
        ),
        ],
    "middlewares": [GlobalTrajectoryMiddleware(pretty=True)],
    "role": "Red Hat Enterprise Linux developer",
    "instructions": get_instructions(),
    "templates": {"system": copy.deepcopy(RequirementAgentSystemPrompt)}
    }

def create_clones_analyzer_agent(mcp_tools: list[Tool], local_tool_options: dict[str, Any]) -> RequirementAgent:
    return RequirementAgent(**get_agent_definition(mcp_tools))

WORKFLOW_STEP_INSTRUCTIONS = dedent("""
                            The final answer must be a JSON object with the following fields:
                            - `clones`: a list of Jira issue keys and branches that are clones of the given Jira issue or the given Jira issue is a clone of the found Jira issues
                            - `links`: a list of links you have added between the given Jira issue and the found Jira issues or the found Jira issues and the given Jira issue
                            ```json
                            {
                            "clones": [{"jira_issue": "RHEL-12345", "branch": "rhel-9.6z"},
                                       {"jira_issue": "RHEL-12346", "branch": "rhel-9.7"}],
                            "links": [{"source": "RHEL-12345", "target": "RHEL-12346"},
                                      {"source": "RHEL-12346", "target": "RHEL-12345"}]
                            }
                            ```
""")

async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    setup_observability(os.environ["COLLECTOR_ENDPOINT"])

    dry_run = os.getenv("DRY_RUN", "False").lower() == "true"

    class State(BaseModel):
        jira_issue: str
        clones_result: ClonesOutputSchema | None = Field(default=None)

    async def run_workflow(jira_issue):
        async with mcp_tools(os.getenv("MCP_GATEWAY_URL")) as gateway_tools:
            clones_analyzer_agent = RequirementAgent(
                **get_agent_definition(gateway_tools),
            )

            async def identify_existing_clones(state):
                """Identify and link clones of the given Jira issue"""
                logger.info(f"Identifying and linking clones of {state.jira_issue}")
                response = await clones_analyzer_agent.run(
                    get_prompt(ClonesInputSchema(jira_issue=state.jira_issue)),
                    expected_output=WORKFLOW_STEP_INSTRUCTIONS,
                    **get_agent_execution_config(),
                    )

                state.clones_result = ClonesOutputSchema.model_validate_json(response.last_message.text)
                return Workflow.END

            workflow = Workflow(State, name="ClonesAnalyzerWorkflow")
            workflow.add_step("identify_existing_clones", identify_existing_clones)
            await workflow.run(State(jira_issue=jira_issue))

    jira_issue = os.getenv("JIRA_ISSUE")
    if not jira_issue:
        logger.error("JIRA_ISSUE environment variable is required")
        return

    await run_workflow(jira_issue)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
