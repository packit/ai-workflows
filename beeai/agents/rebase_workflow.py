import asyncio
import os
import logging
from beeai_framework.emitter import EmitterOptions
from beeai_framework.backend.chat import ChatModel
from beeai_framework.workflows.agent import AgentWorkflow, AgentWorkflowInput
from beeai_framework.agents import AgentExecutionConfig
from mcp import ClientSession
from mcp.client.sse import sse_client
from rebase_agent import RebaseAgent
from copr_validator_agent import CoprValidatorAgent
from observability import setup_observability
from utils import get_mcp_tools, format_event_output

from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool
from tools.commands import RunShellCommandTool

# Set up logging to see more detailed output
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

max_retries_per_step = int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5))
total_max_retries = int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 10))
max_iterations = int(os.getenv("BEEAI_MAX_ITERATIONS", 100))


async def main() -> None:
    llm=ChatModel.from_name(os.getenv("CHAT_MODEL")),

    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))

    mcp_gateway_url = os.getenv("MCP_GATEWAY_URL")
    if not mcp_gateway_url:
        logging.error("MCP_GATEWAY_URL not set - cannot connect to MCP gateway")
    async with sse_client(mcp_gateway_url) as (read, write), ClientSession(read, write) as session:
        await session.initialize()    

        workflow = AgentWorkflow(name="Rebase workflow")

        rebase_agent = RebaseAgent()
        copr_validator_agent = CoprValidatorAgent()

        execution=AgentExecutionConfig(
            max_retries_per_step=max_retries_per_step,
            total_max_retries=total_max_retries,
            max_iterations=max_iterations,
        )

        base_tools = [ThinkTool(), RunShellCommandTool(), DuckDuckGoSearchTool()]

        package = os.getenv("PACKAGE", "libwebp")
        version = os.getenv("VERSION", "1.4.0")
        jira_issue = os.getenv("JIRA_ISSUE", "RHEL-12345")
        branch = os.getenv("BRANCH", "c9s")
        dry_run = os.getenv("DRY_RUN", "false")

        rebase_instructions = rebase_agent._render_prompt(rebase_agent.input_schema(
            package=package,
            version=version,
            jira_issue=jira_issue,
            dist_git_branch=branch,
        ))
        print(f"Rebase instructions: {rebase_instructions}")

        workflow.add_agent(
            rebase_agent,
            execution=execution,
            tools=base_tools + await get_mcp_tools(session, filter=lambda t: t in ("fork_repository", "open_merge_request", "push_to_remote_repository")),
            name="RebaseAgent",
            role="You are a rebase assistant. You are tasked to rebase a CentOS package to a newer version.",
            instructions=rebase_instructions,
        )

        copr_validator_instructions = copr_validator_agent._render_prompt(copr_validator_agent.input_schema(
            package=package,
            version=version,
            jira_issue=jira_issue,
            dist_git_branch=branch,
            srpm_path="<PREVIOUS_STEP_SRPM_PATH>",
            mr_url="<PREVIOUS_STEP_MR_URL>",
        ))
        print(f"Copr validator instructions: {copr_validator_instructions}")

        workflow.add_agent( 
            copr_validator_agent,
            execution=execution,
            tools=base_tools + await get_mcp_tools(session, filter=lambda t: t in ("build_package",)),
            name="CoprValidatorAgent",
            role="You are a copr build/validation assistant. You are tasked to validate a package patch by building it in Copr and analyzing the build results.",
            instructions=copr_validator_instructions,
        )


        print(f"Starting workflow: Rebase the {package} package to version {version} for branch {branch}, referencing JIRA issue {jira_issue} (dry run: {dry_run})")
        
        response = await workflow.run(
            inputs=[
                AgentWorkflowInput(
                    prompt=(
                        f"Rebase a CentOS package to a newer version, use the rebase agent assistant. "
                        f"The package is {package}, the version is {version}, the branch is {branch}, the JIRA issue is {jira_issue}. "
                        f"The dry run mode is {dry_run}."
                    ),
                    expected_output="SRPM generated and path provided; spec updated; MR URL if applicable; status summary."
                ),

                AgentWorkflowInput(
                    prompt=(
                        f"Validate the SRPM built in the previous step for {package} {version}, use the copr build/validation assistant. "
                        f"Use chroot appropriate for {branch}. If build fails, analyze logs and suggest fixes. "
                        f"The SRPM path is <PREVIOUS_STEP_SRPM_PATH>."
                    ),
                    expected_output="Copr build result with URLs; pass/fail; error analysis and next actions."
                ),
            ]
        ).on(
            lambda event: True,
            format_event_output,
            EmitterOptions(match_nested=True),
        )

        print("==== Rebase Operation Completed ====")
        print(response.result.final_answer)


if __name__ == "__main__":
    asyncio.run(main())