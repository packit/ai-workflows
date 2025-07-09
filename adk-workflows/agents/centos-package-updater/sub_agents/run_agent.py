#!/usr/bin/env python3
"""
Minimal agent runner for ADK sub-agents.
Executes the selected agent based on AGENT_TYPE environment variable.
"""
import asyncio
import os
import sys
import logging
import traceback

from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types

from issue_analyzer import create_issue_analyzer_agent, mcp_connection
from package_updater import create_package_updater_agent
from centos_workflow_agent import create_centos_workflow_agent

def setup_logging():
    """Set up simple logging for tool usage tracking."""
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger('adk_runner')

async def run_centos_agent():
    """Run the selected agent based on AGENT_TYPE environment variable."""
    logger = setup_logging()
    agent_type = os.environ.get('AGENT_TYPE', 'workflow')
    logger.info(f"Starting agent: {agent_type}")

    # Validate agent type
    valid_types = ['workflow', 'issue_analyzer', 'package_updater']
    if agent_type not in valid_types:
        logger.error(f"Unknown AGENT_TYPE '{agent_type}'. Must be one of: {', '.join(valid_types)}")
        sys.exit(1)

    # Handle workflow type (requires JIRA_ISSUE)
    if agent_type == 'workflow':
        jira_issue = os.environ.get('JIRA_ISSUE')
        if not jira_issue:
            logger.error("JIRA_ISSUE environment variable must be set for workflow agent")
            sys.exit(1)
        logger.info(f"Running CentOS workflow agent for JIRA issue: {jira_issue}")
        agent = create_centos_workflow_agent(jira_issue)
        await run_agent_with_session(logger, agent, agent_type)

    # Handle issue_analyzer type (requires MCP connection)
    elif agent_type == 'issue_analyzer':
        async with mcp_connection() as mcp_tools:
            logger.info("MCP connection established")
            agent = create_issue_analyzer_agent(mcp_tools=mcp_tools)
            logger.info(f"Created {agent_type} agent: {agent.name}")
            await run_agent_with_session(logger, agent, agent_type)
        logger.info("MCP connection properly closed")

    # Handle package_updater type
    else:  # package_updater
        agent = create_package_updater_agent()
        logger.info(f"Created {agent_type} agent: {agent.name}")
        await run_agent_with_session(logger, agent, agent_type)

async def run_agent_with_session(logger, agent, agent_type):
    """Run an agent with proper session management."""
    session_service = InMemorySessionService()

    # Session configuration
    app_name = "centos_package_updater"
    user_id = f"user_1_{agent_type}"
    session_id = f"session_001_{agent_type}"

    session = await session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    logger.info(f"Created session: {session_id}")

    runner = None
    try:
        runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
        logger.info("Runner initialized, starting execution...")

        # Create initial message based on agent type
        messages = {
            'workflow': "Start the full CentOS package workflow. Analyze the JIRA issue, make decisions, and execute actions.",
            'issue_analyzer': "Start the JIRA issue analysis now. Use the configured JIRA issue key and fetch it using the available JIRA tools.",
            'package_updater': "Start the CentOS package update process. Check for package updates and perform the necessary update operations."
        }
        message_text = messages.get(agent_type, "Start the agent execution.")
        content = types.Content(role="user", parts=[types.Part(text=message_text)])

        # Run agent
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
            if event.is_final_response() and event.content and event.content.parts:
                logger.info("\n" + "="*60)
                logger.info("AGENT RESPONSE:")
                logger.info("="*60)
                logger.info(event.content.parts[0].text)

    finally:
        # Cleanup runner
        if runner is not None:
            try:
                await runner.close()
            except Exception as cleanup_error:
                logger.debug(f"Cleanup warning: {cleanup_error}")

        # Cleanup session
        try:
            await session_service.delete_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
        except Exception as session_error:
            logger.debug(f"Session cleanup warning: {session_error}")

if __name__ == "__main__":
    try:
        asyncio.run(run_centos_agent())
    except Exception as e:
        logger = logging.getLogger('adk_runner')
        logger.error(f"Execution failed: {str(e)}")
        logger.debug(traceback.format_exc())
        sys.exit(1)
